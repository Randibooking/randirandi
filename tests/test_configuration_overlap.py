# SPDX-License-Identifier: AGPL-3.0-only
"""Dated offer windows overlap by their occupied intervals, across midnight."""

from datetime import date, datetime, time, timedelta
from itertools import product

import pytest

from randirandi.core.models import BookingConfiguration


def configuration(
    start: date | None,
    stop: date | None,
    weekday: int,
    begins: int,
    ends: int,
    end_day: int = 0,
) -> BookingConfiguration:
    return BookingConfiguration(
        start_date=start,
        stop_date=stop,
        period_start_weekdays=[weekday],
        period_start=begins,
        period_end=ends,
        end_day=end_day,
    )


SUNDAY = date(2027, 1, 3)
MONDAY = date(2027, 1, 4)


@pytest.mark.parametrize(
    ("night_start", "night_stop", "morning_start", "morning_stop", "begins", "overlap"),
    [
        (SUNDAY, SUNDAY, MONDAY, MONDAY, 6 * 60, True),
        (SUNDAY, SUNDAY, MONDAY, MONDAY, 8 * 60 - 1, True),
        (SUNDAY, SUNDAY, MONDAY, MONDAY, 8 * 60, False),
        (SUNDAY, SUNDAY, MONDAY, MONDAY, 9 * 60, False),
        (None, SUNDAY, MONDAY, None, 6 * 60, True),
        (None, SUNDAY, MONDAY, None, 8 * 60, False),
        (None, None, None, None, 6 * 60, True),
        (None, None, None, None, 8 * 60, False),
        (SUNDAY, SUNDAY, date(2027, 1, 11), None, 6 * 60, False),
        # The ranges intersect, but contain no Sunday or no Monday.
        (MONDAY, MONDAY, MONDAY, MONDAY, 6 * 60, False),
        (SUNDAY, SUNDAY, SUNDAY, SUNDAY, 6 * 60, False),
        # Intersecting date ranges contain both weekdays, in different weeks.
        (date(2027, 1, 4), date(2027, 1, 10), MONDAY, MONDAY, 6 * 60, False),
        (date(2027, 1, 4), date(2027, 1, 10), date(2027, 1, 11), None, 6 * 60, True),
    ],
)
def test_overnight_overlap_uses_full_dated_intervals(
    night_start: date | None,
    night_stop: date | None,
    morning_start: date | None,
    morning_stop: date | None,
    begins: int,
    overlap: bool,
) -> None:
    night = configuration(night_start, night_stop, 6, 17 * 60, 8 * 60, 1)
    morning = configuration(morning_start, morning_stop, 0, begins, 10 * 60)

    assert night.covers_the_same_time_as(morning) is overlap
    assert morning.covers_the_same_time_as(night) is overlap


def test_occupied_time_can_cross_several_midnights() -> None:
    weekend = configuration(date(2027, 1, 2), date(2027, 1, 2), 5, 17 * 60, 8 * 60, 2)
    morning = configuration(MONDAY, MONDAY, 0, 6 * 60, 8 * 60)

    assert weekend.covers_the_same_time_as(morning)
    assert morning.covers_the_same_time_as(weekend)
    morning.period_start, morning.period_end = 8 * 60, 10 * 60
    assert not weekend.covers_the_same_time_as(morning)
    assert not morning.covers_the_same_time_as(weekend)


def test_an_overnight_overlap_can_cross_a_year_boundary() -> None:
    night = configuration(date(2026, 12, 31), date(2026, 12, 31), 3, 17 * 60, 8 * 60, 1)
    morning = configuration(date(2027, 1, 1), date(2027, 1, 1), 4, 6 * 60, 8 * 60)

    assert night.covers_the_same_time_as(morning)
    assert morning.covers_the_same_time_as(night)


def test_multiple_weekdays_and_a_distant_finite_date_range() -> None:
    night = configuration(None, None, 6, 17 * 60, 8 * 60, 1)
    night.period_start_weekdays = [2, 6]
    morning = configuration(date(9000, 1, 1), date(9000, 1, 31), 3, 6 * 60, 8 * 60)

    assert night.covers_the_same_time_as(morning)
    assert morning.covers_the_same_time_as(night)


def test_empty_occurrence_ranges_at_the_date_limits() -> None:
    # No Sunday occurs on date.min (Monday), or date.max (Friday).
    forever = configuration(None, None, 6, 17 * 60, 8 * 60, 1)
    for day in (date.min, date.max):
        absent = configuration(day, day, 6, 17 * 60, 8 * 60, 1)
        assert not absent.covers_the_same_time_as(forever)
        assert not forever.covers_the_same_time_as(absent)


def test_weekly_comparison_agrees_with_explicit_dated_intervals() -> None:
    cases = []
    date_ranges = [(0, 0), (0, 1), (1, 7), (6, 14), (14, 28)]
    periods = [
        ((6,), 17 * 60, 8 * 60, 1),
        ((0,), 6 * 60, 10 * 60, 0),
        ((2, 6), 22 * 60, 8 * 60, 1),
        ((5,), 17 * 60, 8 * 60, 2),
    ]
    for (first, last), (weekdays, begins, ends, end_day) in product(
        date_ranges, periods
    ):
        item = configuration(
            SUNDAY + timedelta(days=first),
            SUNDAY + timedelta(days=last),
            weekdays[0],
            begins,
            ends,
            end_day,
        )
        item.period_start_weekdays = list(weekdays)
        cases.append(item)

    def intervals(item: BookingConfiguration) -> list[tuple[datetime, datetime]]:
        day, stop = item.start_date, item.stop_date
        assert day is not None and stop is not None
        found = []
        while day <= stop:
            if day.weekday() in item.period_start_weekdays:
                start = datetime.combine(day, time()) + timedelta(
                    minutes=item.period_start
                )
                end = datetime.combine(day, time()) + timedelta(
                    days=item.end_day, minutes=item.period_end
                )
                found.append((start, end))
            day += timedelta(days=1)
        return found

    for one, other in product(cases, repeat=2):
        expected = any(
            start < other_end and other_start < end
            for start, end in intervals(one)
            for other_start, other_end in intervals(other)
        )
        assert one.covers_the_same_time_as(other) is expected
        assert other.covers_the_same_time_as(one) is expected
