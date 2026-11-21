# SPDX-License-Identifier: AGPL-3.0-only
"""What admin's event pages read : the
list, an event with its days and skipped days, the preview in words, the
people a contact can be, and the event days in the way of a closure.

Reading writes no event. Every query is of one organization. Times are
written on the event's clock: the asset's, or the organization's for an
event without an asset ("2027-03-06T10:00"). The categories are keys; their
words are the organization's labels (`event.category.*`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce

from randirandi import labels
from randirandi.core import availability_admin, event_admin, event_plan
from randirandi.core.event_plan import DayPlan, InWay
from randirandi.core.models import (
    Asset,
    Block,
    Booking,
    Event,
    EventCategory,
    EventDay,
    EventNotice,
    EventRepeat,
    EventSkippedDay,
    Member,
    MemberStatus,
    Organization,
)
from randirandi.core.models.base import DAY

PER_PAGE = 25
WHEN = ("coming", "past", "day", "period")
# The most people the contact search offers.
MOST_PEOPLE = 20
LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


@dataclass(frozen=True)
class EventsQuery:
    """The list asked for. `asset` is "all", "none" or an asset's id."""

    when: str = "coming"
    since: date | None = None
    until: date | None = None
    category: str = "all"
    asset: str | int = "all"
    search: str = ""
    newest: bool = False
    page: int = 0


def name_of(member: Member) -> str:
    return member.full_name or f"{member.first_name} {member.last_name}".strip()


def zone_of(org: Organization, asset: Asset | None) -> ZoneInfo:
    return asset.zone if asset is not None else ZoneInfo(org.time_zone)


def local(at: datetime, zone: ZoneInfo) -> str:
    return at.astimezone(zone).strftime(LOCAL_FORMAT)


def clock(minute: int) -> str:
    """A time of day in minutes as "HH:MM"; the end of the day is "24:00"."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def categories(org: Organization) -> list[dict[str, str]]:
    """The four categories by key, with the organization's words."""
    return [
        {
            "key": EventCategory.SOCIAL.value,
            "label": labels.word(org, "event.category.social"),
        },
        {
            "key": EventCategory.VOLUNTEER_WORK.value,
            "label": labels.word(org, "event.category.volunteer_work"),
        },
        {
            "key": EventCategory.ORGANIZATION.value,
            "label": labels.word(org, "event.category.organization"),
        },
        {
            "key": EventCategory.OPEN_HOUSE.value,
            "label": labels.word(org, "event.category.open_house"),
        },
    ]


def assets(org: Organization) -> list[dict[str, object]]:
    return [
        {"id": asset.pk, "name": asset.name}
        for asset in Asset.objects.of(org).order_by("name", "pk")
    ]


def options(org: Organization) -> dict[str, object]:
    """What a sheet chooses from: the categories and the assets."""
    return {"categories": categories(org), "assets": assets(org)}


# --- The list ---------------------------------------------------------------------


def _live(org: Organization) -> QuerySet[EventDay]:
    return EventDay.objects.of(org).filter(
        event=OuterRef("pk"), removed_at__isnull=True
    )


def events_query(
    org: Organization, query: EventsQuery, now: datetime
) -> QuerySet[Event]:
    live = _live(org)
    coming = live.filter(ends_at__gt=now)
    found = (
        Event.objects.of(org)
        .select_related("asset", "contact")
        .annotate(
            next_start=Subquery(coming.order_by("starts_at").values("starts_at")[:1]),
            next_end=Subquery(coming.order_by("starts_at").values("ends_at")[:1]),
            last_start=Subquery(live.order_by("-starts_at").values("starts_at")[:1]),
            last_end=Subquery(live.order_by("-starts_at").values("ends_at")[:1]),
        )
        .annotate(at=Coalesce("next_start", "last_start"))
    )
    if query.when == "coming":
        found = found.filter(Exists(coming))
    elif query.when == "past":
        found = found.filter(~Exists(coming) | Exists(live.filter(ends_at__lte=now)))
    else:
        assert query.since is not None and query.until is not None  # noqa: S101
        found = found.filter(
            Exists(live.filter(day__gte=query.since, day__lte=query.until))
        )
    if query.category != "all":
        found = found.filter(category=query.category)
    if query.asset == "none":
        found = found.filter(asset__isnull=True)
    elif query.asset != "all":
        found = found.filter(asset_id=int(query.asset))
    words = query.search.strip()
    if words:
        found = found.filter(title__icontains=words)
    order = (
        F("at").desc(nulls_last=True) if query.newest else F("at").asc(nulls_last=True)
    )
    return found.order_by(order, "pk")


def _contact_words(event: Event) -> str | None:
    contact = event_admin.contact_of(event)
    return contact.name if contact is not None else None


def _asset_words(event: Event) -> dict[str, object] | None:
    return (
        {"id": event.asset.pk, "name": event.asset.name}
        if event.asset is not None
        else None
    )


def _repeats(org: Organization, ids: Sequence[int]) -> dict[int, EventRepeat]:
    return {
        row.event_id: row
        for row in EventRepeat.objects.of(org).filter(event_id__in=list(ids))
    }


def listing(org: Organization, query: EventsQuery, now: datetime) -> dict[str, object]:
    """One page of the events (25), each once: a repeat by its next day, or
    its last when none is left."""
    found = events_query(org, query, now)
    count = found.count()
    page = list(found[query.page * PER_PAGE : (query.page + 1) * PER_PAGE])
    repeats = _repeats(org, [event.pk for event in page])
    rows = []
    for event in page:
        zone = zone_of(org, event.asset)
        rule = repeats.get(event.pk)
        shown: Any = event  # carries the annotated times
        start = shown.next_start or shown.last_start
        end = shown.next_end if shown.next_start else shown.last_end
        if start is not None and end is not None:
            begins, ends = local(start, zone), local(end, zone)
        elif rule is not None:
            begins = f"{rule.first_date.isoformat()}T{clock(rule.start_minute)}"
            ends = f"{rule.first_date.isoformat()}T{clock(rule.end_minute % DAY)}"
        else:
            begins = ends = ""
        contact = _contact_words(event)
        rows.append(
            {
                "id": event.pk,
                "title": event.title,
                "category": event.category,
                "asset": _asset_words(event),
                "start": begins,
                "end": ends,
                "weekdays": list(event_plan.weekdays_of(rule.weekdays))
                if rule is not None
                else None,
                "contact": contact,
            }
        )
    return {
        "events": rows,
        "count": count,
        "per_page": PER_PAGE,
        **options(org),
    }


# --- One event ----------------------------------------------------------------------


def _told(
    org: Organization, skipped: Sequence[EventSkippedDay]
) -> dict[int, dict[str, object]]:
    """Whom each skipped day was told to by email and when (the day the notice
    was made); none for a day the preview listed."""
    found: dict[int, dict[str, object]] = {}
    notices = (
        EventNotice.objects.of(org)
        .filter(skipped_day__in=list(skipped))
        .select_related("recipient")
        .order_by("pk")
    )
    for notice in notices:
        told = found.setdefault(
            notice.skipped_day_id,
            {"date": notice.skipped_day.created_at.date().isoformat(), "to": []},
        )
        told["to"].append(name_of(notice.recipient))  # type: ignore[attr-defined]
    return found


def _skipped_name(org: Organization, rows: Sequence[EventSkippedDay]) -> dict[int, str]:
    """The title of the event, or the note of the block, that held a skipped
    day: what the row names by id."""
    events = {
        row.in_the_way
        for row in rows
        if row.reason == "event" and row.in_the_way is not None
    }
    blocks = {
        row.in_the_way
        for row in rows
        if row.reason == "blocked" and row.in_the_way is not None
    }
    titles = dict(
        Event.objects.of(org).filter(pk__in=events).values_list("pk", "title")
    )
    notes = dict(Block.objects.of(org).filter(pk__in=blocks).values_list("pk", "note"))
    names = {}
    for row in rows:
        if row.in_the_way is None:
            continue
        text = (titles if row.reason == "event" else notes).get(row.in_the_way, "")
        if row.reason in ("event", "blocked") and text:
            names[row.pk] = text
    return names


def event_data(org: Organization, event_id: int, now: datetime) -> dict[str, object]:
    """One event as admin reads it: its fields, the contact and what members
    see of them, who made it, a repeat's rule with every day it has and the
    days it skipped."""
    event = (
        Event.objects.of(org)
        .select_related("asset", "contact", "made_by")
        .filter(pk=event_id)
        .first()
    )
    if event is None:
        raise event_admin.EventNotFoundError
    zone = zone_of(org, event.asset)
    rule = EventRepeat.objects.of(org).filter(event=event).first()
    days = list(EventDay.objects.of(org).filter(event=event).order_by("day"))
    contact = event.contact
    answer: dict[str, object] = {
        "id": event.pk,
        "title": event.title,
        "category": event.category,
        "description": event.description,
        "asset": _asset_words(event),
        "contact": (
            {"id": contact.pk, "name": name_of(contact)}
            if event_admin.contact_of(event) is not None and contact is not None
            else None
        ),
        "show_email": event.show_email,
        "show_phone": event.show_phone,
        "made_by": name_of(event.made_by),
        "created": event.created_at.astimezone(zone).date().isoformat(),
        "repeat": None,
        "start": None,
        "end": None,
        "days": [],
        "skipped": [],
        **options(org),
    }
    if rule is None:
        if days:
            answer["start"] = local(days[0].starts_at, zone)
            answer["end"] = local(days[0].ends_at, zone)
        return answer
    answer["repeat"] = {
        "weekdays": list(event_plan.weekdays_of(rule.weekdays)),
        "from": clock(rule.start_minute),
        "to": clock(rule.end_minute),
        "first": rule.first_date.isoformat(),
        "end": rule.end_date.isoformat() if rule.end_date else None,
    }
    answer["days"] = [
        {
            "id": day.pk,
            "date": day.day.isoformat(),
            "from": clock(
                day.own_start if day.own_start is not None else rule.start_minute
            ),
            "to": clock(day.own_end if day.own_end is not None else rule.end_minute),
            "other": day.own_start is not None,
            "removed": day.removed_at is not None,
            "over": day.ends_at <= now,
        }
        for day in days
    ]
    skipped = list(EventSkippedDay.objects.of(org).filter(event=event).order_by("day"))
    told = _told(org, skipped)
    names = _skipped_name(org, skipped)
    answer["skipped"] = [
        {
            "date": row.day.isoformat(),
            "reason": row.reason,
            "name": names.get(row.pk),
            "told": told.get(row.pk),
        }
        for row in skipped
    ]
    return answer


# --- The preview, in words ----------------------------------------------------------


def _way(
    item: InWay,
    day: date | None,
    zone: ZoneInfo,
    lookups: Lookups,
) -> dict[str, object]:
    """One thing in the way, as the sheet shows it and settles it. A booking
    names its member: reading who booked is a look-up (`names_members`)."""
    days, blocks, bookings = lookups
    answer: dict[str, object] = {
        "reason": item.reason,
        "date": day.isoformat() if day is not None else None,
        "ways": list(item.ways),
        "settled": item.settled,
    }
    if item.reason == "booked":
        answer.update(number=item.number, reference=item.reference)
        mine = bookings.get(item.claim_id) if item.claim_id is not None else None
        if mine is not None:
            member, begins, ends = mine
            answer.update(
                member=member, start=local(begins, zone), end=local(ends, zone)
            )
    elif (
        item.reason == "blocked"
        and item.block_id is not None
        and item.block_id in blocks
    ):
        note, begins, ends = blocks[item.block_id]
        answer.update(note=note, start=local(begins, zone), end=local(ends, zone))
    elif item.reason == "event":
        answer.update(
            day=item.day_id,
            event=item.event_id,
            title=item.title,
            repeating=item.repeating,
        )
        held = days.get(item.day_id) if item.day_id is not None else None
        if held is not None:
            answer.update(
                start=local(held.starts_at, zone), end=local(held.ends_at, zone)
            )
    return answer


# Another event's days by id, blocks by id (note, start, end) and bookings by
# the claim they hold (the member's name, start, end).
Lookups = tuple[
    dict[int, EventDay],
    dict[int, tuple[str, datetime, datetime]],
    dict[int, tuple[str, datetime, datetime]],
]


def names_members(answer: Mapping[str, object]) -> bool:
    """Whether a preview's (or a refusal's) answer names a member who booked."""
    ways: list[object] = list(answer.get("way") or [])  # type: ignore[call-overload]
    for skipped in answer.get("skipped") or []:  # type: ignore[attr-defined]
        ways.append(skipped.get("way"))
    return any(isinstance(way, dict) and "member" in way for way in ways)


def _lookups(org: Organization, plans: Sequence[DayPlan]) -> Lookups:
    day_ids = {
        item.day_id for plan in plans for item in plan.in_way if item.day_id is not None
    }
    block_ids = {
        item.block_id
        for plan in plans
        for item in plan.in_way
        if item.block_id is not None
    }
    claim_ids = {
        item.claim_id
        for plan in plans
        for item in (*plan.in_way, *([plan.cause] if plan.cause else []))
        if item.claim_id is not None
    }
    days = {row.pk: row for row in EventDay.objects.of(org).filter(pk__in=day_ids)}
    bookings = {
        row.taken_time_id: (
            row.member.full_name,
            row.taken_time.starts_at,
            row.taken_time.ends_at,
        )
        for row in Booking.objects.of(org)
        .filter(taken_time_id__in=claim_ids)
        .select_related("taken_time", "member")
    }
    blocks = {
        row.pk: (row.note, row.taken_time.starts_at, row.taken_time.ends_at)
        for row in Block.objects.of(org)
        .filter(pk__in=block_ids)
        .select_related("taken_time")
    }
    return days, blocks, bookings


def _left(plan: DayPlan, zone: ZoneInfo) -> list[list[str]]:
    """The part of a cell the day leaves that nobody can book, across the
    containers, each piece once."""
    pieces = sorted({span for leaving in plan.leaves for span in leaving.spans})
    return [[local(a, zone), local(b, zone)] for a, b in pieces]


def preview_data(org: Organization, shown: event_admin.Preview) -> dict[str, object]:
    """What a make or a change would do: for a one-off, its time, what it
    leaves and what is in the way; for a repeat, the days it takes, what each
    leaves, and the days it skips counted by reason and listed."""
    zone = ZoneInfo(shown.time_zone)
    looked = _lookups(org, shown.days)
    answer: dict[str, object] = {
        "repeat": shown.repeating,
        "asset": shown.asset_id,
        "over": shown.over,
        "refusal": shown.refusal,
    }
    if not shown.repeating:
        plan = shown.days[0]
        answer.update(
            start=local(plan.starts_at, zone),
            end=local(plan.ends_at, zone),
            left=_left(plan, zone) if shown.asset_id is not None else [],
            way=[_way(item, plan.day, zone, looked) for item in plan.in_way],
        )
        return answer
    taken = shown.takes
    first = taken[0] if taken else (shown.days[0] if shown.days else None)
    answer.update(
        takes=[day.day.isoformat() for day in taken],
        left=_left(first, zone) if first is not None and shown.asset_id else [],
        skipped=[
            {
                "date": plan.day.isoformat(),
                "reason": plan.reason,
                "way": _way(plan.cause, plan.day, zone, looked)
                if plan.cause is not None
                else None,
            }
            for plan in shown.skipped
        ],
        counts=shown.skipped_by_reason(),
    )
    return answer


_EPOCH = datetime(1, 1, 1, tzinfo=UTC)


def refused_way(org: Organization, in_way: Sequence[InWay]) -> list[dict[str, object]]:
    """What a refusal names as holding the time, without a day or hours."""
    plans = [DayPlan(date.min, _EPOCH, _EPOCH, tuple(in_way))]
    looked = _lookups(org, plans)
    zone = ZoneInfo(org.time_zone)
    return [_way(item, None, zone, looked) for item in in_way]


# --- People ----------------------------------------------------------------------


def people(org: Organization, search: str) -> list[dict[str, object]]:
    """Active members a contact can be, by name, at most twenty."""
    found = Member.objects.of(org).filter(status=MemberStatus.ACTIVE)
    words = search.strip()
    if words:
        found = found.filter(
            Q(full_name__icontains=words)
            | Q(first_name__icontains=words)
            | Q(last_name__icontains=words)
        )
    return [
        {"id": member.pk, "name": name_of(member)}
        for member in found.order_by("full_name", "pk")[:MOST_PEOPLE]
    ]


# --- In the way of a closure --------------------------------------------------------


def days_in_the_way(
    org: Organization,
    asset: Asset,
    spans: Sequence[tuple[date, date]],
    now: datetime,
) -> list[dict[str, object]]:
    """The event days on the asset a closure over these dates (or the public
    holidays, one span each) would be refused for, each once, by start; a
    repeat's day can be removed in the same save, a one-off's cannot."""
    found: dict[int, EventDay] = {}
    for first, last in spans:
        for day in availability_admin.event_days_between(org, asset, first, last, now):
            found[day.pk] = day
    zone = asset.zone
    return [
        {
            "day": day.pk,
            "event": day.event_id,
            "title": day.event.title,
            "repeating": hasattr(day.event, "repeat"),
            "date": day.day.isoformat(),
            "start": local(day.starts_at, zone),
            "end": local(day.ends_at, zone),
        }
        for day in sorted(found.values(), key=lambda d: (d.starts_at, d.pk))
    ]
