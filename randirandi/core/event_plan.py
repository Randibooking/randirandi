# SPDX-License-Identifier: AGPL-3.0-only
"""What an event takes and what is in its way : the
reading half of the event services, written nowhere.

A `World` reads one asset once for a window of time: its weekly hours and
closures (as the run-time offer reads them, `availability.timeline`), the
claims that hold the asset or its space, and its containers' cells. It then
answers, day by day and in memory, whether an event fits (`DayPlan`), what
is in the way with the way out of each (`InWay`), and the part of a cell the
event leaves that nobody can book (`Leaving`).
"""

from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q

from randirandi.core import availability, calendar
from randirandi.core.models import (
    HOLDING,
    Asset,
    AvailabilityClosure,
    Block,
    Booking,
    BookingConfiguration,
    EventDay,
    EventRepeat,
    Organization,
    SkipReason,
    Status,
    TakenTime,
)
from randirandi.core.models.base import DAY
from randirandi.core.models.orders import BLOCKED, EVENT_STATE

type Span = tuple[datetime, datetime]

MINUTE = timedelta(minutes=1)
# How far ahead a repeat's days are made, from today (S6: a year).
HORIZON = timedelta(days=365)

# What an administrator can do about each thing in the way, by key. The words
# are the screens'.
CANCEL_BOOKING = "cancel_booking"
REMOVE_BLOCK = "remove_block"
CHANGE_CLOSURE = "change_closure"
REMOVE_CLOSURE = "remove_closure"
CHANGE_HOURS = "change_hours"
REMOVE_EVENT = "remove_event"
REMOVE_DAY = "remove_day"
OTHER_HOURS = "other_hours"

# The reason a day is skipped when several things are in the way: the first.
PRIORITY = (SkipReason.CLOSED, SkipReason.BOOKED, SkipReason.BLOCKED, SkipReason.EVENT)


@dataclass(frozen=True)
class InWay:
    """One thing in an event's way, and what can be done about it."""

    reason: str
    ways: tuple[str, ...]
    # A booking: its number and its reference. An order's number is what
    # people read, the reference what they quote.
    number: int | None = None
    reference: str = ""
    # The time a booking holds (its claim), to read its hours and member.
    claim_id: int | None = field(default=None, compare=False)
    # A block.
    block_id: int | None = None
    # Another event's day: the day, its event, the event's title, and whether
    # the event repeats (a repeat's day can be settled in the same save).
    day_id: int | None = None
    event_id: int | None = None
    title: str = ""
    repeating: bool = False
    # A closure, or the closures behind a closed time.
    closure_ids: tuple[int, ...] = ()
    # The thing the administrator chose to settle in the same save.
    settled: bool = False

    def what(self) -> int | None:
        """The number stored with a skipped day: a booking's order number, a
        block's or an event's id, a closure's id; none for the weekly hours."""
        match self.reason:
            case SkipReason.BOOKED:
                return self.number
            case SkipReason.BLOCKED:
                return self.block_id
            case SkipReason.EVENT:
                return self.event_id
            case _:
                return self.closure_ids[0] if self.closure_ids else None


@dataclass(frozen=True)
class Leaving:
    """The part of a container's cells that an event touches but does not
    fill: nobody can book it."""

    container: int
    booking_type: str
    spans: tuple[Span, ...]


@dataclass(frozen=True)
class DayPlan:
    """One day of an event as it would be taken."""

    day: date
    starts_at: datetime
    ends_at: datetime
    in_way: tuple[InWay, ...] = ()
    leaves: tuple[Leaving, ...] = ()

    @property
    def reason(self) -> str | None:
        """Why the day cannot be taken (a repeat skips it), or none."""
        found = {item.reason for item in self.in_way if not item.settled}
        for reason in PRIORITY:
            if reason in found:
                return str(reason)
        return None

    @property
    def cause(self) -> InWay | None:
        reason = self.reason
        return next(
            (i for i in self.in_way if i.reason == reason and not i.settled), None
        )


# --- Clock ----------------------------------------------------------------------


def moment(day: date, minute: int, zone: ZoneInfo) -> datetime:
    """The moment of a local time of day (minutes after midnight, up to 24:00).
    A time that does not exist that day moves to the first moment after the
    gap; a time that comes twice is the first of the two ."""
    wall = datetime.combine(day, time()) + minute * MINUTE
    for _ in range(DAY):
        found = calendar._instants(wall, zone)
        if found:
            return found[0]
        wall += MINUTE
    raise ValueError("No moment follows the time.")


def wall(at: datetime, zone: ZoneInfo) -> datetime:
    """A moment as the local clock reads it, without the zone."""
    return at.astimezone(zone).replace(tzinfo=None)


def hours(day: date, first: int, last: int, zone: ZoneInfo) -> Span | None:
    """The moments of a day's hours, or none when the clock change leaves no
    time between them."""
    start, end = moment(day, first, zone), moment(day, last, zone)
    return (start, end) if start < end else None


def repeat_dates(
    weekdays: int, first: date, end: date | None, low: date, high: date
) -> Iterator[date]:
    """The dates in [low, high] on a chosen weekday (bit 0 is Monday), from
    the first date to the end date, which may be none."""
    day = max(first, low)
    stop = high if end is None else min(end, high)
    while day <= stop:
        if weekdays >> day.weekday() & 1:
            yield day
        day += timedelta(days=1)


def horizon(today: date) -> date:
    return today + HORIZON


def mask(weekdays: Iterable[int]) -> int:
    """Weekdays (0 is Monday) as the stored bits."""
    return sum(1 << day for day in set(weekdays))


def weekdays_of(bits: int) -> tuple[int, ...]:
    return tuple(day for day in range(7) if bits >> day & 1)


# --- The world an event meets -----------------------------------------------------


@dataclass(frozen=True)
class _Claim:
    pk: int
    starts_at: datetime
    ends_at: datetime
    state: str
    asset_id: int


class World:
    """One asset, read once for [low, high). Without an asset nothing is ever
    in an event's way and nothing is taken."""

    def __init__(
        self,
        org: Organization,
        asset: Asset | None,
        low: datetime,
        high: datetime,
        *,
        own: Collection[int] = (),
    ) -> None:
        self.org = org
        self.asset = asset
        self.claims: list[_Claim] = []
        self.containers: list[BookingConfiguration] = []
        self._bookings: dict[int, tuple[int, str]] = {}
        self._blocks: dict[int, int] = {}
        self._days: dict[int, tuple[int, int, str, bool]] = {}
        if asset is None:
            return
        self.zone = asset.zone
        self.line = availability.timeline(org, asset)
        self.weekly = availability.TimeLine(self.zone, self.line.weeks, ())
        self.closures = list(
            AvailabilityClosure.objects.of(org)
            .filter(asset=asset)
            .values_list("pk", "first_date", "last_date")
        )
        found = TakenTime.objects.of(org).filter(
            scope_key=asset.scope_key,
            state__in=HOLDING,
            starts_at__lt=high,
            ends_at__gt=low,
        )
        if not asset.whole_space:
            # What the database rule keeps apart from an exclusive claim on an
            # asset that does not cover its space: that asset, and any asset
            # that does (`capacity_no_double_booking`).
            found = found.filter(Q(asset=asset.pk) | Q(whole_space=True))
        rows = list(
            found.exclude(pk__in=list(own)).values_list(
                "pk", "starts_at", "ends_at", "state", "asset_id"
            )
        )
        self.claims = [_Claim(*row) for row in rows]
        ids = [claim.pk for claim in self.claims]
        self._bookings = {
            taken: (number, reference)
            for taken, number, reference in Booking.objects.of(org)
            .filter(taken_time__in=ids)
            .values_list("taken_time_id", "order__number", "order__reference")
        }
        self._blocks = dict(
            Block.objects.of(org)
            .filter(taken_time__in=ids)
            .values_list("taken_time_id", "pk")
        )
        events = [
            (taken, pk, event, title)
            for taken, pk, event, title in EventDay.objects.of(org)
            .filter(taken_time__in=ids)
            .values_list("taken_time_id", "pk", "event_id", "event__title")
        ]
        repeating = set(
            EventRepeat.objects.of(org)
            .filter(event_id__in=[event for _, _, event, _ in events])
            .values_list("event_id", flat=True)
        )
        self._days = {
            taken: (pk, event, title, event in repeating)
            for taken, pk, event, title in events
        }
        self.containers = list(
            BookingConfiguration.objects.of(org)
            .filter(asset=asset, status=Status.ENABLED)
            .select_related("booking_type")
            .order_by("number")
        )

    # --- What is in the way ---

    def _closed(self, starts: datetime, ends: datetime) -> InWay | None:
        zone = self.zone
        first = starts.astimezone(zone).date()
        last = (ends - timedelta(microseconds=1)).astimezone(zone).date()
        spans = self.line.spans(first - timedelta(days=1), last + timedelta(days=1))
        if calendar._covered(starts, ends, spans):
            return None
        behind = tuple(
            pk
            for pk, begins, finishes in self.closures
            if begins <= last and first <= finishes
        )
        weekly = self.weekly.spans(first - timedelta(days=1), last + timedelta(days=1))
        # The weekly hours close it too when they do not cover it alone.
        hours_too = not calendar._covered(starts, ends, weekly)
        ways = (CHANGE_CLOSURE, REMOVE_CLOSURE) if behind else ()
        if hours_too or not behind:
            ways = (*ways, CHANGE_HOURS)
        return InWay(SkipReason.CLOSED, ways, closure_ids=behind)

    def in_the_way(self, starts: datetime, ends: datetime) -> list[InWay]:
        """What stops an event at these moments: the asset closed, a booking,
        a block, another event, each with the ways out."""
        if self.asset is None:
            return []
        found: list[InWay] = []
        closed = self._closed(starts, ends)
        if closed is not None:
            found.append(closed)
        for claim in sorted(self.claims, key=lambda c: (c.starts_at, c.pk)):
            if not (claim.starts_at < ends and starts < claim.ends_at):
                continue
            if claim.state == BLOCKED:
                found.append(
                    InWay(
                        SkipReason.BLOCKED,
                        (REMOVE_BLOCK,),
                        block_id=self._blocks.get(claim.pk),
                    )
                )
            elif claim.state == EVENT_STATE:
                day_id, event_id, title, repeating = self._days.get(
                    claim.pk, (None, None, "", False)
                )
                found.append(
                    InWay(
                        SkipReason.EVENT,
                        (REMOVE_DAY, OTHER_HOURS) if repeating else (REMOVE_EVENT,),
                        day_id=day_id,
                        event_id=event_id,
                        title=title,
                        repeating=repeating,
                    )
                )
            else:
                number, reference = self._bookings.get(claim.pk, (None, ""))
                found.append(
                    InWay(
                        SkipReason.BOOKED,
                        (CANCEL_BOOKING,),
                        number=number,
                        reference=reference,
                        claim_id=claim.pk,
                    )
                )
        return found

    # --- What it leaves ---

    def leaves(self, starts: datetime, ends: datetime) -> tuple[Leaving, ...]:
        """For each container, the part of the cells the time touches that it
        does not fill and that is open: nobody can book it."""
        if self.asset is None:
            return ()
        first = starts.astimezone(self.zone).date() - timedelta(days=1)
        last = ends.astimezone(self.zone).date() + timedelta(days=1)
        open_spans = self.line.spans(first, last)
        found = []
        for config in self.containers:
            pieces: list[Span] = []
            for begins, finishes in calendar.cells_touching(
                config, starts, ends, self.zone
            ):
                for lo, hi in (
                    (begins, min(starts, finishes)),
                    (max(ends, begins), finishes),
                ):
                    if lo >= hi:
                        continue
                    pieces.extend(
                        (max(lo, a), min(hi, b))
                        for a, b in open_spans
                        if max(lo, a) < min(hi, b)
                    )
            if pieces:
                found.append(
                    Leaving(
                        config.number,
                        config.booking_type.name,
                        tuple(sorted(set(pieces))),
                    )
                )
        return tuple(found)

    def plan(self, day: date, starts: datetime, ends: datetime) -> DayPlan:
        return DayPlan(
            day,
            starts,
            ends,
            tuple(self.in_the_way(starts, ends)),
            self.leaves(starts, ends),
        )
