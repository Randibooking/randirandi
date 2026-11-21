# SPDX-License-Identifier: AGPL-3.0-only
"""Price books, their prices and price tiers, as an administrator sets them
.

Every change runs in `setup_admin.organization_transaction`: the organization's
row is locked, so two administrators editing the same price take turns and the
second works on what the first left, and an order being placed (which takes
the same lock) prices from one coherent book. A change writes its audit event
in the same transaction. The rules are the model's own: a row is checked with
`full_clean` as the setup file's loader checks it, a tier's values with the
setup file's own checks (`setup_file._length_fault` and the others), and the
tiers must reach what the container can be booked for
(`prices.what_the_tiers_leave_out`), so an export made after any change on
the screens reads and loads again.

A price is a set: the entries one container has in one booking category in its
price book (the normal price and, on a shared container, one for each group of
spots with its own). It is saved and removed as a whole, all or none.

Nothing here touches an order already made. A booking line keeps copies of the
tier's "up to" and amount and of the price book's number, and names the book
(protected), never an entry or a tier (BOOKING-MODEL rule 9).
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Count
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from randirandi import setup_file
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.calendar import _instants
from randirandi.core.models import (
    BillingBasis,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    ConfigurationCategory,
    Currency,
    Member,
    Organization,
    PeriodType,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    SpotGroup,
    TiersGoBy,
)
from randirandi.core.models.base import DAY
from randirandi.core.models.prices import (
    ENTRY_FIXES_CURRENCY,
    what_the_tiers_leave_out,
)
from randirandi.core.pricing import what_it_costs
from randirandi.core.setup_admin import (
    Fields,
    SetupNotFoundError,
    SetupRefusedError,
    _name,
    _only,
    _taken,
    organization_transaction,
)
from randirandi.price_entries_rule import what_the_entries_of_a_category_get_wrong

MOST_TIERS = 50
MOST_ENTRIES = 100


@dataclass(frozen=True)
class TierValues:
    up_to: int
    paid_per: int
    amount: int


@dataclass(frozen=True)
class EntryValues:
    group: int | None
    tiers_go_by: str
    billing_basis: str
    tiers: tuple[TierValues, ...]


# --- What the place shows -------------------------------------------------------


MINUTE = timedelta(minutes=1)
MOST_EXAMPLES = 4
LOOK_AHEAD_DAYS = 400


def _example_times(
    config: BookingConfiguration, entry: PriceBookEntry, tiers: Sequence[PriceTier]
) -> list[tuple[int, int]]:
    """The bookings an entry's examples show, as (minute of the day it starts,
    minutes long): a fixed period whole; a flexible one by length at the
    shortest, past each tier and at the longest; by time of day from the start
    of the period to each tier's end and the last shortest booking."""
    start = config.period_start
    whole = config.end_day * DAY + config.period_end
    if config.period_type == PeriodType.FIXED:
        return [(start, whole - start)]
    low, high, step = config.minimum_length, config.maximum_length, config.interval
    if entry.tiers_go_by == TiersGoBy.LENGTH:
        lengths = {low, high, *(t.up_to + step for t in tiers)}
        return [
            (start, m)
            for m in sorted(lengths)
            if low <= m <= high and m % step == 0 and start + m <= whole
        ][:MOST_EXAMPLES]
    ends = {t.up_to for t in tiers} | {whole}
    spans = [(start, x - start) for x in sorted(ends) if x <= whole]
    spans.append((max(start, whole - low), min(low, whole - start)))
    return [
        (a, m)
        for a, m in spans
        if low <= m <= high and m % step == 0 and (a - start) % step == 0
    ]


def _clean_day(
    config: BookingConfiguration, times: Sequence[tuple[int, int]]
) -> date | None:
    """The first day from today on that the container starts, where every
    example begins and ends at one moment of the asset's time zone and lasts
    as long as the clock says, so that a clock change does not move one."""
    zone = config.asset.zone
    today = timezone.now().astimezone(zone).date()
    for ahead in range(LOOK_AHEAD_DAYS):
        day = today + timedelta(days=ahead)
        if not config.starts_on(day):
            continue
        midnight = datetime.combine(day, time())
        walls = [(midnight + a * MINUTE, midnight + (a + m) * MINUTE) for a, m in times]
        found = [(_instants(a, zone), _instants(b, zone)) for a, b in walls]
        if all(
            len(a) == len(b) == 1 and (b[0] - a[0]) // MINUTE == m
            for (a, b), (_, m) in zip(found, times, strict=True)
        ):
            return day
    return None


def _examples(
    config: BookingConfiguration,
    category: BookingCategory,
    entries: Sequence[PriceBookEntry],
    tiers: Mapping[int, Sequence[PriceTier]],
    groups: Mapping[int, SpotGroup],
) -> dict[int, list[dict[str, int | str]]]:
    """A few bookings of one spot and what each costs, by entry: priced by
    `pricing.what_it_costs`, the function Book and the order price with, on
    the first day the container starts that has no clock change in it."""
    times = {e.pk: _example_times(config, e, tiers.get(e.pk, [])) for e in entries}
    day = _clean_day(config, sorted({t for each in times.values() for t in each}))
    if day is None:
        return {e.pk: [] for e in entries}
    zone = config.asset.zone
    midnight = datetime.combine(day, time())

    def instants(minute: int) -> datetime:
        return _instants(midnight + minute * MINUTE, zone)[0]

    wanted = {
        e.spot_group_id: [(instants(a), instants(a + m)) for a, m in times[e.pk]]
        for e in entries
    }
    totals = what_it_costs(config, category, wanted, groups)
    return {
        e.pk: [
            {"day": day.isoformat(), "start": a, "length": m, "total": total}
            for (a, m), total in zip(times[e.pk], totals[e.spot_group_id], strict=True)
            if total is not None
        ]
        for e in entries
    }


def prices_place(org: Organization) -> dict[str, object]:
    """The price books with what holds each, the containers with payment with
    the prices they have in their book, and the booking categories (in the
    order members see them). Amounts are in the book's smallest unit."""
    containers_of = dict(
        BookingConfiguration.objects.of(org)
        .exclude(price_book=None)
        .values_list("price_book")
        .annotate(n=Count("pk"))
    )
    entries_of = dict(
        PriceBookEntry.objects.of(org).values_list("price_book").annotate(n=Count("pk"))
    )
    lines_of = dict(
        BookingLine.objects.of(org).values_list("price_book").annotate(n=Count("pk"))
    )
    categories = list(BookingCategory.objects.of(org))
    containers = list(
        BookingConfiguration.objects.of(org)
        .exclude(price_book=None)
        .select_related("asset", "booking_type", "floor_plan")
        .order_by("number")
    )
    offered: dict[int, list[int]] = {}
    for config_id, category_id in (
        ConfigurationCategory.objects.of(org)
        .order_by("booking_category__position", "booking_category_id")
        .values_list("booking_configuration", "booking_category")
    ):
        offered.setdefault(config_id, []).append(category_id)
    plans = {c.floor_plan_id for c in containers if c.floor_plan_id is not None}
    groups: dict[int, list[dict[str, object]]] = {}
    for group in (
        SpotGroup.objects.of(org).filter(floor_plan__in=plans).order_by("number")
    ):
        groups.setdefault(group.floor_plan_id, []).append(
            {"id": group.pk, "name": group.name, "word": group.word}
        )
    tiers: dict[int, list[dict[str, int]]] = {}
    tier_rows: dict[int, list[PriceTier]] = {}
    for tier in PriceTier.objects.of(org).order_by("up_to"):
        tier_rows.setdefault(tier.price_book_entry_id, []).append(tier)
        tiers.setdefault(tier.price_book_entry_id, []).append(
            {"up_to": tier.up_to, "paid_per": tier.paid_per, "amount": tier.amount}
        )
    spot_groups = {g.pk: g for g in SpotGroup.objects.of(org)}
    category_of = {c.pk: c for c in categories}
    examples: dict[int, list[dict[str, int | str]]] = {}
    held: dict[tuple[int, int], list[PriceBookEntry]] = {}
    for row in PriceBookEntry.objects.of(org).order_by(
        "booking_category__position", "spot_group__number", "pk"
    ):
        held.setdefault(
            (row.booking_configuration_id, row.booking_category_id), []
        ).append(row)
    stamps: dict[int, dict[str, str]] = {}
    for c in containers:
        for category_id in offered.get(c.pk, []):
            rows = [
                row
                for row in held.get((c.pk, category_id), [])
                if row.price_book_id == c.price_book_id
            ]
            stamps.setdefault(c.pk, {})[str(category_id)] = _stamp_of(
                (
                    row.spot_group_id,
                    row.tiers_go_by,
                    row.billing_basis,
                    [
                        (t.up_to, t.paid_per, t.amount)
                        for t in tier_rows.get(row.pk, [])
                    ],
                )
                for row in rows
            )
            if rows:
                examples.update(
                    _examples(c, category_of[category_id], rows, tier_rows, spot_groups)
                )
    entries: dict[int, list[dict[str, object]]] = {}
    for entry in PriceBookEntry.objects.of(org).order_by(
        "booking_category__position", "spot_group__number", "pk"
    ):
        if entry.price_book_id is None:
            continue
        entries.setdefault(entry.booking_configuration_id, []).append(
            {
                "id": entry.pk,
                "book": entry.price_book_id,
                "category": entry.booking_category_id,
                "group": entry.spot_group_id,
                "tiers_go_by": entry.tiers_go_by,
                "billing_basis": entry.billing_basis,
                "tiers": tiers.get(entry.pk, []),
                "examples": examples.get(entry.pk, []),
            }
        )
    return {
        "price_books": [
            {
                "id": book.pk,
                "number": book.number,
                "name": book.name,
                "currency": book.currency,
                "containers": containers_of.get(book.pk, 0),
                "entries": entries_of.get(book.pk, 0),
                "bookings": lines_of.get(book.pk, 0),
                "removable": not containers_of.get(book.pk)
                and not lines_of.get(book.pk),
            }
            for book in PriceBook.objects.of(org)
        ],
        "currencies": list(Currency.values),
        "currency": org.currency,
        "booking_categories": [{"id": c.pk, "name": c.name} for c in categories],
        "containers": [
            {
                "id": c.pk,
                "number": c.number,
                "book": c.price_book_id,
                "type": c.booking_type.name,
                "asset": c.asset.name,
                "weekdays": sorted(set(c.period_start_weekdays)),
                "start": c.period_start,
                "end": c.end_day * DAY + c.period_end,
                "period": c.period_type,
                "interval": c.interval,
                "minimum": c.minimum_length,
                "maximum": c.maximum_length,
                "mode": c.consumption_mode,
                "unit": c.floor_plan.unit if c.floor_plan else "",
                "groups": groups.get(c.floor_plan_id, []) if c.floor_plan_id else [],
                "categories": offered.get(c.pk, []),
                "stamps": stamps.get(c.pk, {}),
                # Only the prices in the book it names now; one left in
                # another is not read (the setup file refuses it).
                "entries": [
                    {key: value for key, value in e.items() if key not in {"book"}}
                    for e in entries.get(c.pk, [])
                    if e["book"] == c.price_book_id
                ],
            }
            for c in containers
        ],
    }


# --- Price books ----------------------------------------------------------------


def _book_values(fields: Fields, *, new: bool) -> dict[str, str]:
    _only(fields, {"name", "currency"}, required={"name"} if new else set())
    found: dict[str, str] = {}
    if "name" in fields:
        found["name"] = _name(fields["name"])
    if "currency" in fields:
        currency = fields["currency"]
        if not isinstance(currency, str):
            raise ValueError("A currency is text.")
        if currency not in Currency.values:
            raise SetupRefusedError(
                "unknown-currency",
                reason="Choose one of " + ", ".join(Currency.values) + ".",
            )
        found["currency"] = currency
    return found


def _book_checked(row: PriceBook) -> None:
    try:
        row.full_clean()
    except ValidationError as error:
        fields = set(getattr(error, "error_dict", {}))
        if "name" in fields:
            taken = any(e.code == "unique" for e in error.error_dict["name"])
            raise SetupRefusedError("name-taken" if taken else "invalid-name") from None
        if "currency" in fields:
            raise SetupRefusedError("unknown-currency") from None
        raise SetupRefusedError("invalid") from None


def make_book(administrator: Member, fields: Fields) -> None:
    """Add a price book {"name", "currency"?}; the currency is the
    organization's when left out."""
    values = _book_values(fields, new=True)

    def apply(org: Organization) -> None:
        _taken(PriceBook, org, values["name"], 0)
        row = PriceBook(organization=org, **values)
        _book_checked(row)
        try:
            row.save()
        except IntegrityError:
            raise SetupRefusedError("invalid") from None
        record_event(
            actor=administrator, action=AuditAction.MADE_PRICE_BOOK, target=row
        )

    organization_transaction(administrator.organization_id, apply)


def change_book(administrator: Member, pk: int, fields: Fields) -> None:
    """Rename a price book, or change its currency while it has no price . A change that
    changes nothing writes nothing."""
    values = _book_values(fields, new=False)
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> None:
        row = PriceBook.objects.of(org).select_for_update().filter(pk=pk).first()
        if row is None:
            raise SetupNotFoundError
        if all(getattr(row, key) == value for key, value in values.items()):
            return
        if "name" in values:
            _taken(PriceBook, org, values["name"], row.pk)
        if values.get("currency", row.currency) != row.currency and (
            PriceBookEntry.objects.of(org).filter(price_book=row).exists()
        ):
            raise SetupRefusedError("currency-fixed", reason=ENTRY_FIXES_CURRENCY)
        record_event(
            actor=administrator, action=AuditAction.CHANGED_PRICE_BOOK, target=row
        )
        for key, value in values.items():
            setattr(row, key, value)
        _book_checked(row)
        try:
            row.save()
        except ValidationError as error:
            raise SetupRefusedError(
                "currency-fixed", reason=ENTRY_FIXES_CURRENCY
            ) from error
        except IntegrityError:
            raise SetupRefusedError("invalid") from None

    organization_transaction(administrator.organization_id, apply)


def remove_book(administrator: Member, pk: int) -> None:
    """Remove a price book no container names and no booking line is made at
    (a line keeps its book). Its prices go with it, each with its event."""

    def apply(org: Organization) -> None:
        row = PriceBook.objects.of(org).select_for_update().filter(pk=pk).first()
        if row is None:
            raise SetupNotFoundError
        held = {
            "containers": BookingConfiguration.objects.of(org)
            .filter(price_book=row)
            .count(),
            "bookings": BookingLine.objects.of(org).filter(price_book=row).count(),
        }
        if any(held.values()):
            raise SetupRefusedError("in-use", {k: v for k, v in held.items() if v})
        for entry in (
            PriceBookEntry.objects.of(org).filter(price_book=row).order_by("pk")
        ):
            record_event(
                actor=administrator, action=AuditAction.REMOVED_PRICE, target=entry
            )
            entry.delete()
        record_event(
            actor=administrator, action=AuditAction.REMOVED_PRICE_BOOK, target=row
        )
        try:
            row.delete()
        except ProtectedError:
            raise SetupRefusedError("in-use") from None

    organization_transaction(administrator.organization_id, apply)


# --- Prices: the entries of a container in a booking category --------------------


def _whole(value: object) -> int:
    if type(value) is not int:
        raise ValueError("A whole number.")
    return value


def _tier_values(go_by: str, fields: object) -> TierValues:
    if not isinstance(fields, dict):
        raise ValueError("A tier is an object.")
    _only(
        fields,
        {"up_to", "paid_per", "amount"},
        required={"up_to", "paid_per", "amount"},
    )
    up_to, paid_per, amount = (
        _whole(fields[k]) for k in ("up_to", "paid_per", "amount")
    )
    # The file's own rules for each value: a time of day is up to 24:00 (the
    # end of the day), a length and "paid per" 1 minute or
    # more, an amount zero or more.
    fault = (
        setup_file._time_of_day_fault(up_to, end_of_day=True)
        if go_by == TiersGoBy.TIME_OF_DAY
        else setup_file._length_fault(up_to)
    )
    if fault:
        raise SetupRefusedError("invalid-tier", reason=f'A tier\'s "up to" {fault}')
    if fault := setup_file._length_fault(paid_per):
        raise SetupRefusedError("invalid-tier", reason=f'"Paid per" {fault}')
    if fault := setup_file._amount_fault(amount):
        raise SetupRefusedError("invalid-tier", reason=f"An amount {fault}")
    return TierValues(up_to, paid_per, amount)


def _entry_values(item: object) -> EntryValues:
    if not isinstance(item, dict):
        raise ValueError("A price is an object.")
    _only(
        item,
        {"group", "tiers_go_by", "billing_basis", "tiers"},
        required={"tiers_go_by", "billing_basis", "tiers"},
    )
    group = item.get("group")
    if group is not None:
        group = _whole(group)
    go_by, basis, tiers = item["tiers_go_by"], item["billing_basis"], item["tiers"]
    if not isinstance(go_by, str) or not isinstance(basis, str):
        raise ValueError("Words.")
    if go_by not in TiersGoBy.values:
        raise SetupRefusedError("invalid-tiers-go-by")
    if basis not in BillingBasis.values:
        raise SetupRefusedError("invalid-billing-basis")
    if not isinstance(tiers, list):
        raise ValueError("Tiers are a list.")
    if not tiers:
        raise SetupRefusedError("no-tier", reason="It has no price tier.")
    if len(tiers) > MOST_TIERS:
        raise ValueError("Too many tiers.")
    made = tuple(_tier_values(go_by, tier) for tier in tiers)
    if any(later.up_to <= earlier.up_to for earlier, later in pairwise(made)):
        raise SetupRefusedError(
            "tiers-overlap", reason="Each tier reaches past the one before."
        )
    return EntryValues(group, go_by, basis, made)


def _stamp_of(
    rows: Iterable[tuple[int | None, str, str, Iterable[tuple[int, int, int]]]],
) -> str:
    """What a sheet saw of the prices of a container in a category: a short
    digest of its entries (group, what the tiers go by, billing basis) and
    their tiers. The sheet sends it back with its change, which is refused
    when the prices are no longer what it saw."""
    held = sorted(
        (group if group is not None else 0, go_by, basis, sorted(tiers))
        for group, go_by, basis, tiers in rows
    )
    return hashlib.sha256(json.dumps(held).encode()).hexdigest()[:24]


def _stamp_in_the_database(
    org: Organization, config: BookingConfiguration, category: BookingCategory
) -> str:
    entries = list(
        PriceBookEntry.objects.of(org).filter(
            price_book=config.price_book_id,
            booking_configuration=config,
            booking_category=category,
        )
    )
    tiers: dict[int, list[tuple[int, int, int]]] = {}
    for tier in PriceTier.objects.of(org).filter(price_book_entry__in=entries):
        tiers.setdefault(tier.price_book_entry_id, []).append(
            (tier.up_to, tier.paid_per, tier.amount)
        )
    return _stamp_of(
        (e.spot_group_id, e.tiers_go_by, e.billing_basis, tiers.get(e.pk, []))
        for e in entries
    )


def _stamp_must_match(
    org: Organization,
    config: BookingConfiguration,
    category: BookingCategory,
    seen: str | None,
) -> None:
    if seen is not None and seen != _stamp_in_the_database(org, config, category):
        raise SetupRefusedError(
            "changed-meanwhile",
            reason="The prices were changed by someone else after this sheet "
            "opened. Nothing was saved.",
        )


def the_stamp(fields: Mapping[str, object]) -> str | None:
    seen = fields.get("stamp")
    if seen is not None and (not isinstance(seen, str) or len(seen) > 64):
        raise ValueError("A stamp is text.")
    return seen


def _entries_of(fields: Fields) -> list[EntryValues]:
    _only(fields, {"entries", "stamp"}, required={"entries"})
    given = fields["entries"]
    if not isinstance(given, list) or not given or len(given) > MOST_ENTRIES:
        raise ValueError("Entries are a list.")
    return [_entry_values(item) for item in given]


def _the_model_refuses(entry: PriceBookEntry) -> None:
    """The model's own validation of an entry, with its reason."""
    try:
        entry.full_clean()
    except ValidationError as error:
        found: Mapping[str, Sequence[ValidationError]] = getattr(
            error, "error_dict", {}
        )
        codes = {
            "booking_configuration": "container-without-payment",
            "booking_category": "category-not-on-container",
            "spot_group": "group-not-on-floor-plan",
            "billing_basis": "billing-basis",
        }
        for field, code in codes.items():
            if field in found:
                raise SetupRefusedError(
                    code, reason=" ".join(found[field][0].messages)
                ) from None
        raise SetupRefusedError("invalid") from None


def _container_and_category(
    org: Organization, container: int, category: int
) -> tuple[BookingConfiguration, BookingCategory]:
    config = (
        BookingConfiguration.objects.of(org)
        .select_related("asset", "floor_plan")
        .filter(pk=container)
        .first()
    )
    found = BookingCategory.objects.of(org).filter(pk=category).first()
    if config is None or found is None:
        raise SetupNotFoundError
    return config, found


def save_prices(
    administrator: Member, container: int, category: int, fields: Fields
) -> None:
    """Save the prices of a container in a booking category, in the book the
    container names: {"entries": [{"group": id | null, "tiers_go_by",
    "billing_basis", "tiers": [{"up_to", "paid_per", "amount"}]}]}. The set
    replaces what is there: an entry the set leaves out is removed, one it
    adds is made, and one that differs is changed, all or none. With the
    "stamp" the sheet was given (`prices_place`), the change is refused when
    the prices are no longer what the sheet saw (last write does not win on
    stale data); without one it is not checked."""
    given = _entries_of(fields)
    seen = the_stamp(fields)

    def apply(org: Organization) -> None:
        config, found = _container_and_category(org, container, category)
        book = config.price_book
        if book is None:
            raise SetupRefusedError(
                "container-without-payment",
                reason="The container is without payment: it has no price book, "
                "and no price applies to it.",
            )
        offers = ConfigurationCategory.objects.of(org).filter(
            booking_configuration=config, booking_category=found
        )
        if not offers.exists():
            raise SetupRefusedError(
                "category-not-on-container",
                reason="The container does not apply to this booking category.",
            )
        _stamp_must_match(org, config, found, seen)
        if fault := what_the_entries_of_a_category_get_wrong(
            [(entry.group, entry.billing_basis) for entry in given]
        ):
            raise SetupRefusedError(fault)
        groups = {
            group.pk: group
            for group in SpotGroup.objects.of(org).filter(
                pk__in=[e.group for e in given if e.group is not None]
            )
        }
        if any(e.group is not None and e.group not in groups for e in given):
            raise SetupRefusedError(
                "group-not-on-floor-plan",
                reason="The group is not on the floor plan the container books.",
            )
        existing = {
            entry.spot_group_id: entry
            for entry in PriceBookEntry.objects.of(org)
            .select_for_update()
            .filter(
                price_book=book, booking_configuration=config, booking_category=found
            )
            .order_by("pk")
        }
        wanted = {entry.group for entry in given}
        rows: list[tuple[PriceBookEntry, EntryValues, bool]] = []
        for values in given:
            row = existing.get(values.group) or PriceBookEntry(
                organization=org,
                price_book=book,
                booking_configuration=config,
                booking_category=found,
                spot_group=groups.get(values.group) if values.group else None,
            )
            row.tiers_go_by = values.tiers_go_by
            row.billing_basis = values.billing_basis
            _the_model_refuses(row)
            if reason := what_the_tiers_leave_out(
                config, values.tiers_go_by, [tier.up_to for tier in values.tiers]
            ):
                raise SetupRefusedError("tiers-incomplete", reason=reason)
            for tier in values.tiers:
                _tier_checked(org, tier)
            rows.append((row, values, row.pk is None))
        for group, old in existing.items():
            if group not in wanted:
                record_event(
                    actor=administrator, action=AuditAction.REMOVED_PRICE, target=old
                )
                old.delete()
        for row, values, is_new in rows:
            _write(administrator, org, row, values, is_new)

    organization_transaction(administrator.organization_id, apply)


def _tier_checked(org: Organization, tier: TierValues) -> None:
    try:
        PriceTier(
            organization=org,
            up_to=tier.up_to,
            paid_per=tier.paid_per,
            amount=tier.amount,
        ).full_clean(exclude=["price_book_entry"])
    except ValidationError as error:
        raise SetupRefusedError(
            "invalid-tier", reason=" ".join(error.messages)
        ) from None


def _stored(org: Organization, row: PriceBookEntry) -> list[TierValues]:
    return [
        TierValues(*values)
        for values in PriceTier.objects.of(org)
        .filter(price_book_entry=row)
        .order_by("up_to")
        .values_list("up_to", "paid_per", "amount")
    ]


def _write(
    administrator: Member,
    org: Organization,
    row: PriceBookEntry,
    values: EntryValues,
    is_new: bool,
) -> None:
    """Write one entry and its tiers. A price that is as it was writes
    nothing and no event."""
    try:
        if is_new:
            row.save()
            record_event(actor=administrator, action=AuditAction.MADE_PRICE, target=row)
        else:
            stored = PriceBookEntry.objects.of(org).get(pk=row.pk)
            if (
                stored.tiers_go_by == values.tiers_go_by
                and stored.billing_basis == values.billing_basis
                and _stored(org, row) == list(values.tiers)
            ):
                return
            record_event(
                actor=administrator, action=AuditAction.CHANGED_PRICE, target=row
            )
            row.save()
            PriceTier.objects.of(org).filter(price_book_entry=row).delete()
        for tier in values.tiers:
            PriceTier.objects.create(
                organization=org,
                price_book_entry=row,
                up_to=tier.up_to,
                paid_per=tier.paid_per,
                amount=tier.amount,
            )
    except IntegrityError:
        raise SetupRefusedError("invalid") from None


def remove_prices(
    administrator: Member, container: int, category: int, stamp: str | None = None
) -> None:
    """Remove the prices of a container in a booking category, in the book the
    container names, the normal price and the groups' together. Orders already
    made keep their lines. With the sheet's stamp (see `save_prices`), refused
    when the prices changed since."""

    def apply(org: Organization) -> None:
        config, found = _container_and_category(org, container, category)
        _stamp_must_match(org, config, found, stamp)
        rows = list(
            PriceBookEntry.objects.of(org)
            .select_for_update()
            .filter(
                price_book=config.price_book_id,
                booking_configuration=config,
                booking_category=found,
            )
            .order_by("pk")
        )
        if not rows or config.price_book_id is None:
            raise SetupNotFoundError
        for row in rows:
            record_event(
                actor=administrator, action=AuditAction.REMOVED_PRICE, target=row
            )
            row.delete()

    organization_transaction(administrator.organization_id, apply)
