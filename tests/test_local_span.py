# SPDX-License-Identifier: AGPL-3.0-only
"""The one-span shortcut in `_local_span` gives what the minute loop gave."""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pytest

from randirandi.core.calendar import (
    MINUTE,
    Instants,
    Span,
    _instants,
    _local_span,
    _merge,
)

# Long windows start on the hour only, to keep the reference loop affordable.
LENGTHS = [
    timedelta(minutes=minutes) for minutes in (1, 30, 59, 60, 61, 90, 720, 1440, 1560)
]


def _wall(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A local wall-clock time, which has no zone."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC).replace(tzinfo=None)


def _old_local_span(start: datetime, end: datetime, instants: Instants) -> list[Span]:
    parts: list[Span] = []
    wall = start
    while wall < end:
        parts.extend((instant, instant + MINUTE) for instant in instants(wall))
        wall += MINUTE
    return _merge(parts)


def _transitions(zone: ZoneInfo) -> list[datetime]:
    """The UTC moments in 2026-2028 where the zone's offset changes."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    stop = datetime(2029, 1, 1, tzinfo=UTC)
    found = []
    while moment < stop:
        after = moment + timedelta(minutes=30)
        if moment.astimezone(zone).utcoffset() != after.astimezone(zone).utcoffset():
            found.append(after)
        moment = after
    return found


@pytest.mark.parametrize(
    "name", ["Europe/Oslo", "America/New_York", "Australia/Lord_Howe"]
)
def test_the_shortcut_matches_the_minute_loop(name: str) -> None:
    zone = ZoneInfo(name)
    instants = lru_cache(maxsize=None)(lambda wall: _instants(wall, zone))
    transitions = _transitions(zone)
    assert len(transitions) >= 6
    checked = 0
    for moment in transitions:
        centre = moment.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        centre -= timedelta(minutes=centre.minute % 15)
        for step in range(-26 * 4, 26 * 4 + 1):
            start = centre + timedelta(minutes=15 * step)
            for length in LENGTHS:
                if length > timedelta(hours=2) and step % 4:
                    continue
                assert _local_span(start, start + length, instants) == (
                    _old_local_span(start, start + length, instants)
                ), (name, start, length)
                checked += 1
    assert checked > 1000


def test_an_empty_window_has_no_span() -> None:
    wall = _wall(2026, 6, 1, 10)
    zone = ZoneInfo("Europe/Oslo")
    assert _local_span(wall, wall, lambda w: _instants(w, zone)) == []


BACK = datetime(2026, 6, 2, 21, tzinfo=UTC)


def _made_up_instants(wall: datetime) -> tuple[datetime, ...]:
    """A made-up zone at UTC, but two hours behind from 21:00 to 22:30 UTC
    on 2 June 2026: the clocks go back two hours, then forward again. Local
    21:00 to 22:29 never happens; the ends of a window around it each have
    one moment at the same offset. No real zone does this; only the whole
    hours inside the window show it."""
    found = []
    moment = wall.replace(tzinfo=UTC)
    if not BACK <= moment < BACK + timedelta(minutes=90):
        found.append(moment)
    behind = moment + timedelta(hours=2)
    if BACK <= behind < BACK + timedelta(minutes=90):
        found.append(behind)
    return tuple(sorted(found))


def test_two_changes_in_one_window_are_seen_between_its_ends() -> None:
    start, end = _wall(2026, 6, 2, 20, 40), _wall(2026, 6, 3, 0, 21)
    found = _local_span(start, end, _made_up_instants)
    assert found == _old_local_span(start, end, _made_up_instants)
    assert found == [
        (datetime(2026, 6, 2, 20, 40, tzinfo=UTC), BACK),
        (BACK + timedelta(minutes=90), end.replace(tzinfo=UTC)),
    ]


def test_a_week_across_the_autumn_change() -> None:
    zone = ZoneInfo("Europe/Oslo")
    instants = lru_cache(maxsize=None)(lambda wall: _instants(wall, zone))
    for start in (_wall(2026, 10, 21, 22), _wall(2026, 10, 25, 2, 30)):
        end = start + timedelta(days=7)
        assert _local_span(start, end, instants) == _old_local_span(
            start, end, instants
        )
