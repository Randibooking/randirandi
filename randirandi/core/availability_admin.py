# SPDX-License-Identifier: AGPL-3.0-only
"""An asset's availability, as an administrator sets it : weekly
availabilities from a date, closures by date and public holidays picked as
closures.

Availability is god mode (W10, W11): a change to it is never refused for the
bookings it strands. Every booking in the time a change removes is cancelled and
refunded, a booking only partly in that time whole, after a preview of how many
bookings, how many members and how much money.

How a change is made, and why the preview and the save agree:

* Every change runs in `organization_transaction`: the organization's row is
  locked. A checkout locks the same row before it reads a claim or the
  availability (`ordering._attempt`), so a booking is either made before the
  change reads the bookings, and is among those it finds, or after it commits,
  and then the availability it reads is the new one.
* The change is made on the rows, the bookings it strands are found by comparing
  the time line before and after (`availability.loses`), and then a preview rolls
  everything back. A save carries the preview's `expect` token, a digest of the
  bookings it found; when the bookings now differ (one made, cancelled or ended in
  between) nothing is saved and the answer is the fresh preview, which is the
  second preview.
* The bookings the change strands are cancelled in the same transaction
  (`cancel_stranded`), from the saved records, with no provider call: once the
  change commits every one of them is cancelled, whatever the provider would
  answer, and a crash after the commit loses nothing. Each order is cancelled with
  `payment_adjustments.cancel_in_transaction`: one audit event for the change and
  one for each cancelled booking, the cancellation mail due as the cancel sends
  it. Paid money becomes a queued `Refund` (the existing refund queue): the worker
  sends it again and again until the provider takes it (`payment_refunds.execute`,
  `payment_reconciliation.reconcile_requested`), and the availability page lists
  what is still owed (`owed`). An order still being paid is cancelled whole and
  its payment given the cancel intent, which the worker carries out.
"""

import hashlib
import re
import uuid
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import F, QuerySet, Sum
from django.utils import timezone

from randirandi.core import (
    availability,
    event_admin,
    event_plan,
    payment_adjustments,
    public_holidays,
)
from randirandi.core.asset_admin import LayoutRefusedError, _checked, _lock, _saved
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.booking_admin import _currencies, _totals
from randirandi.core.models import (
    Asset,
    AuditEvent,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    Booking,
    BookingState,
    ClosureReason,
    EventDay,
    Member,
    Order,
    Organization,
    Refund,
)
from randirandi.core.models.base import DAY
from randirandi.core.models.bookable import CLOSURE_FIRST_DAY, CLOSURE_LAST_DAY
from randirandi.core.setup_admin import organization_transaction

type Fields = Mapping[str, object]

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MOST_SPANS_A_DAY = 24
MOST_HOLIDAYS = 20
# The event days one closure or holidays save may remove with it.
MOST_EVENT_DAYS = 400
# The cancelled bookings a preview lists; the counts are always whole.
LISTED = 100
ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
KINDS = (
    "week-make",
    "week-change",
    "week-remove",
    "closure-make",
    "closure-change",
    "closure-remove",
    "holidays",
)
# Why a cancel ends with nothing more to do: the time is over or it is cancelled.
SETTLED = {"past", "already-cancelled", "not-cancellable"}


class PreviewedError(Exception):
    """The change was made in memory of the transaction and rolled back."""

    def __init__(self, preview: dict[str, object]) -> None:
        super().__init__("preview")
        self.preview = preview


class ChangedSincePreviewError(LayoutRefusedError):
    """The bookings the change strands are not those the preview showed."""

    def __init__(self, preview: dict[str, object]) -> None:
        super().__init__("changed")
        self.preview = preview


# --- The body a change may carry ---------------------------------------------------


def _day(value: object) -> date:
    if not isinstance(value, str) or not ISO_DAY.fullmatch(value):
        raise ValueError("A date is YYYY-MM-DD.")
    day = date.fromisoformat(value)
    if not CLOSURE_FIRST_DAY <= day <= CLOSURE_LAST_DAY:
        raise ValueError("The date is outside 2000 to 2100.")
    return day


def _whole(value: object) -> int:
    if type(value) is not int:
        raise ValueError("A whole number.")
    return value


def _id(value: object) -> int:
    pk = _whole(value)
    if not 1 <= pk <= 2**31:
        raise ValueError("An id.")
    return pk


def _event_days(value: object) -> tuple[int, ...]:
    """Ids of event days to remove in the same save; left out is none."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MOST_EVENT_DAYS:
        raise ValueError("Event days are a list of ids.")
    return tuple(sorted({_id(item) for item in value}))


def _week(value: object) -> tuple[availability.Day, ...]:
    """Seven days, Monday first, each a list of [start, end] in minutes; the
    spans of a day that meet are joined."""
    if not isinstance(value, list) or len(value) != 7:
        raise ValueError("A week is seven days.")
    days = []
    for day in value:
        if not isinstance(day, list) or len(day) > MOST_SPANS_A_DAY:
            raise ValueError("A day is a list of spans.")
        spans = []
        for span in day:
            if not isinstance(span, list) or len(span) != 2:
                raise ValueError("A span is a start and an end.")
            first, last = _whole(span[0]), _whole(span[1])
            if not 0 <= first < last <= DAY:
                raise LayoutRefusedError("invalid-span")
            spans.append((first, last))
        days.append(availability.merge_minutes(spans))
    return tuple(days)


def _reason(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or value not in ClosureReason.values:
        raise LayoutRefusedError("invalid-reason")
    return value


@dataclass(frozen=True)
class Change:
    kind: str
    pk: int = 0
    start_date: date | None = None
    week: tuple[availability.Day, ...] = ()
    first_date: date | None = None
    last_date: date | None = None
    reason: str = ""
    year: int = 0
    dates: tuple[date, ...] = ()
    # Event days to remove in the same save : a closure or the
    # public holidays over them.
    remove_days: tuple[int, ...] = ()


def parse(fields: Fields) -> Change:
    """The change a body asks for, or ValueError (400), or a refusal."""
    kind = fields.get("kind")
    if kind not in KINDS or not isinstance(kind, str):
        raise ValueError("Unknown change.")
    allowed = {
        "week-make": {"start_date", "week"},
        "week-change": {"id", "start_date", "week"},
        "week-remove": {"id"},
        "closure-make": {"first_date", "last_date", "reason", "remove_event_days"},
        "closure-change": {
            "id",
            "first_date",
            "last_date",
            "reason",
            "remove_event_days",
        },
        "closure-remove": {"id"},
        "holidays": {"year", "dates", "remove_event_days"},
    }[kind]
    extra = set(fields) - allowed - {"kind", "expect", "preview"}
    if extra or allowed - {"reason", "remove_event_days"} - set(fields):
        raise ValueError("The body is wrong.")
    removing = _event_days(fields.get("remove_event_days"))
    if kind == "holidays":
        dates = fields["dates"]
        if not isinstance(dates, list) or not 1 <= len(dates) <= MOST_HOLIDAYS:
            raise ValueError("Dates are a list.")
        return Change(
            kind,
            year=_whole(fields["year"]),
            dates=tuple(sorted({_day(item) for item in dates})),
            remove_days=removing,
        )
    change = Change(
        kind,
        pk=_id(fields["id"]) if "id" in allowed else 0,
        start_date=_day(fields["start_date"]) if "start_date" in allowed else None,
        week=_week(fields["week"]) if "week" in allowed else (),
        first_date=_day(fields["first_date"]) if "first_date" in allowed else None,
        last_date=_day(fields["last_date"]) if "last_date" in allowed else None,
        reason=_reason(fields.get("reason")) if "reason" in allowed else "",
        remove_days=removing,
    )
    if change.first_date and change.last_date and change.first_date > change.last_date:
        raise LayoutRefusedError("ends-first")
    return change


# --- What the page shows -----------------------------------------------------------


def _today(asset: Asset, now: datetime) -> date:
    return now.astimezone(asset.zone).date()


def _made(
    org: Organization, kind: str, ids: list[int], action: AuditAction
) -> dict[int, tuple[str, str]]:
    """Who made each row and when, from the audit events (none for a row the
    setup file made)."""
    found: dict[int, tuple[str, str]] = {}
    events = (
        AuditEvent.objects.of(org)
        .filter(action=action, object_kind=kind, object_id__in=ids)
        .select_related("administrator")
        .order_by("-at")
    )
    for event in events:
        found[event.object_id] = (
            event.administrator.full_name,
            event.at.astimezone(ZoneInfo(org.time_zone)).strftime("%Y-%m-%d, %H:%M"),
        )
    return found


def page(
    org: Organization, asset: Asset, *, now: datetime | None = None
) -> dict[str, Any]:
    """The availability page of one asset: its weekly availabilities and
    closures with who made them, and the reasons a closure may have."""
    now = now or timezone.now()
    today = _today(asset, now)
    line = availability.timeline(org, asset)
    in_force = line.week_on(today)
    calendars = list(
        AvailabilityCalendar.objects.of(org).filter(asset=asset).order_by("start_date")
    )
    made = _made(
        org,
        "availability_calendar",
        [row.pk for row in calendars],
        AuditAction.MADE_AVAILABILITY,
    )
    weeks = {week.start_date: week for week in line.weeks}
    closures = list(AvailabilityClosure.objects.of(org).filter(asset=asset))
    closure_made = _made(
        org,
        "availability_closure",
        [row.pk for row in closures],
        AuditAction.MADE_CLOSURE,
    )
    return {
        "asset": {"id": asset.pk, "name": asset.name, "time_zone": asset.time_zone},
        "today": today.isoformat(),
        "weeks": [
            {
                "id": row.pk,
                "from": row.start_date.isoformat(),
                "until": (
                    (nxt.start_date - timedelta(days=1)).isoformat()
                    if (
                        nxt := next(
                            (c for c in calendars if c.start_date > row.start_date),
                            None,
                        )
                    )
                    else None
                ),
                "week": availability.minutes_json(weeks[row.start_date].days),
                "state": (
                    "in-force"
                    if in_force and in_force.start_date == row.start_date
                    else "coming"
                    if row.start_date > today
                    else "past"
                ),
                "by": made.get(row.pk, ("", ""))[0],
                "at": made.get(row.pk, ("", ""))[1],
            }
            for row in calendars
        ],
        "closures": [
            {
                "id": row.pk,
                "first": row.first_date.isoformat(),
                "last": row.last_date.isoformat(),
                "reason": row.reason,
                "over": row.last_date < today,
                "by": closure_made.get(row.pk, ("", ""))[0],
                "at": closure_made.get(row.pk, ("", ""))[1],
            }
            for row in closures
        ],
        "owed": owed(org, asset, asset_zone=asset.zone),
        "by_hand": by_hand(org, asset, asset_zone=asset.zone),
        "reasons": [
            {"value": value, "label": str(label)}
            for value, label in ClosureReason.choices
        ],
    }


def owed(
    org: Organization, asset: Asset, *, asset_zone: ZoneInfo
) -> list[dict[str, Any]]:
    """The refunds still to be sent for bookings on the asset: queued, and not
    yet taken by the provider. The worker sends them again until it does."""
    rows = (
        Refund.objects.of(org)
        .filter(
            refund_key__isnull=False,
            completed_at__isnull=True,
            payment__order__bookings__taken_time__asset=asset,
        )
        .select_related("payment__order")
        .order_by("made_at", "pk")
        .distinct()
    )
    return [
        {
            "id": row.pk,
            "number": row.payment.order.number,
            "reference": row.payment.order.reference,
            "amount": row.amount,
            "currency": row.payment.currency,
            "since": row.made_at.astimezone(asset_zone).strftime("%Y-%m-%d %H:%M"),
        }
        for row in rows
    ]


def by_hand(
    org: Organization, asset: Asset, *, asset_zone: ZoneInfo
) -> list[dict[str, Any]]:
    """The orders a change cancelled whose money no refund was queued for, from
    the saved "left refund by hand" events: they stay listed until a refund
    is made for the order's payment after the event. A reload loses nothing."""
    events = (
        AuditEvent.objects.of(org)
        .filter(
            action=AuditAction.LEFT_REFUND_BY_HAND,
            object_kind="order",
            object_id__in=Order.objects.of(org)
            .filter(bookings__taken_time__asset=asset)
            .values("pk"),
        )
        .order_by("at", "pk")
    )
    orders = {
        row.pk: row
        for row in Order.objects.of(org).filter(
            pk__in=[event.object_id for event in events]
        )
    }
    latest_refund: dict[int, datetime] = {}
    for order_id, made_at in (
        Refund.objects.of(org)
        .filter(payment__order_id__in=orders)
        .values_list("payment__order_id", "made_at")
    ):
        if order_id not in latest_refund or made_at > latest_refund[order_id]:
            latest_refund[order_id] = made_at
    found: dict[int, dict[str, Any]] = {}
    for event in events:
        # Only a refund made after the event can have returned this money.
        if event.object_id in found or (
            event.object_id in latest_refund
            and latest_refund[event.object_id] > event.at
        ):
            continue
        order = orders[event.object_id]
        found[event.object_id] = {
            "number": order.number,
            "reference": order.reference,
            "since": event.at.astimezone(asset_zone).strftime("%Y-%m-%d %H:%M"),
        }
    return list(found.values())


def month(org: Organization, asset: Asset, first: date) -> dict[str, Any]:
    """Each day of the month that starts on `first`: its open time after the
    weekly availability in force and the closures, and whether a weekly
    availability starts on it."""
    if first.day != 1:
        raise ValueError("A month starts on its first.")
    line = availability.timeline(org, asset)
    starts = {week.start_date for week in line.weeks}
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(
        days=1
    )
    days = []
    for ordinal in range(first.toordinal(), last.toordinal() + 1):
        day = date.fromordinal(ordinal)
        days.append(
            {
                "date": day.isoformat(),
                "open": [[a, b] for a, b in line.minutes_on(day)],
                "closed": line.closed_on(day),
                "week_starts": day in starts,
            }
        )
    return {"month": first.isoformat()[:7], "days": days}


def holidays(
    org: Organization, asset: Asset, year: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """A year's public holidays, each with whether it is closed already (a
    closure covers it), is over, or falls on a Sunday."""
    today = _today(asset, now or timezone.now())
    if year not in (today.year, today.year + 1):
        raise ValueError("The year is this one or the next.")
    line = availability.timeline(org, asset)
    return {
        "year": year,
        "years": [today.year, today.year + 1],
        "holidays": [
            {
                "date": day.isoformat(),
                "name": name,
                "weekday": day.weekday(),
                "closed": line.closed_on(day),
                "over": day < today,
            }
            for day, name in public_holidays.holidays_of(year)
        ],
    }


# --- The change, the bookings it strands, the preview ------------------------------


def _spans_of(row: AvailabilityCalendar, week: tuple[availability.Day, ...]) -> None:
    for weekday, day in enumerate(week):
        for first, last in day:
            AvailableSpan.objects.create(
                organization_id=row.organization_id,
                availability_calendar=row,
                weekday=weekday,
                start_time=first,
                end_time=last,
            )


def _calendar_day(
    change: Change, today: date, *, own: int = 0, org: Organization, asset: Asset
) -> date:
    start = change.start_date
    if start is None or start < today:
        raise LayoutRefusedError("past")
    if (
        AvailabilityCalendar.objects.of(org)
        .filter(asset=asset, start_date=start)
        .exclude(pk=own)
        .exists()
    ):
        raise LayoutRefusedError("date-taken")
    return start


def _coming_week(
    org: Organization, asset: Asset, pk: int, today: date
) -> AvailabilityCalendar:
    row = _lock(AvailabilityCalendar, org, pk, asset=asset)
    if row.start_date <= today:
        raise LayoutRefusedError("not-coming")
    return row


def _closure(
    org: Organization, asset: Asset, pk: int, today: date
) -> AvailabilityClosure:
    row = _lock(AvailabilityClosure, org, pk, asset=asset)
    if row.last_date < today:
        raise LayoutRefusedError("over")
    return row


def event_days_between(
    org: Organization,
    asset: Asset,
    first: date,
    last: date,
    now: datetime | None = None,
) -> QuerySet[EventDay]:
    """The days of events on the asset that hold time between two local
    dates (inclusive) and are not over, by start: what a closure over those
    dates is refused for ."""
    begins = event_plan.moment(first, 0, asset.zone)
    finishes = event_plan.moment(last + timedelta(days=1), 0, asset.zone)
    return (
        EventDay.objects.of(org)
        .select_related("event", "event__repeat")
        .filter(
            event__asset=asset,
            removed_at__isnull=True,
            taken_time__isnull=False,
            starts_at__lt=finishes,
            ends_at__gt=max(begins, now or timezone.now()),
        )
        .order_by("starts_at", "pk")
    )


def _not_over_events(
    org: Organization,
    asset: Asset,
    first: date,
    last: date,
    now: datetime | None = None,
) -> None:
    """A closure is refused over an event's day already made, naming the
    event . A closure further ahead than the days made is not:
    the repeat skips that day. Shorter weekly hours are not either; they
    leave event days standing. A day already over does not count."""
    held = event_days_between(org, asset, first, last, now).first()
    if held is not None:
        raise LayoutRefusedError(
            "event",
            {"event": held.event_id},
            f"{held.event.title} holds the time.",
        )


def _remove_event_days(
    org: Organization,
    administrator: Member,
    asset: Asset,
    change: Change,
    now: datetime,
    spans: Collection[tuple[date, date]],
) -> None:
    """Remove the event days the change names, in its own transaction, before
    the check that refuses a closure over them: the administrator's "Remove
    that day" in the same step. Only days in the way of this change's dates
    are removed; any other is refused."""
    if not change.remove_days:
        return
    in_the_way = {
        pk
        for first, last in spans
        for pk in event_days_between(org, asset, first, last, now).values_list(
            "pk", flat=True
        )
    }
    if not set(change.remove_days) <= in_the_way:
        raise LayoutRefusedError("event-day")
    try:
        event_admin.remove_days_in_transaction(
            org, administrator, asset, change.remove_days, now
        )
    except event_admin.EventNotFoundError, event_admin.EventRefusedError:
        raise LayoutRefusedError("event-day") from None


def _make_closure(
    org: Organization,
    administrator: Member,
    asset: Asset,
    first: date,
    last: date,
    reason: str,
    now: datetime,
) -> None:
    _not_over_events(org, asset, first, last, now)
    row = AvailabilityClosure(
        organization=org, asset=asset, first_date=first, last_date=last, reason=reason
    )
    _checked(row)
    _saved(row)
    record_event(actor=administrator, action=AuditAction.MADE_CLOSURE, target=row)


def _dates(change: Change) -> tuple[date, date]:
    """The first and last date of a closure change (parse has checked them)."""
    if change.first_date is None or change.last_date is None:
        raise ValueError("A closure has dates.")
    return change.first_date, change.last_date


def make(
    org: Organization,
    administrator: Member,
    asset: Asset,
    change: Change,
    now: datetime,
) -> None:
    """Make the change on the rows (inside the organization's transaction)."""
    today = _today(asset, now)
    if change.kind == "week-make":
        row = AvailabilityCalendar(
            organization=org,
            asset=asset,
            start_date=_calendar_day(change, today, org=org, asset=asset),
        )
        _checked(row)
        _saved(row)
        _spans_of(row, change.week)
        record_event(
            actor=administrator, action=AuditAction.MADE_AVAILABILITY, target=row
        )
    elif change.kind == "week-change":
        row = _coming_week(org, asset, change.pk, today)
        row.start_date = _calendar_day(change, today, own=row.pk, org=org, asset=asset)
        _checked(row)
        _saved(row)
        AvailableSpan.objects.of(org).filter(availability_calendar=row).delete()
        _spans_of(row, change.week)
        record_event(
            actor=administrator, action=AuditAction.CHANGED_AVAILABILITY, target=row
        )
    elif change.kind == "week-remove":
        row = _coming_week(org, asset, change.pk, today)
        record_event(
            actor=administrator, action=AuditAction.REMOVED_AVAILABILITY, target=row
        )
        row.delete()
    elif change.kind == "closure-make":
        first, last = _dates(change)
        if last < today:
            raise LayoutRefusedError("past")
        _remove_event_days(org, administrator, asset, change, now, [(first, last)])
        _make_closure(org, administrator, asset, first, last, change.reason, now)
    elif change.kind == "closure-change":
        first, last = _dates(change)
        closure = _closure(org, asset, change.pk, today)
        if last < today:
            raise LayoutRefusedError("past")
        _remove_event_days(org, administrator, asset, change, now, [(first, last)])
        _not_over_events(org, asset, first, last, now)
        closure.first_date = first
        closure.last_date = last
        closure.reason = change.reason
        _checked(closure)
        _saved(closure)
        record_event(
            actor=administrator, action=AuditAction.CHANGED_CLOSURE, target=closure
        )
    elif change.kind == "closure-remove":
        closure = _closure(org, asset, change.pk, today)
        record_event(
            actor=administrator, action=AuditAction.REMOVED_CLOSURE, target=closure
        )
        closure.delete()
    else:
        _make_holidays(org, administrator, asset, change, today, now)


def _make_holidays(
    org: Organization,
    administrator: Member,
    asset: Asset,
    change: Change,
    today: date,
    now: datetime,
) -> None:
    if change.year not in (today.year, today.year + 1):
        raise ValueError("The year is this one or the next.")
    listed = dict(public_holidays.holidays_of(change.year))
    _remove_event_days(
        org, administrator, asset, change, now, [(day, day) for day in change.dates]
    )
    line = availability.timeline(org, asset)
    for day in change.dates:
        if day not in listed:
            raise ValueError("Not a public holiday of the year.")
        if day < today:
            raise LayoutRefusedError("past")
        # A day a closure covers is closed already; a second would only repeat it.
        if line.closed_on(day):
            continue
        _make_closure(org, administrator, asset, day, day, ClosureReason.HOLIDAY, now)


@dataclass(frozen=True)
class Stranded:
    """A booking the change takes the time of."""

    booking: Booking
    refund: int
    currency: str


def stranded(
    org: Organization,
    asset: Asset,
    old: availability.TimeLine,
    new: availability.TimeLine,
    now: datetime,
) -> list[Stranded]:
    """The live bookings of the asset that have time the change removes, and
    with them the other live lines of an order still being paid (it is cancelled
    whole)."""
    rows = list(
        Booking.objects.of(org)
        .filter(
            taken_time__asset=asset,
            taken_time__state__in=[BookingState.HELD, BookingState.CONFIRMED],
            taken_time__ends_at__gt=now,
        )
        .select_related("taken_time", "order", "member")
        .order_by("taken_time__starts_at", "pk")
    )
    hit = [
        row
        for row in rows
        if availability.loses(
            old, new, row.taken_time.starts_at, row.taken_time.ends_at, now
        )
    ]
    being_paid = {
        row.order_id for row in hit if row.taken_time.state == BookingState.HELD
    }
    if being_paid:
        known = {row.pk for row in hit}
        hit += [
            row
            for row in Booking.objects.of(org)
            .filter(
                order_id__in=being_paid,
                taken_time__state__in=[BookingState.HELD, BookingState.CONFIRMED],
            )
            .select_related("taken_time", "order", "member")
            .order_by("taken_time__starts_at", "pk")
            if row.pk not in known
        ]
    ids = [row.pk for row in hit]
    totals = _totals(org, ids)
    currencies = _currencies(org, list({row.order_id for row in hit}))
    refunded = dict(
        Refund.objects.of(org)
        .filter(booking_id__in=ids)
        .values("booking_id")
        .annotate(total=Sum("amount"))
        .values_list("booking_id", "total")
    )
    return [
        Stranded(
            row,
            0
            if row.taken_time.state != BookingState.CONFIRMED
            else max(0, (totals.get(row.pk) or 0) - (refunded.get(row.pk) or 0)),
            currencies[row.order_id],
        )
        for row in hit
    ]


def token(found: list[Stranded]) -> str:
    """A digest of the bookings a change strands, each with its state and what it
    would refund: the save compares it, so a booking made, cancelled or ended, or
    money moved on one, between the preview and the save makes it differ."""
    text = ",".join(
        f"{item.booking.pk}:{item.booking.taken_time.state}:{item.refund}"
        for item in sorted(found, key=lambda s: s.booking.pk)
    )
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _stamp(moment: datetime, asset: Asset) -> str:
    return moment.astimezone(asset.zone).strftime("%Y-%m-%d %H:%M")


def preview_of(found: list[Stranded], asset: Asset) -> dict[str, object]:
    """How many bookings the change cancels, how many members, and what is
    refunded in each currency; the first of them listed."""
    refunds: dict[str, int] = defaultdict(int)
    for item in found:
        if item.refund:
            refunds[item.currency] += item.refund
    return {
        "bookings": len(found),
        "members": len({item.booking.member_id for item in found}),
        "refund": [
            {"currency": currency, "amount": amount}
            for currency, amount in sorted(refunds.items())
        ],
        "token": token(found),
        "listed": [
            {
                "id": item.booking.pk,
                "number": item.booking.order.number,
                "reference": item.booking.order.reference,
                "member": item.booking.member.full_name,
                "type": item.booking.booking_type_name,
                "state": "paid"
                if item.booking.taken_time.state == BookingState.CONFIRMED
                else "held",
                "start": _stamp(item.booking.taken_time.starts_at, asset),
                "end": _stamp(item.booking.taken_time.ends_at, asset),
                "amount": item.refund,
                "currency": item.currency,
            }
            for item in found[:LISTED]
        ],
    }


@dataclass(frozen=True)
class Saved:
    """A change that was committed, and the bookings it must now cancel."""

    found: list[Stranded]
    asset: Asset
    # How many lines the change cancelled, in the same transaction.
    cancelled: int = 0
    # Orders cancelled whose paid money could not be recorded as owed.
    unfinished: tuple[dict[str, object], ...] = ()


def apply(
    administrator: Member,
    asset_pk: int,
    fields: Fields,
    *,
    now: datetime | None = None,
) -> Saved:
    """Preview or save a change to an asset's availability.

    With `"preview": true` nothing is kept: the answer is raised as `PreviewedError`.
    Otherwise the change is committed only when the bookings it strands are those
    of the body's `expect` (the preview's token); when they differ the fresh
    preview is raised as `ChangedSincePreviewError`.
    """
    change = parse(fields)
    preview = fields.get("preview") is True
    expect = fields.get("expect")
    if not preview and expect is not None and not isinstance(expect, str):
        raise ValueError("The token is text.")
    moment = now or timezone.now()

    def run(org: Organization) -> Saved:
        asset = _lock(Asset, org, asset_pk)
        # A checkout is SERIALIZABLE and takes this row's lock first
        # (`ordering._attempt`). Its snapshot may be older than this commit, and
        # the lock alone does not tell it: updating the row makes such a
        # checkout fail to serialise and read again, so it sees the new
        # availability and is refused in time that was removed. (Orders do the
        # same with the order number.)
        Organization.objects.filter(pk=org.pk).update(
            last_order_number=F("last_order_number")
        )
        old = availability.timeline(org, asset)
        make(org, administrator, asset, change, moment)
        new = availability.timeline(org, asset)
        found = stranded(org, asset, old, new, moment)
        if preview:
            raise PreviewedError(preview_of(found, asset))
        if (found or expect is not None) and expect != token(found):
            raise ChangedSincePreviewError(preview_of(found, asset))
        cancelled, unfinished = cancel_stranded(org, administrator, found, moment)
        return Saved(found, asset, cancelled, tuple(unfinished))

    return organization_transaction(administrator.organization_id, run)


# --- The cancellations -----------------------------------------------------------


def cancel_stranded(
    org: Organization,
    administrator: Member,
    found: list[Stranded],
    now: datetime,
) -> tuple[int, list[dict[str, object]]]:
    """Cancel the bookings the change strands, in the change's own transaction.

    No provider is asked here: a booking is cancelled in the database whatever
    the provider would answer, and a refund is queued as owed
    (`payment_adjustments.cancel_in_transaction`) for the worker to send, again
    and again until the provider takes it. A crash after the commit loses
    nothing. Returns how many lines were cancelled and the orders whose paid
    money could not be recorded as owed (their number and why): they are
    cancelled and need a refund by hand."""
    orders: dict[int, list[Stranded]] = {}
    for item in found:
        orders.setdefault(item.booking.order_id, []).append(item)
    cancelled = 0
    unfinished: list[dict[str, object]] = []
    for items in orders.values():
        order = items[0].booking.order
        being_paid = any(i.booking.taken_time.state == BookingState.HELD for i in items)
        try:
            done = payment_adjustments.cancel_in_transaction(
                org,
                administrator,
                order.pk,
                () if being_paid else tuple(i.booking.public_id for i in items),
                uuid.uuid4(),
                now,
            )
        except payment_adjustments.CancelRefusedError as refused:
            # Over or cancelled already: nothing is left to cancel.
            if refused.code not in SETTLED:
                raise
            continue
        cancelled += len(done.booking_ids)
        if done.unrecorded:
            unfinished.append({"number": order.number, "code": done.unrecorded})
    return cancelled, unfinished
