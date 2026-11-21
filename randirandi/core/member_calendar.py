# SPDX-License-Identifier: AGPL-3.0-only
"""The member's booking calendar, answered per container .

For one asset, or every asset of one space: each container the member's
role can book, with its floor plan's spots , or one column when it is exclusive, and
each of its date periods
that has time in the query dates, whole: its rows, its taken cells by spot
as spans with their state, and the full name of another member's booking
and nothing else about them , and its bookable starts for the query's
quantity and length (each flexible container's own minimum when none is
asked), each with the spots it would take without a pick and the free
spots in each group. Booking type and booking category
filter which containers are answered, and the quantity filters the shared
ones. Asked for a group, only the containers that book its floor plan are
answered, with its spots only: the columns, the taken cells and the
bookable starts count no others. Each container with payment also answers
what it costs per booking category, as numbers in the smallest unit of the
price book's currency . The member's cart counts as taken in the
bookable starts and is left out of the taken cells (T4). One read-only
snapshot answers all of it, and nothing is reserved . See the
booking container, 2026-10-17 (W4, W8, W15).
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from randirandi.core import ordering
from randirandi.core.booking_quote import (
    Choice,
    Placed,
    _beside,
    _check,
    _Reads,
    _together,
)
from randirandi.core.calendar import (
    MAX_DAYS,
    MINUTE,
    CalendarLimitError,
    ClosedDay,
    _Budget,
    _fits,
    _Grid,
    _merge,
    _open,
    _Period,
    _read,
    _Snapshot,
    category_allows,
    closed_days,
    free_spots,
    spots_in,
    without_a_pick,
)
from randirandi.core.models import (
    Asset,
    Booking,
    BookingConfiguration,
    BookingState,
    BookingType,
    CapacityAllocation,
    ConsumptionMode,
    EventDay,
    Member,
    PeriodType,
    PriceBookEntry,
    PriceTier,
    Space,
    Spot,
    SpotGroup,
)
from randirandi.core.models.bookable import kept_whole
from randirandi.core.models.orders import BLOCKED, EVENT_STATE
from randirandi.core.pricing import PriceUnavailableError, _priced_by_book

# A taken cell's state, as the answer names it.
STATES = {
    BookingState.HELD: "held",
    BookingState.CONFIRMED: "confirmed",
    BLOCKED: "block",
    EVENT_STATE: "event",
}
# Where two claims share a cell (one ends inside a row the next starts in),
# the cell shows the firmer.
FIRMER = {"confirmed": 0, "block": 1, "event": 1, "held": 2}


@dataclass(frozen=True)
class EventRef:
    """The event a taken span is, for the members' calendar : its
    id, to open its page, and its title, the only text an event adds. Never
    who made it, its contact or its description."""

    id: int
    title: str


@dataclass(frozen=True)
class TakenSpan:
    """Taken cells in one column, from row `first` to row `last`: their state,
    and for another member's held or confirmed booking that member's full
    name , never the email, phone, unit, order or member id. The
    asking member's own bookings are marked `mine` and carry no name. Two
    bookings one after the other are two spans."""

    # The spot's number; None for the one column of an exclusive container.
    spot: int | None
    first: int
    last: int
    # "held", "confirmed", "block" or "event".
    state: str
    # The cells are held by a booking of the asking member; never a block.
    mine: bool = False
    # The event, when the state is "event"; a span of two events is cut
    # between them.
    event: EventRef | None = None
    # The other member's full name; empty for the asking member's own
    # booking, a block, an event and an erased member's booking.
    name: str = ""


@dataclass(frozen=True)
class GroupFree:
    """The numbers of the free spots in one group (None: the spots in no
    group), for the whole length of a bookable start."""

    group: int | None
    spots: tuple[int, ...]


@dataclass(frozen=True)
class Start:
    """A bookable start for the query: its row, its end, and the numbers of
    the spots it takes without a pick (none when exclusive). `free` has the
    free spots of each group of the floor plan, by the groups' numbers and
    then the spots in no group; empty when it has no groups."""

    row: int
    ends_at: datetime
    spots: tuple[int, ...]
    free: tuple[GroupFree, ...] = ()


@dataclass(frozen=True)
class CategorySpans:
    """The time inside a date period that the container offers one booking
    category when it has hours of its own for it , as moments like the
    rows: a booking of the category lies wholly inside them."""

    category_id: int
    spans: tuple[tuple[datetime, datetime], ...]


@dataclass(frozen=True)
class DatePeriod:
    """One date period, whole. It belongs to the day it starts on (rule 8)."""

    day: date
    # Each row's start; the last row ends at `ends_at`.
    rows: tuple[datetime, ...]
    ends_at: datetime
    # Where each row ends (before the next start where hours break off), and
    # the indexes of the rows outside the asset's availability.
    row_ends: tuple[datetime, ...]
    closed_rows: tuple[int, ...]
    taken: tuple[TakenSpan, ...]
    starts: tuple[Start, ...]
    # For each category with hours of its own, by category; a category
    # without is not here and has the container's hours.
    category_hours: tuple[CategorySpans, ...] = ()


@dataclass(frozen=True)
class GroupColumn:
    """A group of spots: its number, name and word ("table", "section")."""

    id: int
    number: int
    name: str
    word: str


@dataclass(frozen=True)
class SpotColumn:
    id: int
    number: int
    name: str
    # None for a spot in no group.
    group: GroupColumn | None = None


@dataclass(frozen=True)
class FloorPlan:
    name: str
    spot_word: str
    spots: tuple[SpotColumn, ...]
    # The groups of the spots answered, by number.
    groups: tuple[GroupColumn, ...] = ()


@dataclass(frozen=True)
class TierPrice:
    """One price tier: up to `up_to` minutes, `amount` for each started
    `paid_per` minutes."""

    up_to: int
    paid_per: int
    amount: int


@dataclass(frozen=True)
class GroupPrice:
    """The price of a spot in one group, which its entry sets apart."""

    group: int
    tiers_go_by: str
    tiers: tuple[TierPrice, ...]


@dataclass(frozen=True)
class CategoryPrice:
    """What a container costs in one booking category: the price book entry
    the price call uses, and the entries for groups of its floor plan."""

    category_id: int
    billing_basis: str
    tiers_go_by: str
    tiers: tuple[TierPrice, ...]
    groups: tuple[GroupPrice, ...] = ()


@dataclass(frozen=True)
class ContainerPrices:
    # In whose smallest unit the amounts are, e.g. "NOK".
    currency: str
    categories: tuple[CategoryPrice, ...]


@dataclass(frozen=True)
class ContainerCalendar:
    id: int
    number: int
    asset_id: int
    asset_name: str
    # Information only (W3, W14).
    maximum_occupancy: int | None
    booking_type_id: int
    booking_type_name: str
    # The role's categories it is offered for.
    category_ids: tuple[int, ...]
    period_type: str
    consumption_mode: str
    with_payment: bool
    interval: int
    minimum_length: int
    maximum_length: int
    # None when exclusive: one column, the whole asset.
    floor_plan: FloorPlan | None
    # The most one booking can take: the floor plan's spots, or one.
    max_quantity: int
    periods: tuple[DatePeriod, ...]
    # None when the container takes no payment.
    prices: ContainerPrices | None = None


@dataclass(frozen=True)
class DayCategories:
    """A query date with open time for other booking categories of the
    member than the one asked for, in the containers asked for: what an empty
    day offers instead ."""

    day: date
    category_ids: tuple[int, ...]


@dataclass(frozen=True)
class MemberCalendar:
    # The moment the answer was worked out for.
    worked_out_at: datetime
    containers: tuple[ContainerCalendar, ...]
    # Only when a category is asked for; by date.
    other_categories: tuple[DayCategories, ...] = ()
    # The time zone of the asset or space asked for, which every time of the
    # answer is read in; empty when none was found.
    time_zone: str = ""
    # The assets asked for, and the dates each is closed on in the query (by a
    # closure with its reason's key, or by its weekly availability): the page
    # says Closed for a day on which every asset is.
    asset_ids: tuple[int, ...] = ()
    closed_days: tuple[ClosedDay, ...] = ()


def _rows(grid: _Grid, period: _Period) -> tuple[list[datetime], list[datetime]] | None:
    """The date period's rows: their starts and their ends.

    A fixed date period has one row, from its local start to its local end,
    and none when either does not exist or comes twice. A flexible one has a
    row for each interval of its local grid: none for the hour the clocks
    skip, twice the rows of the hour they repeat, told apart by their
    offsets (model 6). A row ends at the next one's start, or sooner where
    the date period's time breaks off.
    """
    if period.config.period_type == PeriodType.FIXED:
        span = grid.fixed_span(period)
        return None if span is None else ([span[0]], [span[1]])
    starts = sorted(set(grid.flexible_starts(period)))
    ends = []
    for index, start in enumerate(starts):
        edge = next(last for first, last in period.window if first <= start < last)
        ends.append(min(edge, starts[index + 1]) if index + 1 < len(starts) else edge)
    return starts, ends


def _taken(
    asset: Asset,
    shared: bool,
    spots: Sequence[Spot],
    rows: list[datetime],
    ends: list[datetime],
    claims: Sequence[CapacityAllocation],
    own: frozenset[int],
    budget: _Budget,
    events: Mapping[int, EventRef],
    names: Mapping[int, str],
) -> tuple[TakenSpan, ...]:
    """The cells the claims in the asset's space key take, by column (spot
    number) as spans. An exclusive claim on the asset, any claim on an
    exclusive container's asset and a claim kept whole beside it take every
    column; a shared claim takes the column of its spot (`calendar._held`).
    `own` holds the taken times of the asking member's bookings: a cell is
    `mine` when the claim it shows is one of them, and of two claims of one
    state the member's is shown. `names` holds the full name of the member
    behind each other member's booking, by taken time (none for an erased
    member). A booking's cells join only with its own: two bookings side by
    side are two spans.
    """
    every: list[int | None] = [spot.number for spot in spots] if shared else [None]
    number = {spot.pk: spot.number for spot in spots}
    cells: dict[
        int | None, dict[int, tuple[str, bool, EventRef | None, int | None]]
    ] = {}
    for claim in claims:
        first = bisect_right(ends, claim.starts_at)
        last = bisect_left(rows, claim.ends_at) - 1
        state = STATES.get(claim.state)
        mine = state not in ("block", "event") and claim.taken_time_id in own
        event = events.get(claim.taken_time_id) if state == "event" else None
        # Only a booking is told apart from the one beside it.
        booking = None if state in ("block", "event") else claim.taken_time_id
        if state is None or first > last:
            continue
        if claim.asset_id != asset.pk:
            columns = every if kept_whole(asset.whole_space, claim.whole_space) else []
        elif claim.mode == ConsumptionMode.EXCLUSIVE or not shared:
            columns = every
        else:
            columns = [number[claim.spot_id]] if claim.spot_id in number else []
        for column in columns:
            taken = cells.setdefault(column, {})
            for row in range(first, last + 1):
                before = taken.get(row)
                if (
                    before is None
                    or FIRMER[state] < FIRMER[before[0]]
                    or (state == before[0] and mine and not before[1])
                    # Two bookings in one cell: the earlier one's, always.
                    or (
                        state == before[0]
                        and mine == before[1]
                        and booking is not None
                        and before[3] is not None
                        and booking < before[3]
                    )
                    # Two events in one row: the earlier one's, always.
                    or (
                        event is not None
                        and before[2] is not None
                        and state == before[0]
                        and event.id < before[2].id
                    )
                ):
                    taken[row] = (state, mine, event, booking)
    found: list[TakenSpan] = []
    for column in every:
        span: TakenSpan | None = None
        last_booking: int | None = None
        for row, (state, mine, event, booking) in sorted(cells.get(column, {}).items()):
            name = "" if mine or booking is None else names.get(booking, "")
            if (
                span
                and span.last == row - 1
                and span.state == state
                and span.mine == mine
                and span.event == event
                and last_booking == booking
            ):
                span = TakenSpan(column, span.first, row, state, mine, event, name)
                continue
            if span:
                found.append(span)
            span = TakenSpan(column, row, row, state, mine, event, name)
            last_booking = booking
        if span:
            found.append(span)
    budget.taken(len(found))
    return tuple(found)


def _titles(org_id: int, claims: set[int]) -> dict[int, EventRef]:
    """The id and title of the event each claim in `claims` belongs to, of
    this organization only. A removed day holds no claim, so none is here."""
    if not claims:
        return {}
    return {
        day.taken_time_id: EventRef(day.event_id, day.event.title)
        for day in EventDay.objects.of(org_id)
        .filter(taken_time_id__in=claims, removed_at__isnull=True)
        .select_related("event")
        if day.taken_time_id is not None
    }


def _names(member: Member, claims: set[int]) -> dict[int, str]:
    """The full name of the member behind each of these other members'
    bookings , of the asking member's organization only. An
    erased member has none, so none is here. Nothing else of the member is
    read."""
    if not claims:
        return {}
    found = {}
    for taken_time_id, full, first, last in (
        Booking.objects.of(member.organization_id)
        .filter(taken_time_id__in=claims)
        .exclude(member=member)
        .values_list(
            "taken_time_id",
            "member__full_name",
            "member__first_name",
            "member__last_name",
        )
    ):
        name = (full or f"{first} {last}").strip()
        if name:
            found[taken_time_id] = name
    return found


def _column(group: SpotGroup) -> GroupColumn:
    return GroupColumn(group.pk, group.number, group.name, group.word)


def _groups_of(spots: Sequence[Spot]) -> tuple[GroupColumn, ...]:
    """The groups the spots are in, by number."""
    found = {spot.group_id: spot.group for spot in spots if spot.group is not None}
    return tuple(
        _column(group) for group in sorted(found.values(), key=lambda g: g.number)
    )


def _free_by_group(
    spots: Sequence[Spot], free: Sequence[Spot]
) -> tuple[GroupFree, ...]:
    """The free spots of each group of `spots`; nothing when none is in a
    group."""
    groups = _groups_of(spots)
    if not groups:
        return ()
    found = [
        GroupFree(
            group.id,
            tuple(spot.number for spot in free if spot.group_id == group.id),
        )
        for group in groups
    ]
    if any(spot.group_id is None for spot in spots):
        found.append(
            GroupFree(
                None, tuple(spot.number for spot in free if spot.group_id is None)
            )
        )
    return tuple(found)


def _category_spans(
    data: _Snapshot, grid: _Grid, period: _Period
) -> tuple[CategorySpans, ...]:
    """The time of the date period inside each category's own hours."""
    config = period.config
    found = []
    for category_id in data.categories[config.pk]:
        if (config.pk, category_id) not in data.category_hours:
            continue
        spans = _merge(
            span
            for ordinal in range(
                period.day.toordinal(), period.wall_end.date().toordinal() + 1
            )
            for span in grid.category_open(
                config, category_id, date.fromordinal(ordinal)
            )
            or ()
        )
        found.append(
            CategorySpans(
                category_id,
                tuple(
                    (max(first, begin), min(last, end))
                    for first, last in spans
                    for begin, end in period.window
                    if max(first, begin) < min(last, end)
                ),
            )
        )
    return tuple(found)


def _period(
    data: _Snapshot,
    grid: _Grid,
    period: _Period,
    query: tuple[int, int | None],
    window: tuple[datetime, datetime],
    cart: tuple[list[CapacityAllocation], list[Placed]],
    own: frozenset[int],
    moment: datetime,
    budget: _Budget,
    group_id: int | None,
    category_id: int | None,
    events: Mapping[int, EventRef],
    names: Mapping[int, str],
) -> DatePeriod | None:
    """One date period with time in the query dates and some open time
    left (the asset available, no clashing container); else None, the same
    as no container (W4). A start is bookable for the category asked for
    only inside the container's own hours for it ; with none asked
    for, the starts are the container's."""
    found = _rows(grid, period)
    if found is None:
        return None
    rows, ends = found
    budget.consider(len(rows))
    if not (rows[0] < window[1] and window[0] < ends[-1]):
        return None
    if not any(_open(period, row, end) for row, end in zip(rows, ends, strict=True)):
        return None
    config, asset = period.config, data.asset
    shared = config.consumption_mode == ConsumptionMode.SHARED
    spots = (
        spots_in(data.spots.get(config.floor_plan_id or 0, []), group_id)
        if shared
        else []
    )
    first, last = rows[0], ends[-1]
    stored = [
        claim
        for claim in data.allocations
        if claim.starts_at < last and first < claim.ends_at
    ]
    held, placed = cart
    claims = [
        *stored,
        *(claim for claim in held if claim.starts_at < last and first < claim.ends_at),
    ]
    quantity, asked = query
    lengths = range(config.minimum_length, config.maximum_length + 1, config.interval)
    starts: list[Start] = []
    for index, start in enumerate(rows):
        if config.period_type == PeriodType.FIXED:
            end = ends[0]
        elif (wanted := config.minimum_length if asked is None else asked) in lengths:
            # With no length asked, the container's own minimum.
            end = start + wanted * MINUTE
        else:
            break
        if start < moment:
            continue
        budget.consider()
        if not _open(period, start, end) or (
            category_id is not None
            and not category_allows(grid, config, category_id, start, end)
        ):
            continue
        if shared:
            free = free_spots(asset, spots, start, end, claims)
            if len(free) < quantity:
                continue
            keep = ordering._keep(config, start, data.zone, placed)
            taking = tuple(spot.number for spot in without_a_pick(free, quantity, keep))
            free_in = _free_by_group(spots, free)
        elif _fits(asset, config.consumption_mode, 1, [], start, end, claims):
            taking, free_in = (), ()
        else:
            continue
        starts.append(Start(index, end, taking, free_in))
        budget.result()
    return DatePeriod(
        period.day,
        tuple(rows),
        last,
        tuple(ends),
        tuple(
            index
            for index, (row, end) in enumerate(zip(rows, ends, strict=True))
            if not _open(period, row, end)
        ),
        _taken(asset, shared, spots, rows, ends, stored, own, budget, events, names),
        tuple(starts),
        _category_spans(data, grid, period),
    )


def _tiers(tiers: Sequence[PriceTier]) -> tuple[TierPrice, ...]:
    return tuple(TierPrice(tier.up_to, tier.paid_per, tier.amount) for tier in tiers)


def _prices(
    org_id: int, found: Sequence[tuple[_Snapshot, BookingConfiguration]]
) -> dict[int, ContainerPrices]:
    """The prices of the containers with payment, by container: one read of
    their tiers with their entries, whatever the days, periods and categories
    asked for. The entry and tiers of a category are chosen as the price call
    does (`pricing._priced_by_book`)."""
    configs = {
        config.pk: (data, config) for data, config in found if config.with_payment
    }
    if not configs:
        return {}
    read = (
        PriceTier.objects.of(org_id)
        .filter(
            price_book_entry__booking_configuration__in=configs,
            price_book_entry__price_book=F(
                "price_book_entry__booking_configuration__price_book"
            ),
        )
        .select_related("price_book_entry")
    )
    entries: dict[tuple[int, int], dict[int, PriceBookEntry]] = defaultdict(dict)
    tiers: dict[int, list[PriceTier]] = defaultdict(list)
    for tier in read:
        entry = tier.price_book_entry
        entries[entry.booking_configuration_id, entry.booking_category_id][entry.pk] = (
            entry
        )
        tiers[entry.pk].append(tier)
    answered: dict[int, ContainerPrices] = {}
    for pk, (data, config) in configs.items():
        categories = []
        for category_id in data.categories[pk]:
            rows = list(entries[pk, category_id].values())
            try:
                entry, entry_tiers, groups = _priced_by_book(
                    config, rows, {row.pk: tuple(tiers[row.pk]) for row in rows}
                )
            except PriceUnavailableError:
                continue
            categories.append(
                CategoryPrice(
                    category_id,
                    entry.billing_basis,
                    entry.tiers_go_by,
                    _tiers(entry_tiers),
                    tuple(
                        GroupPrice(group, row.tiers_go_by, _tiers(row_tiers))
                        for group, (row, row_tiers) in sorted(groups.items())
                    ),
                )
            )
        # The container's price book's, not the organization's (7.3).
        book = config.price_book
        answered[pk] = ContainerPrices(
            book.currency if book is not None else config.organization.currency,
            tuple(categories),
        )
    return answered


def _container(
    data: _Snapshot,
    config: BookingConfiguration,
    periods: tuple[DatePeriod, ...],
    group_id: int | None,
    prices: ContainerPrices | None,
) -> ContainerCalendar:
    asset = data.asset
    plan = None
    spots = spots_in(data.spots.get(config.floor_plan_id or 0, []), group_id)
    if config.consumption_mode == ConsumptionMode.SHARED and config.floor_plan:
        plan = FloorPlan(
            config.floor_plan.name,
            config.floor_plan.unit,
            tuple(
                SpotColumn(
                    spot.pk,
                    spot.number,
                    spot.name,
                    None if spot.group is None else _column(spot.group),
                )
                for spot in spots
            ),
            _groups_of(spots),
        )
    return ContainerCalendar(
        config.pk,
        config.number,
        asset.pk,
        asset.name,
        asset.maximum_occupancy,
        config.booking_type_id,
        config.booking_type.name,
        data.categories[config.pk],
        config.period_type,
        config.consumption_mode,
        config.with_payment,
        config.interval,
        config.minimum_length,
        config.maximum_length,
        plan,
        len(spots) if plan else 1,
        periods,
        prices,
    )


def _answer(
    member: Member,
    assets: Sequence[Asset],
    booking_type_id: int | None,
    booking_category_id: int | None,
    query: tuple[int, int | None],
    start_date: date,
    end_date: date,
    moment: datetime,
    cart: Sequence[Choice],
    group: tuple[int, int] | None,
    role_id: int | None = None,
) -> tuple[tuple[ContainerCalendar, ...], tuple[DayCategories, ...]]:
    """`group` is the group asked for and its floor plan."""
    group_id = None if group is None else group[0]
    snapshots = []
    for asset in assets:
        # From a week before: the date periods that start before the query
        # dates and run into them come whole, with their taken cells. A date
        # period is shorter than the time to the next day it starts on, so a
        # week reaches the longest (`models/bookable.py`).
        data = _read(
            member,
            asset,
            None if booking_type_id is None else BookingType(pk=booking_type_id),
            date.fromordinal(max(1, start_date.toordinal() - 7)),
            end_date,
            moment,
            role_id,
        )
        if data is not None:
            snapshots.append(data)
    quantity, _ = query

    def asked(data: _Snapshot, config: BookingConfiguration) -> bool:
        """Whether the container books the group and the quantity asked."""
        return (
            group is None
            or (
                config.consumption_mode == ConsumptionMode.SHARED
                and config.floor_plan_id == group[1]
            )
        ) and not (
            config.consumption_mode == ConsumptionMode.SHARED
            and quantity
            > len(spots_in(data.spots.get(config.floor_plan_id or 0, ()), group_id))
        )

    budget = _Budget()
    grids = {id(data): _Grid(data) for data in snapshots}
    others = (
        ()
        if booking_category_id is None
        else _elsewhere(
            snapshots,
            grids,
            asked,
            booking_category_id,
            (start_date, end_date),
            moment,
            budget,
        )
    )
    chosen = [
        (data, config)
        for data in snapshots
        for config in sorted(data.configurations, key=lambda row: row.number)
        if (
            booking_category_id is None
            or booking_category_id in data.categories[config.pk]
        )
        and asked(data, config)
    ]
    if not chosen:
        return (), others
    # The spots the cart's bookings would hold, as an order takes them.
    reads = _Reads(member, moment)
    _, held, placed = _together(
        member, _beside(reads, cart, snapshots[0].asset.scope_key), moment, reads
    )
    # The member's own bookings among all the claims, in one read.
    own = frozenset(
        Booking.objects.of(member.organization_id)
        .filter(
            member=member,
            taken_time_id__in={
                claim.taken_time_id for data in snapshots for claim in data.allocations
            },
        )
        .values_list("taken_time_id", flat=True)
    )
    named = _names(
        member,
        {
            claim.taken_time_id
            for data in snapshots
            for claim in data.allocations
            if claim.state in (BookingState.HELD, BookingState.CONFIRMED)
        }
        - own,
    )
    titled = _titles(
        member.organization_id,
        {
            claim.taken_time_id
            for data in snapshots
            for claim in data.allocations
            if claim.state == EVENT_STATE
        },
    )
    zone = snapshots[0].zone
    window = (
        datetime.combine(start_date, time(), zone).astimezone(UTC),
        datetime.combine(end_date, time(), zone).astimezone(UTC),
    )
    answered = []
    for data, config in chosen:
        grid = grids[id(data)]
        periods = tuple(
            found
            for period in grid.date_periods(
                config,
                date.fromordinal(max(1, start_date.toordinal() - config.end_day)),
                end_date,
            )
            if (
                found := _period(
                    data,
                    grid,
                    period,
                    query,
                    window,
                    (held, placed),
                    own,
                    moment,
                    budget,
                    group_id,
                    booking_category_id,
                    titled,
                    named,
                )
            )
            is not None
        )
        if periods:
            answered.append((data, config, periods))
    prices = _prices(
        member.organization_id, [(data, config) for data, config, _ in answered]
    )
    return tuple(
        _container(data, config, periods, group_id, prices.get(config.pk))
        for data, config, periods in answered
    ), others


def _elsewhere(
    snapshots: Sequence[_Snapshot],
    grids: dict[int, _Grid],
    asked: Callable[[_Snapshot, BookingConfiguration], bool],
    booking_category_id: int,
    dates: tuple[date, date],
    moment: datetime,
    budget: _Budget,
) -> tuple[DayCategories, ...]:
    """The query dates on which another of the member's categories has time
    not yet passed in the containers asked for (the same asset, type, group
    and quantity), as the demo's `whyEmpty` asks. A date period counts on the
    date it starts on; time already booked counts, as the day shows it."""
    found: dict[date, set[int]] = defaultdict(set)
    for data in snapshots:
        grid = grids[id(data)]
        for config in data.configurations:
            wanted = [
                category_id
                for category_id in data.categories[config.pk]
                if category_id != booking_category_id
            ]
            if not wanted or not asked(data, config):
                continue
            for period in grid.date_periods(config, *dates):
                rows = _rows(grid, period)
                if rows is None:
                    continue
                budget.consider(len(rows[0]))
                open_rows = [
                    (row, end)
                    for row, end in zip(*rows, strict=True)
                    if moment < end and _open(period, row, end)
                ]
                for category_id in wanted:
                    if any(
                        category_allows(grid, config, category_id, row, end)
                        for row, end in open_rows
                    ):
                        found[period.day].add(category_id)
    return tuple(
        DayCategories(day, tuple(sorted(ids))) for day, ids in sorted(found.items())
    )


def member_calendar(
    *,
    member: Member,
    asset_id: int | None = None,
    space_id: int | None = None,
    booking_type_id: int | None = None,
    booking_category_id: int | None = None,
    quantity: int = 1,
    length: int | None = None,
    start_date: date,
    end_date: date,
    now: datetime | None = None,
    cart: Sequence[Choice] = (),
    group_id: int | None = None,
    role_id: int | None = None,
) -> MemberCalendar:
    """The booking calendar of one asset or one space, local query dates in
    [start, end).

    Requires its own transaction: one REPEATABLE READ, READ ONLY snapshot.
    Exactly one of the asset and the space is asked for. A date period comes
    when it has time in the query dates, whole, also one that starts before
    them. A container comes when it has such a date period with open time;
    an unknown or foreign asset, space, type or category, and an inactive
    member, get no containers. `length` (minutes), left out, is each flexible
    container's own minimum; a fixed container ignores it. The calendar's
    limits apply to the whole answer. A time free only without the member's
    `cart` (less any booking being changed) is no bookable start; the
    cart's own times are not among the taken cells. With a `group_id` , only the
    containers that book the group's floor plan come, and
    only the group's spots count in them; an unknown or foreign group gets no
    containers. `role_id`, left out, is the member's own role: the categories
    and prices come for it. The price list asks for the organization's
    standard role, so every member sees the same list .
    """
    if (asset_id is None) == (space_id is None):
        raise ValueError("Ask for one asset or one space.")
    for value in (
        asset_id,
        space_id,
        booking_type_id,
        booking_category_id,
        quantity,
        length,
        group_id,
    ):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("Identifiers, quantity and length must be positive.")
    if type(start_date) is not date or type(end_date) is not date:
        raise ValueError("The range needs local dates, not moments.")
    if not 0 < (end_date - start_date).days <= MAX_DAYS:
        raise CalendarLimitError("Request between one and 31 local days.")
    moment = now if now is not None else timezone.now()
    if timezone.is_naive(moment):
        raise ValueError("The current moment must include a time zone.")
    moment = moment.astimezone(UTC)
    rows = [_check(row) for row in cart]
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("The calendar requires its own read-only transaction.")
    org_id = member.organization_id
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
            group = None
            if group_id is not None:
                plan_id = (
                    SpotGroup.objects.of(org_id)
                    .filter(pk=group_id)
                    .values_list("floor_plan_id", flat=True)
                    .first()
                )
                if plan_id is None:
                    return MemberCalendar(moment, ())
                group = (group_id, plan_id)
            assets: list[Asset] = []
            if asset_id is not None:
                assets = list(Asset.objects.of(org_id).filter(pk=asset_id))
            elif (
                space_id is not None
                and Space.objects.of(org_id).filter(pk=space_id).exists()
            ):
                assets = list(
                    Asset.objects.of(org_id)
                    .filter(space_id=space_id)
                    .order_by("number")
                )
            containers, others = _answer(
                member,
                assets,
                booking_type_id,
                booking_category_id,
                (quantity, length),
                start_date,
                end_date,
                moment,
                rows,
                group,
                role_id,
            )
            closed = closed_days(member, assets, start_date, end_date)
            return MemberCalendar(
                moment,
                containers,
                others,
                assets[0].time_zone if assets else "",
                tuple(asset.pk for asset in assets),
                closed,
            )
    except OverflowError:
        raise ValueError(
            "The requested dates exceed the supported calendar range."
        ) from None
