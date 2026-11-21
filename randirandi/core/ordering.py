# SPDX-License-Identifier: AGPL-3.0-only
"""All-or-none priced checkouts and automatic capacity acquisition ."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone
from psycopg import Error as DatabaseError

from randirandi.core import booking_conditions, booking_limits, calendar, pricing
from randirandi.core.models import (
    Asset,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    BookingState,
    CapacityAllocation,
    ConsumptionMode,
    Member,
    Order,
    Organization,
    Spot,
)
from randirandi.core.models.orders import HOLDING

MAX_BOOKINGS = 50
MAX_ATTEMPTS = 5
HOLD_LENGTH = timedelta(minutes=10)  # Provisional unpaid-hold default, index P4.


class OrderUnavailableError(ValueError):
    """The requested checkout cannot currently be reserved; no order was saved."""


class OrderLimitError(ValueError):
    """The checkout exceeds supported transaction/storage bounds."""


class OrderTotalChangedError(OrderUnavailableError):
    """The bookings no longer cost the total the member saw; nothing was saved."""


class OrderOverDayLimitError(OrderUnavailableError):
    """The member would pass the organization's limit of bookings in one day;
    nothing was saved. `left` is how many the member may still make today,
    `limit` the organization's limit for one day."""

    def __init__(self, left: int, limit: int) -> None:
        super().__init__("The order passes the most bookings a member may make a day.")
        self.left = left
        self.limit = limit


class OrderTooFarAheadError(OrderUnavailableError):
    """A booking starts after the last date the organization lets a member
    book; nothing was saved."""


class OrderConditionsError(OrderUnavailableError):
    """The order did not accept the booking conditions in force : the
    call carried none (`missing`), or the identifier of another text, one the
    cart showed before the conditions changed; nothing was saved."""

    def __init__(self, message: str, *, missing: bool) -> None:
        super().__init__(message)
        self.missing = missing


class OrderContendedError(OrderUnavailableError):
    """Every attempt met other orders being made at the same moment; nothing
    was saved, and trying again may well work."""


@dataclass(frozen=True)
class BookingRequest:
    configuration_id: int
    booking_category_id: int
    starts_at: datetime
    ends_at: datetime
    quantity: int = 1
    # The cart row it comes from, kept on the booking .
    cart_row: str = ""
    # The spot numbers the member picked, one for each of the quantity, on
    # the configuration's floor plan; empty for the lowest free ones.
    spots: tuple[int, ...] = ()
    # The group of spots asked for : the booking takes only its
    # spots. None for any spot of the floor plan.
    group_id: int | None = None


def _utc(moment: datetime) -> datetime:
    """Normalize input before any writes, including UTC-range refusal."""
    if not isinstance(moment, datetime):
        raise ValueError("Moments must be datetimes with a time zone.")
    try:
        if timezone.is_naive(moment):
            raise ValueError("Moments must include a time zone.")
        return moment.astimezone(UTC)
    except OverflowError:
        raise OrderUnavailableError("Dates exceed the supported range.") from None


def _opening(
    data: calendar._Snapshot,
    start: datetime,
    end: datetime,
    grid: calendar._Grid | None = None,
) -> bool:
    """Check one exact span without expanding the calendar's entire grid.

    A `grid` of the same snapshot keeps each day's availability for the
    next check (the booking page checks a whole cart).
    """
    grid = grid or calendar._Grid(data)
    # All UTC offsets are strictly less than a day. Include every potentially
    # occupied local date, also for multi-day fixed periods and clock changes.
    first = start.date() - timedelta(days=1)
    last = end.date() + timedelta(days=1)
    spans = [
        span
        for ordinal in range(first.toordinal(), last.toordinal() + 1)
        for span in grid.available(date.fromordinal(ordinal))
    ]
    return calendar._covered(start, end, calendar._merge(spans))


@dataclass(frozen=True)
class _Captured:
    """What an order read for one booking."""

    request: BookingRequest
    config: BookingConfiguration
    asset: Asset
    price: pricing.BookingPrice
    # The spots of the configuration's floor plan; none when exclusive.
    spots: list[Spot]
    data: pricing._PricingData


def _capture(member: Member, request: BookingRequest, now: datetime) -> _Captured:
    org_id = member.organization_id
    config = (
        BookingConfiguration.objects.of(org_id)
        .filter(pk=request.configuration_id)
        .first()
    )
    category = (
        BookingCategory.objects.of(org_id)
        .filter(pk=request.booking_category_id)
        .first()
    )
    if config is None or category is None:
        raise OrderUnavailableError("The requested configuration is unavailable.")
    try:
        price_data = pricing._read(config, category)
        config = price_data.configuration
        data = calendar._read(
            member,
            config.asset,
            config.booking_type,
            request.starts_at.astimezone(config.asset.zone).date(),
            request.ends_at.astimezone(config.asset.zone).date() + timedelta(days=1),
            now,
        )
    except pricing.PriceUnavailableError as error:
        raise OrderUnavailableError(str(error)) from None
    asset, price = _priced(price_data, data, request)
    # `_priced` refuses a missing snapshot.
    spots = data.spots.get(config.floor_plan_id or 0, []) if data else []
    if request.group_id is not None and not any(
        spot.group_id == request.group_id for spot in spots
    ):
        # Also an exclusive container, which has no spots.
        raise OrderUnavailableError("The requested group is unavailable.")
    return _Captured(request, config, asset, price, spots, price_data)


def _priced(
    price_data: pricing._PricingData,
    data: calendar._Snapshot | None,
    request: BookingRequest,
    grid: calendar._Grid | None = None,
) -> tuple[Asset, pricing.BookingPrice]:
    """The checks and the price an order takes, from what is already read.

    The booking page's price  calls it with its own snapshot:
    a snapshot's period changes only the claims it holds, never the
    configurations, categories or availability checked here.
    """
    try:
        price = pricing._calculate(
            price_data, request.starts_at, request.ends_at, request.quantity
        )
    except pricing.PriceUnavailableError as error:
        raise OrderUnavailableError(str(error)) from None
    config = price_data.configuration
    if data is None:
        raise OrderUnavailableError(
            "The member cannot book this configuration and time."
        )
    grid = grid or calendar._Grid(data)
    start, end = request.starts_at, request.ends_at
    if (
        request.booking_category_id not in data.categories.get(config.pk, ())
        # The container's own hours for the category (), before the
        # run-time guard.
        or not calendar.category_allows(
            grid, config, request.booking_category_id, start, end
        )
        or not _opening(data, start, end, grid)
        # The run-time guard: no time that a rival container (two shared
        # containers on different floor plans of one asset) takes too.
        or any(
            first < end and start < last
            for first, last in grid.clashing(
                config,
                start.astimezone(data.zone).date(),
                end.astimezone(data.zone).date(),
            )
        )
    ):
        raise OrderUnavailableError(
            "The member cannot book this configuration and time."
        )
    return config.asset, price


def _claims(asset: Asset, start: datetime, end: datetime) -> list[CapacityAllocation]:
    return list(
        CapacityAllocation.objects.of(asset.organization_id).filter(
            scope_key=asset.scope_key,
            state__in=HOLDING,
            starts_at__lt=end,
            ends_at__gt=start,
        )
    )


def _take(
    asset: Asset,
    price: pricing.BookingPrice,
    spots: Sequence[Spot],
    claims: list[CapacityAllocation],
    *,
    pick: Sequence[int] = (),
    keep: Sequence[int] = (),
    group_id: int | None = None,
) -> list[Spot] | None:
    """The spots a booking holds, given the claims already in its space key:
    None for an exclusive booking, which holds the whole asset.

    Pure: the cart  runs it on stored claims plus the cart's
    own earlier bookings, so it agrees with what an order would take. A
    `pick` of spot numbers is taken as it is, or refused when one of them is
    not free for the whole time; without one, a party stays together in the
    lowest-numbered group with room, at its lowest free numbers, else the
    lowest free numbers anywhere, the spots in `keep` first (W18;
    `calendar.without_a_pick`). With a `group_id`, only the spots of that
    group are taken, picked or not.
    """
    start, end = price.starts_at, price.ends_at
    if price.consumption_mode == ConsumptionMode.EXCLUSIVE:
        if pick:
            raise OrderUnavailableError(
                "An exclusive booking takes the whole asset, never a spot."
            )
        if not calendar._fits(asset, price.consumption_mode, 1, [], start, end, claims):
            raise OrderUnavailableError(
                "The requested capacity is no longer available."
            )
        return None
    free = calendar.free_spots(
        asset, calendar.spots_in(spots, group_id), start, end, claims
    )
    if pick:
        by_number = {spot.number: spot for spot in free}
        if len(pick) != price.quantity or any(
            number not in by_number for number in pick
        ):
            raise OrderUnavailableError("A picked spot is not free for the whole time.")
        return [by_number[number] for number in pick]
    if len(free) < price.quantity:
        raise OrderUnavailableError("The requested capacity is no longer available.")
    return calendar.without_a_pick(free, price.quantity, keep)


def _keep(
    config: BookingConfiguration,
    starts_at: datetime,
    zone: ZoneInfo,
    taken: Sequence[tuple[BookingConfiguration, datetime, list[Spot] | None]],
) -> list[int]:
    """The spots of the cart's booking on the date period before this one in
    the same configuration, which this one takes when they are free (W18)."""
    day = calendar.period_day(config, starts_at.astimezone(zone).replace(tzinfo=None))
    for other, other_start, spots in reversed(taken):
        if (
            other.pk == config.pk
            and spots
            and calendar.next_period_day(
                config,
                calendar.period_day(
                    config, other_start.astimezone(zone).replace(tzinfo=None)
                ),
            )
            == day
        ):
            return [spot.pk for spot in spots]
    return []


def _in_turn[T](items: Sequence[T], start: Callable[[T], datetime]) -> list[T]:
    """An order takes its bookings by start, then as given, so a booking
    sees the cart's booking on the date period before it (W18)."""
    return [
        item
        for _, item in sorted(
            enumerate(items), key=lambda pair: (start(pair[1]), pair[0])
        )
    ]


def _attempt(
    member: Member,
    requests: tuple[BookingRequest, ...],
    now: datetime | None,
    accepted_version: str,
    expected: int | None = None,
) -> Order:
    # Orders of one organization serialise on its row. This lock takes it
    # before capture reads a claim; the order number, counted on the same row
    # (`Order.objects.create` below), would take it too, but only after the
    # claims were read. So the second of two orders waits here and fails to
    # serialise (40001) before reading anything, and it stays serialised if
    # the number moves. With either alone the tests stay green: the race
    # tests cannot tell them apart. NO KEY avoids blocking unrelated FK inserts.
    org = (
        Organization.objects.select_for_update(no_key=True)
        .filter(pk=member.organization_id)
        .first()
    )
    if org is None:
        raise OrderUnavailableError("The requested organization is unavailable.")
    moment = now if now is not None else timezone.now()
    # The text in force is read here, from the organization's locked row, in
    # the order's own transaction . Every order accepts them.
    text = booking_conditions.current(org)
    if not accepted_version:
        raise OrderConditionsError(
            "The booking conditions were not accepted.", missing=True
        )
    if accepted_version != text.digest:
        raise OrderConditionsError(
            "The booking conditions have changed.", missing=False
        )
    captured = [_capture(member, request, moment) for request in requests]
    # The spots are placed before the order is made: a spot in a group may
    # cost what another does not , so the total follows them.
    # Keeping the spots of the date period before (W18) is a preference: when
    # it leaves a later booking no spot, the bookings are placed again lowest
    # first, as the quote does (`booking_quote._together`).
    try:
        placed = _place(org, captured, keeping=True)
    except OrderUnavailableError:
        placed = _place(org, captured, keeping=False)
    prices = [
        pricing.reprice(item.data, item.price, spots)
        for item, spots in zip(captured, placed, strict=True)
    ]
    # An order is one payment in one currency; free bookings have none.
    if len({price.currency for price in prices if price.lines}) > 1:
        raise OrderUnavailableError("An order takes bookings in one currency.")
    total = sum(price.total for price in prices)
    maximum = connection.ops.integer_field_range("PositiveIntegerField")[1]
    if maximum is not None and total > maximum:
        raise OrderLimitError("The order total exceeds the supported stored amount.")
    if expected is not None and total != expected:
        raise OrderTotalChangedError("The bookings no longer cost the total shown.")
    if any(price.starts_at < moment for price in prices):
        raise OrderUnavailableError("A booking cannot start in the past.")
    # The organization's row is locked above, so two orders of one member
    # cannot both pass this count.
    if (limit := booking_limits.starts_before(org, moment)) is not None and any(
        price.starts_at >= limit for price in prices
    ):
        raise OrderTooFarAheadError("A booking starts too far ahead.")
    most = org.max_bookings_a_day
    left = booking_limits.left_today(member, org, moment)
    if most is not None and left is not None and len(prices) > left:
        raise OrderOverDayLimitError(left, most)
    order = Order.objects.create(
        organization=org,
        member_id=member.pk,
        created_at=moment,
        hold_expires_at=moment + HOLD_LENGTH if total else None,
        total=total,
        # A free order is confirmed at once, and its confirmation email is
        # due ; a paid one's when its payment is taken.
        confirmation_due_at=None if total else moment,
        conditions=booking_conditions.freeze(org, text, moment),
        conditions_accepted_at=moment,
    )
    state = BookingState.HELD if total else BookingState.CONFIRMED
    _book(org, member, order, captured, prices, placed, state)
    return order


def held_claims(
    asset: Asset, price: pricing.BookingPrice, spots: list[Spot] | None
) -> list[CapacityAllocation]:
    """Unsaved claims shaped like the ones an order would store."""
    common = {
        "organization_id": asset.organization_id,
        "asset_id": asset.pk,
        "scope_key": asset.scope_key,
        "whole_space": asset.whole_space,
        "mode": price.consumption_mode,
        "quantity": price.quantity,
        "state": BookingState.HELD,
        "starts_at": price.starts_at,
        "ends_at": price.ends_at,
    }
    if spots is None:
        # An exclusive claim is one row with no spot: the whole asset.
        return [CapacityAllocation(**common, ordinal=1)]
    return [
        CapacityAllocation(
            **common,
            ordinal=ordinal,
            inventory_id=spot.floor_plan_id,
            spot_id=spot.pk,
        )
        for ordinal, spot in enumerate(spots, 1)
    ]


def _place(
    org: Organization, captured: Sequence[_Captured], *, keeping: bool
) -> list[list[Spot] | None]:
    """The spots each booking would hold, in the order given: by start, then
    as given, each after the ones before it (`_in_turn`)."""
    taken: list[tuple[BookingConfiguration, datetime, list[Spot] | None]] = []
    held: list[CapacityAllocation] = []
    placed: dict[int, list[Spot] | None] = {}
    for index in _in_turn(
        range(len(captured)), lambda index: captured[index].price.starts_at
    ):
        item = captured[index]
        asset, price, request, config = (
            item.asset,
            item.price,
            item.request,
            item.config,
        )
        claims = [
            *_claims(asset, price.starts_at, price.ends_at),
            *(
                claim
                for claim in held
                if claim.scope_key == asset.scope_key
                and claim.starts_at < price.ends_at
                and price.starts_at < claim.ends_at
            ),
        ]
        spots = _take(
            asset,
            price,
            item.spots,
            claims,
            pick=request.spots,
            keep=_keep(config, price.starts_at, asset.zone, taken) if keeping else (),
            group_id=request.group_id,
        )
        taken.append((config, price.starts_at, spots))
        held.extend(held_claims(asset, price, spots))
        placed[index] = spots
    return [placed[index] for index in range(len(captured))]


def _book(
    org: Organization,
    member: Member,
    order: Order,
    captured: Sequence[_Captured],
    prices: Sequence[pricing.BookingPrice],
    placed: Sequence[list[Spot] | None],
    state: BookingState,
) -> None:
    """Take each booking in turn, with its spots and its lines."""
    for index in _in_turn(
        range(len(captured)), lambda index: captured[index].price.starts_at
    ):
        item, price, spots = captured[index], prices[index], placed[index]
        booking = Booking.objects.take(
            organization=org,
            asset=item.asset,
            booking_configuration=item.config,
            starts_at=price.starts_at,
            ends_at=price.ends_at,
            state=state,
            units=spots,
            order=order,
            member_id=member.pk,
            booking_type_id=price.booking_type_id,
            booking_category_id=price.booking_category_id,
            booking_type_name=price.booking_type_name,
            booking_category_name=price.booking_category_name,
            cart_row=item.request.cart_row,
        )
        for line in price.lines:
            BookingLine.objects.create(
                organization=org,
                booking=booking,
                booking_configuration_id=line.booking_configuration_id,
                price_book_id=line.price_book_id,
                configuration_number=line.configuration_number,
                price_book_number=line.price_book_number,
                up_to=line.up_to,
                quantity=line.quantity,
                amount=line.amount,
                billing_basis=line.billing_basis,
                capacity_quantity=line.capacity_quantity,
                capacity_unit=line.capacity_unit,
                spot_group_id=line.spot_group_id,
            )


def _retryable(error: IntegrityError | OperationalError) -> bool:
    cause = error.__cause__
    return isinstance(cause, DatabaseError) and (
        cause.sqlstate in ("40001", "40P01")
        or (
            cause.sqlstate == "23P01"
            and cause.diag.constraint_name == "capacity_no_double_booking"
        )
    )


def create_order(
    *,
    member: Member,
    requests: Sequence[BookingRequest],
    now: datetime | None = None,
    total: int | None = None,
    accepted_version: str,
) -> Order:
    """Reserve and price every requested booking, or save nothing.

    The member must come from the server's authenticated context. IDs and
    request quantities are inputs; quotes, prices, states and stock units are
    not. A SERIALIZABLE snapshot covers permission, schedule, prices and stock.
    Native constraints arbitrate competing writers. Five fresh attempts handle
    capacity exclusions, serialization failures and deadlocks; when all five
    meet them, `OrderContendedError`. No payment is started here. A passed
    hold deadline alone never releases allocations. With `total`, the total
    the member saw, a different total saves nothing (`OrderTotalChangedError`).
    With `accepted_version`, the identifier of the booking conditions the
    member accepted (`booking_conditions.Text.digest`), the text in force is
    read inside the same transaction: an empty or another identifier saves
    nothing (`OrderConditionsError`), and the order keeps the frozen text and
    the moment. It is required: every order accepts the conditions.
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Order creation requires its own transaction.")
    if not 0 < len(requests) <= MAX_BOOKINGS:
        raise OrderLimitError(f"Request between one and {MAX_BOOKINGS} bookings.")
    if total is not None and (type(total) is not int or total < 0):
        raise ValueError("The total shown must be a whole number, zero or more.")
    moment = _utc(now) if now is not None else None
    normalized = []
    for request in requests:
        if any(
            type(value) is not int or value < 1
            for value in (
                request.configuration_id,
                request.booking_category_id,
                request.quantity,
            )
        ):
            raise ValueError("Identifiers and quantities must be positive integers.")
        if type(request.cart_row) is not str or len(request.cart_row) > 16:
            raise ValueError("A cart row is text of at most 16 characters.")
        spots = tuple(request.spots)
        if spots and (
            any(type(number) is not int or number < 1 for number in spots)
            or len(set(spots)) != len(spots)
            or len(spots) != request.quantity
        ):
            raise ValueError(
                "A pick names different spot numbers, one for each of the quantity."
            )
        if request.group_id is not None and (
            type(request.group_id) is not int or request.group_id < 1
        ):
            raise ValueError("A group is a positive whole number.")
        start, end = _utc(request.starts_at), _utc(request.ends_at)
        if end <= start:
            raise ValueError("A booking must end after it starts.")
        normalized.append(
            BookingRequest(
                request.configuration_id,
                request.booking_category_id,
                start,
                end,
                request.quantity,
                request.cart_row,
                spots,
                request.group_id,
            )
        )
    for attempt in range(MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"
                    )
                order = _attempt(
                    member, tuple(normalized), moment, accepted_version, total
                )
            return order  # Deferred allocation checks have also passed at commit.
        except (IntegrityError, OperationalError) as error:
            if not _retryable(error):
                raise
            if attempt == MAX_ATTEMPTS - 1:
                raise OrderContendedError(
                    "Capacity could not be reserved during concurrent changes; retry."
                ) from None
        except OverflowError:
            raise OrderUnavailableError("Dates exceed the supported range.") from None
    raise AssertionError("Unreachable: each attempt returns, retries or raises.")
