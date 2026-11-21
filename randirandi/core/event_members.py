# SPDX-License-Identifier: AGPL-3.0-only
"""What members read of the organization's events .

The Events list: every event of one organization, with and without an asset,
for the coming days or the past ones or a period, optionally narrowed to
categories and to one asset or none. A repeating event comes once, with its
rule and its days in the period; a day an administrator removed comes as
cancelled, a day the repeat skipped is not sent at all. A contact person who
is no longer active reads as none.

Members never get what only an administrator reads: who made the event, a
skipped day and its reason, the contact's fields the event does not show.
Times are wall times on the event's clock, the asset's or the organization's
("2026-11-11T18:00"); the categories are keys, their words the organization's
labels. Reading writes nothing, and every query is of one organization.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from randirandi.core import event_admin, event_pages, event_plan
from randirandi.core.models import (
    Asset,
    Event,
    EventCategory,
    EventDay,
    EventRepeat,
    Organization,
)

# The most events one answer holds, and the most days it sends of a repeat.
MOST_EVENTS = 200
MOST_DAYS = 16
WHEN = ("coming", "past")


@dataclass(frozen=True)
class MembersQuery:
    """The list asked for. `since` and `until` (local dates, both) ask for a
    period and then `when` is not read. `asset` is "all", "none" or an id.
    `only` asks for one event."""

    when: str = "coming"
    since: date | None = None
    until: date | None = None
    categories: tuple[str, ...] = ()
    asset: str | int = "all"
    only: int | None = None


def valid_categories(keys: Sequence[str]) -> bool:
    return all(key in EventCategory.values for key in keys)


def _asset(event: Event) -> dict[str, object] | None:
    asset = event.asset
    return None if asset is None else {"id": asset.pk, "name": asset.name}


def _contact(event: Event) -> dict[str, str] | None:
    """The name always; the email and the phone only when the event shows
    them; none when the member is not active. Never the member's id."""
    found = event_admin.contact_of(event)
    if found is None:
        return None
    shown = {"name": found.name}
    if found.email:
        shown["email"] = found.email
    if found.phone:
        shown["phone"] = found.phone
    return shown


def places(org: Organization) -> list[dict[str, object]]:
    """The assets that have an event, by name: where the Filter can look."""
    return [
        {"id": asset.pk, "name": asset.name}
        for asset in Asset.objects.of(org)
        .filter(
            pk__in=Event.objects.of(org).filter(asset__isnull=False).values("asset_id")
        )
        .order_by("name", "pk")
    ]


def _days_of(
    rows: Sequence[EventDay],
    rule: EventRepeat,
    query: MembersQuery,
    today: date,
    past: bool,
) -> list[dict[str, object]]:
    """A repeat's days: those of the period; else the next `MOST_DAYS` from
    today, or the last ones before it. A removed day is cancelled, with no
    hours."""
    if query.since is not None and query.until is not None:
        chosen = list(rows)
    elif past:
        chosen = [row for row in rows if row.day < today][-MOST_DAYS:]
    else:
        chosen = [row for row in rows if row.day >= today][:MOST_DAYS]
    found: list[dict[str, object]] = []
    for row in chosen:
        if row.removed_at is not None:
            found.append({"date": row.day.isoformat(), "cancelled": True})
            continue
        start = row.own_start if row.own_start is not None else rule.start_minute
        end = row.own_end if row.own_end is not None else rule.end_minute
        found.append(
            {
                "date": row.day.isoformat(),
                "from": event_pages.clock(start),
                "to": event_pages.clock(end),
            }
        )
    return found


def listing(org: Organization, query: MembersQuery, now: datetime) -> dict[str, Any]:
    """The members' events answer. An unknown `only` gives no events."""
    found = Event.objects.of(org).select_related("asset", "contact")
    if query.only is not None:
        found = found.filter(pk=query.only)
    if query.categories:
        found = found.filter(category__in=query.categories)
    if query.asset == "none":
        found = found.filter(asset__isnull=True)
    elif query.asset != "all":
        found = found.filter(asset_id=int(query.asset))
    period = query.since is not None and query.until is not None
    one = query.only is not None
    # Rows near the moment asked for; the exact test is made on each event's
    # own clock below.
    days = EventDay.objects.of(org).filter(event__in=found).order_by("day", "pk")
    if period:
        days = days.filter(day__gte=query.since, day__lte=query.until)
    elif one:
        pass
    elif query.when == "past":
        days = days.filter(starts_at__lte=now + timedelta(days=2))
    else:
        days = days.filter(ends_at__gt=now - timedelta(days=2))
    by_event: dict[int, list[EventDay]] = {}
    for row in days:
        by_event.setdefault(row.event_id, []).append(row)
    rules = {
        row.event_id: row for row in EventRepeat.objects.of(org).filter(event__in=found)
    }
    shown: list[tuple[tuple[str, int], dict[str, object]]] = []
    for event in found:
        zone = event_pages.zone_of(org, event.asset)
        today = now.astimezone(zone).date()
        rule = rules.get(event.pk)
        rows = by_event.get(event.pk, [])
        answer: dict[str, object] = {
            "id": event.pk,
            "category": event.category,
            "title": event.title,
            "description": event.description,
            "asset": _asset(event),
            "contact": _contact(event),
            "repeat": None,
            "start": None,
            "end": None,
            "days": [],
        }
        if rule is None:
            live = [row for row in rows if row.removed_at is None]
            if not live:
                continue
            row = live[0]
            if not (period or one) and (row.ends_at > now) == (query.when == "past"):
                continue
            answer["start"] = event_pages.local(row.starts_at, zone)
            answer["end"] = event_pages.local(row.ends_at, zone)
            shown.append(((str(answer["start"]), event.pk), answer))
            continue
        ended = rule.end_date is not None and rule.end_date < today
        if period:
            if not rows:
                continue
        elif not one and ended != (query.when == "past"):
            continue
        answer["repeat"] = {
            "weekdays": list(event_plan.weekdays_of(rule.weekdays)),
            "from": event_pages.clock(rule.start_minute),
            "to": event_pages.clock(rule.end_minute),
            "first": rule.first_date.isoformat(),
            "end": rule.end_date.isoformat() if rule.end_date else None,
        }
        listed = _days_of(rows, rule, query, today, ended)
        answer["days"] = listed
        first = str(listed[0]["date"]) if listed else rule.first_date.isoformat()
        shown.append(((first, event.pk), answer))
    shown.sort(
        key=lambda row: row[0], reverse=query.when == "past" and not (period or one)
    )
    events = [answer for _, answer in shown][:MOST_EVENTS]
    return {"events": events, "count": len(shown), "places": places(org)}
