# SPDX-License-Identifier: AGPL-3.0-only
"""The event on the server : what an administrator
does with events, and what each of those does to time.

Only an active administrator of the organization acts (S2). Every service
that writes owns its transaction (`member_data.organization_transaction`: the
organization's row locked, SERIALIZABLE), so an event and a booking for the
same time end with exactly one of them: a checkout takes the same row first
(`ordering._attempt`), and the database refuses the second claim all the same
(`capacity_no_double_booking`). The preview and the save read the same
`event_plan.World`, so what the preview promises, the save checks again.

* A one-off event is refused when anything holds its time or the asset is
  closed then (S4); it never cancels anything. One exception, in the same
  save: another event's repeat day in the way may be removed or given other
  hours (S5).
* A repeat is never refused for what is in the way: such a day is skipped and
  recorded with its reason (S5, S6). Days are made a year ahead; the daily
  top-up (`top_up`) makes the next ones.
* A removed day stays removed. Changing a repeat's time or asset makes its
  days again, but a removed day stays removed and a day with its own hours
  keeps them.
* Every change writes one audit event; a day's names its date (S12). The
  top-up writes none.
"""

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from randirandi.core import event_plan, ordering
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.event_plan import DayPlan, InWay, World
from randirandi.core.member_data import organization_transaction
from randirandi.core.models import (
    EVENT_STATE,
    Asset,
    ConsumptionMode,
    Event,
    EventCategory,
    EventDay,
    EventNotice,
    EventRepeat,
    EventSkippedDay,
    Member,
    MemberStatus,
    Organization,
    TakenTime,
)
from randirandi.core.models.base import DAY
from randirandi.core.models.bookable import CLOSURE_FIRST_DAY, CLOSURE_LAST_DAY
from randirandi.core.models.events import LONGEST_DESCRIPTION, LONGEST_TITLE

logger = logging.getLogger(__name__)

# The longest a one-off event runs, as a block entry does.
MOST_DAYS = 366


class EventNotFoundError(LookupError):
    """The event or the day is not in this organization."""


class EventRefusedError(Exception):
    """The change is refused and nothing was saved. `code` is a field
    (category, title, description, asset, contact, dates, weekdays, hours,
    settle), "past" (the time is over), a reason (closed, booked, blocked,
    event: the time is not free) or "busy" (a booking or another change kept
    meeting the save). `in_way` says what holds the time, with a way out of
    each."""

    def __init__(self, code: str, in_way: tuple[InWay, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.in_way = in_way


# --- What an administrator asks for ---------------------------------------------


@dataclass(frozen=True)
class OneOff:
    """One time, on the event's clock: wall times without a zone, whole
    minutes. It may run past midnight."""

    starts: datetime
    ends: datetime


@dataclass(frozen=True)
class Repeat:
    """Weekly, on chosen weekdays (0 is Monday), from a first date to an end
    date or none, between two local times of day in minutes (up to 1440)."""

    weekdays: frozenset[int]
    first_date: date
    end_date: date | None
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class EventSpec:
    """Every field of an event. A change carries all of them."""

    category: str
    title: str
    description: str
    contact_id: int
    when: OneOff | Repeat
    asset_id: int | None = None
    show_email: bool = True
    show_phone: bool = False


@dataclass(frozen=True)
class Settle:
    """What to do with another event's repeat day that is in the way: remove
    it, or give it these hours (minutes after local midnight)."""

    start_minute: int | None = None
    end_minute: int | None = None

    @property
    def removes(self) -> bool:
        return self.start_minute is None


REMOVE = Settle()


@dataclass(frozen=True)
class Preview:
    """What a make or a change would do, written nowhere."""

    asset_id: int | None
    time_zone: str
    repeating: bool
    # Every day: the one day of a one-off, or each day a repeat falls on in
    # the days it is made for, with what is in its way and what it leaves.
    days: tuple[DayPlan, ...]
    # A one-off on an asset that is over already; make refuses it as "past".
    over: bool = False

    @property
    def takes(self) -> tuple[DayPlan, ...]:
        """The days the event takes: with an asset, the whole asset."""
        if self.asset_id is None:
            return ()
        return tuple(day for day in self.days if day.reason is None)

    @property
    def skipped(self) -> tuple[DayPlan, ...]:
        """A repeat's days it would skip."""
        return tuple(day for day in self.days if day.reason is not None)

    @property
    def refusal(self) -> str | None:
        """Why a one-off could not be saved as it stands; a repeat is never
        refused."""
        if self.repeating or not self.days:
            return None
        if self.over:
            return "past"
        return self.days[0].reason

    def skipped_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for day in self.skipped:
            counts[str(day.reason)] = counts.get(str(day.reason), 0) + 1
        return counts


# --- Checking what was asked ------------------------------------------------------


@dataclass(frozen=True)
class _Clean:
    title: str
    description: str
    asset: Asset | None
    contact: Member
    zone: ZoneInfo


def _in_range(day: date) -> bool:
    return CLOSURE_FIRST_DAY <= day <= CLOSURE_LAST_DAY


def _whole_minute(at: datetime) -> bool:
    return at.tzinfo is None and at.second == 0 and at.microsecond == 0


def _check(org: Organization, spec: EventSpec) -> _Clean:
    """The spec, checked before any write; EventRefusedError names the field."""
    if spec.category not in EventCategory.values:
        raise EventRefusedError("category")
    title = spec.title.strip() if isinstance(spec.title, str) else ""
    if not title or len(title) > LONGEST_TITLE:
        raise EventRefusedError("title")
    description = (
        spec.description.strip() if isinstance(spec.description, str) else None
    )
    if description is None or len(description) > LONGEST_DESCRIPTION:
        raise EventRefusedError("description")
    asset = None
    if spec.asset_id is not None:
        asset = (
            Asset.objects.of(org).filter(pk=spec.asset_id).first()
            if type(spec.asset_id) is int
            else None
        )
        if asset is None:
            raise EventRefusedError("asset")
    contact = (
        Member.objects.of(org)
        .filter(pk=spec.contact_id, status=MemberStatus.ACTIVE)
        .first()
        if type(spec.contact_id) is int
        else None
    )
    if contact is None:
        raise EventRefusedError("contact")
    zone = asset.zone if asset is not None else ZoneInfo(org.time_zone)
    if isinstance(spec.when, OneOff):
        _check_one_off(spec.when)
    else:
        _check_repeat(spec.when)
    return _Clean(title, description, asset, contact, zone)


def _check_one_off(when: OneOff) -> None:
    if not (_whole_minute(when.starts) and _whole_minute(when.ends)):
        raise EventRefusedError("dates")
    if not (_in_range(when.starts.date()) and _in_range(when.ends.date())):
        raise EventRefusedError("dates")
    if when.ends <= when.starts or when.ends - when.starts > timedelta(days=MOST_DAYS):
        raise EventRefusedError("dates")


def _check_repeat(when: Repeat) -> None:
    if not when.weekdays or not all(
        type(day) is int and 0 <= day <= 6 for day in when.weekdays
    ):
        raise EventRefusedError("weekdays")
    if not _in_range(when.first_date) or (
        when.end_date is not None
        and (not _in_range(when.end_date) or when.end_date < when.first_date)
    ):
        raise EventRefusedError("dates")
    if not (
        type(when.start_minute) is int
        and type(when.end_minute) is int
        and 0 <= when.start_minute < when.end_minute <= DAY
    ):
        raise EventRefusedError("hours")


def _one_off_span(when: OneOff, zone: ZoneInfo) -> event_plan.Span:
    start = event_plan.moment(
        when.starts.date(), when.starts.hour * 60 + when.starts.minute, zone
    )
    end = event_plan.moment(
        when.ends.date(), when.ends.hour * 60 + when.ends.minute, zone
    )
    if start >= end:
        raise EventRefusedError("dates")
    return start, end


@dataclass(frozen=True)
class _Rule:
    """A repeat's rule as the days are worked out from it."""

    weekdays: int
    first: date
    end: date | None
    start_minute: int
    end_minute: int

    @classmethod
    def of(cls, when: Repeat) -> _Rule:
        return cls(
            event_plan.mask(when.weekdays),
            when.first_date,
            when.end_date,
            when.start_minute,
            when.end_minute,
        )

    @classmethod
    def stored(cls, row: EventRepeat) -> _Rule:
        return cls(
            row.weekdays, row.first_date, row.end_date, row.start_minute, row.end_minute
        )


@dataclass
class _Wanted:
    day: date
    starts_at: datetime
    ends_at: datetime
    own: tuple[int, int] | None
    old: EventDay | None


def _wanted(
    rule: _Rule, old: Mapping[date, EventDay], zone: ZoneInfo, today: date
) -> list[_Wanted]:
    """The days a repeat is made for, from today to the horizon: not a day
    removed, a day with its own hours at those hours."""
    found = []
    for day in event_plan.repeat_dates(
        rule.weekdays, rule.first, rule.end, today, event_plan.horizon(today)
    ):
        before = old.get(day)
        if before is not None and before.removed_at is not None:
            continue
        own = None
        if before is not None and before.own_start is not None:
            assert before.own_end is not None  # noqa: S101 — the table's rule
            own = (before.own_start, before.own_end)
        first, last = own or (rule.start_minute, rule.end_minute)
        span = event_plan.hours(day, first, last, zone)
        if span is not None:
            found.append(_Wanted(day, span[0], span[1], own, before))
    return found


def _world(
    org: Organization,
    asset: Asset | None,
    spans: list[event_plan.Span],
    own: list[int],
) -> World:
    low = min((a for a, _ in spans), default=timezone.now())
    high = max((b for _, b in spans), default=low + timedelta(minutes=1))
    return World(org, asset, low, high, own=own)


# --- Preview ----------------------------------------------------------------------


def _staff(org: Organization, administrator: Member) -> Member:
    current = (
        Member.objects.of(org)
        .select_related("role")
        .filter(pk=administrator.pk)
        .first()
    )
    if current is None or not current.is_staff:
        raise PermissionDenied("An active administrator is required.")
    return current


def _event(org: Organization, event_id: int) -> Event:
    event = (
        Event.objects.of(org)
        .select_related("asset", "contact")
        .filter(pk=event_id)
        .first()
    )
    if event is None:
        raise EventNotFoundError
    return event


def preview(
    administrator: Member,
    spec: EventSpec,
    *,
    event_id: int | None = None,
    settle: Mapping[int, Settle] | None = None,
    now: datetime | None = None,
) -> Preview:
    """What the event would take, the part of a cell it leaves that nobody
    can book, and what is in its way with a way out of each; for a repeat,
    the days it takes and the days it skips with their reasons. Given an
    event's id, the preview is of a change: the event's own time is not in its
    own way. `settle` says what to do with other events' repeat days in the
    way; those are shown as settled. Written nowhere."""
    org = Organization.objects.get(pk=administrator.organization_id)
    _staff(org, administrator)
    moment = now or timezone.now()
    clean = _check(org, spec)
    old = _event(org, event_id) if event_id is not None else None
    held = (
        list(EventDay.objects.of(org).filter(event=old).select_related("taken_time"))
        if old is not None
        else []
    )
    own = [day.taken_time_id for day in held if day.taken_time_id is not None]
    asset = clean.asset
    if isinstance(spec.when, OneOff):
        start, end = _one_off_span(spec.when, clean.zone)
        world = _world(org, asset, [(start, end)], own)
        plan = world.plan(spec.when.starts.date(), start, end)
        plan = _settled(plan, settle or {}, strict=False)
        over = asset is not None and end <= moment
        return Preview(
            asset.pk if asset else None, str(clean.zone), False, (plan,), over
        )
    rule = _Rule.of(spec.when)
    today = moment.astimezone(clean.zone).date()
    wanted = _wanted(rule, {day.day: day for day in held}, clean.zone, today)
    world = _world(org, asset, [(w.starts_at, w.ends_at) for w in wanted], own)
    plans = tuple(world.plan(w.day, w.starts_at, w.ends_at) for w in wanted)
    return Preview(asset.pk if asset else None, str(clean.zone), True, plans)


def _settled(plan: DayPlan, settle: Mapping[int, Settle], *, strict: bool) -> DayPlan:
    """The plan with the other events' repeat days named in `settle` marked
    settled. With `strict`, a name that is not in the way is refused."""
    used: set[int] = set()
    marked = []
    for item in plan.in_way:
        if (
            item.reason == "event"
            and item.repeating
            and item.day_id is not None
            and item.day_id in settle
        ):
            used.add(item.day_id)
            marked.append(replace(item, settled=True))
        else:
            marked.append(item)
    if strict and set(settle) - used:
        raise EventRefusedError("settle")
    return DayPlan(plan.day, plan.starts_at, plan.ends_at, tuple(marked), plan.leaves)


# --- The transaction every write owns ------------------------------------------------


def _run[T](administrator: Member, work: Callable[[Organization, Member], T]) -> T:
    """Run `work` in its own transaction as an active administrator of the
    organization. A booking that lands between the check and the write is met
    by a fresh attempt, as an order's is (`ordering.create_order`)."""

    def inside(org: Organization) -> T:
        current = _staff(org, administrator)
        # A checkout reads the claims after taking this row; updating it makes
        # one whose snapshot is older fail to serialise and read again
        # (`availability_admin.apply`).
        Organization.objects.filter(pk=org.pk).update(
            last_order_number=F("last_order_number")
        )
        return work(org, current)

    for attempt in range(ordering.MAX_ATTEMPTS):
        try:
            return organization_transaction(administrator.organization_id, inside)
        except IntegrityError as error:
            if not ordering._retryable(error):
                raise
            if attempt == ordering.MAX_ATTEMPTS - 1:
                raise EventRefusedError("busy") from None
    raise AssertionError("Unreachable: each attempt returns, retries or raises.")


# --- Time on the asset ------------------------------------------------------------


def _free(org: Organization, day: EventDay) -> None:
    """Free the time a day holds."""
    if day.taken_time_id is not None:
        TakenTime.objects.of(org).filter(pk=day.taken_time_id).delete()
        day.taken_time = None


def _take(
    org: Organization, asset: Asset | None, starts: datetime, ends: datetime
) -> TakenTime | None:
    """The whole asset, on every container, as one claim in the state event."""
    if asset is None:
        return None
    return TakenTime.objects.take(
        organization=org,
        asset=asset,
        starts_at=starts,
        ends_at=ends,
        state=EVENT_STATE,
        mode=ConsumptionMode.EXCLUSIVE,
        quantity=1,
    )


def _new_day(
    org: Organization,
    event: Event,
    asset: Asset | None,
    wanted: _Wanted,
) -> EventDay:
    taken = _take(org, asset, wanted.starts_at, wanted.ends_at)
    return EventDay.objects.create(
        organization=org,
        event=event,
        day=wanted.day,
        starts_at=wanted.starts_at,
        ends_at=wanted.ends_at,
        own_start=wanted.own[0] if wanted.own else None,
        own_end=wanted.own[1] if wanted.own else None,
        taken_time=taken,
    )


def _held_by(skip: EventSkippedDay, way: InWay) -> bool:
    return skip.reason == way.reason and skip.in_the_way == way.what()


def notice_recipients(org: Organization, event: Event) -> list[Member]:
    """Who is told of a skipped day now: the event's maker if still an active
    administrator, else every active administrator (S6)."""
    admins = Member.objects.of(org).filter(
        status=MemberStatus.ACTIVE, role__admin_access=True
    )
    maker = admins.filter(pk=event.made_by_id)
    return list(maker if maker.exists() else admins)


def _notify(org: Organization, event: Event, skipped: EventSkippedDay) -> None:
    """Make the notices for a skipped day (S6)."""
    for recipient in notice_recipients(org, event):
        EventNotice.objects.of(org).get_or_create(
            organization=org, skipped_day=skipped, recipient=recipient
        )


def _record_skip(
    org: Organization,
    event: Event,
    plan: DayPlan,
    kept: dict[date, EventSkippedDay],
    *,
    tell: bool,
    now: datetime,
) -> None:
    """Record that a day was skipped. A day skipped before for the same reason
    keeps its record, and with it the record of its emails."""
    cause = plan.cause
    assert cause is not None  # noqa: S101 — only a skipped day is recorded
    reason, what = str(plan.reason), cause.what()
    before = kept.pop(plan.day, None)
    if before is not None:
        if before.reason == reason:
            if before.in_the_way != what:
                before.in_the_way = what
                before.save(update_fields=["in_the_way"])
            return
        before.delete()
    row = EventSkippedDay.objects.create(
        organization=org,
        event=event,
        day=plan.day,
        reason=reason,
        in_the_way=what,
        created_at=now,
    )
    if tell:
        _notify(org, event, row)


def _remake(
    org: Organization,
    event: Event,
    rule: _Rule,
    asset: Asset | None,
    zone: ZoneInfo,
    now: datetime,
    *,
    before_asset: int | None,
) -> None:
    """Work a repeat's days out again, from today to the horizon: each day
    that fits is made (or kept, or moved), each that does not is skipped and
    recorded. A removed day stays removed; a day with its own hours keeps
    them. Days before today are history and stay."""
    today = now.astimezone(zone).date()
    old = {day.day: day for day in EventDay.objects.of(org).filter(event=event)}
    wanted = _wanted(rule, old, zone, today)
    own = [day.taken_time_id for day in old.values() if day.taken_time_id is not None]
    world = _world(org, asset, [(w.starts_at, w.ends_at) for w in wanted], own)
    plans = {w.day: world.plan(w.day, w.starts_at, w.ends_at) for w in wanted}
    same_asset = before_asset == (asset.pk if asset is not None else None)
    keeps = {
        w.day
        for w in wanted
        if plans[w.day].reason is None
        and w.old is not None
        and same_asset
        and (w.old.starts_at, w.old.ends_at) == (w.starts_at, w.ends_at)
        and (w.old.taken_time_id is not None) == (asset is not None)
    }
    # Free the time before taking it again: the database keeps claims apart.
    reached = {w.day for w in wanted}
    for day in old.values():
        if day.removed_at is not None or day.day < today:
            continue
        if day.day not in reached:
            _free(org, day)
            day.delete()
        elif plans[day.day].reason:
            _free(org, day)
            if day.own_start is None:
                day.delete()
            else:
                # Its other hours are kept without a claim, for the day the
                # obstacle clears; the skip is recorded below.
                day.taken_time = None
                day.save(update_fields=["taken_time"])
        elif day.day not in keeps:
            _free(org, day)
    for item in wanted:
        if plans[item.day].reason is not None or item.day in keeps:
            continue
        if item.old is None:
            _new_day(org, event, asset, item)
        else:
            item.old.starts_at, item.old.ends_at = item.starts_at, item.ends_at
            item.old.taken_time = _take(org, asset, item.starts_at, item.ends_at)
            item.old.save()
    kept = {row.day: row for row in EventSkippedDay.objects.of(org).filter(event=event)}
    for item in wanted:
        if plans[item.day].reason is not None:
            _record_skip(org, event, plans[item.day], kept, tell=False, now=now)
        else:
            gone = kept.pop(item.day, None)
            if gone is not None:
                gone.delete()
    for skipped_on, row in kept.items():
        # A skipped day the rule no longer reaches goes; history stays.
        if skipped_on >= today:
            row.delete()
    EventRepeat.objects.of(org).filter(event=event).update(
        made_through=event_plan.horizon(today)
    )


# --- Making and changing -----------------------------------------------------------


def _settle_days(
    org: Organization,
    administrator: Member,
    plan: DayPlan,
    settle: Mapping[int, Settle],
    now: datetime,
) -> list[tuple[EventDay, Settle]]:
    """Refuse a one-off for what is in its way, unless it is another event's
    repeat day that `settle` deals with; remove those that are to go and free
    the time of those that get other hours (given afterwards)."""
    marked = _settled(plan, settle, strict=True)
    reason = marked.reason
    if reason is not None:
        raise EventRefusedError(reason, marked.in_way)
    later = []
    for item in marked.in_way:
        if not item.settled:
            continue
        assert item.day_id is not None  # noqa: S101 — settled days have one
        how = settle[item.day_id]
        day = EventDay.objects.of(org).select_related("event").get(pk=item.day_id)
        if how.removes:
            _remove_day(org, administrator, day, now)
        else:
            _free(org, day)
            later.append((day, how))
    return later


def _remove_day(
    org: Organization, administrator: Member, day: EventDay, now: datetime
) -> None:
    record_event(actor=administrator, action=AuditAction.REMOVED_EVENT_DAY, target=day)
    _free(org, day)
    day.removed_at = now
    day.save()


def _give_hours(
    org: Organization,
    administrator: Member,
    day: EventDay,
    first: int,
    last: int,
    now: datetime,
) -> None:
    """Give a repeat's day other hours: checked like any time, then taken."""
    event = day.event
    asset = event.asset if event.asset_id is not None else None
    zone = asset.zone if asset is not None else ZoneInfo(org.time_zone)
    if not 0 <= first < last <= DAY:
        raise EventRefusedError("hours")
    span = event_plan.hours(day.day, first, last, zone)
    if span is None:
        raise EventRefusedError("hours")
    if span[1] <= now:
        raise EventRefusedError("past")
    own = [day.taken_time_id] if day.taken_time_id is not None else []
    plan = _world(org, asset, [span], own).plan(day.day, *span)
    if plan.reason is not None:
        raise EventRefusedError(plan.reason, plan.in_way)
    _free(org, day)
    day.starts_at, day.ends_at = span
    rule = event.repeat
    day.own_start, day.own_end = (
        (None, None)
        if (first, last) == (rule.start_minute, rule.end_minute)
        else (first, last)
    )
    day.taken_time = _take(org, asset, *span)
    day.save()
    record_event(actor=administrator, action=AuditAction.CHANGED_EVENT_DAY, target=day)


def _one_off(
    org: Organization,
    administrator: Member,
    event: Event,
    clean: _Clean,
    when: OneOff,
    settle: Mapping[int, Settle],
    now: datetime,
    *,
    held: EventDay | None,
) -> None:
    """Save a one-off's only day: refused for what is in its way, but another
    event's repeat day may be settled in the same save."""
    start, end = _one_off_span(when, clean.zone)
    asset = clean.asset
    if asset is not None and end <= now:
        raise EventRefusedError("past")
    own = [held.taken_time_id] if held and held.taken_time_id is not None else []
    plan = _world(org, asset, [(start, end)], own).plan(when.starts.date(), start, end)
    if asset is None and settle:
        raise EventRefusedError("settle")
    later = _settle_days(org, administrator, plan, settle, now)
    if held is not None:
        _free(org, held)
        held.day, held.starts_at, held.ends_at = when.starts.date(), start, end
        held.taken_time = _take(org, asset, start, end)
        held.save()
    else:
        _new_day(org, event, asset, _Wanted(when.starts.date(), start, end, None, None))
    for day, how in later:
        assert how.start_minute is not None and how.end_minute is not None  # noqa: S101
        _give_hours(org, administrator, day, how.start_minute, how.end_minute, now)


def _schedule(
    org: Organization,
    administrator: Member,
    event: Event,
    clean: _Clean,
    when: OneOff | Repeat,
    settle: Mapping[int, Settle],
    now: datetime,
    *,
    before_asset: int | None,
) -> None:
    """Make the event's days for a new event or a changed time or asset."""
    stored = EventRepeat.objects.of(org).filter(event=event).first()
    if isinstance(when, OneOff):
        if stored is not None:
            for day in EventDay.objects.of(org).filter(event=event):
                _free(org, day)
            EventSkippedDay.objects.of(org).filter(event=event).delete()
            EventDay.objects.of(org).filter(event=event).delete()
            stored.delete()
        held = EventDay.objects.of(org).filter(event=event).first()
        _one_off(org, administrator, event, clean, when, settle, now, held=held)
        return
    if settle:
        raise EventRefusedError("settle")
    rule = _Rule.of(when)
    if stored is None:
        for day in EventDay.objects.of(org).filter(event=event):
            _free(org, day)
        EventDay.objects.of(org).filter(event=event).delete()
        EventRepeat.objects.create(
            organization=org,
            event=event,
            weekdays=rule.weekdays,
            first_date=rule.first,
            end_date=rule.end,
            start_minute=rule.start_minute,
            end_minute=rule.end_minute,
        )
        before_asset = None
    else:
        stored.weekdays, stored.first_date, stored.end_date = (
            rule.weekdays,
            rule.first,
            rule.end,
        )
        stored.start_minute, stored.end_minute = rule.start_minute, rule.end_minute
        stored.save()
    _remake(org, event, rule, clean.asset, clean.zone, now, before_asset=before_asset)


def _fields(event: Event, spec: EventSpec, clean: _Clean) -> None:
    event.category = spec.category
    event.title = clean.title
    event.description = clean.description
    event.asset = clean.asset
    event.contact = clean.contact
    event.show_email = bool(spec.show_email)
    event.show_phone = bool(spec.show_phone)


def make_event(
    administrator: Member,
    spec: EventSpec,
    *,
    settle: Mapping[int, Settle] | None = None,
    now: datetime | None = None,
) -> Event:
    """Make the event and its days, or nothing: one transaction, one audit
    event. A one-off is refused for what is in its way (EventRefusedError,
    which names it); a repeat skips such days and records them."""
    moment = now or timezone.now()

    def work(org: Organization, admin: Member) -> Event:
        return make_inside(org, admin, spec, settle or {}, moment)

    return _run(administrator, work)


def make_inside(
    org: Organization,
    admin: Member,
    spec: EventSpec,
    settle: Mapping[int, Settle],
    moment: datetime,
) -> Event:
    """What `make_event` does inside its transaction, for a caller that holds
    the organization's row and has checked the administrator (`_staff`):
    `old_booking_import.check` makes events in a transaction it rolls back."""
    clean = _check(org, spec)
    event = Event(organization=org, made_by=admin, created_at=moment)
    _fields(event, spec, clean)
    event.save()
    _schedule(org, admin, event, clean, spec.when, settle, moment, before_asset=None)
    record_event(actor=admin, action=AuditAction.MADE_EVENT, target=event)
    return event


def change_event(
    administrator: Member,
    event_id: int,
    spec: EventSpec,
    *,
    settle: Mapping[int, Settle] | None = None,
    now: datetime | None = None,
) -> Event:
    """Change an event's details, time, asset or contact: every field is
    given. A changed time or asset is checked again as when the event was
    made, in the same transaction; the event's own time is not in its own
    way. One audit event."""
    moment = now or timezone.now()

    def work(org: Organization, admin: Member) -> Event:
        event = _event(org, event_id)
        clean = _check(org, spec)
        before_asset = event.asset_id
        before_zone = (
            event.asset.zone if event.asset is not None else ZoneInfo(org.time_zone)
        )
        timed = before_asset != (clean.asset.pk if clean.asset else None) or (
            _when_changed(org, event, spec.when, before_zone)
        )
        _fields(event, spec, clean)
        event.save()
        if timed:
            _schedule(
                org,
                admin,
                event,
                clean,
                spec.when,
                settle or {},
                moment,
                before_asset=before_asset,
            )
        elif settle:
            raise EventRefusedError("settle")
        record_event(actor=admin, action=AuditAction.CHANGED_EVENT, target=event)
        return event

    return _run(administrator, work)


def _when_changed(
    org: Organization, event: Event, when: OneOff | Repeat, zone: ZoneInfo
) -> bool:
    """Whether the time asked for differs from the one stored."""
    stored = EventRepeat.objects.of(org).filter(event=event).first()
    if isinstance(when, Repeat):
        return stored is None or _Rule.stored(stored) != _Rule.of(when)
    if stored is not None:
        return True
    day = EventDay.objects.of(org).filter(event=event).first()
    return (
        day is None
        or event_plan.wall(day.starts_at, zone) != when.starts
        or event_plan.wall(day.ends_at, zone) != when.ends
    )


def remove_event(administrator: Member, event_id: int) -> None:
    """Remove an event and free its time: its days, its skipped days and its
    notices go with it. The audit event is written before the delete."""

    def work(org: Organization, admin: Member) -> None:
        event = _event(org, event_id)
        record_event(actor=admin, action=AuditAction.REMOVED_EVENT, target=event)
        for day in EventDay.objects.of(org).filter(event=event):
            _free(org, day)
        event.delete()

    _run(administrator, work)


def _day(org: Organization, day_id: int) -> EventDay:
    day = (
        EventDay.objects.of(org)
        .select_related("event", "event__asset", "event__repeat")
        .filter(pk=day_id)
        .first()
    )
    if day is None:
        raise EventNotFoundError
    return day


def remove_day(
    administrator: Member, day_id: int, *, now: datetime | None = None
) -> None:
    """Remove one day of a repeat and free its time. The day stays removed:
    the repeat never makes it again. A one-off's only day is the event: remove
    the event instead (code "one-off")."""
    moment = now or timezone.now()

    def work(org: Organization, admin: Member) -> None:
        day = _day(org, day_id)
        if not hasattr(day.event, "repeat"):
            raise EventRefusedError("one-off")
        if day.removed_at is not None:
            raise EventRefusedError("removed")
        if day.ends_at <= moment:
            raise EventRefusedError("past")
        _remove_day(org, admin, day, moment)

    _run(administrator, work)


def remove_days_in_transaction(
    org: Organization,
    administrator: Member,
    asset: Asset,
    day_ids: Collection[int],
    now: datetime,
) -> None:
    """Remove repeat days of events on `asset` by id, inside the caller's
    transaction (a closure or the public holidays removing the days in their
    way in the same save). Each day is audited as any removed day is. A day
    that is not in this organization is EventNotFoundError; a one-off's day,
    a day on another asset, one removed already or one that is over is
    EventRefusedError ("one-off", "asset", "removed", "past")."""
    for pk in sorted(set(day_ids)):
        day = _day(org, pk)
        if not hasattr(day.event, "repeat"):
            raise EventRefusedError("one-off")
        if day.event.asset_id != asset.pk:
            raise EventRefusedError("asset")
        if day.removed_at is not None:
            raise EventRefusedError("removed")
        if day.ends_at <= now:
            raise EventRefusedError("past")
        _remove_day(org, administrator, day, now)


def give_day_other_hours(
    administrator: Member,
    day_id: int,
    start_minute: int,
    end_minute: int,
    *,
    now: datetime | None = None,
) -> None:
    """Give one day of a repeat hours of its own (minutes after the local
    midnight, up to 1440), checked like any time. A change of the repeat's
    time later leaves them."""
    moment = now or timezone.now()

    def work(org: Organization, admin: Member) -> None:
        day = _day(org, day_id)
        if not hasattr(day.event, "repeat"):
            raise EventRefusedError("one-off")
        if day.removed_at is not None:
            raise EventRefusedError("removed")
        if day.ends_at <= moment:
            raise EventRefusedError("past")
        if type(start_minute) is not int or type(end_minute) is not int:
            raise EventRefusedError("hours")
        _give_hours(org, admin, day, start_minute, end_minute, moment)

    _run(administrator, work)


# --- The daily top-up -----------------------------------------------------------


@dataclass
class TopUp:
    """What one run made: days taken and days skipped."""

    made: int = 0
    skipped: int = 0


def _top_up_event(
    org: Organization, event_id: int, now: datetime, result: TopUp
) -> None:
    """Make the next days of one repeat, from the day after the last it
    worked out up to the horizon. Idempotent: a day with a row (made or
    removed) or a skip record is left; a run again finds nothing to do. It
    writes no audit event (S12); a day it skips is recorded and told."""
    repeat = (
        EventRepeat.objects.of(org)
        .select_related("event", "event__asset")
        .filter(event_id=event_id)
        .first()
    )
    if repeat is None:
        return
    event = repeat.event
    asset = event.asset
    zone = asset.zone if asset is not None else ZoneInfo(org.time_zone)
    today = now.astimezone(zone).date()
    high = event_plan.horizon(today)
    low = today
    if repeat.made_through is not None:
        low = max(today, repeat.made_through + timedelta(days=1))
    if low > high:
        return
    rule = _Rule.stored(repeat)
    done = set(
        EventDay.objects.of(org).filter(event=event).values_list("day", flat=True)
    )
    done |= set(
        EventSkippedDay.objects.of(org)
        .filter(event=event)
        .values_list("day", flat=True)
    )
    spans = []
    for day in event_plan.repeat_dates(rule.weekdays, rule.first, rule.end, low, high):
        if day in done:
            continue
        span = event_plan.hours(day, rule.start_minute, rule.end_minute, zone)
        if span is not None:
            spans.append((day, span))
    world = _world(org, asset, [span for _, span in spans], [])
    for day, span in spans:
        plan = world.plan(day, *span)
        if plan.reason is None:
            _new_day(org, event, asset, _Wanted(day, *span, None, None))
            result.made += 1
        else:
            _record_skip(org, event, plan, {}, tell=True, now=now)
            result.skipped += 1
    repeat.made_through = high
    repeat.save(update_fields=["made_through"])


def top_up(now: datetime | None = None) -> TopUp:
    """The daily job: make the next days of every organization's repeating
    events, a year ahead. One repeat at a time, each in its own transaction, so
    a fault in one holds up no other (the next run tries it again)."""
    moment = now or timezone.now()
    result = TopUp()
    for org in Organization.objects.order_by("pk"):
        ids = list(
            EventRepeat.objects.of(org)
            .order_by("pk")
            .values_list("event_id", flat=True)
        )
        for event_id in ids:
            try:
                organization_transaction(
                    org.pk,
                    partial(
                        _top_up_event, event_id=event_id, now=moment, result=result
                    ),
                )
            except Exception:
                logger.exception(
                    "event days failed: organization-%s event-%s", org.pk, event_id
                )
    return result


# --- Contact ------------------------------------------------------------------------


@dataclass(frozen=True)
class Contact:
    """What members see of an event's contact person: the name always, the
    email and the phone as the event chose."""

    member_id: int
    name: str
    email: str | None
    phone: str | None


def contact_of(event: Event) -> Contact | None:
    """The contact members see, read from the member now (never copied onto
    the event), or none when the event has none or the member is no longer
    active (S13)."""
    member = event.contact
    if member is None or member.status != MemberStatus.ACTIVE:
        return None
    name = member.full_name or f"{member.first_name} {member.last_name}".strip()
    return Contact(
        member.pk,
        name,
        member.email if event.show_email and member.email else None,
        member.phone if event.show_phone and member.phone else None,
    )


def needing_a_contact(org: Organization) -> QuerySet[Event]:
    """The events admin marks as needing a contact person: none, or one that
    is no longer active (erased, blocked or disabled)."""
    return Event.objects.of(org).filter(
        Q(contact__isnull=True) | ~Q(contact__status=MemberStatus.ACTIVE)
    )
