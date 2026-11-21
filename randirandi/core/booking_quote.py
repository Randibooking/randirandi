# SPDX-License-Identifier: AGPL-3.0-only
"""The price of one choice on the booking page .

The member picks a bookable start from the calendar; the page asks what it
costs before adding it to the cart. The page names the container the
calendar answered it in, and may pick its spots ; the server
prices it with the order's own capture, so the price shown is the price an
order would take. The cart's bookings are checked together, in the order an
order takes them (T4), on the spots an order would hold. Reserves nothing
.
"""

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.db import connection, transaction
from django.utils import timezone

from randirandi.core import booking_words, calendar, ordering, pricing
from randirandi.core.models import (
    Asset,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    CapacityAllocation,
    ConsumptionMode,
    Member,
    Spot,
)


@dataclass(frozen=True)
class QuoteLine:
    """One booking line: a part of the time at its tier's price."""

    up_to: int
    paid_per: int
    paid_units: int
    unit_price: int
    times: int
    times_unit: str
    billing_basis: str
    total: int
    # The group of spots the line is for ; none when the
    # container has no prices for groups, or the spots are in no group.
    group_id: int | None = None


@dataclass(frozen=True)
class Quote:
    """The price of one choice, or why there is none.

    `reason` is "not-offered" when the booking type does not offer this time
    for the category; "taken" when it is offered but no longer free;
    "does-not-fit-beside-cart" when it would be free but for the member's own
    cart; "already-started" when its start has passed; "too-far-ahead" when its
    start is after the last date the organization lets a member book. Amounts are in the
    currency's smallest unit. A shared booking
    that no longer fits says how many units are left (T6). A container
    without payment gives no lines and costs 0 (W12).
    """

    reason: str | None
    period_type: str = ""
    consumption_mode: str = ""
    quantity: int = 0
    tiers_go_by: str = ""
    currency: str = ""
    lines: tuple[QuoteLine, ...] = ()
    # The container priced.
    configuration_id: int = 0
    # For a taken shared booking, the spots still free for its whole time (T6).
    places_left: int | None = None

    @property
    def total(self) -> int:
        return sum(line.total for line in self.lines)


@dataclass(frozen=True)
class Choice:
    """One time the member picked: what the page sends. The container is the
    one the calendar answered the time in ."""

    configuration_id: int
    booking_category_id: int
    quantity: int
    starts_at: datetime
    ends_at: datetime
    # The spot numbers picked, one for each of the quantity; empty for the
    # lowest free.
    spots: tuple[int, ...] = ()
    # The group of spots asked for : the booking takes only its
    # spots. None for any spot of the floor plan.
    group_id: int | None = None


class QuoteInputError(ValueError):
    """The request itself is malformed; nothing about availability."""


NOT_OFFERED = Quote(booking_words.NOT_OFFERED)
TAKEN = Quote(booking_words.TAKEN)
BESIDE_CART = Quote(booking_words.BESIDE_CART)
STARTED = Quote(booking_words.STARTED)
TOO_FAR_AHEAD = Quote(booking_words.TOO_FAR_AHEAD)
# The reasons a pass without the cart's earlier bookings can undo.
UNFIT = (booking_words.TAKEN, booking_words.BESIDE_CART)
OTHER_CURRENCY = Quote(booking_words.OTHER_CURRENCY)
# A booking placed in turn: its container, its start and the spots it holds.
Placed = tuple[BookingConfiguration, datetime, list[Spot] | None]


class _Reads:
    """What one answer reads once: each container, each asset and type's
    snapshot, each configuration's prices for a category, the claims on each
    time.

    A snapshot's period changes only the claims it holds, never its
    configurations, categories or availability, so one snapshot serves
    every choice on its asset and type. Each choice reads the claims on its
    own time with the order's own query (`ordering._claims`).
    """

    def __init__(self, member: Member, moment: datetime) -> None:
        self.member = member
        self.moment = moment
        self.containers: dict[int, BookingConfiguration | None] = {}
        self.grids: dict[tuple[int, int], calendar._Grid | None] = {}
        self.prices: dict[tuple[int, int], pricing._PricingData | None] = {}
        self.claims: dict[tuple[int, datetime, datetime], list[CapacityAllocation]] = {}

    def read(self, ids: Iterable[int]) -> None:
        """Read the containers not read yet, in one query."""
        wanted = set(ids) - set(self.containers)
        if not wanted:
            return
        found = {
            config.pk: config
            for config in BookingConfiguration.objects.of(self.member.organization_id)
            .filter(pk__in=wanted)
            .select_related("asset", "price_book")
        }
        for pk in wanted:
            self.containers[pk] = found.get(pk)

    def container(self, pk: int) -> BookingConfiguration | None:
        self.read([pk])
        return self.containers[pk]

    def grid(
        self, container: BookingConfiguration, choice: Choice
    ) -> calendar._Grid | None:
        """The snapshot of the container's asset and type, in a grid that
        keeps each day's availability."""
        key = (container.asset_id, container.booking_type_id)
        if key not in self.grids:
            data = calendar._read(
                self.member,
                Asset(pk=container.asset_id),
                BookingType(pk=container.booking_type_id),
                choice.starts_at.date() - timedelta(days=1),
                choice.ends_at.date() + timedelta(days=1),
                self.moment,
            )
            self.grids[key] = None if data is None else calendar._Grid(data)
        return self.grids[key]

    def priced(
        self, config: BookingConfiguration, booking_category_id: int
    ) -> pricing._PricingData | None:
        key = (config.pk, booking_category_id)
        if key not in self.prices:
            try:
                self.prices[key] = pricing._read(
                    config, BookingCategory(pk=booking_category_id)
                )
            except pricing.PriceUnavailableError:
                self.prices[key] = None
        return self.prices[key]

    def stored(
        self, asset: Asset, start: datetime, end: datetime
    ) -> list[CapacityAllocation]:
        key = (asset.scope_key, start, end)
        if key not in self.claims:
            self.claims[key] = ordering._claims(asset, start, end)
        return self.claims[key]


def _quote(
    reads: _Reads,
    choice: Choice,
    moment: datetime,
    extra: Sequence[CapacityAllocation] = (),
    taken: Sequence[Placed] = (),
) -> tuple[
    Quote,
    list[CapacityAllocation],
    tuple[BookingConfiguration, list[Spot] | None] | None,
]:
    """Price one choice against stored claims plus `extra` (the cart's).

    Returns the claims an order would hold for it, so a cart can check its
    bookings together (T4), and its configuration with the spots it would
    hold. `taken` are the cart's bookings before it (`ordering._keep`).
    """
    booking_category_id = choice.booking_category_id
    start, end = choice.starts_at, choice.ends_at
    container = reads.container(choice.configuration_id)
    grid = None if container is None else reads.grid(container, choice)
    if grid is None:
        return NOT_OFFERED, [], None
    data = grid.data
    # The container named, when the member can book it for the category now.
    config = next(
        (row for row in data.configurations if row.pk == choice.configuration_id),
        None,
    )
    if config is None or booking_category_id not in data.categories[config.pk]:
        return NOT_OFFERED, [], None
    try:
        pricing._check_period(config, start, end, data.zone)
    except pricing.PriceUnavailableError:
        return NOT_OFFERED, [], None
    # Shared: the quantity asked for; exclusive: one, the whole asset, for a
    # party of any size.
    shared = config.consumption_mode == ConsumptionMode.SHARED
    quantity = choice.quantity if shared else 1
    if (choice.spots or choice.group_id is not None) and not shared:
        return NOT_OFFERED, [], None
    priced = reads.priced(config, booking_category_id)
    if priced is None:
        return NOT_OFFERED, [], None
    try:
        # The order's own checks and price (`ordering._capture`).
        _, price = ordering._priced(
            priced,
            data,
            ordering.BookingRequest(
                config.pk, booking_category_id, start, end, quantity
            ),
            grid,
        )
    except ordering.OrderUnavailableError:
        return NOT_OFFERED, [], None
    if start < moment:
        return STARTED, [], None
    if data.starts_before is not None and start >= data.starts_before:
        return TOO_FAR_AHEAD, [], None
    asset = data.asset
    stored = reads.stored(asset, start, end)
    claims = [
        *stored,
        *(
            claim
            for claim in extra
            if claim.scope_key == asset.scope_key
            and claim.starts_at < end
            and start < claim.ends_at
        ),
    ]
    spots = data.spots.get(config.floor_plan_id or 0, [])
    if choice.group_id is not None and not any(
        spot.group_id == choice.group_id for spot in spots
    ):
        return NOT_OFFERED, [], None
    try:
        held = ordering._take(
            asset,
            price,
            spots,
            claims,
            pick=choice.spots,
            keep=ordering._keep(config, start, data.zone, taken),
            group_id=choice.group_id,
        )
    except ordering.OrderUnavailableError:
        # A second pass without the cart's claims tells the two apart: it
        # fits there, so only the member's own cart is in the way.
        reason = booking_words.TAKEN
        if len(claims) > len(stored):
            try:
                ordering._take(
                    asset,
                    price,
                    spots,
                    stored,
                    pick=choice.spots,
                    group_id=choice.group_id,
                )
                reason = booking_words.BESIDE_CART
            except ordering.OrderUnavailableError:
                pass
        if not shared:
            return Quote(reason), [], None
        left = len(
            calendar.free_spots(
                asset, calendar.spots_in(spots, choice.group_id), start, end, claims
            )
        )
        return Quote(reason, places_left=left), [], None
    # Where the book has prices for groups, the spots are priced by group.
    price = pricing.reprice(priced, price, held)
    found = Quote(
        None,
        config.period_type,
        config.consumption_mode,
        quantity,
        # Empty without payment: no price applies (W12).
        priced.entry.tiers_go_by if priced.entry else "",
        price.currency,
        tuple(
            QuoteLine(
                line.up_to,
                line.paid_per,
                line.quantity,
                line.amount,
                line.capacity_quantity,
                line.capacity_unit,
                line.billing_basis,
                line.total,
                line.spot_group_id,
            )
            for line in price.lines
        ),
        config.pk,
    )
    return found, ordering.held_claims(asset, price, held), (config, held)


def _together(
    member: Member,
    choices: Sequence[Choice],
    moment: datetime,
    reads: _Reads | None = None,
) -> tuple[list[Quote], list[CapacityAllocation], list[Placed]]:
    """Each choice priced as one order would take them all: by start, then
    as given (`ordering._in_turn`), each booking after the ones before it;
    a booking that does not fit holds nothing. Also returns the claims the
    fitting ones would hold, and each one's container, start and spots.

    Keeping the spots of the date period before (W18) is a preference: when
    it leaves a booking that has not started no spot, they are all placed
    again lowest first, as the order does (`ordering._attempt`).
    """
    reads = reads or _Reads(member, moment)
    reads.read(choice.configuration_id for choice in choices)
    found = _placed(reads, choices, moment, keeping=True)
    if any(
        quote.reason in UNFIT and choice.starts_at >= moment
        for quote, choice in zip(found[0], choices, strict=True)
    ):
        return _placed(reads, choices, moment, keeping=False)
    return found


def _placed(
    reads: _Reads, choices: Sequence[Choice], moment: datetime, *, keeping: bool
) -> tuple[list[Quote], list[CapacityAllocation], list[Placed]]:
    quotes = [NOT_OFFERED] * len(choices)
    held: list[CapacityAllocation] = []
    placed: list[Placed] = []
    for index in ordering._in_turn(
        range(len(choices)), lambda index: choices[index].starts_at
    ):
        quotes[index], claims, spots = _quote(
            reads, choices[index], moment, held, placed if keeping else ()
        )
        held.extend(claims)
        if spots is not None:
            placed.append((spots[0], choices[index].starts_at, spots[1]))
    return quotes, held, placed


def _currency_of(container: BookingConfiguration) -> str | None:
    """The currency a container is paid in: its price book's; none for a
    container without payment, which has no book ."""
    book = container.price_book
    return book.currency if container.with_payment and book is not None else None


def _one_currency(quotes: Sequence[Quote]) -> list[Quote]:
    """The cart's quotes with a paid booking in another currency than the
    first paid one's refused: an order is one payment in one currency."""
    currency = None
    kept = []
    for found in quotes:
        if found.reason is None and found.lines:
            if currency is None:
                currency = found.currency
            elif found.currency != currency:
                found = OTHER_CURRENCY
        kept.append(found)
    return kept


def _beside(reads: _Reads, cart: Sequence[Choice], scope_key: int) -> list[Choice]:
    """The cart's bookings that can meet one in this space key."""
    reads.read(row.configuration_id for row in cart)
    return [
        row
        for row in cart
        if (container := reads.containers[row.configuration_id]) is not None
        and container.asset.scope_key == scope_key
    ]


def _check(choice: Choice) -> Choice:
    """The choice in UTC, or `QuoteInputError` for a malformed one."""
    for value in (
        choice.configuration_id,
        choice.booking_category_id,
        choice.quantity,
    ):
        if type(value) is not int or value < 1:
            raise QuoteInputError(
                "Identifiers and the quantity must be positive whole numbers."
            )
    for instant in (choice.starts_at, choice.ends_at):
        if not isinstance(instant, datetime) or timezone.is_naive(instant):
            raise QuoteInputError("Moments must include a time zone.")
    if choice.group_id is not None and (
        type(choice.group_id) is not int or choice.group_id < 1
    ):
        raise QuoteInputError("A group is a positive whole number.")
    spots = tuple(choice.spots)
    if spots and (
        any(type(number) is not int or number < 1 for number in spots)
        or len(set(spots)) != len(spots)
        or len(spots) != choice.quantity
    ):
        raise QuoteInputError(
            "A pick names different spot numbers, one for each of the quantity."
        )
    try:
        start, end = choice.starts_at.astimezone(UTC), choice.ends_at.astimezone(UTC)
    except OverflowError:
        raise QuoteInputError("The moments exceed the supported range.") from None
    if end <= start:
        raise QuoteInputError("A booking must end after it starts.")
    return Choice(
        choice.configuration_id,
        choice.booking_category_id,
        choice.quantity,
        start,
        end,
        spots,
        choice.group_id,
    )


def _now(now: datetime | None) -> datetime:
    moment = now if now is not None else timezone.now()
    if timezone.is_naive(moment):
        raise QuoteInputError("The current moment must include a time zone.")
    return moment.astimezone(UTC)


@contextmanager
def snapshot() -> Iterator[None]:
    """One REPEATABLE READ, READ ONLY transaction of its own."""
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("The price requires its own read-only transaction.")
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
            yield
    except OverflowError:
        # Dates at the edge of the supported range, in any of the reads.
        raise QuoteInputError("The moments exceed the supported range.") from None


def quote(
    *,
    member: Member,
    configuration_id: int,
    booking_category_id: int,
    quantity: int,
    starts_at: datetime,
    ends_at: datetime,
    now: datetime | None = None,
    cart: Sequence[Choice] = (),
    spots: Sequence[int] = (),
    group_id: int | None = None,
) -> Quote:
    """Price one time the member picked in a container, in its own
    read-only snapshot.

    Unknown or foreign containers, a category the container is not offered
    for the member, and times that are not a start and length of the
    container are not offered. With the member's `cart` (less any booking
    being changed), a time it would not fit beside does not fit beside the
    cart, and so does a picked spot (`spots`, numbers) the cart holds; a time
    or spot others hold is taken. A start that has passed is already started.
    Nothing here reserves the time.
    """
    choice = _check(
        Choice(
            configuration_id,
            booking_category_id,
            quantity,
            starts_at,
            ends_at,
            tuple(spots),
            group_id,
        )
    )
    rows = [_check(row) for row in cart]
    moment = _now(now)
    with snapshot():
        return _joined(member, rows, choice, moment)


def _joined(
    member: Member, cart: Sequence[Choice], choice: Choice, moment: datetime
) -> Quote:
    """The choice's price once it is in the cart.

    Does not fit beside the cart when it does not fit beside the cart's
    bookings, or when an order of both would push out a booking that fits
    today: the order takes them by start, so an earlier booking may take the
    spots a later one holds. Taken when others' bookings are in the way.
    """
    reads = _Reads(member, moment)
    container = reads.container(choice.configuration_id)
    if container is None:
        return NOT_OFFERED
    beside = _beside(reads, cart, container.asset.scope_key)
    before, _, _ = _together(member, beside, moment, reads=reads)
    after, _, _ = _together(member, [*beside, choice], moment, reads=reads)
    found = after[-1]
    if found.reason is None and (own := _currency_of(container)) is not None:
        # An order is one payment in one currency (7.3): the cart's paid
        # bookings are in one, and a paid booking in another is refused.
        for row in cart:
            other = reads.containers[row.configuration_id]
            if (
                other is not None
                and (currency := _currency_of(other)) is not None
                and currency != own
            ):
                return OTHER_CURRENCY
    if found.reason is None and any(
        old.reason is None and new.reason is not None
        for old, new in zip(before, after, strict=False)
    ):
        return BESIDE_CART
    return found


def cart_quotes(
    *, member: Member, cart: Sequence[Choice], now: datetime | None = None
) -> list[Quote]:
    """Each booking in the cart priced as one order of them all would take it."""
    rows = [_check(row) for row in cart]
    moment = _now(now)
    with snapshot():
        return _one_currency(_together(member, rows, moment)[0])
