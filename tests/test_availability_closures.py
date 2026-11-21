# SPDX-License-Identifier: AGPL-3.0-only
"""Closures by date at run time and in the model : the offer
treats a closed local date as closed, for fixed and flexible periods, over
midnight and in the asset's time zone; the public holidays of two years; the
rules the database holds itself."""

from datetime import date, timedelta

import pytest
from django.db import IntegrityError, connection, transaction

from randirandi.core.models import AvailabilityClosure
from randirandi.core.models.bookable import ClosureReason
from randirandi.core.ordering import OrderUnavailableError
from randirandi.core.public_holidays import easter, holidays_of
from randirandi.setup_file import CLOSURE_REASONS
from tests.test_calendar import DAY, Example, at, times
from tests.test_calendar import made as made
from tests.test_ordering import checkout, request

pytestmark = pytest.mark.django_db(transaction=True)
NEXT = DAY + timedelta(days=1)
SUNDAY = DAY - timedelta(days=1)


def close(made: Example, first: date, last: date | None = None) -> None:
    AvailabilityClosure.objects.create(
        organization=made.org,
        asset=made.asset,
        first_date=first,
        last_date=last or first,
        reason="",
    )


def test_a_closed_date_has_no_offers_and_the_next_day_is_untouched(
    made: Example,
) -> None:
    before = times(made.offers())
    assert before
    close(made, DAY)
    assert made.offers() == ()
    close(made, DAY + timedelta(days=30))
    assert made.offers() == ()


def test_a_closure_elsewhere_changes_nothing(made: Example) -> None:
    before = times(made.offers())
    close(made, DAY + timedelta(days=1), DAY + timedelta(days=9))
    close(made, SUNDAY - timedelta(days=9), SUNDAY)
    assert times(made.offers()) == before


def test_a_closed_date_refuses_the_booking_too(made: Example) -> None:
    close(made, DAY)
    with pytest.raises(OrderUnavailableError):
        checkout(made, request(made, starts_at=at(DAY, 10), ends_at=at(DAY, 12)))


@pytest.mark.parametrize(
    ("first", "last", "offered"),
    [
        (SUNDAY, SUNDAY, True),
        (DAY, DAY, False),
        (NEXT, NEXT, False),
        (NEXT + timedelta(days=1), NEXT + timedelta(days=2), True),
        (SUNDAY, DAY, False),
    ],
)
def test_a_fixed_night_over_midnight_needs_both_dates_open(
    made: Example, first: date, last: date, offered: bool
) -> None:
    made.configure(
        period_type="fixed", period_start=17 * 60, period_end=8 * 60, end_day=1
    )
    made.available([(17 * 60, 1440)], weekday=0)
    made.available([(0, 8 * 60)], weekday=1)
    assert times(made.offers()) == {(at(hour=17), at(NEXT, 8))}
    close(made, first, last)
    assert bool(made.offers()) is offered


def test_a_flexible_period_is_cut_by_the_closure_of_the_next_date(
    made: Example,
) -> None:
    made.configure(period_start=22 * 60, period_end=2 * 60, end_day=1)
    made.available([(22 * 60, 1440)], weekday=0)
    made.available([(0, 2 * 60)], weekday=1)
    whole = times(made.offers())
    assert (at(hour=22), at(NEXT, 2)) in whole
    close(made, NEXT)
    after = times(made.offers())
    assert after and all(end <= at(NEXT, 0) for _, end in after)
    assert (at(hour=22), at(NEXT, 2)) not in after


def test_the_date_is_the_assets_local_date_not_utc(made: Example) -> None:
    """00:00 in Oslo is 23:00 UTC of the day before: a closure of the day
    before in Oslo does not close it."""
    made.configure(
        period_start=22 * 60,
        period_end=2 * 60,
        end_day=1,
        start_date=SUNDAY - timedelta(days=6),
        stop_date=SUNDAY,
        period_start_weekdays=[6],
    )
    whole = times(made.offers())
    assert (at(hour=0), at(hour=1)) in whole
    close(made, SUNDAY)
    assert (at(hour=0), at(hour=1)) in times(made.offers())
    close(made, DAY)
    assert made.offers() == ()


def test_the_holidays_of_two_years_include_easter_and_merge_a_shared_day() -> None:
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)
    names = {year: dict(holidays_of(year)) for year in (2026, 2027)}
    assert names[2026][date(2026, 4, 2)] == "Maundy Thursday"
    assert names[2026][date(2026, 4, 3)] == "Good Friday"
    assert names[2026][date(2026, 4, 6)] == "Easter Monday"
    assert names[2026][date(2026, 5, 14)] == "Ascension Day"
    assert names[2027][date(2027, 3, 25)] == "Maundy Thursday"
    assert names[2027][date(2027, 3, 26)] == "Good Friday"
    assert names[2027][date(2027, 3, 29)] == "Easter Monday"
    assert names[2027][date(2027, 5, 17)] == "Constitution Day, Whit Monday"
    assert len(names[2026]) == 12 and len(names[2027]) == 11
    for year in (2026, 2027):
        listed = [day for day, _ in holidays_of(year)]
        assert listed == sorted(set(listed))
        assert listed[0] == date(year, 1, 1) and listed[-1] == date(year, 12, 26)
    with pytest.raises(ValueError):
        easter(1899)


def test_closure_reasons_are_the_models(made: Example) -> None:
    assert set(CLOSURE_REASONS) == set(ClosureReason.values)


@pytest.mark.parametrize(
    ("first", "last", "reason"),
    [
        ("2027-03-02", "2027-03-01", ""),
        ("1999-12-31", "2027-03-01", ""),
        ("2027-03-01", "2101-01-01", ""),
        ("2027-03-01", "2027-03-01", "party"),
    ],
)
def test_the_database_refuses_a_bad_closure_beneath_the_model(
    made: Example, first: str, last: str, reason: str
) -> None:
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO core_availabilityclosure "
            "(organization_id, asset_id, first_date, last_date, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            [made.org.pk, made.asset.pk, first, last, reason],
        )


def test_a_closure_cannot_name_another_organizations_asset(made: Example) -> None:
    from tests import made_up

    elsewhere = made_up.everything("elsewhere")
    with pytest.raises(IntegrityError), transaction.atomic():
        AvailabilityClosure.objects.create(
            organization=made.org,
            asset=elsewhere.asset,
            first_date=DAY,
            last_date=DAY,
        )
