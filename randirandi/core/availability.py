# SPDX-License-Identifier: AGPL-3.0-only
"""An asset's available time: its weekly availabilities by start date and its
closures by date, as one time line .

The time line is what the run-time offer reads (`calendar._Grid._available`):
the weekly availability in force on a day, the latest start date reached, its
spans for the weekday merged, and nothing at all on a closed day. This module
builds the same answer from rows, or from rows changed in memory, so that a
change to the availability can say, before it is saved, which time it removes.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from randirandi.core import calendar
from randirandi.core.models import (
    Asset,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    Organization,
)

type Minutes = tuple[int, int]
type Day = tuple[Minutes, ...]

MINUTE = timedelta(minutes=1)


def merge_minutes(spans: Iterable[Minutes]) -> Day:
    """Spans of one day in order, those that meet or overlap joined."""
    result: list[Minutes] = []
    for first, last in sorted(spans):
        if result and first <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], last))
        else:
            result.append((first, last))
    return tuple(result)


@dataclass(frozen=True)
class Week:
    """A weekly availability: from a date on, for each weekday (Monday first)
    the spans of the day, joined."""

    start_date: date
    days: tuple[Day, ...]


@dataclass
class TimeLine:
    zone: ZoneInfo
    # By start date.
    weeks: tuple[Week, ...]
    # First and last date of each closure.
    closures: tuple[tuple[date, date], ...]
    _cache: dict[date, tuple[calendar.Span, ...]] = field(default_factory=dict)
    _instants: dict[datetime, tuple[datetime, ...]] = field(default_factory=dict)

    def week_on(self, day: date) -> Week | None:
        """The weekly availability in force on a day: the latest start reached."""
        found = None
        for week in self.weeks:
            if week.start_date <= day:
                found = week
        return found

    def closed_on(self, day: date) -> bool:
        return any(first <= day <= last for first, last in self.closures)

    def minutes_on(self, day: date) -> Day:
        """The available time of a day in minutes after its local midnight."""
        week = self.week_on(day)
        if week is None or self.closed_on(day):
            return ()
        return week.days[day.weekday()]

    def _instants_of(self, wall: datetime) -> tuple[datetime, ...]:
        if wall not in self._instants:
            self._instants[wall] = calendar._instants(wall, self.zone)
        return self._instants[wall]

    def spans_on(self, day: date) -> tuple[calendar.Span, ...]:
        """The available moments of a day, as the run-time offer has them."""
        if day not in self._cache:
            midnight = datetime.combine(day, time())
            self._cache[day] = tuple(
                calendar._merge(
                    part
                    for first, last in self.minutes_on(day)
                    for part in calendar._local_span(
                        midnight + first * MINUTE,
                        midnight + last * MINUTE,
                        self._instants_of,
                    )
                )
            )
        return self._cache[day]

    def spans(self, first: date, last: date) -> list[calendar.Span]:
        """The available moments from one local date to another, joined."""
        return calendar._merge(
            span
            for ordinal in range(first.toordinal(), last.toordinal() + 1)
            for span in self.spans_on(date.fromordinal(ordinal))
        )


def week_of_rows(start_date: date, spans: Iterable[tuple[int, int, int]]) -> Week:
    """A week from (weekday, start, end) rows."""
    days: list[list[Minutes]] = [[] for _ in range(7)]
    for weekday, first, last in spans:
        days[weekday].append((first, last))
    return Week(start_date, tuple(merge_minutes(day) for day in days))


def timeline(org: Organization | int, asset: Asset) -> TimeLine:
    """The asset's time line as the rows stand."""
    calendars = list(
        AvailabilityCalendar.objects.of(org)
        .filter(asset=asset)
        .order_by("start_date")
        .values_list("pk", "start_date")
    )
    rows: dict[int, list[tuple[int, int, int]]] = {pk: [] for pk, _ in calendars}
    for calendar_id, weekday, first, last in (
        AvailableSpan.objects.of(org)
        .filter(availability_calendar__in=[pk for pk, _ in calendars])
        .values_list("availability_calendar_id", "weekday", "start_time", "end_time")
    ):
        rows[calendar_id].append((weekday, first, last))
    closures = tuple(
        AvailabilityClosure.objects.of(org)
        .filter(asset=asset)
        .values_list("first_date", "last_date")
    )
    return TimeLine(
        asset.zone,
        tuple(week_of_rows(start, rows[pk]) for pk, start in calendars),
        closures,
    )


def loses(
    old: TimeLine, new: TimeLine, start: datetime, end: datetime, now: datetime
) -> bool:
    """Whether any moment of [start, end) from now on is available on the old
    time line and not on the new one."""
    start = max(start, now)
    if start >= end:
        return False
    first = start.astimezone(old.zone).date() - timedelta(days=1)
    last = end.astimezone(old.zone).date() + timedelta(days=1)
    after = new.spans(first, last)
    for opened, closed in old.spans(first, last):
        cursor = max(opened, start)
        stop = min(closed, end)
        if cursor >= stop:
            continue
        for kept_from, kept_to in after:
            if kept_to <= cursor:
                continue
            if kept_from >= stop:
                break
            if kept_from > cursor:
                return True
            cursor = max(cursor, kept_to)
            if cursor >= stop:
                break
        if cursor < stop:
            return True
    return False


def minutes_json(days: tuple[Day, ...]) -> list[list[list[int]]]:
    return [[[first, last] for first, last in day] for day in days]


def summary(
    org: Organization, assets: list[Asset], *, now: datetime | None = None
) -> dict[int, dict[str, Any]]:
    """What each asset's page shows of its availability: the week in force, the
    next one and the closures still to come."""
    now = now or timezone.now()
    out: dict[int, dict[str, Any]] = {}
    for asset in assets:
        today = now.astimezone(asset.zone).date()
        line = timeline(org, asset)
        in_force = line.week_on(today)
        coming = next((week for week in line.weeks if week.start_date > today), None)
        out[asset.pk] = {
            "in_force": None
            if in_force is None
            else {
                "from": in_force.start_date.isoformat(),
                "week": minutes_json(in_force.days),
            },
            "next": None if coming is None else coming.start_date.isoformat(),
            "closures_coming": sum(1 for _, last in line.closures if last >= today),
        }
    return out
