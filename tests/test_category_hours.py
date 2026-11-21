# SPDX-License-Identifier: AGPL-3.0-only
"""A booking category's own hours on a container .

A container may give one of its booking categories weekday spans inside its
hours. A booking of that category lies wholly inside them; a category without
spans has the container's hours. Fjordside's work hire (container 2 of
`setups/fjordside.toml`) runs to 22:00 Monday to Thursday and to 17:00 on
Friday, while private hire runs to 22:00 all week. The calendar, the quote, the cart,
the order and the validator all ask one function (`calendar.category_allows`).
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi import setup_file
from randirandi.core import booking_cart, booking_words, calendar, setup, validator
from randirandi.core.booking_cart import CartRow, Refusal
from randirandi.core.booking_quote import Choice, Quote, quote
from randirandi.core.container_admin import ContainerRefusedError
from randirandi.core.member_calendar import ContainerCalendar
from randirandi.core.models import (
    Asset,
    BookingCategory,
    BookingConfiguration,
    CategoryHoursSpan,
    Member,
    MemberStatus,
    Order,
    Organization,
    Origin,
    Role,
)
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from randirandi.setup_file import SetupError, parse
from tests import made_up
from tests.test_booking_scenarios import NOW, Journey, loaded
from tests.test_calendar import DAY, ZONE, at, scenario
from tests.test_member_calendar import ask
from tests.test_setup_file import FILES, SETUPS
from tests.test_stacked_containers import loaded_text, replaced

pytestmark = pytest.mark.django_db(transaction=True)
FRIDAY = DAY + timedelta(days=4)
MONDAY_TO_THURSDAY = [(day, 10 * 60, 22 * 60) for day in range(4)]
FRIDAY_TO_17 = (4, 10 * 60, 17 * 60)
FJORDSIDE = (SETUPS / "fjordside.toml").read_text()
THE_HOURS = (
    "category_hours = [\n"
    '  { booking_category = "Work", weekdays = ["mon", "tue", "wed", "thu"], '
    'start = "10:00", end = "22:00" },\n'
    '  { booking_category = "Work", weekdays = ["fri"], '
    'start = "10:00", end = "17:00" },\n'
    "]\n"
)
SCENARIOS = sorted((SETUPS / "scenarios").glob("*.toml"))


def without_the_hours() -> str:
    return replaced(FJORDSIDE, THE_HOURS, "")


def category(org: Organization, name: str) -> BookingCategory:
    return BookingCategory.objects.of(org).get(name=name)


def container(org: Organization, number: int) -> BookingConfiguration:
    return BookingConfiguration.objects.of(org).get(number=number)


def stored_hours(
    org: Organization, number: int, name: str
) -> list[tuple[int, int, int]]:
    return sorted(
        CategoryHoursSpan.objects.of(org)
        .filter(
            configuration_category__booking_configuration__number=number,
            configuration_category__booking_category__name=name,
        )
        .values_list("weekday", "start_time", "end_time")
    )


def row_of(found: Sequence[ContainerCalendar], number: int) -> ContainerCalendar:
    return next(row for row in found if row.number == number)


def start_hours(
    journey: Journey,
    number: int,
    name: str | None,
    *,
    length: int = 60,
    day: date = FRIDAY,
) -> set[int]:
    """The local hours the calendar offers bookings of a container to start
    at, for the category asked for (all the container's starts when None)."""
    found = ask(
        journey.org,
        journey.member,
        asset="Hall",
        kind=container(journey.org, number).booking_type.name,
        category=name,
        length=length,
        day=day,
    ).containers
    row = row_of(found, number)
    # A date period that starts the day before comes whole: leave its starts.
    moments = [
        period.rows[start.row].astimezone(ZONE)
        for period in row.periods
        for start in period.starts
    ]
    return {moment.hour for moment in moments if moment.date() == day}


def request(
    journey: Journey, number: int, start: datetime, end: datetime, name: str
) -> BookingRequest:
    return journey.request(number, start, end, category=name)


def priced(journey: Journey, wanted: BookingRequest) -> Quote:
    return quote(
        member=journey.member,
        configuration_id=wanted.configuration_id,
        booking_category_id=wanted.booking_category_id,
        quantity=wanted.quantity,
        starts_at=wanted.starts_at,
        ends_at=wanted.ends_at,
        now=NOW,
    )


def ordered(journey: Journey, wanted: BookingRequest) -> Order:
    return create_order(
        member=journey.member,
        requests=[wanted],
        now=NOW,
        accepted_version=made_up.accepted(journey.member),
    )


def spans_of(
    journey: Journey, number: int, name: str, day: date
) -> tuple[tuple[datetime, datetime], ...]:
    """The spans the calendar answer names for a category in the container's
    date period that starts on `day` (part 2.99)."""
    found = ask(
        journey.org,
        journey.member,
        asset="Hall",
        kind=container(journey.org, number).booking_type.name,
        length=60,
        day=day,
    ).containers
    (period,) = (item for item in row_of(found, number).periods if item.day == day)
    wanted = category(journey.org, name).pk
    (spans,) = (
        item.spans for item in period.category_hours if item.category_id == wanted
    )
    return spans


# --- No spans: the container's hours -------------------------------------------


# The setups without hours of a category's own.
OTHERS = [
    path for path in FILES + SCENARIOS if "category_hours" not in path.read_text()
]


@pytest.mark.parametrize("path", OTHERS, ids=lambda path: path.stem)
def test_a_setup_without_category_hours_answers_the_containers_hours(
    path: Path,
) -> None:
    org, member = scenario(str(path.relative_to(SETUPS)))
    assert CategoryHoursSpan.objects.of(org).count() == 0
    for asset in Asset.objects.of(org):
        data = calendar._read(member, asset, None, DAY, DAY + timedelta(days=1), NOW)
        if data is not None:
            assert data.category_hours == {}


def test_without_the_hours_work_runs_to_22_all_week_like_private() -> None:
    with loaded_text(without_the_hours()) as journey:
        assert CategoryHoursSpan.objects.of(journey.org).count() == 0
        assert max(start_hours(journey, 2, "Work")) == 21
        assert start_hours(journey, 2, "Work") == start_hours(journey, 2, "Private")


# --- Friday, container 2 of fjordside.toml ------------------------------------


def test_the_calendar_ends_work_on_friday_at_17_and_private_at_22() -> None:
    with loaded("fjordside.toml") as journey:
        # An hour of Work starts at 16:00 at the latest, three hours at 14:00.
        assert max(start_hours(journey, 2, "Work")) == 16
        assert max(start_hours(journey, 2, "Work", length=180)) == 14
        assert min(start_hours(journey, 2, "Work")) == 10
        # Private is still to 21:00 for an hour, 19:00 for three.
        assert max(start_hours(journey, 2, "Private")) == 21
        assert max(start_hours(journey, 2, "Private", length=180)) == 19
        # With no category asked for, the starts are the container's.
        assert max(start_hours(journey, 2, None)) == 21
        # Tuesday Work is to 21:00.
        assert max(start_hours(journey, 2, "Work", day=DAY + timedelta(days=1))) == 21
        assert max(start_hours(journey, 2, "Work", day=DAY)) == 21


def test_the_calendar_names_the_spans_of_a_category_with_hours_of_its_own() -> None:
    with loaded("fjordside.toml") as journey:
        work = category(journey.org, "Work").pk
        for day, last in ((FRIDAY, 17), (DAY, 22)):
            found = ask(
                journey.org,
                journey.member,
                asset="Hall",
                kind="Hall hire",
                length=60,
                day=day,
            ).containers
            (period,) = row_of(found, 2).periods
            # Only the category with hours of its own is named, in moments.
            assert [
                (item.category_id, item.spans) for item in period.category_hours
            ] == [(work, ((at(day, 10), at(day, last)),))]


def test_work_on_friday_is_refused_by_the_quote_the_cart_and_the_order() -> None:
    with loaded("fjordside.toml") as journey:
        late = request(journey, 2, at(FRIDAY, 16), at(FRIDAY, 18), "Work")
        assert priced(journey, late) == Quote(booking_words.NOT_OFFERED)
        session = SessionStore()
        choice = Choice(
            late.configuration_id,
            late.booking_category_id,
            1,
            late.starts_at,
            late.ends_at,
        )
        assert booking_cart.add(
            session, journey.member, choice, 100000, NOW
        ) == Refusal(booking_words.NOT_OFFERED)
        before = journey.stored()
        with pytest.raises(OrderUnavailableError):
            ordered(journey, late)
        assert journey.stored() == before
        # Private takes the same time.
        evening = request(journey, 2, at(FRIDAY, 16), at(FRIDAY, 18), "Private")
        assert priced(journey, evening).reason is None
        assert ordered(journey, evening).bookings.get().taken_time.ends_at == at(
            FRIDAY, 18
        )


def test_work_on_friday_ending_at_17_is_sold_by_the_quote_the_cart_and_the_order() -> (
    None
):
    with loaded("fjordside.toml") as journey:
        early = request(journey, 2, at(FRIDAY, 14), at(FRIDAY, 17), "Work")
        found = priced(journey, early)
        assert found.reason is None
        session = SessionStore()
        choice = Choice(
            early.configuration_id,
            early.booking_category_id,
            1,
            early.starts_at,
            early.ends_at,
        )
        assert isinstance(
            booking_cart.add(session, journey.member, choice, found.total, NOW), CartRow
        )
        booking = ordered(journey, early).bookings.get()
        assert booking.taken_time.ends_at == at(FRIDAY, 17)
        assert booking.booking_category_name == "Work"
        # Monday to Thursday Work still runs to 22:00.
        evening = request(journey, 2, at(DAY, 19), at(DAY, 22), "Work")
        assert priced(journey, evening).reason is None
        assert ordered(journey, evening).bookings.count() == 1


# --- The validator's service ----------------------------------------------------


def test_hours_outside_the_containers_hours_are_refused() -> None:
    with loaded("fjordside.toml") as journey:
        org, work = journey.org, category(journey.org, "Work")
        for hours, reason in (
            ([(0, 9 * 60, 12 * 60)], "is not inside the hours of container"),
            ([(5, 10 * 60, 12 * 60)], "is not inside the hours of container"),
            ([(4, 10 * 60, 23 * 60)], "is not inside the hours of container"),
            ([(4, 10 * 60, 12 * 60), (4, 11 * 60, 13 * 60)], "share time"),
            ([(7, 10 * 60, 12 * 60)], "is not a stretch of one weekday"),
            ([(4, 12 * 60, 12 * 60)], "is not a stretch of one weekday"),
        ):
            with pytest.raises(ValidationError, match=reason):
                validator.set_category_hours(container(org, 2), work, hours, now=NOW)
        # Nothing was replaced.
        assert stored_hours(org, 2, "Work") == [*MONDAY_TO_THURSDAY, FRIDAY_TO_17]


def test_a_category_the_container_does_not_name_is_refused() -> None:
    with loaded("fjordside.toml") as journey:
        with pytest.raises(ValidationError, match="does not name Work"):
            validator.set_category_hours(
                container(journey.org, 3),
                category(journey.org, "Work"),
                [(5, 14 * 60, 18 * 60)],
                now=NOW,
            )
        assert CategoryHoursSpan.objects.of(journey.org).count() == 5


def test_a_change_cannot_leave_a_booking_to_come_outside_the_new_hours() -> None:
    with loaded("fjordside.toml") as journey:
        org, work = journey.org, category(journey.org, "Work")
        booked = ordered(
            journey, request(journey, 2, at(FRIDAY, 15), at(FRIDAY, 17), "Work")
        )
        shorter = [*MONDAY_TO_THURSDAY, (4, 10 * 60, 15 * 60)]
        with pytest.raises(ValidationError, match=f"order {booked.reference}"):
            validator.set_category_hours(container(org, 2), work, shorter, now=NOW)
        assert stored_hours(org, 2, "Work") == [*MONDAY_TO_THURSDAY, FRIDAY_TO_17]
        # A change that keeps the booking inside is accepted.
        validator.set_category_hours(
            container(org, 2),
            work,
            [*MONDAY_TO_THURSDAY, (4, 12 * 60, 17 * 60)],
            now=NOW,
        )
        # After the booking ends, it no longer holds the hours.
        validator.set_category_hours(
            container(org, 2), work, shorter, now=at(FRIDAY, 18)
        )
        assert stored_hours(org, 2, "Work") == sorted(shorter)


def test_the_containers_hours_cannot_leave_a_categorys_own_hours_outside() -> None:
    with loaded("fjordside.toml") as journey:
        org, work = journey.org, category(journey.org, "Work")
        weekdays = container(org, 2)
        weekdays.period_start_weekdays = [0, 1, 2, 3]
        with pytest.raises(
            ValidationError,
            match="Work Friday 10:00 to 17:00 is not inside the hours of container",
        ):
            weekdays.save(now=NOW)
        assert container(org, 2).period_start_weekdays == [0, 1, 2, 3, 4]
        # With Work's Friday taken out first, the container may drop Friday.
        validator.set_category_hours(weekdays, work, MONDAY_TO_THURSDAY, now=NOW)
        weekdays.save(now=NOW)
        assert container(org, 2).period_start_weekdays == [0, 1, 2, 3]


# A second container on the Hall, stacked on Hall hire (2.97), that names Work
# without hours of its own.
STACKED = """
[[booking_configurations]]
number = 5
asset = "Hall"
booking_type = "Hall hire"
period_type = "flexible"
period_start = "10:00"
period_end = "22:00"
end_day = 0
period_start_weekdays = ["mon", "tue", "wed", "thu", "fri"]
interval = "1 h"
minimum_length = "1 h"
maximum_length = "12 h"
status = "enabled"
price_book = "Standard"
booking_categories = ["Work"]
"""
STACKED_PRICE = """
[[price_books.entries]]
booking_configuration = 5
booking_category = "Work"
tiers_go_by = "length"
tiers = [{ up_to = "12 h", paid_per = "1 h", amount = 500 }]
"""


def test_a_categorys_hours_hold_on_their_own_container_only() -> None:
    text = replaced(FJORDSIDE, "\n[[price_books]]\n", STACKED + "\n[[price_books]]\n")
    with loaded_text(text + STACKED_PRICE) as journey:
        # Work stops at 17:00 on Friday on container 2, not on container 5.
        assert max(start_hours(journey, 2, "Work")) == 16
        assert max(start_hours(journey, 5, "Work")) == 21
        evening = request(journey, 5, at(FRIDAY, 18), at(FRIDAY, 20), "Work")
        assert priced(journey, evening).reason is None
        on_two = request(journey, 2, at(FRIDAY, 18), at(FRIDAY, 20), "Work")
        assert priced(journey, on_two).reason == booking_words.NOT_OFFERED
        # Sold on container 5, the time is taken on container 2 as well.
        ordered(journey, evening)
        private = start_hours(journey, 2, "Private")
        assert 17 in private and not {18, 19} & private


def test_none_gives_the_category_the_containers_hours_again() -> None:
    with loaded("fjordside.toml") as journey:
        org, work = journey.org, category(journey.org, "Work")
        late = request(journey, 2, at(FRIDAY, 16), at(FRIDAY, 18), "Work")
        assert priced(journey, late).reason == booking_words.NOT_OFFERED
        validator.set_category_hours(container(org, 2), work, [], now=NOW)
        assert stored_hours(org, 2, "Work") == []
        assert priced(journey, late).reason is None
        assert ordered(journey, late).bookings.get().taken_time.ends_at == at(
            FRIDAY, 18
        )


def test_removing_a_category_from_a_container_is_refused_while_booked() -> None:
    # The owner decided it is refused while a booking of it is still to come
    # Container editing applies this check, which every
    # change on the screens goes through. The category's hours stay.
    with loaded("fjordside.toml") as journey:
        org = journey.org
        administrator = Member.objects.create(
            organization=org,
            role=Role.objects.of(org).get(name="Admin"),
            full_name="Sam Field",
            first_name="Sam",
            last_name="Field",
            email="sam@example.com",
            phone="+4790000002",
            status=MemberStatus.ACTIVE,
            origin=Origin.ADMINISTRATOR,
        )
        ordered(journey, request(journey, 2, at(FRIDAY, 13), at(FRIDAY, 15), "Work"))
        kept = [
            {"category": category(org, name).pk} for name in ("Private", "Internal")
        ]
        with pytest.raises(ContainerRefusedError) as refusal:
            made_up.change_categories_as_seen(
                administrator, container(org, 2).pk, {"categories": kept}, now=NOW
            )
        assert (
            "Work stays while the container has a booking"
            in (refusal.value.reasons[0]["text"])
        )
        assert stored_hours(org, 2, "Work") != []


# --- Over midnight ---------------------------------------------------------------


def overnight(second: str) -> str:
    """Container 1 (the night from 22:00 to 10:00 on Sunday to Thursday) gives
    Private Monday's evening and the next morning to `second`."""
    return replaced(
        FJORDSIDE,
        'end_day = 1\nperiod_start_weekdays = ["sun", "mon", "tue", "wed", "thu"]\n'
        'status = "enabled"\nprice_book = "Standard"\n'
        'booking_categories = ["Private", "Internal"]\n',
        'end_day = 1\nperiod_start_weekdays = ["sun", "mon", "tue", "wed", "thu"]\n'
        'status = "enabled"\nprice_book = "Standard"\n'
        'booking_categories = ["Private", "Internal"]\n'
        "category_hours = [\n"
        '  { booking_category = "Private", weekdays = ["mon"], '
        'start = "22:00", end = "24:00" },\n'
        '  { booking_category = "Private", weekdays = ["tue"], '
        f'start = "00:00", end = "{second}" }},\n'
        "]\n",
    )


def test_a_booking_across_midnight_needs_the_spans_of_both_days_to_meet() -> None:
    night = (at(DAY, 22), at(DAY + timedelta(days=1), 10))
    with loaded_text(overnight("10:00")) as journey:
        monday = request(journey, 1, *night, "Private")
        assert priced(journey, monday).reason is None
        assert 22 in start_hours(journey, 1, "Private", day=DAY)
        # The answer names Monday's night as one span across midnight.
        assert spans_of(journey, 1, "Private", DAY) == (night,)
        # Tuesday evening has no span: closed to Private.
        tuesday = request(
            journey,
            1,
            at(DAY + timedelta(days=1), 22),
            at(DAY + timedelta(days=2), 10),
            "Private",
        )
        assert priced(journey, tuesday).reason == booking_words.NOT_OFFERED
        assert start_hours(journey, 1, "Private", day=DAY + timedelta(days=1)) == set()
        assert spans_of(journey, 1, "Private", DAY + timedelta(days=1)) == ()
        assert ordered(journey, monday).bookings.get().taken_time.ends_at == night[1]
        with pytest.raises(OrderUnavailableError):
            ordered(journey, tuesday)


def test_spans_of_both_days_that_stop_short_of_each_other_sell_no_night() -> None:
    night = (at(DAY, 22), at(DAY + timedelta(days=1), 10))
    with loaded_text(overnight("08:00")) as journey:
        # The morning span stops at 08:00: the two days' spans do not meet to 10:00.
        monday = request(journey, 1, *night, "Private")
        assert priced(journey, monday).reason == booking_words.NOT_OFFERED
        assert start_hours(journey, 1, "Private", day=DAY) == set()
        with pytest.raises(OrderUnavailableError):
            ordered(journey, monday)


def test_hours_of_a_night_must_lie_inside_the_nights_window() -> None:
    with loaded("fjordside.toml") as journey:
        private = category(journey.org, "Private")
        night = container(journey.org, 1)
        # Tuesday 00:00 to 10:00 is inside the night that started on Monday.
        validator.set_category_hours(
            night, private, [(0, 22 * 60, 24 * 60), (1, 0, 10 * 60)], now=NOW
        )
        with pytest.raises(ValidationError, match="is not inside the hours"):
            validator.set_category_hours(night, private, [(1, 0, 11 * 60)], now=NOW)
        with pytest.raises(ValidationError, match="is not inside the hours"):
            validator.set_category_hours(
                night, private, [(4, 22 * 60, 24 * 60)], now=NOW
            )


# --- A day the clocks change -----------------------------------------------------


SUNDAYS = [date(2027, 3, 28), date(2027, 10, 31)]


def sundays_private() -> str:
    # The second Sunday lies beyond the setup's six months ahead :
    # this test is about hours, not about how far ahead.
    return replaced(
        replaced(FJORDSIDE, "book_ahead_months = 6\n", ""),
        'maximum_length = "12 h"\nstatus = "enabled"\nprice_book = "Standard"\n'
        'booking_categories = ["Private", "Internal"]\n',
        'maximum_length = "12 h"\nstatus = "enabled"\nprice_book = "Standard"\n'
        'booking_categories = ["Private", "Internal"]\n'
        "category_hours = [\n"
        '  { booking_category = "Private", weekdays = ["sun"], '
        'start = "14:00", end = "18:00" },\n'
        "]\n",
    )


@pytest.mark.parametrize("sunday", SUNDAYS, ids=lambda day: day.isoformat())
def test_the_calendar_and_the_orphan_check_answer_the_same_local_hours(
    sunday: date,
) -> None:
    with loaded_text(sundays_private()) as journey:
        org, private = journey.org, category(journey.org, "Private")
        assert start_hours(journey, 3, "Private", day=sunday) == {14, 15, 16, 17}
        assert spans_of(journey, 3, "Private", sunday) == (
            (at(sunday, 14), at(sunday, 18)),
        )
        last = request(journey, 3, at(sunday, 17), at(sunday, 18), "Private")
        assert priced(journey, last).reason is None
        after = request(journey, 3, at(sunday, 18), at(sunday, 19), "Private")
        assert priced(journey, after).reason == booking_words.NOT_OFFERED
        ordered(journey, last)
        with pytest.raises(ValidationError, match="would lie outside them"):
            validator.set_category_hours(
                container(org, 3), private, [(6, 14 * 60, 17 * 60)], now=NOW
            )
        validator.set_category_hours(
            container(org, 3), private, [(6, 14 * 60, 18 * 60)], now=NOW
        )


# --- The setup file -----------------------------------------------------------------


def test_fjordside_loads_with_its_category_hours_and_exports_the_same_hours() -> None:
    org, _ = scenario("fjordside.toml")
    assert stored_hours(org, 2, "Work") == [*MONDAY_TO_THURSDAY, FRIDAY_TO_17]
    assert CategoryHoursSpan.objects.of(org).count() == 5
    exported = setup.export(org)
    written = {
        item.number: item.category_hours for item in exported.booking_configurations
    }
    assert written[1] == written[3] == written[4] == ()
    assert written[2] == parse(FJORDSIDE).booking_configurations[1].category_hours
    # The file reads back as the same setup, and says the hours as the file does.
    text = setup_file.write(exported)
    assert parse(text).booking_configurations == exported.booking_configurations
    assert THE_HOURS in text
    # The file as it is in the repository says them the same way, from format 1.
    assert THE_HOURS in FJORDSIDE
    assert setup_file.write(parse(FJORDSIDE)).count(THE_HOURS) == 1


def line(name: str, day: str, start: str, end: str) -> str:
    return (
        f'{{ booking_category = "{name}", weekdays = ["{day}"], '
        f'start = "{start}", end = "{end}" }},'
    )


@pytest.mark.parametrize(
    ("rows", "fault"),
    [
        (
            [
                line("Internal", "mon", "10:00", "11:00"),
                line("Private", "sat", "10:00", "11:00"),
            ],
            "category_hours[2]: sat 10:00 to 11:00 is not inside",
        ),
        (
            [line("Work", "mon", "09:00", "12:00")],
            "category_hours[1]: mon 09:00 to 12:00 is not inside",
        ),
        (
            [line("lodging", "mon", "10:00", "12:00")],
            "category_hours[1].booking_category: configuration 2 does not apply to "
            "'lodging'",
        ),
        (
            [
                line("Work", "mon", "10:00", "13:00"),
                line("Work", "mon", "12:00", "14:00"),
            ],
            "'Work' has mon 10:00 to 13:00 and 12:00 to 14:00, which share time",
        ),
        (
            [line("Work", "mon", "12:00", "11:00")],
            "category_hours[1].end: must be later than the start",
        ),
        ([line("Work", "monday", "10:00", "11:00")], "category_hours[1].weekdays"),
    ],
)
def test_a_bad_category_hours_row_is_refused_with_the_row_named(
    rows: list[str], fault: str
) -> None:
    text = replaced(
        FJORDSIDE, THE_HOURS, "category_hours = [\n  " + "\n  ".join(rows) + "\n]\n"
    )
    with pytest.raises(SetupError) as refused:
        parse(text)
    assert any(fault in line for line in refused.value.faults), refused.value.faults


def test_a_setup_built_in_code_is_checked_as_a_file_is() -> None:
    built = parse(FJORDSIDE)
    items = list(built.booking_configurations)
    items[1] = replace(
        items[1], category_hours=(setup_file.CategoryHours("Work", "sat", 600, 660),)
    )
    with pytest.raises(SetupError) as refused:
        setup_file.validate(replace(built, booking_configurations=tuple(items)))
    assert any("category_hours[1]" in line for line in refused.value.faults)


# --- The migration -------------------------------------------------------------------

BEFORE = [("core", "0021_member_list_layouts")]
TABLE = "core_categoryhoursspan"


def tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_the_migration_makes_the_table_and_refuses_a_reverse_that_loses_hours() -> None:
    try:
        MigrationExecutor(connection).migrate(BEFORE)
        assert TABLE not in tables()
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())
        assert TABLE in tables()
        made = made_up.with_category_hours(made_up.everything())
        with pytest.raises(RuntimeError, match="Cannot reverse while a container"):
            MigrationExecutor(connection).migrate(BEFORE)
        # The refusal rolled the migration back whole.
        assert TABLE in tables()
        assert CategoryHoursSpan.objects.of(made.organization).count() == 1
        CategoryHoursSpan.objects.of(made.organization).delete()
        MigrationExecutor(connection).migrate(BEFORE)
        assert TABLE not in tables()
    finally:
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())
