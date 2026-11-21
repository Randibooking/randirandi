# SPDX-License-Identifier: AGPL-3.0-only
"""Calendar weeks: a container starts in whole weeks, every N weeks from the
first, counted straight across New Year .
"""

from datetime import date, timedelta
from itertools import product
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi import setup_file
from randirandi.core import setup as setups
from randirandi.core.models import BookingConfiguration
from randirandi.core.models.bookable import MOST_WEEKS
from randirandi.core.pricing import PriceUnavailableError
from randirandi.setup_file import SetupError, parse
from tests import made_up
from tests.test_calendar import Example, at
from tests.test_calendar import made as made
from tests.test_pricing import quote
from tests.test_setup_file import SMALLEST
from tests.test_setup_load import TODAY, in_order


def monday(week: str) -> date:
    """The Monday of a calendar week, written as 2027-W01."""
    year, number = week.split("-W")
    return date.fromisocalendar(int(year), int(number), 1)


def sunday(week: str) -> date:
    return monday(week) + timedelta(days=6)


def container(
    first: str | None,
    last: str | None = None,
    *,
    every: int = 1,
    weekdays: tuple[int, ...] = (0,),
    begins: int = 10 * 60,
    ends: int = 12 * 60,
    end_day: int = 0,
) -> BookingConfiguration:
    return BookingConfiguration(
        start_date=None if first is None else monday(first),
        stop_date=None if last is None else sunday(last),
        every_weeks=every,
        period_start_weekdays=list(weekdays),
        period_start=begins,
        period_end=ends,
        end_day=end_day,
    )


def test_every_other_week_counts_straight_across_new_year_and_week_53() -> None:
    every_other = container("2026-W51", every=2)

    in_force = ["2026-W51", "2026-W53", "2027-W02", "2027-W04"]
    off = ["2026-W52", "2027-W01", "2027-W03"]
    assert all(every_other.starts_on(monday(week)) for week in in_force)
    assert not any(every_other.starts_on(monday(week)) for week in off)
    # Counting by the weeks' numbers would have put 2026-W53 and 2027-W01,
    # both odd, in force one after the other.
    assert not every_other.starts_on(monday("2026-W51") - timedelta(weeks=2))


def test_an_open_ended_container_goes_on_without_end() -> None:
    every_third = container("2027-W01", every=3)
    far = monday("2027-W01") + timedelta(weeks=3 * 4000)

    assert every_third.starts_on(far)
    assert not every_third.starts_on(far + timedelta(weeks=1))
    assert every_third.starts_from(far)
    # Without a first week it is every week, from always.
    always = container(None)
    assert always.starts_on(date.min)
    assert always.starts_from(date.max)


def test_a_single_screening_is_one_week_and_one_weekday() -> None:
    friday = container("2027-W09", "2027-W09", weekdays=(4,))
    days = [monday("2027-W07") + timedelta(days=n) for n in range(35)]

    assert [day for day in days if friday.starts_on(day)] == [date(2027, 3, 5)]
    assert friday.starts_from(date(2027, 3, 5))
    assert not friday.starts_from(date(2027, 3, 6))


def test_the_last_start_is_in_the_last_week_in_force() -> None:
    # Every other week from week 1 to week 10: week 10 is off, so the last
    # start is the Monday of week 9, though the stop is a week later.
    mondays = container("2027-W01", "2027-W10", every=2)

    assert mondays.starts_from(monday("2027-W09"))
    assert not mondays.starts_from(monday("2027-W09") + timedelta(days=1))


def test_two_alternating_containers_do_not_overlap() -> None:
    odd_weeks = container("2027-W01", every=2)
    even_weeks = container("2027-W02", every=2)

    assert not odd_weeks.covers_the_same_time_as(even_weeks)
    assert not even_weeks.covers_the_same_time_as(odd_weeks)
    also_odd = container("2027-W03", every=2)
    assert odd_weeks.covers_the_same_time_as(also_odd)


def test_a_period_into_the_next_week_meets_the_alternate_week() -> None:
    # Sunday 17:00 to Monday 08:00, every other week from week 1.
    night = container(
        "2027-W01", every=2, weekdays=(6,), begins=17 * 60, ends=8 * 60, end_day=1
    )
    # Monday 06:00 to 10:00, in the weeks between.
    mornings = container("2027-W02", every=2, begins=6 * 60, ends=10 * 60)

    assert night.covers_the_same_time_as(mornings)
    assert mornings.covers_the_same_time_as(night)
    # In the night's own weeks, the Monday before it is its week's first day.
    own_weeks = container("2027-W03", every=2, begins=6 * 60, ends=10 * 60)
    assert not night.covers_the_same_time_as(own_weeks)


def test_every_two_and_every_three_weeks_meet_in_their_sixth_week() -> None:
    every_two = container("2027-W02", every=2)
    every_three = container("2027-W03", every=3)

    both = [
        number
        for number in range(1, 13)
        if every_two.starts_on(monday(f"2027-W{number:02d}"))
        and every_three.starts_on(monday(f"2027-W{number:02d}"))
    ]
    assert both == [6, 12]
    assert every_two.covers_the_same_time_as(every_three)
    assert every_three.covers_the_same_time_as(every_two)
    before = container("2027-W02", "2027-W05", every=2)
    assert not before.covers_the_same_time_as(every_three)
    assert not every_three.covers_the_same_time_as(before)


def test_every_two_and_every_four_weeks_a_week_apart_never_meet() -> None:
    every_two = container("2027-W01", every=2)
    every_four = container("2027-W02", every=4)

    assert not every_two.covers_the_same_time_as(every_four)
    assert not every_four.covers_the_same_time_as(every_two)
    assert every_two.covers_the_same_time_as(container("2027-W03", every=4))


def test_an_open_ended_container_against_one_with_a_stop_week() -> None:
    open_ended = container("2027-W01", every=2)

    assert open_ended.covers_the_same_time_as(container("2030-W02", "2030-W20"))
    assert not open_ended.covers_the_same_time_as(container("2027-W02", "2027-W02"))
    for week in ("2040-W01", "2040-W02", "2041-W30"):
        one_week = container(week, week)
        assert open_ended.covers_the_same_time_as(one_week) is open_ended.starts_on(
            monday(week)
        )


def test_the_overlap_agrees_with_every_start_written_out() -> None:
    periods = [
        ((0,), 10 * 60, 12 * 60, 0),
        ((6,), 17 * 60, 8 * 60, 1),
        ((2, 6), 22 * 60, 8 * 60, 1),
        ((5,), 17 * 60, 8 * 60, 2),
    ]
    weeks = [("2027-W01", None), ("2027-W02", "2027-W09"), ("2027-W04", None)]
    cases = [
        container(
            first,
            last,
            every=every,
            weekdays=weekdays,
            begins=begins,
            ends=ends,
            end_day=end_day,
        )
        for (first, last), every, (weekdays, begins, ends, end_day) in product(
            weeks, (1, 2, 3, 4), periods
        )
    ]
    # Every case starts by week 4 and repeats within 12 weeks.
    days = [monday("2026-W52") + timedelta(days=n) for n in range(7 * 20)]

    def moments(item: BookingConfiguration) -> list[tuple[int, int]]:
        return [
            (
                day.toordinal() * 1440 + item.period_start,
                day.toordinal() * 1440 + item.period_length + item.period_start,
            )
            for day in days
            if item.starts_on(day)
        ]

    written = {id(item): moments(item) for item in cases}
    for one, other in product(cases, repeat=2):
        expected = any(
            start < other_end and other_start < end
            for start, end in written[id(one)]
            for other_start, other_end in written[id(other)]
        )
        assert one.covers_the_same_time_as(other) is expected


@pytest.mark.django_db(transaction=True)
def test_the_calendar_and_the_price_follow_every_other_week(made: Example) -> None:
    made.configure(start_date=monday("2026-W51"), every_weeks=2)

    assert made.offers(monday("2026-W53"))
    assert made.offers(monday("2027-W01")) == ()
    assert made.offers(monday("2027-W02"))
    off_week = monday("2027-W01")
    with pytest.raises(PriceUnavailableError):
        quote(made, starts_at=at(off_week, 10), ends_at=at(off_week, 12))
    in_force = monday("2027-W02")
    assert quote(made, starts_at=at(in_force, 10), ends_at=at(in_force, 12)).total


WEEKS = (
    'status = "enabled"',
    'start_week = "2026-W53"\nstop_week = "2027-W52"\nevery_weeks = 2\n'
    'status = "enabled"',
)


def test_the_setup_file_reads_and_writes_calendar_weeks() -> None:
    setup = parse(SMALLEST.replace(*WEEKS))
    item = setup.booking_configurations[0]

    assert (item.start_date, item.stop_date) == (
        date(2026, 12, 28),
        date(2028, 1, 2),
    )
    assert item.every_weeks == 2
    text = setup_file.write(setup)
    assert 'start_week = "2026-W53"' in text
    assert 'stop_week = "2027-W52"' in text
    assert "every_weeks = 2" in text
    assert parse(text) == setup
    # Every week is the default, and is left out.
    assert "every_weeks" not in setup_file.write(parse(SMALLEST))


@pytest.mark.django_db
def test_calendar_weeks_load_and_export_the_same() -> None:
    setup = parse(SMALLEST.replace(*WEEKS))

    organization = setups.load(setup, today=TODAY)
    stored = BookingConfiguration.objects.of(organization).get()
    assert (stored.start_date, stored.stop_date, stored.every_weeks) == (
        date(2026, 12, 28),
        date(2028, 1, 2),
        2,
    )
    assert in_order(setups.export(organization)) == in_order(setup)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("weeks", "fault"),
    [
        (
            'every_weeks = 2\nstatus = "enabled"',
            "booking_configurations[1].start_date: Every N weeks counts from a "
            "first calendar week, so it needs one.",
        ),
        (
            'start_week = "2027-W10"\nstop_week = "2027-W09"\nstatus = "enabled"',
            "booking_configurations[1].stop_date: The last calendar week is not "
            "before the first.",
        ),
    ],
    ids=["every other week from no first week", "a stop week before the first"],
)
def test_the_loader_refuses_calendar_weeks_that_cannot_be(
    weeks: str, fault: str
) -> None:
    with pytest.raises(SetupError) as refused:
        setups.load(parse(SMALLEST.replace('status = "enabled"', weeks)), today=TODAY)

    assert fault in refused.value.faults


BEFORE = [("core", "0013_booking_cart_row")]


@pytest.fixture
def before_the_weeks(transactional_db: None) -> Any:
    """The made-up organization, with its configuration's dates as they could
    be stored before calendar weeks.
    """
    made = made_up.everything()
    made_up.plain_floor_plans(made.organization)
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        yield made
    finally:
        latest = MigrationExecutor(connection)
        latest.migrate(latest.loader.graph.leaf_nodes())


def stored_dates(pk: int) -> tuple[date | None, date | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT start_date, stop_date FROM core_bookingconfiguration WHERE id=%s",
            [pk],
        )
        row = cursor.fetchone()
    return row[0], row[1]


def store_dates(pk: int, start: date | None, stop: date | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_bookingconfiguration SET start_date=%s, stop_date=%s "
            "WHERE id=%s",
            [start, stop, pk],
        )


@pytest.mark.django_db(transaction=True)
def test_the_migration_moves_dates_to_whole_weeks_when_no_start_is_added(
    before_the_weeks: Any,
) -> None:
    pk = before_the_weeks.configuration.pk
    # It starts on Mondays. Wednesday 3 March to Saturday 20 March 2027 adds
    # Monday 1 March and Tuesday 2 March, and Sunday 21 March: a Monday is
    # added, so nothing moves and the configuration is named.
    store_dates(pk, date(2027, 3, 3), date(2027, 3, 20))
    with pytest.raises(RuntimeError, match="configuration 001: whole weeks would"):
        MigrationExecutor(connection).migrate([("core", "0014_calendar_weeks")])
    assert stored_dates(pk) == (date(2027, 3, 3), date(2027, 3, 20))
    # From Monday 1 March, to Saturday 20 March: only a Sunday is added.
    store_dates(pk, date(2027, 3, 1), date(2027, 3, 20))
    MigrationExecutor(connection).migrate([("core", "0015_calendar_week_rules")])
    assert stored_dates(pk) == (date(2027, 3, 1), date(2027, 3, 21))


def test_the_file_refuses_more_than_a_years_weeks() -> None:
    # The file reader holds the configuration's limit without importing Django.
    assert setup_file._MOST_WEEKS == MOST_WEEKS
    with pytest.raises(SetupError) as refused:
        parse(
            SMALLEST.replace(
                'status = "enabled"',
                'start_week = "2027-W01"\nevery_weeks = 53\nstatus = "enabled"',
            )
        )

    assert "booking_configurations[1].every_weeks: must be 52 or less." in (
        refused.value.faults
    )


@pytest.mark.django_db
def test_every_n_weeks_is_one_to_a_years_weeks(made: Example) -> None:
    made.configure(start_date=monday("2027-W01"), every_weeks=52)

    for every in (0, 53):
        made.config.every_weeks = every
        with pytest.raises(ValidationError) as refused:
            made.config.clean()
        assert refused.value.message_dict["every_weeks"] == ["It is 1 to 52."]
        with pytest.raises(IntegrityError), transaction.atomic():
            BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
                every_weeks=every
            )


@pytest.mark.django_db(transaction=True)
def test_the_migration_reverses_only_while_every_container_is_every_week(
    before_the_weeks: Any,
) -> None:
    pk = before_the_weeks.configuration.pk
    MigrationExecutor(connection).migrate([("core", "0015_calendar_week_rules")])
    store_dates(pk, date(2027, 1, 4), None)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_bookingconfiguration SET every_weeks=2 WHERE id=%s", [pk]
        )
    with pytest.raises(RuntimeError, match="other than every week"):
        MigrationExecutor(connection).migrate(BEFORE)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_bookingconfiguration SET every_weeks=1 WHERE id=%s", [pk]
        )
    MigrationExecutor(connection).migrate(BEFORE)
    # Back before calendar weeks, the whole weeks stay and the column is gone.
    assert stored_dates(pk) == (date(2027, 1, 4), None)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE "
            "table_name='core_bookingconfiguration' AND column_name='every_weeks'"
        )
        assert cursor.fetchone() is None


@pytest.mark.django_db(transaction=True)
def test_a_refused_migration_leaves_the_table_as_it_was(
    before_the_weeks: Any,
) -> None:
    # It starts on Mondays: a Tuesday start would add a Monday.
    store_dates(before_the_weeks.configuration.pk, date(2027, 3, 2), None)
    with pytest.raises(RuntimeError):
        MigrationExecutor(connection).migrate([("core", "0014_calendar_weeks")])
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE "
            "table_name='core_bookingconfiguration' AND column_name='every_weeks'"
        )
        assert cursor.fetchone() is None
    # Dates that can move, so the fixture brings the table back up to date.
    store_dates(before_the_weeks.configuration.pk, None, None)
