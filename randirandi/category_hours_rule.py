# SPDX-License-Identifier: AGPL-3.0-only
"""Where a booking category's own hours may lie (W26, W28), asked once.

Kept free of Django so the setup file's checks (`randirandi.setup_file`) can
ask it; the validator (`randirandi.core.validator`) asks it when a service sets
a category's hours. Hours are `(weekday, start, end)`: a weekday from 0
(Monday) to 6, and minutes after midnight, the end up to 24:00.
"""

from collections.abc import Iterable

DAY = 24 * 60
WEEK = 7 * DAY

Hours = tuple[int, int, int]


def hours_outside(
    period_start: int,
    period_end: int,
    end_day: int,
    period_start_weekdays: Iterable[int],
    hours: Iterable[Hours],
) -> list[Hours]:
    """The hours that do not lie inside the container's hours on a generic
    week: each inside one of its weekly windows, from the period start on a
    start weekday to the period end `end_day` days later, wrapping at the
    week's end. Two windows that meet are one (an overnight container
    started on every weekday is open round the clock).
    """
    windows: list[tuple[int, int]] = []
    for weekday in period_start_weekdays:
        first = weekday * DAY + period_start
        last = weekday * DAY + end_day * DAY + period_end
        windows.append((first, last))
        if last > WEEK:
            windows.append((first - WEEK, last - WEEK))
    merged: list[tuple[int, int]] = []
    for first, last in sorted(windows):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return [
        (weekday, start, end)
        for weekday, start, end in hours
        if not any(
            first <= weekday * DAY + start and weekday * DAY + end <= last
            for first, last in merged
        )
    ]


def overlapping(hours: Iterable[Hours]) -> tuple[Hours, Hours] | None:
    """The first two hours of one weekday that share time, or None. Hours
    that only meet (one ends where the next starts) do not overlap."""
    last: Hours | None = None
    for current in sorted(hours):
        if last is not None and last[0] == current[0] and current[1] < last[2]:
            return last, current
        if last is None or last[0] != current[0] or current[2] > last[2]:
            last = current
    return None
