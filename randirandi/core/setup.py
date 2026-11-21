# SPDX-License-Identifier: AGPL-3.0-only
"""An organization's setup, between its file and the database.

`load` makes a new organization from a setup that `setup_file` has read. Every
object goes through its own validation, the same checks the admin screens
use, and everything is made in one transaction: a setup that fails a rule is
not loaded in part. `check` does the same and keeps nothing.

`export` gives a running organization's setup back, in format 3, in the
shape `setup_file.write` turns into a file.
"""

from collections.abc import Callable, Iterable
from datetime import date, datetime, time
from typing import Any, Protocol

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from psycopg import sql

from randirandi import labels as label_set
from randirandi import setup_file
from randirandi.core import validator
from randirandi.core.booking_words import DEFAULT_SPOT_WORD
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    CategoryHoursSpan,
    ConfigurationCategory,
    ConsumptionMode,
    Organization,
    PeriodType,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Role,
    RoleCategory,
    Space,
    Spot,
    SpotGroup,
)
from randirandi.core.models.base import clock
from randirandi.setup_file import WEEKDAYS, Setup, SetupError

# Where a field of an object has another name in the file.
_IN_THE_FILE = {"start_time": "start", "end_time": "end"}


def load(setup: Setup, *, today: date) -> Organization:
    """Make a new organization from the setup, or say everything that is
    wrong with it and make nothing.

    `today` is the day in the organization's time zone. An availability
    calendar without a start date applies from it, and the validator checks
    the date periods that end after it starts.
    """
    organization = _make(setup, today, keep=True)
    assert organization is not None  # noqa: S101
    return organization


def check(setup: Setup, *, today: date) -> None:
    """Put the setup through every check of `load`, and keep nothing."""
    _make(setup, today, keep=False)


def _make(setup: Setup, today: date, *, keep: bool) -> Organization | None:
    setup_file.validate(setup)
    faults: list[str] = []
    with transaction.atomic():
        organization = _Loading(setup, today, faults).run()
        if faults or not keep:
            transaction.set_rollback(True)
    if faults:
        raise SetupError(faults)
    return organization if keep else None


class _Loading:
    """One load. A fault is noted with its place in the file, and the load
    goes on, so that everything wrong with a setup is found in one go. What
    hangs on an object that could not be made is passed over: its fault is
    the object's.
    """

    def __init__(self, setup: Setup, today: date, faults: list[str]) -> None:
        self.setup = setup
        self.today = today
        self.faults = faults

    def run(self) -> Organization | None:
        setup = self.setup
        about = setup.organization
        if Organization.objects.filter(subdomain=about.subdomain).exists():
            self.faults.append(
                "organization.subdomain: an organization with this subdomain "
                "exists already. A setup file makes a new organization."
            )
            return None
        organization = Organization(
            name=about.name,
            subdomain=about.subdomain,
            time_zone=about.time_zone,
            currency=about.currency,
            contact_email=about.contact_email,
            member_retention_months=about.member_retention_months,
            booking_retention_months=about.booking_retention_months,
            max_bookings_a_day=about.max_bookings_a_day,
            book_ahead_months=about.book_ahead_months,
            tax_rate=about.tax_rate,
            free_cancellation_days=about.free_cancellation_days,
            language=about.language,
            labels=dict(setup.labels),
        )
        # Its standard role is one of its own roles, which come after it.
        if not self.make("organization", organization, exclude=["standard_role"]):
            return None

        categories = self.named(
            "booking_categories",
            setup.booking_categories,
            lambda place, item: BookingCategory(
                organization=organization,
                name=item.name,
                position=place,
                icon=item.icon,
            ),
        )
        roles = self.named(
            "roles",
            setup.roles,
            lambda _, item: Role(
                organization=organization,
                name=item.name,
                admin_access=item.admin_access,
            ),
        )
        for place, role in enumerate(setup.roles, start=1):
            for name in role.booking_categories:
                if role.name in roles and name in categories:
                    self.make(
                        f"roles[{place}].booking_categories",
                        RoleCategory(
                            organization=organization,
                            role=roles[role.name],
                            booking_category=categories[name],
                        ),
                    )
        if about.standard_role in roles:
            organization.standard_role = roles[about.standard_role]
            self.make("organization", organization)

        spaces = self.named(
            "spaces",
            setup.spaces,
            lambda _, item: Space(
                organization=organization,
                number=item.number,
                name=item.name,
                description=item.description,
                booking_page=item.booking_page,
            ),
        )
        assets: dict[str, Asset] = {}
        # Each asset's floor plans by name; one without a name under "".
        floor_plans: dict[str, dict[str, AssetInventory]] = {}
        for place, written_asset in _by_number(setup.assets):
            where = f"assets[{place}]"
            if written_asset.space and written_asset.space not in spaces:
                continue
            asset = Asset(
                organization=organization,
                number=written_asset.number,
                name=written_asset.name,
                description=written_asset.description,
                space=spaces.get(written_asset.space),
                whole_space=written_asset.whole_space,
                maximum_occupancy=written_asset.maximum_occupancy,
                # Empty for the organization's: the model fills it in.
                time_zone=written_asset.time_zone,
            )
            # The first floor plan is made with its asset; without one written,
            # the asset makes its own of one spot without a name.
            plans = [
                AssetInventory(
                    organization=organization,
                    name=plan.name,
                    unit=plan.spot_word,
                    capacity=len(plan.spots),
                )
                for plan in written_asset.floor_plans
            ]
            if plans and not self.clean(f"{where}.floor_plans[1]", plans[0], ["asset"]):
                continue
            if not self.make(where, asset, floor_plan=plans[0] if plans else None):
                continue
            assets[written_asset.name] = asset
            if not plans:
                plans = [AssetInventory.objects.of(organization).get(asset=asset)]
            for number, (plan, written) in enumerate(
                zip(plans, written_asset.floor_plans or (None,), strict=True), start=1
            ):
                at = f"{where}.floor_plans[{number}]"
                plan.asset = asset
                if number > 1 and not self.make(at, plan):
                    continue
                floor_plans.setdefault(written_asset.name, {})[plan.name] = plan
                if written is not None:
                    self.spots_and_groups(at, plan, written)
        for place, calendar in enumerate(setup.availability_calendars, start=1):
            where = f"availability_calendars[{place}]"
            if calendar.asset not in assets:
                continue
            its_row = AvailabilityCalendar(
                organization=organization,
                asset=assets[calendar.asset],
                start_date=calendar.start_date or self.today,
            )
            if not self.make(where, its_row):
                continue
            for span in calendar.spans:
                self.make(
                    f"{where}.spans[{span.weekday} {clock(span.start)}]",
                    AvailableSpan(
                        organization=organization,
                        availability_calendar=its_row,
                        weekday=WEEKDAYS.index(span.weekday),
                        start_time=span.start,
                        end_time=span.end,
                    ),
                )
        for place, closure in enumerate(setup.closures, start=1):
            where = f"closures[{place}]"
            if closure.asset not in assets:
                continue
            self.make(
                where,
                AvailabilityClosure(
                    organization=organization,
                    asset=assets[closure.asset],
                    first_date=closure.first_date,
                    last_date=closure.last_date,
                    reason=closure.reason,
                ),
            )
        booking_types = self.named(
            "booking_types",
            setup.booking_types,
            lambda place, item: BookingType(
                organization=organization,
                name=item.name,
                description=item.description,
                position=place,
            ),
        )
        price_books = self.named(
            "price_books",
            setup.price_books,
            lambda _, item: PriceBook(
                organization=organization,
                number=item.number,
                name=item.name,
                # Empty for the organization's: the model fills it in.
                currency=item.currency,
            ),
        )

        configurations: dict[int, BookingConfiguration] = {}
        for place, item in _by_number(setup.booking_configurations):
            where = f"booking_configurations[{place}]"
            if (
                item.asset not in assets
                or item.booking_type not in booking_types
                or (item.price_book and item.price_book not in price_books)
            ):
                continue
            configuration = BookingConfiguration(
                organization=organization,
                number=item.number,
                asset=assets[item.asset],
                booking_type=booking_types[item.booking_type],
                period_type=item.period_type,
                period_start=item.period_start,
                period_end=item.period_end,
                end_day=item.end_day,
                period_start_weekdays=[
                    WEEKDAYS.index(day) for day in item.period_start_weekdays
                ],
                start_date=item.start_date,
                stop_date=item.stop_date,
                every_weeks=item.every_weeks,
                status=item.status,
                # None without payment (W12).
                price_book=price_books.get(item.price_book),
                consumption_mode=item.consumption_mode,
                floor_plan=self.floor_plan(floor_plans[item.asset], item),
            )
            # Entered for a flexible period. The system sets them for a fixed.
            for field in ("interval", "minimum_length", "maximum_length"):
                if getattr(item, field) is not None:
                    setattr(configuration, field, getattr(item, field))
            # The validator checks the date periods that end after today
            # starts on the container's asset's clock.
            now = datetime.combine(self.today, time(), assets[item.asset].zone)
            if not self.make(where, configuration, now=now):
                continue
            configurations[item.number] = configuration
            named = [name for name in item.booking_categories if name in categories]
            if not named:
                self.faults.append(
                    f"{where}.booking_categories: It names at least one booking "
                    "category."
                )
            linked = [
                name
                for name in named
                if self.make(
                    f"{where}.booking_categories",
                    ConfigurationCategory(
                        organization=organization,
                        booking_configuration=configuration,
                        booking_category=categories[name],
                    ),
                )
            ]
            self.category_hours(where, item, configuration, categories, linked, now)

        for book_place, price_book in enumerate(setup.price_books, start=1):
            for entry_place, entry in enumerate(price_book.entries, start=1):
                where = f"price_books[{book_place}].entries[{entry_place}]"
                if (
                    price_book.name not in price_books
                    or entry.booking_configuration not in configurations
                    or entry.booking_category not in categories
                ):
                    continue
                group = None
                if entry.group:
                    booked = configurations[entry.booking_configuration].floor_plan
                    group = (
                        SpotGroup.objects.of(organization)
                        .filter(floor_plan=booked, name=entry.group)
                        .first()
                        if booked is not None
                        else None
                    )
                    if group is None:
                        self.faults.append(
                            f"{where}.group: the floor plan the container books "
                            f"has no group named {entry.group!r}."
                        )
                        continue
                row = PriceBookEntry(
                    organization=organization,
                    spot_group=group,
                    price_book=price_books[price_book.name],
                    booking_configuration=configurations[entry.booking_configuration],
                    booking_category=categories[entry.booking_category],
                    tiers_go_by=entry.tiers_go_by,
                    billing_basis=entry.billing_basis,
                )
                if not self.make(where, row):
                    continue
                made = [
                    self.make(
                        f"{where}.tiers[{tier_place}]",
                        PriceTier(
                            organization=organization,
                            price_book_entry=row,
                            up_to=tier.up_to,
                            paid_per=tier.paid_per,
                            amount=tier.amount,
                        ),
                    )
                    for tier_place, tier in enumerate(entry.tiers, start=1)
                ]
                left_out = row.what_its_tiers_leave_out() if all(made) else None
                if left_out:
                    self.faults.append(f"{where}.tiers: {left_out}")
        return organization

    def spots_and_groups(
        self, where: str, plan: AssetInventory, written: setup_file.FloorPlan
    ) -> None:
        """Give a floor plan's spots their names, and make its groups. Its
        spots were made with it, numbered from 1 without names."""
        spots = {
            spot.number: spot
            for spot in Spot.objects.of(plan.organization_id).filter(floor_plan=plan)
        }
        for number, name in enumerate(written.spots, start=1):
            if name:
                spots[number].name = name
                self.make(f"{where}.spots[{number}]", spots[number])
        for number, group in enumerate(written.groups, start=1):
            at = f"{where}.groups[{number}]"
            row = SpotGroup(
                organization=plan.organization,
                floor_plan=plan,
                name=group.name,
                word=group.word,
            )
            if not self.make(at, row):
                continue
            for spot in group.spots:
                spots[spot].group = row
                self.make(f"{at}.spots", spots[spot])

    @staticmethod
    def floor_plan(
        plans: dict[str, AssetInventory], item: setup_file.BookingConfiguration
    ) -> AssetInventory | None:
        """The floor plan a container books: none when it is exclusive; the
        one it names, or its asset's only one."""
        if item.consumption_mode != ConsumptionMode.SHARED:
            return None
        if item.floor_plan or len(plans) != 1:
            return plans.get(item.floor_plan)
        return next(iter(plans.values()))

    def clean(self, where: str, row: models.Model, exclude: list[str]) -> bool:
        """Check one object the way the admin screens do, before what it is
        saved with."""
        try:
            row.full_clean(exclude=exclude)
        except ValidationError as error:
            self.faults.extend(_faults(where, error))
            return False
        return True

    def make(
        self,
        where: str,
        row: models.Model,
        *,
        exclude: list[str] | None = None,
        **saving: Any,
    ) -> bool:
        """Check one object the way the admin screens do, and save it.
        `saving` goes to its save: a container's takes the moment the
        validator checks from.
        """
        try:
            # Native capacity rules also run at save time. A rejected row
            # must not poison the outer transaction or prevent later faults.
            with transaction.atomic():
                row.full_clean(exclude=exclude)
                row.save(**saving)
        except ValidationError as error:
            self.faults.extend(_faults(where, error))
            return False
        except IntegrityError as error:
            diagnostic = getattr(error.__cause__, "diag", None)
            rule = getattr(diagnostic, "constraint_name", None) or "integrity"
            self.faults.append(f"{where}: database rule {rule!r} refused this object.")
            return False
        return True

    def category_hours(
        self,
        where: str,
        item: setup_file.BookingConfiguration,
        configuration: BookingConfiguration,
        categories: dict[str, BookingCategory],
        linked: list[str],
        now: datetime,
    ) -> None:
        """A container's own hours for each booking category it names, set
        through the validator's service, as an administrator's change is."""
        for name in linked:
            hours = [
                (WEEKDAYS.index(row.weekday), row.start, row.end)
                for row in item.category_hours
                if row.booking_category == name
            ]
            if not hours:
                continue
            try:
                validator.set_category_hours(
                    configuration, categories[name], hours, now=now
                )
            except ValidationError as error:
                self.faults.extend(_faults(where, error))

    def named[Written: _Named, Row: models.Model](
        self,
        section: str,
        written: tuple[Written, ...],
        row: Callable[[int, Written], Row],
    ) -> dict[str, Row]:
        """Make one section's objects, and give them back by name. Those
        with a number are made in the order of their numbers, since a number
        is taken only if it is above every number given so far.
        """
        made: dict[str, Row] = {}
        for place, item in _by_number(written):
            one = row(place, item)
            if self.make(f"{section}[{place}]", one):
                made[item.name] = one
        return made


class _Named(Protocol):
    """What a section of the file holds that others point to by name."""

    @property
    def name(self) -> str: ...


def _by_number[T](written: tuple[T, ...]) -> list[tuple[int, T]]:
    """Each with its place in the file, in the order of their numbers. What
    has no number keeps the order of the file.
    """
    placed = list(enumerate(written, start=1))
    return sorted(placed, key=lambda one: getattr(one[1], "number", 0))


def _faults(where: str, error: ValidationError) -> list[str]:
    if not hasattr(error, "error_dict"):
        return [f"{where}: {message}" for message in error.messages]
    faults: list[str] = []
    for field, messages in error.message_dict.items():
        place = (
            where if field == "__all__" else f"{where}.{_IN_THE_FILE.get(field, field)}"
        )
        faults.extend(f"{place}: {message}" for message in messages)
    return faults


def export(organization: Organization) -> Setup:
    """Capture one coherent setup, in format 3.

    SHARE locks briefly block writes to configuration tables for every
    organization, including inserts and new order-number allocation on
    Organization. Reservation tables themselves remain unlocked.
    Inside an existing transaction the locks last until that transaction ends.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        # READ COMMITTED alone, even inside atomic(), lets the reads observe
        # different committed configurations. Lock the complete configuration
        # graph before its first read, then materialize the export (including
        # a fresh organization) under it.
        cursor.execute(
            sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
                sql.SQL(", ").join(
                    sql.Identifier(model._meta.db_table)
                    for model in (
                        Organization,
                        Space,
                        AssetInventory,
                        SpotGroup,
                        Spot,
                        Asset,
                        AvailabilityCalendar,
                        AvailabilityClosure,
                        AvailableSpan,
                        BookingType,
                        BookingCategory,
                        Role,
                        RoleCategory,
                        BookingConfiguration,
                        ConfigurationCategory,
                        CategoryHoursSpan,
                        PriceBook,
                        PriceBookEntry,
                        PriceTier,
                    )
                )
            )
        )
        return _export(Organization.objects.get(pk=organization.pk))


def _export(organization: Organization) -> Setup:
    categories = BookingCategory.objects.of(organization)
    position = {category.pk: place for place, category in enumerate(categories)}

    def names(chosen: Iterable[BookingCategory]) -> tuple[str, ...]:
        in_order = sorted(chosen, key=lambda category: position[category.pk])
        return tuple(category.name for category in in_order)

    standard_role = organization.standard_role
    configurations = (
        BookingConfiguration.objects.of(organization)
        .select_related("asset", "booking_type", "price_book", "floor_plan")
        .order_by("number")
    )
    rank = {category.name: place for place, category in enumerate(categories)}
    hours: dict[int, list[setup_file.CategoryHours]] = {}
    for row in CategoryHoursSpan.objects.of(organization).select_related(
        "configuration_category__booking_category"
    ):
        link = row.configuration_category
        hours.setdefault(link.booking_configuration_id, []).append(
            setup_file.CategoryHours(
                link.booking_category.name,
                WEEKDAYS[row.weekday],
                row.start_time,
                row.end_time,
            )
        )
    for rows in hours.values():
        rows.sort(
            key=lambda row: (
                rank[row.booking_category],
                WEEKDAYS.index(row.weekday),
                row.start,
            )
        )
    return Setup(
        format=setup_file.FORMAT,
        spaces=tuple(
            setup_file.Space(
                space.number, space.name, space.description, space.booking_page
            )
            for space in Space.objects.of(organization).order_by("number")
        ),
        labels=tuple(
            (key, organization.labels[key])
            for key in label_set.DEFAULTS
            if key in organization.labels
        ),
        organization=setup_file.Organization(
            name=organization.name,
            subdomain=organization.subdomain,
            time_zone=organization.time_zone,
            currency=organization.currency,
            contact_email=organization.contact_email,
            standard_role=standard_role.name if standard_role else "",
            member_retention_months=organization.member_retention_months,
            booking_retention_months=organization.booking_retention_months,
            max_bookings_a_day=organization.max_bookings_a_day,
            book_ahead_months=organization.book_ahead_months,
            tax_rate=organization.tax_rate,
            free_cancellation_days=organization.free_cancellation_days,
            language=organization.language,
        ),
        assets=tuple(
            setup_file.Asset(
                number=asset.number,
                name=asset.name,
                description=asset.description,
                space=asset.space.name if asset.space else "",
                whole_space=asset.whole_space,
                maximum_occupancy=asset.maximum_occupancy,
                floor_plans=_floor_plans(asset),
                # Only when it is not the organization's, as a file leaves it out.
                time_zone=(
                    asset.time_zone if asset.time_zone != organization.time_zone else ""
                ),
            )
            for asset in Asset.objects.of(organization)
            .select_related("space")
            .order_by("number")
        ),
        availability_calendars=tuple(
            setup_file.AvailabilityCalendar(
                asset=calendar.asset.name,
                start_date=calendar.start_date,
                spans=tuple(
                    setup_file.AvailableSpan(
                        WEEKDAYS[span.weekday], span.start_time, span.end_time
                    )
                    for span in calendar.spans.order_by("weekday", "start_time")
                ),
            )
            for calendar in AvailabilityCalendar.objects.of(organization)
            .select_related("asset")
            .order_by("asset__number", "start_date")
        ),
        closures=tuple(
            setup_file.Closure(
                asset=closure.asset.name,
                first_date=closure.first_date,
                last_date=closure.last_date,
                reason=closure.reason,
            )
            for closure in AvailabilityClosure.objects.of(organization)
            .select_related("asset")
            .order_by("asset__number", "first_date", "last_date", "id")
        ),
        booking_types=tuple(
            setup_file.BookingType(booking_type.name, booking_type.description)
            for booking_type in BookingType.objects.of(organization)
        ),
        booking_categories=tuple(
            setup_file.BookingCategory(category.name, category.icon)
            for category in categories
        ),
        roles=tuple(
            setup_file.Role(
                name=role.name,
                booking_categories=names(role.booking_categories.all()),
                admin_access=role.admin_access,
            )
            for role in Role.objects.of(organization).order_by("id")
        ),
        booking_configurations=tuple(
            _written(
                configuration,
                names(configuration.booking_categories.all()),
                tuple(hours.get(configuration.pk, ())),
            )
            for configuration in configurations
        ),
        price_books=tuple(
            setup_file.PriceBook(
                number=price_book.number,
                name=price_book.name,
                currency=(
                    price_book.currency
                    if price_book.currency != organization.currency
                    else ""
                ),
                entries=tuple(
                    setup_file.PriceBookEntry(
                        booking_configuration=entry.booking_configuration.number,
                        booking_category=entry.booking_category.name,
                        tiers_go_by=entry.tiers_go_by,
                        billing_basis=entry.billing_basis,
                        group=entry.spot_group.name if entry.spot_group else "",
                        tiers=tuple(
                            setup_file.PriceTier(tier.up_to, tier.paid_per, tier.amount)
                            for tier in entry.tiers.order_by("up_to")
                        ),
                    )
                    for entry in sorted(
                        price_book.entries.select_related(
                            "booking_configuration", "booking_category", "spot_group"
                        ),
                        key=lambda entry: (
                            entry.booking_configuration.number,
                            position[entry.booking_category_id],
                            entry.spot_group.number if entry.spot_group else 0,
                        ),
                    )
                ),
            )
            for price_book in PriceBook.objects.of(organization).order_by("number")
        ),
    )


def _floor_plans(asset: Asset) -> tuple[setup_file.FloorPlan, ...]:
    """An asset's floor plans as a setup file writes them, in the order they
    were made. The floor plan an asset makes for itself, of one spot without
    a name, is left out, as a file leaves it out.
    """
    written: list[setup_file.FloorPlan] = []
    plans = AssetInventory.objects.of(asset.organization_id).filter(asset=asset)
    for plan in plans.order_by("id"):
        spots = list(
            Spot.objects.of(asset.organization_id)
            .filter(floor_plan=plan)
            .order_by("number")
        )
        groups = list(
            SpotGroup.objects.of(asset.organization_id)
            .filter(floor_plan=plan)
            .order_by("number")
        )
        # A file numbers spots and groups from 1 in the order written; a
        # removed spot  leaves a gap in the numbers the database keeps,
        # so a file counts the rows that are left. Bookings hold spots by
        # their ids, never by these numbers.
        place = {spot.pk: at for at, spot in enumerate(spots, start=1)}
        group_place = {group.pk: at for at, group in enumerate(groups, start=1)}
        written.append(
            setup_file.FloorPlan(
                name=plan.name,
                spot_word=plan.unit,
                spots=tuple(spot.name for spot in spots),
                groups=tuple(
                    setup_file.SpotGroup(
                        name=group.name,
                        word=group.word,
                        spots=tuple(
                            place[spot.pk]
                            for spot in spots
                            if spot.group_id is not None
                            and group_place[spot.group_id] == at
                        ),
                    )
                    for at, group in enumerate(groups, start=1)
                ),
            )
        )
    if written == [
        setup_file.FloorPlan(name="", spot_word=DEFAULT_SPOT_WORD, spots=("",))
    ]:
        return ()
    return tuple(written)


def _written(
    configuration: BookingConfiguration,
    booking_categories: tuple[str, ...],
    category_hours: tuple[setup_file.CategoryHours, ...] = (),
) -> setup_file.BookingConfiguration:
    # For a fixed period the system sets the three lengths, and the file
    # leaves them out.
    flexible = configuration.period_type == PeriodType.FLEXIBLE
    return setup_file.BookingConfiguration(
        number=configuration.number,
        asset=configuration.asset.name,
        booking_type=configuration.booking_type.name,
        period_type=configuration.period_type,
        period_start=configuration.period_start,
        period_end=configuration.period_end,
        end_day=configuration.end_day,
        period_start_weekdays=tuple(
            WEEKDAYS[day] for day in configuration.period_start_weekdays
        ),
        interval=configuration.interval if flexible else None,
        minimum_length=configuration.minimum_length if flexible else None,
        maximum_length=configuration.maximum_length if flexible else None,
        start_date=configuration.start_date,
        stop_date=configuration.stop_date,
        every_weeks=configuration.every_weeks,
        status=configuration.status,
        price_book=configuration.price_book.name if configuration.price_book else "",
        booking_categories=booking_categories,
        category_hours=category_hours,
        consumption_mode=configuration.consumption_mode,
        floor_plan=configuration.floor_plan.name if configuration.floor_plan else "",
    )
