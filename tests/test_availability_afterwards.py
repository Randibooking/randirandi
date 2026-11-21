# SPDX-License-Identifier: AGPL-3.0-only
"""what happens to a saved container when its asset's
availability later narrows, by a weekly change or by a later calendar.

The calendar, price and order apply availability coverage at run time.
These tests exercise direct model edits: existing bookings remain recorded.
The explicit administrator cancellation service is tested separately.
Made-up setups only.
"""

from datetime import datetime, time, timedelta

import pytest

from randirandi.core import validator
from randirandi.core.booking_quote import NOT_OFFERED, quote
from randirandi.core.member_calendar import member_calendar
from randirandi.core.models import (
    AvailabilityCalendar,
    AvailableSpan,
    Booking,
    BookingState,
    Order,
    TakenTime,
)
from randirandi.core.models.base import DAY as WHOLE_DAY
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from tests import made_up
from tests.test_calendar import DAY, Example, at, times
from tests.test_calendar import made as made

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))
# The wall moment the validator counts date periods from: before DAY.
SAVED = datetime.combine(DAY - timedelta(days=1), time())


def monday_night(made: Example) -> None:
    """A fixed night from Monday 17:00 to Tuesday 08:00, saved while the
    asset is available around the clock."""
    made.configure(
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        period_start_weekdays=[0],
    )


def spans(
    made: Example, monday: list[tuple[int, int]], tuesday: list[tuple[int, int]]
) -> None:
    made.available(monday, weekday=0)
    made.available(tuesday, weekday=1)


@pytest.mark.parametrize(
    ("monday", "tuesday", "offered"),
    [
        ([(17 * 60, WHOLE_DAY)], [(0, 8 * 60)], True),
        # The spans meet at midnight from two sides, or in one weekday's two.
        ([(17 * 60, 20 * 60), (20 * 60, WHOLE_DAY)], [(0, 9 * 60)], True),
        # One minute short at the end, one minute short at midnight, one late.
        ([(17 * 60, WHOLE_DAY)], [(0, 8 * 60 - 1)], False),
        ([(17 * 60, WHOLE_DAY - 1)], [(0, 8 * 60)], False),
        ([(17 * 60 + 1, WHOLE_DAY)], [(0, 8 * 60)], False),
        ([(17 * 60, WHOLE_DAY)], [(1, 8 * 60)], False),
    ],
    ids=["meeting", "joined", "early end", "gap at midnight", "late start", "late day"],
)
def test_a_night_is_offered_only_while_the_spans_meet_over_midnight(
    made: Example,
    monday: list[tuple[int, int]],
    tuesday: list[tuple[int, int]],
    offered: bool,
) -> None:
    monday_night(made)
    assert validator._uncovered(made.config, SAVED) is None
    assert len(made.offers()) == 1
    spans(made, monday, tuesday)
    # The calendar offers the date period exactly when the validator, asked
    # to save the same container now, finds no gap in it.
    gap = validator._uncovered(made.config, SAVED)
    assert (gap is None) is offered
    assert times(made.offers()) == (
        {(at(hour=17), at(DAY + timedelta(days=1), 8))} if offered else set()
    )


def test_a_narrowed_week_leaves_only_the_nights_it_touches_unoffered(
    made: Example,
) -> None:
    made.configure(
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        period_start_weekdays=list(range(7)),
    )
    assert all(len(made.offers(DAY + timedelta(days=n))) == 1 for n in range(-1, 3))
    # Monday is now open from 17:00 to 22:00 only.
    made.available([(17 * 60, 22 * 60)], weekday=0)
    # Monday night needs Monday until midnight; Sunday night needs Monday
    # morning. Tuesday night is untouched.
    assert made.offers(DAY) == ()
    assert made.offers(DAY - timedelta(days=1)) == ()
    assert len(made.offers(DAY + timedelta(days=1))) == 1


def test_a_later_calendar_removes_a_night_from_its_first_day(made: Example) -> None:
    made.configure(
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        period_start_weekdays=[0],
    )
    next_monday = DAY + timedelta(days=7)
    later = AvailabilityCalendar.objects.create(
        organization=made.org, asset=made.asset, start_date=next_monday
    )
    # From the next Monday the asset is open Monday evening and Tuesday
    # morning until 07:00, an hour short of the night.
    for weekday, first, last in ((0, 17 * 60, WHOLE_DAY), (1, 0, 7 * 60)):
        AvailableSpan.objects.create(
            organization=made.org,
            availability_calendar=later,
            weekday=weekday,
            start_time=first,
            end_time=last,
        )
    assert len(made.offers(DAY)) == 1
    assert made.offers(next_monday) == ()
    assert validator._uncovered(made.config, SAVED) is not None


def test_a_flexible_container_is_cut_to_the_hours_that_stay_open(
    made: Example,
) -> None:
    assert len(made.offers()) == 10
    made.available([(10 * 60 + 30, 13 * 60 + 30)], weekday=DAY.weekday())
    # Cut, not dropped: only the starts and lengths inside 10:30 to 13:30.
    assert times(made.offers()) == {
        (at(hour=11), at(hour=12)),
        (at(hour=11), at(hour=13)),
        (at(hour=12), at(hour=13)),
    }
    # The same container, saved new, would be refused: its period is not
    # covered whole. Run time cuts, the validator refuses.
    assert validator._uncovered(made.config, SAVED) is not None
    # Other weekdays are untouched.
    assert len(made.offers(DAY + timedelta(days=1))) == 10


def test_until_m7_an_availability_change_cancels_nothing(made: Example) -> None:
    first = BookingRequest(made.config.pk, made.category.pk, at(hour=10), at(hour=11))
    placed = create_order(
        member=made.member,
        requests=[first],
        now=NOW,
        total=100,
        accepted_version=made_up.accepted(made.member),
    )
    before = (
        Order.objects.of(made.org).values_list("pk", "total", "hold_expires_at").get(),
        list(TakenTime.objects.of(made.org).values_list("pk", "state")),
        Booking.objects.of(made.org).count(),
    )
    assert before[1][0][1] == BookingState.HELD

    # The administrator closes the asset from 10:00 to 12:00 on every day.
    made.available([(12 * 60, 14 * 60)])

    # Nothing is cancelled or refunded: the order, its time and its booking
    # are as they were.
    after = (
        Order.objects.of(made.org).values_list("pk", "total", "hold_expires_at").get(),
        list(TakenTime.objects.of(made.org).values_list("pk", "state")),
        Booking.objects.of(made.org).count(),
    )
    assert after == before
    assert Order.objects.of(made.org).get().pk == placed.pk

    # It is drawn as taken, with no name, in the removed time.
    found = member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        length=60,
        start_date=DAY,
        end_date=DAY + timedelta(days=1),
        now=NOW,
    )
    ((row,),) = [calendar.periods for calendar in found.containers]
    assert [(span.state, span.first, span.last) for span in row.taken] == [
        ("held", 0, 0)
    ]
    assert row.rows[0] == at(hour=10)

    # Nothing new is sold in the removed time: not offered, priced as not
    # offered, refused by the order; the time that stays open still sells.
    offered = {start.row for start in row.starts}
    assert 1 not in offered and 0 not in offered and 2 in offered

    def priced(hour: int) -> object:
        return quote(
            member=made.member,
            configuration_id=made.config.pk,
            booking_category_id=made.category.pk,
            quantity=1,
            starts_at=at(hour=hour),
            ends_at=at(hour=hour + 1),
            now=NOW,
        )

    assert priced(11) == NOT_OFFERED
    assert priced(12).reason is None  # type: ignore[attr-defined]
    second = BookingRequest(made.config.pk, made.category.pk, at(hour=11), at(hour=12))
    with pytest.raises(OrderUnavailableError):
        create_order(
            member=made.member,
            requests=[second],
            now=NOW,
            total=100,
            accepted_version=made_up.accepted(made.member),
        )
    assert Order.objects.of(made.org).count() == 1
