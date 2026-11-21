# SPDX-License-Identifier: AGPL-3.0-only
"""Containers as an administrator sets them .

The container is the master object: asset -> container -> grid . A
change runs in `organization_transaction` (the organization's row is locked,
so two administrators take turns and the second works on what the first left)
and writes its audit event first, in the same transaction ; a
removal's event is written before the delete.

The rules are the model's own and nothing is copied here: a container is
checked with `full_clean` as the setup file's loader checks it, and saved
through `BookingConfiguration.save`, which runs the validator (docs/booking-model.md,
"The validator"): its own overlaps and week wrap, the availability cover, the
shared containers on other floor plans, the fields that stay while a booking is
still to come, the prices. A refusal carries the model's words (`reasons`).
What the setup file would not read back (a container with no booking category,
a price for a category the container no longer names) is refused too, so an
export made after any change on the screens reads and loads again. A booking
category with a booking still to come cannot be taken off its container , nor one the
container has prices for.

A saved change touches no booking already made: the fields a booking depends
on stay as they are while one is still to come (docs/booking-model.md, "After a
container is saved"), and the others apply to new bookings only, so there is
nothing to cancel or refund and no preview of it.

Prices (price book entries) belong to part 7.97; here a container only names its
price book, and the place says per category whether the container has a price.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from randirandi import labels
from randirandi.core import validator
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    Asset,
    AssetInventory,
    Block,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    CategoryHoursSpan,
    ConfigurationCategory,
    ConsumptionMode,
    Member,
    Organization,
    PeriodType,
    PriceBook,
    PriceBookEntry,
    Status,
    Weekday,
)
from randirandi.core.models.base import clock
from randirandi.core.setup_admin import (
    SetupNotFoundError,
    SetupRefusedError,
    organization_transaction,
)

type Fields = Mapping[str, object]

# The most hours a category may have on a container: one stretch a weekday, a
# few of them; the model refuses overlaps. A bound for the body's size only.
MOST_HOURS = 7 * 8


class ContainerRefusedError(SetupRefusedError):
    """The model refuses the change. `reasons` are its words, each with the
    field it is reported on ("" for the container as a whole)."""

    def __init__(self, code: str, reasons: list[dict[str, str]]) -> None:
        super().__init__(code)
        self.reasons = reasons


def _reasons(error: ValidationError) -> list[dict[str, str]]:
    if hasattr(error, "error_dict"):
        return [
            {"field": "" if field == NON_FIELD_ERRORS else field, "text": text}
            for field, messages in error.message_dict.items()
            for text in messages
        ]
    return [{"field": "", "text": text} for text in error.messages]


def _refused(error: ValidationError) -> ContainerRefusedError:
    return ContainerRefusedError("refused", _reasons(error))


def _named(config: BookingConfiguration) -> str:
    return f"container {config}"


# --- What the place shows -------------------------------------------------------


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _plan_name(plan: AssetInventory, place: int) -> str:
    return plan.name or f"Floor plan {place}"


def containers_place(
    org: Organization, *, now: datetime | None = None
) -> dict[str, object]:
    """The containers by number, with what the screens need to draw and to
    lock each one, and the lists a container chooses from."""
    now = now or timezone.now()
    configs = list(
        BookingConfiguration.objects.of(org)
        .select_related("asset", "floor_plan", "price_book")
        .order_by("number")
    )
    links: dict[int, list[ConfigurationCategory]] = defaultdict(list)
    for link in (
        ConfigurationCategory.objects.of(org)
        .select_related("booking_category")
        .order_by("booking_category__position", "booking_category_id")
    ):
        links[link.booking_configuration_id].append(link)
    hours: dict[int, list[dict[str, int]]] = defaultdict(list)
    for span in CategoryHoursSpan.objects.of(org).order_by(
        "weekday", "start_time", "pk"
    ):
        hours[span.configuration_category_id].append(
            {"weekday": span.weekday, "start": span.start_time, "end": span.end_time}
        )
    priced = set(
        PriceBookEntry.objects.of(org).values_list(
            "booking_configuration", "booking_category"
        )
    )
    priced_containers = {config_id for config_id, _ in priced}
    coming: dict[int, list[tuple[int, int, str, datetime]]] = defaultdict(list)
    for config_id, category_id, number, reference, starts in (
        Booking.objects.of(org)
        .filter(taken_time__state__in=validator.TO_COME, taken_time__ends_at__gt=now)
        .order_by("taken_time__starts_at", "pk")
        .values_list(
            "booking_configuration",
            "booking_category",
            "order__number",
            "order__reference",
            "taken_time__starts_at",
        )
    ):
        coming[config_id].append((category_id, number, reference, starts))
    booked = set(
        Booking.objects.of(org).values_list("booking_configuration", flat=True)
    )
    blocked = set(Block.objects.of(org).values_list("booking_configuration", flat=True))
    plans = {
        asset.pk: list(
            AssetInventory.objects.of(org).filter(asset=asset).order_by("pk")
        )
        for asset in Asset.objects.of(org)
    }
    rows: list[dict[str, object]] = []
    for config in configs:
        ahead = coming.get(config.pk, [])
        last = None
        if ahead:
            _, number, reference, starts = ahead[-1]
            last = {
                "order": number,
                "reference": reference,
                "date": starts.astimezone(config.asset.zone).date().isoformat(),
            }
        has_prices = config.pk in priced_containers
        rows.append(
            {
                "id": config.pk,
                "number": config.number,
                "asset": config.asset_id,
                "booking_type": config.booking_type_id,
                "period_type": config.period_type,
                "period_start": config.period_start,
                "period_end": config.period_end,
                "end_day": config.end_day,
                "weekdays": sorted(config.period_start_weekdays),
                "interval": config.interval,
                "minimum_length": config.minimum_length,
                "maximum_length": config.maximum_length,
                "first_week": _iso(config.start_date),
                "last_week": _iso(
                    None
                    if config.stop_date is None
                    else config.stop_date - timedelta(days=6)
                ),
                "every_weeks": config.every_weeks,
                "status": config.status,
                "uses": config.floor_plan_id,
                "price_book": config.price_book_id,
                "categories": [
                    {
                        "id": link.booking_category_id,
                        "hours": hours.get(link.pk, []),
                        "priced": (config.pk, link.booking_category_id) in priced,
                        "to_come": sum(
                            1 for c, *_ in ahead if c == link.booking_category_id
                        ),
                    }
                    for link in links.get(config.pk, [])
                ],
                "to_come": len(ahead),
                "last_to_come": last,
                "locked": bool(ahead),
                "removable": not (
                    config.pk in booked or config.pk in blocked or has_prices
                ),
            }
        )
    return {
        "containers": rows,
        "assets": [
            {
                "id": asset.pk,
                "name": asset.name,
                "floor_plans": [
                    {
                        "id": plan.pk,
                        "name": _plan_name(plan, place),
                        "capacity": plan.capacity,
                        "unit": plan.unit,
                    }
                    for place, plan in enumerate(plans[asset.pk], start=1)
                ],
            }
            for asset in Asset.objects.of(org).order_by("number")
        ],
        "booking_types": [
            {"id": row.pk, "name": row.name} for row in BookingType.objects.of(org)
        ],
        "booking_categories": [
            {"id": row.pk, "name": row.name} for row in BookingCategory.objects.of(org)
        ],
        "price_books": [
            {"id": row.pk, "name": row.name}
            for row in PriceBook.objects.of(org).order_by("number")
        ],
    }


# --- The body ---------------------------------------------------------------------


def _int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("A whole number.")
    return value


def _optional_id(value: object) -> int | None:
    return None if value is None else _int(value)


def _week(value: object) -> date | None:
    """A calendar week as its Monday, "2027-03-01"; none is open."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A week is a date.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("A week is a date.") from None


SCALARS = {
    "period_type": str,
    "status": str,
    "period_start": int,
    "period_end": int,
    "end_day": int,
    "interval": int,
    "minimum_length": int,
    "maximum_length": int,
    "every_weeks": int,
}
CHANGEABLE = {
    *SCALARS,
    "asset",
    "booking_type",
    "uses",
    "price_book",
    "weekdays",
    "first_week",
    "last_week",
}


def _exact(fields: Fields, allowed: set[str], *, required: set[str]) -> None:
    if set(fields) - allowed or required - set(fields):
        raise ValueError("The body is wrong.")


def _values(fields: Fields, *, allowed: set[str]) -> dict[str, Any]:
    """The values of the body, by their type; their meaning is the model's."""
    found: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError("The body is wrong.")
        if key in SCALARS and SCALARS[key] is int:
            found[key] = _int(value)
        elif key in SCALARS:
            if not isinstance(value, str):
                raise ValueError("Text.")
            found[key] = value
        elif key in ("asset", "booking_type"):
            found[key] = _int(value)
        elif key in ("uses", "price_book"):
            found[key] = _optional_id(value)
        elif key == "weekdays":
            if not isinstance(value, list) or any(type(d) is not int for d in value):
                raise ValueError("Weekdays are a list of numbers.")
            found[key] = value
        else:
            found[key] = _week(value)
    return found


def _of_org(model: Any, org: Organization, pk: int | None, what: str) -> Any:
    if pk is None:
        return None
    row = model.objects.of(org).filter(pk=pk).first()
    if row is None:
        raise ContainerRefusedError(
            "unknown",
            [{"field": what, "text": f"There is no such {what.replace('_', ' ')}."}],
        )
    return row


def _apply(
    org: Organization, row: BookingConfiguration, values: dict[str, Any]
) -> dict[str, Any]:
    """Set the values on the container; what was set, as the container's own
    attributes, for comparing with what it was."""
    set_: dict[str, Any] = {}
    for key in ("period_type", "status"):
        if key in values:
            set_[key] = values[key]
    for key in (
        "period_start",
        "period_end",
        "end_day",
        "interval",
        "minimum_length",
        "maximum_length",
        "every_weeks",
    ):
        if key in values:
            set_[key] = values[key]
    if "weekdays" in values:
        set_["period_start_weekdays"] = sorted(set(values["weekdays"]))
    if "asset" in values:
        asset = _of_org(Asset, org, values["asset"], "asset")
        set_["asset"] = asset
    if "booking_type" in values:
        set_["booking_type"] = _of_org(
            BookingType, org, values["booking_type"], "booking_type"
        )
    if "price_book" in values:
        set_["price_book"] = _of_org(PriceBook, org, values["price_book"], "price_book")
    if "uses" in values:
        plan = _of_org(AssetInventory, org, values["uses"], "floor_plan")
        set_["floor_plan"] = plan
        set_["consumption_mode"] = (
            ConsumptionMode.EXCLUSIVE if plan is None else ConsumptionMode.SHARED
        )
    if "first_week" in values:
        set_["start_date"] = values["first_week"]
    if "last_week" in values:
        last = values["last_week"]
        try:
            set_["stop_date"] = None if last is None else last + timedelta(days=6)
        except OverflowError:
            raise ValueError("A week is a date.") from None
    # Another asset has other floor plans: the container then uses the whole
    # asset unless the body says which plan.
    if (
        "uses" not in values
        and set_.get("asset") is not None
        and set_["asset"].pk != row.asset_id
    ):
        set_["floor_plan"] = None
        set_["consumption_mode"] = ConsumptionMode.EXCLUSIVE
    return set_


def _differs(row: BookingConfiguration, set_: dict[str, Any]) -> bool:
    fixed = set_.get("period_type", row.period_type) == PeriodType.FIXED
    for key, value in set_.items():
        if fixed and key in ("interval", "minimum_length", "maximum_length"):
            continue
        if getattr(row, key) != value:
            return True
    return False


def _checked(row: BookingConfiguration, *, adding: bool, now: datetime) -> None:
    """The model's validation, as the setup file's loader applies it, then the
    save through the validator. Either refuses with the model's words."""
    try:
        row.full_clean(exclude=["number"] if adding else None)
        row.save(now=now)
    except ValidationError as error:
        raise _refused(error) from None
    except IntegrityError:
        raise ContainerRefusedError(
            "refused",
            [{"field": "", "text": "The database refused the change."}],
        ) from None


def _locked(org: Organization, pk: int) -> BookingConfiguration:
    row = (
        BookingConfiguration.objects.of(org)
        .select_for_update(of=("self",))
        .select_related("asset", "booking_type", "floor_plan", "price_book")
        .filter(pk=pk)
        .first()
    )
    if row is None:
        raise SetupNotFoundError
    return row


# --- Add -----------------------------------------------------------------------

# What a new container is, as the drawing makes it: disabled, every day, 10:00
# to 22:00, a flexible one by the hour up to three, a fixed one the whole
# period; every booking category of the organization.
NEW_START = 10 * 60
NEW_END = 22 * 60
NEW_WEEKDAYS = list(range(7))
NEW_INTERVAL = 60
NEW_MINIMUM = 60
NEW_MAXIMUM = 180


def make_container(
    administrator: Member, fields: Fields, *, now: datetime | None = None
) -> int:
    """Add a container, disabled, with every booking category of the
    organization; it is then set on its own page. {"asset", "booking_type",
    "period_type", "uses"} (the floor plan it books, none for the whole
    asset). Returns its id."""
    _exact(
        fields,
        {"asset", "booking_type", "period_type", "uses"},
        required={"asset", "booking_type", "period_type"},
    )
    values = _values(fields, allowed={"asset", "booking_type", "period_type", "uses"})
    if values["period_type"] not in PeriodType.values:
        raise ValueError("The period type is fixed or flexible.")
    when = now or timezone.now()

    def apply(org: Organization) -> int:
        asset = _of_org(Asset, org, values["asset"], "asset")
        kind = _of_org(BookingType, org, values["booking_type"], "booking_type")
        categories = list(BookingCategory.objects.of(org).select_for_update())
        if not categories:
            raise SetupRefusedError("no-category")
        plan = _of_org(AssetInventory, org, values.get("uses"), "floor_plan")
        row = BookingConfiguration(
            organization=org,
            asset=asset,
            booking_type=kind,
            period_type=values["period_type"],
            period_start=NEW_START,
            period_end=NEW_END,
            end_day=0,
            period_start_weekdays=list(NEW_WEEKDAYS),
            interval=NEW_INTERVAL,
            minimum_length=NEW_MINIMUM,
            maximum_length=NEW_MAXIMUM,
            status=Status.DISABLED,
            consumption_mode=(
                ConsumptionMode.EXCLUSIVE if plan is None else ConsumptionMode.SHARED
            ),
            floor_plan=plan,
        )
        _checked(row, adding=True, now=when)
        for category in categories:
            link = ConfigurationCategory(
                organization=org, booking_configuration=row, booking_category=category
            )
            try:
                link.full_clean()
                link.save()
            except ValidationError as error:
                raise _refused(error) from None
        record_event(
            actor=administrator,
            action=AuditAction.MADE_BOOKING_CONFIGURATION,
            target=row,
        )
        return row.pk

    return organization_transaction(administrator.organization_id, apply)


# --- Change --------------------------------------------------------------------


def change_container(
    administrator: Member, pk: int, fields: Fields, *, now: datetime | None = None
) -> None:
    """Change what a container's sheets set: any of "asset", "booking_type",
    "uses" (a floor plan's id, none for the whole asset), "price_book" (none
    is without payment), "period_type", "period_start", "period_end",
    "end_day", "weekdays", "interval", "minimum_length", "maximum_length",
    "first_week" and "last_week" (the Monday of a calendar week, none is open),
    "every_weeks" and "status" ("enabled" or "disabled"). What is left out
    stays. A change that changes nothing writes nothing and is not checked."""
    values = _values(fields, allowed=CHANGEABLE | {"status"})
    if not values:
        raise ValueError("The body is wrong.")
    when = now or timezone.now()

    def apply(org: Organization) -> None:
        row = _locked(org, pk)
        set_ = _apply(org, row, values)
        if not _differs(row, set_):
            return
        record_event(
            actor=administrator,
            action=AuditAction.CHANGED_BOOKING_CONFIGURATION,
            target=row,
        )
        for key, value in set_.items():
            setattr(row, key, value)
        _checked(row, adding=False, now=when)

    organization_transaction(administrator.organization_id, apply)


# --- Booking categories ----------------------------------------------------------


def _hours(raw: object) -> list[tuple[int, int, int]]:
    if not isinstance(raw, list) or len(raw) > MOST_HOURS:
        raise ValueError("Hours are a list.")
    found = []
    for span in raw:
        if not isinstance(span, dict) or set(span) != {"weekday", "start", "end"}:
            raise ValueError("An hour is a weekday, a start and an end.")
        found.append((_int(span["weekday"]), _int(span["start"]), _int(span["end"])))
    return found


def _seen(fields: Fields) -> dict[int, set[tuple[int, int, int]]]:
    """What the sheet saw: each category the container had, with its own hours."""
    given = fields["seen"]
    if not isinstance(given, list):
        raise ValueError("Seen is a list.")
    seen: dict[int, set[tuple[int, int, int]]] = {}
    for item in given:
        if not isinstance(item, dict) or set(item) != {"category", "hours"}:
            raise ValueError("Seen is a category and its hours.")
        pk = _int(item["category"])
        if pk in seen:
            raise ValueError("A category once.")
        seen[pk] = set(_hours(item["hours"]))
    return seen


def _wanted(fields: Fields) -> dict[int, list[tuple[int, int, int]]]:
    _exact(fields, {"categories", "seen"}, required={"categories", "seen"})
    given = fields["categories"]
    if not isinstance(given, list):
        raise ValueError("Categories are a list.")
    wanted: dict[int, list[tuple[int, int, int]]] = {}
    for item in given:
        if not isinstance(item, dict) or set(item) - {"category", "hours"}:
            raise ValueError("A category is an id and its hours.")
        if "category" not in item:
            raise ValueError("A category is an id and its hours.")
        pk = _int(item["category"])
        if pk in wanted:
            raise ValueError("A category once.")
        wanted[pk] = _hours(item.get("hours", []))
    return wanted


def _words(rows: Sequence[tuple[int, int, int]]) -> str:
    return ", ".join(
        f"{Weekday(w).label} {clock(s)} to {clock(e)}" if w in Weekday.values else "?"
        for w, s, e in rows
    )


def change_categories(
    administrator: Member, pk: int, fields: Fields, *, now: datetime | None = None
) -> None:
    """Set the booking categories of a container and each one's own hours:
    {"categories": [{"category": id, "hours": [{"weekday", "start", "end"}]}]},
    "seen": [{"category": id, "hours": [...]}]} the whole list, as the sheet has
    it, and what the sheet saw (each category with its own hours); read under the
    lock, when that is no longer what the container has the sheet is stale and
    nothing is saved. A category with no hours has the
    container's. Refused, with the model's words: none left, one with a booking
    still to come taken off , one the container has prices for
    taken off, hours outside the container's or overlapping, hours that would
    leave a booking still to come outside them."""
    wanted = _wanted(fields)
    seen = _seen(fields)
    when = now or timezone.now()

    def apply(org: Organization) -> None:
        row = _locked(org, pk)
        if not wanted:
            raise SetupRefusedError("no-category")
        chosen = {
            c.pk: c
            for c in BookingCategory.objects.of(org)
            .select_for_update()
            .filter(pk__in=set(wanted))
        }
        if len(chosen) != len(wanted):
            raise SetupRefusedError("unknown-category")
        links = {
            link.booking_category_id: link
            for link in ConfigurationCategory.objects.of(org)
            .select_for_update()
            .select_related("booking_category")
            .filter(booking_configuration=row)
        }
        stored: dict[int, set[tuple[int, int, int]]] = defaultdict(set)
        for link_id, weekday, start, end in CategoryHoursSpan.objects.of(
            org
        ).values_list("configuration_category", "weekday", "start_time", "end_time"):
            stored[link_id].add((weekday, start, end))
        if seen != {category: stored[link.pk] for category, link in links.items()}:
            raise ContainerRefusedError(
                "refused",
                [
                    {
                        "field": "",
                        "text": "The categories or hours of this container "
                        "changed while this sheet was open. Reload and edit again.",
                    }
                ],
            )
        gone = [link for category, link in links.items() if category not in wanted]
        added = [pk_ for pk_ in wanted if pk_ not in links]
        reshaped = [
            pk_
            for pk_ in wanted
            if pk_ in links and set(wanted[pk_]) != stored[links[pk_].pk]
        ]
        if not (gone or added or reshaped):
            return
        faults = _taken_off(org, row, gone, when)
        if faults:
            raise ContainerRefusedError("refused", faults)
        record_event(
            actor=administrator,
            action=AuditAction.CHANGED_BOOKING_CONFIGURATION,
            target=row,
        )
        for category_id in added:
            link = ConfigurationCategory(
                organization=org,
                booking_configuration=row,
                booking_category=chosen[category_id],
            )
            try:
                link.full_clean()
                link.save()
            except ValidationError as error:
                raise _refused(error) from None
        for category_id in [*added, *reshaped]:
            if not wanted[category_id] and category_id in added:
                continue
            try:
                validator.set_category_hours(
                    row, chosen[category_id], wanted[category_id], now=when
                )
            except ValidationError as error:
                raise _refused(error) from None
        for link in gone:
            link.delete()

    organization_transaction(administrator.organization_id, apply)


def _taken_off(
    org: Organization,
    row: BookingConfiguration,
    gone: list[ConfigurationCategory],
    now: datetime,
) -> list[dict[str, str]]:
    """Why the categories cannot be taken off the container: a booking still
    to come under it , or a price of the container for it."""
    faults: list[dict[str, str]] = []
    for link in gone:
        category = link.booking_category
        ahead = list(
            Booking.objects.of(org)
            .filter(
                booking_configuration=row.pk,
                booking_category=category.pk,
                taken_time__state__in=validator.TO_COME,
                taken_time__ends_at__gt=now,
            )
            .select_related("order", "taken_time")
            .order_by("taken_time__starts_at")
        )
        if ahead:
            first = ahead[0]
            day = first.taken_time.starts_at.astimezone(row.asset.zone)
            faults.append(
                {
                    "field": "booking_categories",
                    "text": f"{category.name} stays while the container has a booking "
                    f"still to come: order {first.order.reference}, from "
                    f"{labels.date_words(None, day, year=True)}"
                    + (f" and {len(ahead) - 1} more" if len(ahead) > 1 else "")
                    + ". Cancel those bookings first.",
                }
            )
        elif (
            PriceBookEntry.objects.of(org)
            .filter(booking_configuration=row.pk, booking_category=category.pk)
            .exists()
        ):
            faults.append(
                {
                    "field": "booking_categories",
                    "text": f"{category.name} has a price on this container: remove "
                    "its price first.",
                }
            )
    return faults


# --- Remove ----------------------------------------------------------------------


def remove_container(administrator: Member, pk: int) -> None:
    """Remove a container nothing names: never booked, no block, no price
    (docs/booking-model.md: a container is deleted only if it has never been booked).
    Its booking categories and their hours go with it."""

    def apply(org: Organization) -> None:
        row = _locked(org, pk)
        held = {
            "bookings": Booking.objects.of(org)
            .filter(booking_configuration=row)
            .count(),
            "blocks": Block.objects.of(org).filter(booking_configuration=row).count(),
            "prices": PriceBookEntry.objects.of(org)
            .filter(booking_configuration=row)
            .count(),
        }
        if any(held.values()):
            raise SetupRefusedError("in-use", {k: v for k, v in held.items() if v})
        record_event(
            actor=administrator,
            action=AuditAction.REMOVED_BOOKING_CONFIGURATION,
            target=row,
        )
        try:
            row.delete()
        except ProtectedError:
            raise SetupRefusedError("in-use") from None

    organization_transaction(administrator.organization_id, apply)


__all__ = [
    "ContainerRefusedError",
    "change_categories",
    "change_container",
    "containers_place",
    "make_container",
    "remove_container",
]
