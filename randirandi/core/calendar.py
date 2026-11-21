# SPDX-License-Identifier: AGPL-3.0-only
"""Bookable spans from one coherent, organization-scoped read snapshot.

This is advisory availability, never a reservation. Orders must recheck the
rules and acquire allocations atomically . See ADR-0016.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone

from randirandi.core import booking_limits
from randirandi.core.models import (
    Asset,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    BillingBasis,
    BookingConfiguration,
    BookingType,
    CapacityAllocation,
    CategoryHoursSpan,
    ConfigurationCategory,
    ConsumptionMode,
    Member,
    MemberStatus,
    PeriodType,
    PriceBookEntry,
    PriceTier,
    RoleCategory,
    Spot,
    Status,
)
from randirandi.core.models.base import DAY, WEEK
from randirandi.core.models.bookable import MOST_WEEKS, kept_whole
from randirandi.core.models.orders import HOLDING
from randirandi.core.models.prices import entry_fits_its_mode, what_the_tiers_leave_out

MINUTE = timedelta(minutes=1)
MAX_DAYS = 31
MAX_CANDIDATES = 100_000
MAX_RESULTS = 10_000
Span = tuple[datetime, datetime]
Instants = Callable[[datetime], tuple[datetime, ...]]


class CalendarLimitError(ValueError):
    """The caller must request a smaller date range; no partial result is returned."""


@dataclass(frozen=True)
class BookableInterval:
    starts_at: datetime
    ends_at: datetime
    configuration_id: int
    category_ids: tuple[int, ...]
    consumption_mode: str
    quantity: int


@dataclass
class _Snapshot:
    asset: Asset
    zone: ZoneInfo
    configurations: list[BookingConfiguration]
    categories: dict[int, tuple[int, ...]]
    calendars: list[AvailabilityCalendar]
    spans: dict[int, dict[int, list[tuple[int, int]]]]
    allocations: list[CapacityAllocation]
    # The spots of each shared configuration's floor plan, lowest number
    # first.
    spots: dict[int, list[Spot]]
    # For each configuration, the containers that cover the same time
    # although they may not (`validator.rivals`: two shared containers on
    # different floor plans of one asset).
    rivals: dict[int, list[BookingConfiguration]]
    # A container's own hours for a booking category it offers ():
    # (configuration, category) → weekday → (start, end) in minutes. A pair
    # without spans is not among them: it has the container's hours.
    category_hours: dict[tuple[int, int], dict[int, list[tuple[int, int]]]]
    # The first moment a start is too far ahead for the member's organization
    # (`booking_limits`); a start at or after it is closed time. None: no limit.
    starts_before: datetime | None = None
    # The dates closed beside the weekly availability (first, last, reason):
    # a closed day has no available time at all . Only those
    # near the query are read.
    closures: tuple[tuple[date, date, str], ...] = ()


def valid_configuration(config: BookingConfiguration) -> bool:
    length = config.period_length
    if not 0 < length <= WEEK or not config.period_start_weekdays:
        return False
    if config.consumption_mode not in ConsumptionMode.values:
        return False
    if config._periods_overlap_each_other():
        return False
    if not 1 <= config.every_weeks <= MOST_WEEKS or (
        config.every_weeks > 1 and not config.start_date
    ):
        return False
    if config.period_type == PeriodType.FIXED:
        return (
            config.interval == config.minimum_length == config.maximum_length == length
        )
    if config.period_type != PeriodType.FLEXIBLE:
        return False
    return (
        length <= DAY
        and config.interval > 0
        and 0 < config.minimum_length <= config.maximum_length <= length
        and all(
            value % config.interval == 0
            for value in (length, config.minimum_length, config.maximum_length)
        )
    )


def offered_categories(
    organization_id: int,
    role_id: int,
    configurations: Iterable[BookingConfiguration],
) -> dict[int, tuple[int, ...]]:
    """The role's categories each configuration offers, with a complete price.

    A configuration offers a category only when it names it, the role can book
    it and its own price book covers every allowed length (rules 6 and 12).
    A configuration without payment has no price book and offers every
    category it names that the role can book, at 0 (W12).
    Configurations offering none are left out of the result.
    """
    org_id = organization_id
    config_by_id = {config.pk: config for config in configurations}
    allowed = set(
        RoleCategory.objects.of(org_id)
        .filter(
            role_id=role_id,
        )
        .values_list("booking_category_id", flat=True)
    )
    config_ids = list(config_by_id)
    linked: dict[int, set[int]] = defaultdict(set)
    for config_id, category_id in (
        ConfigurationCategory.objects.of(org_id)
        .filter(
            booking_configuration_id__in=config_ids,
            booking_category_id__in=allowed,
        )
        .values_list("booking_configuration_id", "booking_category_id")
    ):
        linked[config_id].add(category_id)
    entries = list(
        PriceBookEntry.objects.of(org_id).filter(
            booking_configuration_id__in=config_ids,
            booking_category_id__in=allowed,
        )
    )
    tiers: dict[int, list[int]] = defaultdict(list)
    for entry_id, up_to in (
        PriceTier.objects.of(org_id)
        .filter(
            price_book_entry_id__in=[entry.pk for entry in entries],
        )
        .values_list("price_book_entry_id", "up_to")
    ):
        tiers[entry_id].append(up_to)
    priced: dict[int, set[int]] = defaultdict(set)
    for config in config_by_id.values():
        if not config.with_payment:
            priced[config.pk] |= linked[config.pk]
    # A category is priced by its normal entry; the entries that name a group
    # of spots  are per spot, and must be complete too, since a
    # booking of those spots takes them.
    normal: dict[tuple[int, int], PriceBookEntry] = {}
    named: set[tuple[int, int]] = set()
    unfit: set[tuple[int, int]] = set()
    for entry in entries:
        config = config_by_id[entry.booking_configuration_id]
        key = (config.pk, entry.booking_category_id)
        if (
            entry.price_book_id != config.price_book_id
            or entry.booking_category_id not in linked[config.pk]
        ):
            continue
        if not (
            entry_fits_its_mode(config, entry)
            and what_the_tiers_leave_out(config, entry.tiers_go_by, tiers[entry.pk])
            is None
        ):
            unfit.add(key)
        if entry.spot_group_id is None:
            normal[key] = entry
        else:
            named.add(key)
            if entry.billing_basis != BillingBasis.PER_CAPACITY_UNIT:
                unfit.add(key)
    for key, entry in normal.items():
        if key not in unfit and (
            key not in named or entry.billing_basis == BillingBasis.PER_CAPACITY_UNIT
        ):
            priced[key[0]].add(key[1])
    return {pk: tuple(sorted(ids)) for pk, ids in priced.items() if ids}


def category_hours_of(
    organization_id: int, priced: dict[int, tuple[int, ...]]
) -> dict[tuple[int, int], dict[int, list[tuple[int, int]]]]:
    """The own hours of the categories each configuration offers (`priced`,
    as `offered_categories` gives it), in one read."""
    found: dict[tuple[int, int], dict[int, list[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for config_id, category_id, weekday, first, last in (
        CategoryHoursSpan.objects.of(organization_id)
        .filter(configuration_category__booking_configuration_id__in=list(priced))
        .values_list(
            "configuration_category__booking_configuration_id",
            "configuration_category__booking_category_id",
            "weekday",
            "start_time",
            "end_time",
        )
    ):
        if category_id in priced[config_id]:
            found[config_id, category_id][weekday].append((first, last))
    return found


def _read(
    member: Member,
    asset: Asset,
    booking_type: BookingType | None,
    start_date: date,
    end_date: date,
    now: datetime,
    role_id: int | None = None,
) -> _Snapshot | None:
    """The snapshot of one asset for the member, of one booking type or of
    all of them (`None`). `now` decides which disabled containers still keep
    their time. `role_id`, left out, is the member's own role; the price list
    asks for the organization's standard role .
    """
    # Imported here: the validator uses the calendar's own cover test.
    from randirandi.core import validator

    org_id = member.organization_id
    stored_member = (
        Member.objects.of(org_id)
        .select_related("organization")
        .filter(
            pk=member.pk,
            status=MemberStatus.ACTIVE,
        )
        .first()
    )
    stored_asset = Asset.objects.of(org_id).filter(pk=asset.pk).first()
    if (
        stored_member is None
        or stored_asset is None
        or (
            booking_type is not None
            and not BookingType.objects.of(org_id).filter(pk=booking_type.pk).exists()
        )
    ):
        return None
    containers = BookingConfiguration.objects.of(org_id).filter(
        asset=stored_asset, status=Status.ENABLED
    )
    if booking_type is not None:
        containers = containers.filter(booking_type_id=booking_type.pk)
    configurations = [
        config
        for config in containers.select_related(
            "booking_type", "floor_plan", "organization", "price_book"
        )
        if valid_configuration(config)
    ]
    priced = offered_categories(
        org_id, stored_member.role_id if role_id is None else role_id, configurations
    )
    configurations = [config for config in configurations if config.pk in priced]
    if not configurations:
        return None
    zone = stored_asset.zone
    calendars = list(
        AvailabilityCalendar.objects.of(org_id)
        .filter(
            asset=stored_asset,
        )
        .order_by("start_date")
    )
    spans: dict[int, dict[int, list[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for cal_id, weekday, first, last in (
        AvailableSpan.objects.of(org_id)
        .filter(
            availability_calendar_id__in=[calendar.pk for calendar in calendars],
        )
        .values_list("availability_calendar_id", "weekday", "start_time", "end_time")
    ):
        spans[cal_id][weekday].append((first, last))
    # The longest period runs a week, so a closure within two weeks either way
    # of the query can decide a day that is asked for.
    low = date.fromordinal(max(1, start_date.toordinal() - 14))
    high = date.fromordinal(min(date.max.toordinal(), end_date.toordinal() + 14))
    closures = tuple(
        AvailabilityClosure.objects.of(org_id)
        .filter(asset=stored_asset, last_date__gte=low, first_date__lte=high)
        .values_list("first_date", "last_date", "reason")
    )
    # An offset is strictly less than a day. Pad UTC bounds for local dates
    # and for the longest possible fixed period, without guessing a UTC offset.
    first_moment = datetime.combine(start_date, time(), UTC) - timedelta(days=1)
    last_moment = datetime.combine(end_date, time(), UTC) + timedelta(days=8)
    allocations = list(
        CapacityAllocation.objects.of(org_id).filter(
            scope_key=stored_asset.scope_key,
            state__in=HOLDING,
            starts_at__lt=last_moment,
            ends_at__gt=first_moment,
        )
    )
    return _Snapshot(
        stored_asset,
        zone,
        configurations,
        priced,
        calendars,
        spans,
        allocations,
        floor_plan_spots(org_id, configurations),
        validator.rivals(stored_asset, configurations, now, zone),
        category_hours_of(org_id, priced),
        booking_limits.starts_before(stored_member.organization, now),
        closures,
    )


def floor_plan_spots(
    organization_id: int, configurations: Iterable[BookingConfiguration]
) -> dict[int, list[Spot]]:
    """The spots of the shared configurations' floor plans, lowest number
    first."""
    found: dict[int, list[Spot]] = defaultdict(list)
    plans = {
        config.floor_plan_id
        for config in configurations
        if config.consumption_mode == ConsumptionMode.SHARED
        and config.floor_plan_id is not None
    }
    for spot in (
        Spot.objects.of(organization_id)
        .filter(floor_plan_id__in=plans)
        .select_related("group")
        .order_by("number")
    ):
        found[spot.floor_plan_id].append(spot)
    return {plan: found[plan] for plan in plans}


def _instants(wall: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Zero moments in a spring gap, two in a fold, otherwise one."""
    found = set()
    for fold in (0, 1):
        moment = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if moment.astimezone(zone).replace(tzinfo=None) == wall:
            found.add(moment)
    return tuple(sorted(found))


def _merge(spans: Iterable[Span]) -> list[Span]:
    result: list[Span] = []
    for start, end in sorted(spans):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _local_span(start: datetime, end: datetime, instants: Instants) -> list[Span]:
    """Invert a local window at the model's minute resolution.

    Mapping only its endpoints would incorrectly open the gap between two
    occurrences of a short window inside a repeated hour. Mapping all local
    minutes preserves both folds and omits nonexistent wall minutes.

    A window whose start, whole local hours and last minute each have one
    moment, all at the same UTC offset, holds no gap or fold: it is one span.
    That assumes no zone changes its clocks twice within one hour.
    """
    if start >= end:
        return []
    last = end - MINUTE
    samples = [start]
    hour = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while hour <= last:
        samples.append(hour)
        hour += timedelta(hours=1)
    samples.append(last)
    offsets = set()
    for wall in samples:
        found = instants(wall)
        if len(found) != 1:
            break
        offsets.add(wall - found[0].replace(tzinfo=None))
    else:
        if len(offsets) == 1:
            first = instants(start)[0]
            return [(first, instants(last)[0] + MINUTE)]
    parts: list[Span] = []
    wall = start
    while wall < end:
        parts.extend((instant, instant + MINUTE) for instant in instants(wall))
        wall += MINUTE
    return _merge(parts)


def _covered(start: datetime, end: datetime, spans: list[Span]) -> bool:
    return any(a <= start and end <= b for a, b in spans)


def _held(
    asset: Asset,
    start: datetime,
    end: datetime,
    claims: Iterable[CapacityAllocation],
) -> set[int] | None:
    """The spots of the asset that other claims hold on the span; None when
    one holds all of them: an exclusive claim on the asset, or any claim
    beside one on an asset that covers its whole space. The same as the
    database's rule (`capacity_no_double_booking`); `claims` are those in the
    asset's space key."""
    held = set()
    for claim in claims:
        if claim.ends_at <= start or end <= claim.starts_at:
            continue
        if claim.asset_id != asset.pk:
            if kept_whole(asset.whole_space, claim.whole_space):
                return None
        elif claim.mode == ConsumptionMode.EXCLUSIVE:
            return None
        elif claim.spot_id is not None:
            held.add(claim.spot_id)
    return held


def free_spots(
    asset: Asset,
    spots: Iterable[Spot],
    start: datetime,
    end: datetime,
    claims: Iterable[CapacityAllocation],
) -> list[Spot]:
    """The spots free for the whole span, in the order given."""
    held = _held(asset, start, end, claims)
    return [] if held is None else [spot for spot in spots if spot.pk not in held]


def spots_in(spots: Iterable[Spot], group_id: int | None) -> list[Spot]:
    """The spots of the group when one is asked for; all of them otherwise."""
    return [spot for spot in spots if group_id is None or spot.group_id == group_id]


def without_a_pick(
    free: Iterable[Spot], quantity: int, keep: Sequence[int] = ()
) -> list[Spot]:
    """The spots a booking takes when nobody picks .

    A party stays together: it goes to the lowest-numbered group with room
    for all of it, at the lowest spot numbers inside it. A group holding a
    free spot of `keep` (the spots of the date period before, W18) comes
    first, and inside the group those spots come first. When no group has
    room, the lowest free spots anywhere, `keep` first, as before groups.
    """
    rank = {spot: index for index, spot in enumerate(keep)}
    ordered = sorted(free, key=lambda spot: (rank.get(spot.pk, len(rank)), spot.number))
    groups: dict[int, list[Spot]] = {}
    for spot in ordered:
        if spot.group_id is not None:
            groups.setdefault(spot.group_id, []).append(spot)
    with_room = [spots for spots in groups.values() if len(spots) >= quantity]
    if with_room:
        # Each list is in order, so its first spot has the group's best rank.
        best = min(
            with_room,
            key=lambda spots: (
                rank.get(spots[0].pk, len(rank)),
                spots[0].group.number if spots[0].group else 0,
            ),
        )
        return best[:quantity]
    return ordered[:quantity]


def _fits(
    asset: Asset,
    mode: str,
    quantity: int,
    spots: list[Spot],
    start: datetime,
    end: datetime,
    claims: list[CapacityAllocation],
) -> bool:
    """Whether a booking fits: an exclusive one when no claim holds any spot
    of the asset, a shared one when `quantity` of the container's spots are
    each free for the whole span."""
    if mode == ConsumptionMode.EXCLUSIVE:
        return quantity == 1 and _held(asset, start, end, claims) == set()
    return 0 < quantity <= len(free_spots(asset, spots, start, end, claims))


def columns(data: _Snapshot, config: BookingConfiguration) -> int:
    """The configuration's places: its floor plan's spots when shared; an
    exclusive one is one column, the whole asset."""
    if config.consumption_mode == ConsumptionMode.EXCLUSIVE:
        return 1
    return len(data.spots.get(config.floor_plan_id or 0, ()))


def period_day(config: BookingConfiguration, wall: datetime) -> date:
    """The day the date period a booking starting at the wall moment is in
    starts on; the booking's own day when it is in none."""
    for back in range(config.end_day + 1):
        day = wall.date() - timedelta(days=back)
        midnight = datetime.combine(day, time())
        start = midnight + config.period_start * MINUTE
        end = midnight + (config.end_day * DAY + config.period_end) * MINUTE
        if config.starts_on(day) and start <= wall < end:
            return day
    return wall.date()


def next_period_day(config: BookingConfiguration, day: date) -> date | None:
    """The day the configuration's next date period after the day's starts
    on; None when it has none within its longest cycle."""
    for ordinal in range(day.toordinal() + 1, day.toordinal() + 7 * MOST_WEEKS + 1):
        if config.starts_on(date.fromordinal(ordinal)):
            return date.fromordinal(ordinal)
    return None


def on_its_grid(
    config: BookingConfiguration, start: datetime, end: datetime, zone: ZoneInfo
) -> bool:
    """Whether the time is whole cells of the configuration's date periods:
    it starts and ends on the edge of a cell, and every moment of it lies in
    one of its date periods. A fixed period is one cell; a flexible one has a
    cell for each interval. What a block takes (`BlockManager.take`)."""
    if end <= start or not valid_configuration(config):
        return False

    def instants(wall: datetime) -> tuple[datetime, ...]:
        return _instants(wall, zone)

    step = (
        config.period_length
        if config.period_type == PeriodType.FIXED
        else config.interval
    )
    first = start.astimezone(zone).date() - timedelta(days=config.end_day + 1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    windows: list[Span] = []
    edges: set[datetime] = set()
    for ordinal in range(first.toordinal(), last.toordinal() + 1):
        day = date.fromordinal(ordinal)
        if not config.starts_on(day):
            continue
        midnight = datetime.combine(day, time())
        wall_start = midnight + config.period_start * MINUTE
        wall_end = midnight + (config.end_day * DAY + config.period_end) * MINUTE
        windows.extend(_local_span(wall_start, wall_end, instants))
        for offset in range(0, config.period_length + 1, step):
            edges.update(instants(wall_start + offset * MINUTE))
    return start in edges and end in edges and _covered(start, end, _merge(windows))


def _cells(
    config: BookingConfiguration,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    wanted: Callable[[Span], bool],
) -> list[Span]:
    if end <= start or not valid_configuration(config):
        return []

    def instants(wall: datetime) -> tuple[datetime, ...]:
        return _instants(wall, zone)

    step = (
        config.period_length
        if config.period_type == PeriodType.FIXED
        else config.interval
    )
    first = start.astimezone(zone).date() - timedelta(days=config.end_day + 1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    cells: set[Span] = set()
    for ordinal in range(first.toordinal(), last.toordinal() + 1):
        day = date.fromordinal(ordinal)
        if not config.starts_on(day):
            continue
        wall_start = datetime.combine(day, time()) + config.period_start * MINUTE
        for offset in range(0, config.period_length - step + 1, step):
            cells.update(
                cell
                for cell in _local_span(
                    wall_start + offset * MINUTE,
                    wall_start + (offset + step) * MINUTE,
                    instants,
                )
                if wanted(cell)
            )
    return sorted(cells)


def cells_within(
    config: BookingConfiguration, start: datetime, end: datetime, zone: ZoneInfo
) -> list[Span]:
    """The cells of the configuration's date periods that lie wholly inside
    the time, by start: a fixed period is one cell, a flexible one a cell
    for each interval (the cells `on_its_grid` accepts). What the admin's
    Blocks lays a stretch from."""
    return _cells(
        config, start, end, zone, lambda cell: start <= cell[0] and cell[1] <= end
    )


def cells_touching(
    config: BookingConfiguration, start: datetime, end: datetime, zone: ZoneInfo
) -> list[Span]:
    """The cells that share any moment with the time, by start: what an event
    takes out of booking, the part it does not fill included . A night sold
    as one piece is one cell."""
    return _cells(
        config, start, end, zone, lambda cell: cell[0] < end and start < cell[1]
    )


@dataclass(frozen=True)
class _Period:
    """One date period of a configuration, with its local window, the
    availability of every day it touches, and the time that containers
    clashing with it take."""

    config: BookingConfiguration
    # The day it starts on, which it belongs to (rule 8).
    day: date
    wall_start: datetime
    wall_end: datetime
    window: list[Span]
    opening: list[Span]
    clashing: list[Span]
    # Starts at or after it are closed (the snapshot's `starts_before`).
    starts_before: datetime | None = None


def closure_reason(closures: Iterable[tuple[date, date, str]], day: date) -> str | None:
    """Why a day is closed by a closure: its reason's key, "" for a closure with
    none, None when no closure covers the day. Of several, the first with a
    reason decides."""
    covering = [reason for first, last, reason in closures if first <= day <= last]
    if not covering:
        return None
    return next((reason for reason in covering if reason), "")


def weekly_spans(
    calendars: Sequence[AvailabilityCalendar],
    spans: dict[int, dict[int, list[tuple[int, int]]]],
    day: date,
) -> list[tuple[int, int]]:
    """The weekly availability's spans (minutes) on a day: those of the latest
    calendar that has begun, for the day's weekday; none without one."""
    applicable = [calendar for calendar in calendars if calendar.start_date <= day]
    if not applicable:
        return []
    return spans[applicable[-1].pk][day.weekday()]


@dataclass(frozen=True)
class ClosedDay:
    """An asset closed on a local date: by a closure (`by` "closure", with its
    reason's key or "") or because its weekly availability does not open that
    day (`by` "weekly", no reason)."""

    asset_id: int
    day: date
    by: str
    reason: str


def closed_days(
    member: Member, assets: Sequence[Asset], start_date: date, end_date: date
) -> tuple[ClosedDay, ...]:
    """The closed local dates of the assets in [start, end) for a member of
    their organization; nothing for an inactive member, and none for an asset
    of another organization. Read apart from the containers (an asset is
    closed whatever is asked of it), in five queries however many assets."""
    org_id = member.organization_id
    if (
        not assets
        or not Member.objects.of(org_id)
        .filter(pk=member.pk, status=MemberStatus.ACTIVE)
        .exists()
    ):
        return ()
    ids = sorted(
        Asset.objects.of(org_id)
        .filter(pk__in=[asset.pk for asset in assets])
        .values_list("pk", flat=True)
    )
    calendars: dict[int, list[AvailabilityCalendar]] = defaultdict(list)
    for calendar in (
        AvailabilityCalendar.objects.of(org_id)
        .filter(asset_id__in=ids)
        .order_by("start_date")
    ):
        calendars[calendar.asset_id].append(calendar)
    spans: dict[int, dict[int, list[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for cal_id, weekday, first, last in (
        AvailableSpan.objects.of(org_id)
        .filter(availability_calendar__asset_id__in=ids)
        .values_list("availability_calendar_id", "weekday", "start_time", "end_time")
    ):
        spans[cal_id][weekday].append((first, last))
    closures: dict[int, list[tuple[date, date, str]]] = defaultdict(list)
    for owner, begins, ends, why in (
        AvailabilityClosure.objects.of(org_id)
        .filter(asset_id__in=ids, last_date__gte=start_date, first_date__lt=end_date)
        .values_list("asset_id", "first_date", "last_date", "reason")
    ):
        closures[owner].append((begins, ends, why))
    found = []
    for asset_id in ids:
        for offset in range((end_date - start_date).days):
            day = start_date + timedelta(days=offset)
            reason = closure_reason(closures[asset_id], day)
            if reason is not None:
                found.append(ClosedDay(asset_id, day, "closure", reason))
            elif not weekly_spans(calendars[asset_id], spans, day):
                found.append(ClosedDay(asset_id, day, "weekly", ""))
    return tuple(found)


class _Grid:
    """A snapshot's local time: wall moments and availability, cached.

    The caches are plain dictionaries. A `functools.cache` of a bound method
    holds the grid itself: the grid, its snapshot and every answer then wait
    for the cycle collector instead of going when the call returns, and the
    sandbox ran out of memory between collections .
    """

    def __init__(self, data: _Snapshot) -> None:
        self.data = data
        self._instants_at: dict[datetime, tuple[datetime, ...]] = {}
        self._available_on: dict[date, tuple[Span, ...]] = {}
        self._taken_by: dict[tuple[BookingConfiguration, date], tuple[Span, ...]] = {}
        self._open_for: dict[
            tuple[BookingConfiguration, int, date], tuple[Span, ...] | None
        ] = {}

    def instants(self, wall: datetime) -> tuple[datetime, ...]:
        if wall not in self._instants_at:
            self._instants_at[wall] = _instants(wall, self.data.zone)
        return self._instants_at[wall]

    def available(self, day: date) -> tuple[Span, ...]:
        if day not in self._available_on:
            self._available_on[day] = self._available(day)
        return self._available_on[day]

    def taken(self, rival: BookingConfiguration, day: date) -> tuple[Span, ...]:
        key = (rival, day)
        if key not in self._taken_by:
            self._taken_by[key] = self._taken(rival, day)
        return self._taken_by[key]

    def category_open(
        self, config: BookingConfiguration, category_id: int, day: date
    ) -> tuple[Span, ...] | None:
        key = (config, category_id, day)
        if key not in self._open_for:
            self._open_for[key] = self._category_open(config, category_id, day)
        return self._open_for[key]

    def _available(self, day: date) -> tuple[Span, ...]:
        if closure_reason(self.data.closures, day) is not None:
            return ()
        weekly = weekly_spans(self.data.calendars, self.data.spans, day)
        midnight = datetime.combine(day, time())
        return tuple(
            _merge(
                part
                for first, last in weekly
                for part in _local_span(
                    midnight + first * MINUTE, midnight + last * MINUTE, self.instants
                )
            )
        )

    def _category_open(
        self, config: BookingConfiguration, category_id: int, day: date
    ) -> tuple[Span, ...] | None:
        """The local time a container offers a booking category on a day:
        its own spans for the weekday, which may be none (closed to it that
        day). None when the category has no spans on the container: it has
        the container's hours."""
        spans = self.data.category_hours.get((config.pk, category_id))
        if spans is None:
            return None
        midnight = datetime.combine(day, time())
        return tuple(
            _merge(
                part
                for first, last in spans.get(day.weekday(), ())
                for part in _local_span(
                    midnight + first * MINUTE, midnight + last * MINUTE, self.instants
                )
            )
        )

    def _taken(self, rival: BookingConfiguration, day: date) -> tuple[Span, ...]:
        """The time a clashing container's date period starting on the day
        takes; nothing when none starts on it."""
        if not rival.starts_on(day):
            return ()
        midnight = datetime.combine(day, time())
        return tuple(
            _local_span(
                midnight + rival.period_start * MINUTE,
                midnight + (rival.end_day * DAY + rival.period_end) * MINUTE,
                self.instants,
            )
        )

    def clashing(
        self, config: BookingConfiguration, first: date, last: date
    ) -> list[Span]:
        """The run-time guard: the time that containers clashing with the
        configuration take from the first local date to the last. Two
        containers that may not cover the same time (`validator.rivals`) but
        do anyway (a write that skipped the validator) leave it unoffered for
        both.
        """
        return _merge(
            span
            for rival in self.data.rivals.get(config.pk, ())
            for ordinal in range(
                max(1, first.toordinal() - rival.end_day - 1), last.toordinal() + 1
            )
            for span in self.taken(rival, date.fromordinal(ordinal))
        )

    def periods(
        self, start_date: date, end_date: date, quantity: int
    ) -> Iterator[_Period]:
        data = self.data
        for config in data.configurations:
            if (
                config.consumption_mode == ConsumptionMode.EXCLUSIVE and quantity != 1
            ) or quantity > columns(data, config):
                continue
            # A multi-day flexible window can have starts after midnight; those
            # starts belong in the query for that next day, with the prior anchor.
            lookback = (
                config.end_day if config.period_type == PeriodType.FLEXIBLE else 0
            )
            first = date.fromordinal(max(1, start_date.toordinal() - lookback))
            yield from self.date_periods(config, first, end_date)

    def date_periods(
        self, config: BookingConfiguration, first: date, last: date
    ) -> Iterator[_Period]:
        """The configuration's date periods that start from the first day to
        before the last."""
        for ordinal in range(first.toordinal(), last.toordinal()):
            day = date.fromordinal(ordinal)
            if not config.starts_on(day):
                continue
            midnight = datetime.combine(day, time())
            wall_start = midnight + config.period_start * MINUTE
            wall_end = midnight + (config.end_day * DAY + config.period_end) * MINUTE
            yield _Period(
                config,
                day,
                wall_start,
                wall_end,
                _local_span(wall_start, wall_end, self.instants),
                _merge(
                    part
                    for d in range(day.toordinal(), wall_end.date().toordinal() + 1)
                    for part in self.available(date.fromordinal(d))
                ),
                self.clashing(config, day, wall_end.date()),
                self.data.starts_before,
            )

    def fixed_span(self, period: _Period) -> Span | None:
        # A fixed period with an ambiguous/nonexistent boundary has no
        # configured fold choice. Do not invent a different night.
        starts = self.instants(period.wall_start)
        ends = self.instants(period.wall_end)
        return (starts[0], ends[0]) if len(starts) == len(ends) == 1 else None

    def flexible_starts(self, period: _Period) -> Iterator[datetime]:
        config = period.config
        for offset in range(0, config.period_length, config.interval):
            yield from self.instants(period.wall_start + offset * MINUTE)


def category_allows(
    grid: _Grid,
    config: BookingConfiguration,
    category_id: int,
    start: datetime,
    end: datetime,
) -> bool:
    """Whether the container's own hours for the booking category
    hold the whole time; true when it has none. The calendar, the order and
    the validator all ask this one."""
    zone = grid.data.zone
    first = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    days = [date.fromordinal(n) for n in range(first.toordinal(), last.toordinal() + 1)]
    spans = [grid.category_open(config, category_id, day) for day in days]
    if all(part is None for part in spans):
        return True
    return _covered(start, end, _merge(span for part in spans for span in part or ()))


def hours_grid(
    asset: Asset,
    zone: ZoneInfo,
    config: BookingConfiguration,
    category_id: int,
    spans: dict[int, list[tuple[int, int]]],
) -> _Grid:
    """A grid that knows only the container's own hours for one booking
    category: what the validator asks `category_allows` with, for hours it has
    not stored yet."""
    return _Grid(
        _Snapshot(
            asset,
            zone,
            [config],
            {},
            [],
            {},
            [],
            {},
            {},
            {(config.pk, category_id): spans} if spans else {},
        )
    )


def _open(period: _Period, start: datetime, end: datetime) -> bool:
    """Whether a time lies in the date period's window and in the asset's
    availability, no clashing container takes any of it, and its start is not
    beyond how far ahead the organization lets a member book."""
    return (
        start < end
        and (period.starts_before is None or start < period.starts_before)
        and _covered(start, end, period.window)
        and _covered(start, end, period.opening)
        and not any(first < end and start < last for first, last in period.clashing)
    )


def _bookable(
    data: _Snapshot,
    period: _Period,
    quantity: int,
    start: datetime,
    end: datetime,
    now: datetime,
    start_date: date,
    end_date: date,
) -> bool:
    return (
        now <= start
        and start_date <= start.astimezone(data.zone).date() < end_date
        and _open(period, start, end)
        and _fits(
            data.asset,
            period.config.consumption_mode,
            quantity,
            data.spots.get(period.config.floor_plan_id or 0, []),
            start,
            end,
            data.allocations,
        )
    )


class _Budget:
    """The limits of one answer: the starts and rows it works through, the
    bookable starts and the taken spans it gives."""

    def __init__(self) -> None:
        self.considered = 0
        self.results = 0
        self.spans = 0

    def consider(self, count: int = 1) -> None:
        self.considered += count
        if self.considered > MAX_CANDIDATES:
            raise CalendarLimitError("Too many candidate starts; request fewer days.")

    def result(self) -> None:
        self.results += 1
        if self.results > MAX_RESULTS:
            raise CalendarLimitError("Too many bookable starts; request fewer days.")

    def taken(self, count: int) -> None:
        self.spans += count
        if self.spans > MAX_RESULTS:
            raise CalendarLimitError("Too many taken spans; request fewer days.")


def _calculate(
    data: _Snapshot, start_date: date, end_date: date, quantity: int, now: datetime
) -> tuple[BookableInterval, ...]:
    grid = _Grid(data)
    budget = _Budget()
    results = []
    for period in grid.periods(start_date, end_date, quantity):
        config = period.config
        candidates: Iterable[Span]
        if config.period_type == PeriodType.FIXED:
            span = grid.fixed_span(period)
            candidates = (span,) if span else ()
        else:
            candidates = (
                (start, start + minutes * MINUTE)
                for start in grid.flexible_starts(period)
                for minutes in range(
                    config.minimum_length, config.maximum_length + 1, config.interval
                )
            )
        for start, end in candidates:
            budget.consider()
            if not _bookable(
                data, period, quantity, start, end, now, start_date, end_date
            ):
                continue
            results.append(
                BookableInterval(
                    start,
                    end,
                    config.pk,
                    data.categories[config.pk],
                    config.consumption_mode,
                    quantity,
                )
            )
            budget.result()
    return tuple(
        sorted(
            results,
            key=lambda item: (item.starts_at, item.ends_at, item.configuration_id),
        )
    )


def bookable_intervals(
    *,
    member: Member,
    asset: Asset,
    booking_type: BookingType,
    start_date: date,
    end_date: date,
    quantity: int = 1,
    now: datetime | None = None,
) -> tuple[BookableInterval, ...]:
    """Return bookable spans whose local start dates lie in [start, end).

    Requires its own transaction: all ORM reads share a REPEATABLE READ,
    READ ONLY snapshot. No locks are taken to reserve configuration or stock.
    Stale caller objects supply IDs only. Inactive/foreign/missing inputs
    return no configurations. Invalid query bounds raise ValueError. Large
    expansions raise CalendarLimitError and never return a truncated
    calendar.
    """
    if type(quantity) is not int or quantity < 1:
        raise ValueError("Quantity must be a positive whole number.")
    if type(start_date) is not date or type(end_date) is not date:
        raise ValueError("The range needs local dates, not moments.")
    if not 0 < (end_date - start_date).days <= MAX_DAYS:
        raise CalendarLimitError("Request between one and 31 local days.")
    moment = now if now is not None else timezone.now()
    if timezone.is_naive(moment):
        raise ValueError("The current moment must include a time zone.")
    moment = moment.astimezone(UTC)
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("The calendar requires its own read-only transaction.")
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
            data = _read(member, asset, booking_type, start_date, end_date, moment)
            if data is None:
                return ()
            return _calculate(data, start_date, end_date, quantity, moment)
    except OverflowError:
        raise ValueError(
            "The requested dates exceed the supported calendar range."
        ) from None
