# SPDX-License-Identifier: AGPL-3.0-only
"""Server-side tier prices and immutable line snapshots .

A quote reserves nothing and does not authorize a booking. Order creation
must recheck permission/availability and capture current prices atomically.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone

from randirandi.core.calendar import (
    _covered,
    _instants,
    _local_span,
    valid_configuration,
)
from randirandi.core.models import (
    AssetInventory,
    BillingBasis,
    BookingCategory,
    BookingConfiguration,
    ConfigurationCategory,
    ConsumptionMode,
    PeriodType,
    PriceBookEntry,
    PriceTier,
    Spot,
    SpotGroup,
    Status,
    TiersGoBy,
)
from randirandi.core.models.base import DAY
from randirandi.core.models.prices import entry_fits_its_mode, what_the_tiers_leave_out

MINUTE = timedelta(minutes=1)


class PriceUnavailableError(ValueError):
    """No complete configured price for this request; never substitute zero.
    A configuration without payment is not priced at zero by substitution: it
    has no price book, and no price applies (W12)."""


@dataclass(frozen=True)
class PricedLine:
    booking_configuration_id: int
    price_book_id: int
    configuration_number: int
    price_book_number: int
    up_to: int
    quantity: int
    amount: int
    billing_basis: str
    capacity_quantity: int
    capacity_unit: str
    # The tier's "paid per" (minutes).
    paid_per: int = 0
    # The group of spots the line is for; none for spots in no group, and for
    # a container without prices for groups.
    spot_group_id: int | None = None

    @property
    def total(self) -> int:
        return self.quantity * self.amount * self.capacity_quantity


@dataclass(frozen=True)
class BookingPrice:
    organization_id: int
    asset_id: int
    booking_type_id: int
    booking_category_id: int
    booking_type_name: str
    booking_category_name: str
    starts_at: datetime
    ends_at: datetime
    consumption_mode: str
    quantity: int
    currency: str
    lines: tuple[PricedLine, ...]

    @property
    def total(self) -> int:
        return sum(line.total for line in self.lines)


@dataclass(frozen=True)
class _PricingData:
    configuration: BookingConfiguration
    category: BookingCategory
    # None for a configuration without payment, which has no price book.
    entry: PriceBookEntry | None
    tiers: tuple[PriceTier, ...]
    # The entries that name a group of spots, by the group, each with its
    # tiers ; the entry above is the container's normal price.
    groups: Mapping[int, tuple[PriceBookEntry, tuple[PriceTier, ...]]] = field(
        default_factory=dict
    )


def _read(
    configuration: BookingConfiguration,
    category: BookingCategory,
    *,
    enabled_only: bool = True,
) -> _PricingData:
    """Capture inputs inside the caller's coherent transaction.

    The quote wrapper owns a read-only snapshot. Future order creation must
    capture under its own write/locking protocol and use the same calculator.
    """
    org_id = configuration.organization_id
    found = BookingConfiguration.objects.of(org_id).select_related(
        "organization", "asset", "floor_plan", "booking_type", "price_book"
    )
    config = (
        found.filter(pk=configuration.pk, status=Status.ENABLED)
        if enabled_only
        else found.filter(pk=configuration.pk)
    ).first()
    stored_category = BookingCategory.objects.of(org_id).filter(pk=category.pk).first()
    if config is None or stored_category is None or not valid_configuration(config):
        raise PriceUnavailableError(
            "No enabled configuration and category for this request."
        )
    if (
        not ConfigurationCategory.objects.of(org_id)
        .filter(booking_configuration=config, booking_category=stored_category)
        .exists()
    ):
        raise PriceUnavailableError("The configuration does not offer this category.")
    if not config.with_payment:
        return _PricingData(config, stored_category, None, ())
    rows = list(
        PriceBookEntry.objects.of(org_id).filter(
            booking_configuration=config,
            booking_category=stored_category,
            price_book_id=config.price_book_id,
        )
    )
    tiers = {
        row.pk: tuple(PriceTier.objects.of(org_id).filter(price_book_entry=row))
        for row in rows
    }
    entry, entry_tiers, groups = _priced_by_book(config, rows, tiers)
    return _PricingData(config, stored_category, entry, entry_tiers, groups)


def _priced_by_book(
    config: BookingConfiguration,
    rows: Sequence[PriceBookEntry],
    tiers: Mapping[int, tuple[PriceTier, ...]],
) -> tuple[
    PriceBookEntry,
    tuple[PriceTier, ...],
    dict[int, tuple[PriceBookEntry, tuple[PriceTier, ...]]],
]:
    """The container's normal entry for one category with its tiers, and the
    entries for groups of spots, from the entries of its price book for that
    category and their tiers (by entry). Refused when the book has no
    supported price. The quote and the calendar's prices read it alike."""
    entry = next((row for row in rows if row.spot_group_id is None), None)
    named = [row for row in rows if row.spot_group_id is not None]
    if (
        entry is None
        or not entry_fits_its_mode(config, entry)
        or (named and entry.billing_basis != BillingBasis.PER_CAPACITY_UNIT)
        or any(
            row.billing_basis != BillingBasis.PER_CAPACITY_UNIT
            or not entry_fits_its_mode(config, row)
            for row in named
        )
    ):
        raise PriceUnavailableError(
            "The category has no supported price in the current book."
        )
    for row in rows:
        if what_the_tiers_leave_out(
            config, row.tiers_go_by, [tier.up_to for tier in tiers[row.pk]]
        ):
            raise PriceUnavailableError(
                "The price tiers do not cover the configured periods."
            )
    return (
        entry,
        tiers[entry.pk],
        {
            row.spot_group_id: (row, tiers[row.pk])
            for row in named
            if row.spot_group_id is not None
        },
    )


def _check_period(
    config: BookingConfiguration, start: datetime, end: datetime, zone: ZoneInfo
) -> None:
    """Use the calendar's same local-window mapping and recurrence rules."""
    local = start.astimezone(zone)
    if local.second or local.microsecond or (end - start) % MINUTE:
        raise PriceUnavailableError("The booking must use whole configured minutes.")
    elapsed = (end - start) // MINUTE
    lookback = config.end_day if config.period_type == PeriodType.FLEXIBLE else 0
    for back in range(lookback + 1):
        day = local.date() - timedelta(days=back)
        if not config.starts_on(day):
            continue
        midnight = datetime.combine(day, time())
        first = midnight + config.period_start * MINUTE
        last = midnight + (config.end_day * DAY + config.period_end) * MINUTE
        if config.period_type == PeriodType.FIXED:
            starts, ends = _instants(first, zone), _instants(last, zone)
            if len(starts) == len(ends) == 1 and (start, end) == (starts[0], ends[0]):
                return
        else:
            offset = (local.replace(tzinfo=None) - first) // MINUTE
            if (
                0 <= offset < config.period_length
                and offset % config.interval == 0
                and config.minimum_length <= elapsed <= config.maximum_length
                and elapsed % config.interval == 0
                and _covered(
                    start,
                    end,
                    _local_span(first, last, lambda wall: _instants(wall, zone)),
                )
            ):
                return
    raise PriceUnavailableError("The time does not fit the configuration's time rules.")


def _parts(
    data: _PricingData,
    entry: PriceBookEntry,
    tiers: Sequence[PriceTier],
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
) -> list[tuple[PriceTier, int]]:
    """Contiguous priced parts, with their exact charged duration in minutes."""
    config = data.configuration
    if entry.tiers_go_by == TiersGoBy.LENGTH:
        length = (
            config.period_length
            if config.period_type == PeriodType.FIXED
            else (end - start) // MINUTE
        )
        parts = []
        previous = 0
        for tier in tiers:
            minutes = min(length, tier.up_to) - previous
            if minutes > 0:
                parts.append((tier, minutes))
            previous = tier.up_to
            if previous >= length:
                break
        return parts
    if config.period_type == PeriodType.FIXED:
        # A fixed period's price describes its nominal local period, including
        # on clock-change dates. Flexible time below measures actual minutes.
        parts = []
        previous = 0
        for tier in tiers:
            minutes = min(config.period_end, tier.up_to) - max(
                config.period_start, previous
            )
            if minutes > 0:
                parts.append((tier, minutes))
            previous = tier.up_to
        return parts
    parts = []
    moment = start
    while moment < end:
        wall = moment.astimezone(zone)
        clock_minute = wall.hour * 60 + wall.minute
        tier = next(tier for tier in tiers if clock_minute < tier.up_to)
        if parts and parts[-1][0].pk == tier.pk:
            parts[-1] = (tier, parts[-1][1] + 1)
        else:
            parts.append((tier, 1))
        moment += MINUTE
    return parts


def _calculate(
    data: _PricingData,
    start: datetime,
    end: datetime,
    quantity: int,
    spots: Sequence[Spot] | None = None,
) -> BookingPrice:
    """Pure calculation on captured data; no ORM reads, writes or reservations.
    With the `spots` the booking takes, a spot in a group with its own price
    takes it (`reprice`)."""
    config = data.configuration
    asset = config.asset
    zone = asset.zone
    shared = config.consumption_mode == ConsumptionMode.SHARED
    # A shared configuration books the spots of its floor plan, whose
    # capacity is its number of spots; an exclusive one is one column.
    floor_plan = config.floor_plan if shared else None
    if shared and floor_plan is None:
        raise PriceUnavailableError("The configuration has no floor plan.")
    if not 0 < quantity <= (floor_plan.capacity if floor_plan else 1):
        raise PriceUnavailableError(
            "The quantity does not fit the configuration and mode."
        )
    _check_period(config, start, end, zone)
    lines = (
        ()
        if data.entry is None
        else _lines(data, data.entry, floor_plan, start, end, zone, quantity, spots)
    )
    return BookingPrice(
        config.organization_id,
        asset.pk,
        config.booking_type_id,
        data.category.pk,
        config.booking_type.name,
        data.category.name,
        start,
        end,
        config.consumption_mode,
        quantity,
        _currency(data),
        lines,
    )


def _currency(data: _PricingData) -> str:
    """The currency of the price: its book's. A container without payment has
    no book, so its free price says the organization's."""
    config = data.configuration
    if data.entry is not None and config.price_book is not None:
        return config.price_book.currency
    return config.organization.currency


def _lines(
    data: _PricingData,
    entry: PriceBookEntry,
    floor_plan: AssetInventory | None,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    quantity: int,
    spots: Sequence[Spot] | None = None,
) -> tuple[PricedLine, ...]:
    """The priced parts of a configuration with payment; refused when there
    are none, so that a priced booking never costs zero by accident.

    Where the book has prices for groups and the spots are known, the spots
    are priced group by group: a spot in a group takes the group's price, any
    other spot the container's normal price, and the lines add up to the sum
    of the spots. Spots in no group come first, then the groups by number.
    """
    config = data.configuration
    book = config.price_book
    if book is None:  # Read with its entry, in the same snapshot.
        raise PriceUnavailableError("The configuration has no price book.")
    per_spot = entry.billing_basis == BillingBasis.PER_CAPACITY_UNIT
    unit = floor_plan.unit if floor_plan else "booking"
    if per_spot and spots and data.groups:
        counts = Counter(spot.group_id for spot in spots)
        number = {
            spot.group_id: spot.group.number for spot in spots if spot.group is not None
        }
        shares = [
            (group, counts[group])
            for group in sorted(counts, key=lambda group: number.get(group, 0))
        ]
        recorded = True
    else:
        shares = [(None, quantity if per_spot else 1)]
        recorded = False
    lines = tuple(
        PricedLine(
            config.pk,
            book.pk,
            config.number,
            book.number,
            tier.up_to,
            (minutes + tier.paid_per - 1) // tier.paid_per,
            tier.amount,
            row.billing_basis,
            count,
            unit,
            tier.paid_per,
            group if recorded else None,
        )
        for group, count in shares
        for row, row_tiers in [_priced_by(data, entry, group)]
        for tier, minutes in _parts(data, row, row_tiers, start, end, zone)
    )
    if not lines:
        raise PriceUnavailableError("The booking has no priced part.")
    return lines


def _priced_by(
    data: _PricingData, entry: PriceBookEntry, group: int | None
) -> tuple[PriceBookEntry, Sequence[PriceTier]]:
    """The entry and tiers that price a spot of the group: the group's own,
    or the container's normal ones."""
    if group is not None and group in data.groups:
        return data.groups[group]
    return entry, data.tiers


def reprice(
    data: _PricingData, price: BookingPrice, spots: Sequence[Spot] | None
) -> BookingPrice:
    """The price once the spots are known. Where the book has prices for
    groups, a booking over several groups costs the sum of its spots and its
    lines record each group; the price before (`_calculate` without spots)
    is the container's normal price for every spot."""
    if data.entry is None or not data.groups or not spots:
        return price
    config = data.configuration
    return replace(
        price,
        lines=_lines(
            data,
            data.entry,
            config.floor_plan,
            price.starts_at,
            price.ends_at,
            config.asset.zone,
            price.quantity,
            spots,
        ),
    )


def what_it_costs(
    configuration: BookingConfiguration,
    booking_category: BookingCategory,
    wanted: Mapping[int | None, Sequence[tuple[datetime, datetime]]],
    groups: Mapping[int, SpotGroup] | None = None,
) -> dict[int | None, list[int | None]]:
    """What a booking of one spot costs, for the times asked, as Book and the
    order price it (`_calculate` on the stored book): by the group of the
    spot (None for the container's normal price), one total for each time,
    None where the time cannot be priced. The administrator's price page
    shows these as its examples, so it never calculates a price of its own.
    A container that is not enabled is priced too (Book offers nothing from
    it, and no order is made), so that an administrator sees what its prices
    will cost before it is switched on. Nothing is reserved or written."""
    try:
        data = _read(configuration, booking_category, enabled_only=False)
    except PriceUnavailableError:
        return {group: [None] * len(spans) for group, spans in wanted.items()}
    found: dict[int | None, list[int | None]] = {}
    for group, spans in wanted.items():
        spots = [Spot(group=groups[group])] if group is not None and groups else None
        totals: list[int | None] = []
        for start, end in spans:
            try:
                totals.append(_calculate(data, start, end, 1, spots).total)
            except PriceUnavailableError:
                totals.append(None)
        found[group] = totals
    return found


def price_booking(
    *,
    configuration: BookingConfiguration,
    booking_category: BookingCategory,
    starts_at: datetime,
    ends_at: datetime,
    quantity: int = 1,
) -> BookingPrice:
    """Price configured time, without checking membership or reserving stock.

    Amounts and billing choices come only from the current stored price book.
    Returned line values are advisory snapshots, not saved BookingLine rows.
    Requires its own read-only transaction; see ADR-0017 for order integration.
    """
    if type(quantity) is not int or quantity < 1:
        raise ValueError("Quantity must be a positive whole number.")
    if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        raise ValueError("Booking moments must include a time zone.")
    start, end = starts_at.astimezone(UTC), ends_at.astimezone(UTC)
    if end <= start:
        raise ValueError("A booking must end after it starts.")
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Pricing requires its own read-only transaction.")
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
            data = _read(configuration, booking_category)
            return _calculate(data, start, end, quantity)
    except OverflowError:
        raise PriceUnavailableError(
            "Dates exceed the supported calendar range."
        ) from None
