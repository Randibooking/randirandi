# SPDX-License-Identifier: AGPL-3.0-only
"""The objects of docs/data-model.md as tables: the migrations, the numbers and
the checks a booking configuration makes on itself.
"""

from datetime import date, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from randirandi.core.models import (
    Asset,
    BookingConfiguration,
    Order,
    Organization,
    PeriodType,
    PriceBook,
    Status,
    Weekday,
)
from tests import made_up


@pytest.mark.django_db
def test_the_migrations_say_what_the_objects_say() -> None:
    out = StringIO()

    call_command("makemigrations", "--check", "--dry-run", stdout=out)

    assert "No changes detected" in out.getvalue()


# --- Numbers ------------------------------------------------------------------


@pytest.mark.django_db
def test_a_number_counts_per_organization_and_per_kind() -> None:
    ours = made_up.everything("harbourview")
    theirs = made_up.everything("lakeside", made_up.A_DAY + timedelta(days=7))

    second = Asset.objects.create(organization=ours.organization, name="Guest room")

    assert (ours.asset.number, second.number) == (1, 2)
    assert theirs.asset.number == 1
    assert (ours.configuration.number, ours.price_book.number) == (1, 1)
    assert (ours.order.number, ours.spare_order.number) == (1, 2)


@pytest.mark.django_db
def test_a_number_is_never_given_twice() -> None:
    made = made_up.everything()
    gone = Asset.objects.create(organization=made.organization, name="Guest room")
    assert gone.number == 2
    gone.delete()

    after = Asset.objects.create(organization=made.organization, name="Laundry room")

    assert after.number == 3


@pytest.mark.django_db
def test_a_number_stays_when_the_object_is_saved_again() -> None:
    made = made_up.everything()

    made.asset.name = "Great hall"
    made.asset.save()

    assert Asset.objects.of(made.organization).get().number == 1


@pytest.mark.django_db
def test_a_number_written_out_is_taken_when_it_is_above_those_given() -> None:
    # A setup file writes its numbers out.
    made = made_up.everything()

    seventh = PriceBook.objects.create(
        organization=made.organization, name="Summer 2027", number=7
    )
    next_one = PriceBook.objects.create(
        organization=made.organization, name="Winter 2027"
    )

    assert (seventh.number, next_one.number) == (7, 8)


@pytest.mark.django_db
@pytest.mark.parametrize("number", [0, 1])
def test_a_number_written_out_is_refused_when_it_has_been_given(number: int) -> None:
    made = made_up.everything()

    with pytest.raises(ValidationError, match="has been given before"):
        PriceBook.objects.create(
            organization=made.organization, name="Summer 2027", number=number
        )


@pytest.mark.django_db
def test_two_objects_of_one_kind_cannot_share_a_number() -> None:
    made = made_up.everything()

    with pytest.raises(IntegrityError), transaction.atomic():
        Order.objects.of(made.organization).filter(pk=made.spare_order.pk).update(
            number=made.order.number
        )


# --- The organization ---------------------------------------------------------


@pytest.mark.django_db
def test_two_organizations_cannot_share_a_subdomain() -> None:
    made_up.organization("harbourview")

    with pytest.raises(IntegrityError), transaction.atomic():
        made_up.organization("harbourview")


@pytest.mark.django_db
@pytest.mark.parametrize("subdomain", ["Harbourview", "harbour view", "-harbour", ""])
def test_a_subdomain_fits_an_address(subdomain: str) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        made_up.organization(subdomain)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "fault"),
    [
        ("subdomain", "www", "No organization may take this subdomain."),
        ("subdomain", "sandbox", "No organization may take this subdomain."),
        ("time_zone", "Europe/Atlantis", "Not a time zone the service knows."),
    ],
)
def test_validation_refuses_what_an_organization_cannot_be(
    field: str, value: str, fault: str
) -> None:
    organization = Organization(
        name="Harbour View Housing Co-op",
        subdomain="harbourview",
        time_zone="Europe/Oslo",
        currency="NOK",
        contact_email="board@harbourview.example",
        member_retention_months=12,
        booking_retention_months=36,
    )
    organization.full_clean()

    setattr(organization, field, value)
    with pytest.raises(ValidationError) as refused:
        organization.full_clean()

    assert refused.value.message_dict == {field: [fault]}


# --- A booking configuration's checks on itself --------------------------------


def configuration(made: made_up.MadeUp, **fields: Any) -> BookingConfiguration:
    """Room hire 10:00 to 17:00 in steps of an hour, 1 to 3 hours: the operator's
    second example in docs/booking-model.md.
    """
    values: dict[str, Any] = {
        "organization": made.organization,
        "asset": made.asset,
        "booking_type": made.booking_type,
        "period_type": PeriodType.FLEXIBLE,
        "period_start": 10 * 60,
        "period_end": 17 * 60,
        "end_day": 0,
        "period_start_weekdays": [Weekday.MONDAY, Weekday.FRIDAY],
        "interval": 60,
        "minimum_length": 60,
        "maximum_length": 180,
        # A new configuration starts disabled. Enabled, it would be held
        # against the made-up organization's own, which covers the same time.
        "status": Status.DISABLED,
        "price_book": made.price_book,
    }
    return BookingConfiguration(**(values | fields))


@pytest.mark.django_db
def test_the_owners_flexible_example_passes() -> None:
    made = made_up.everything()
    room_hire = configuration(made)

    room_hire.full_clean(exclude=["number"])
    room_hire.save()

    assert room_hire.period_length == 7 * 60
    assert str(room_hire) == "002"


@pytest.mark.django_db
def test_a_fixed_period_gets_its_lengths_from_the_system() -> None:
    # The owner's first example: a night from 17:00 to 08:00 the next day.
    made = made_up.everything()
    night = configuration(
        made,
        period_type=PeriodType.FIXED,
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        interval=None,
        minimum_length=None,
        maximum_length=None,
    )

    night.full_clean(exclude=["number"])
    night.save()

    assert night.period_length == 900
    assert (night.interval, night.minimum_length, night.maximum_length) == (
        900,
        900,
        900,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("validated", [True, False])
def test_a_flexible_period_left_without_a_minimum_gets_its_interval(
    validated: bool,
) -> None:
    made = made_up.everything()
    room_hire = configuration(made, minimum_length=None)

    if validated:
        room_hire.full_clean(exclude=["number"])
    room_hire.save()

    assert (room_hire.interval, room_hire.minimum_length) == (60, 60)


@pytest.mark.django_db
def test_a_fixed_period_gets_its_lengths_also_without_validation() -> None:
    made = made_up.everything()

    night = configuration(
        made,
        period_type=PeriodType.FIXED,
        period_start=22 * 60,
        period_end=10 * 60,
        end_day=1,
        interval=60,
        minimum_length=60,
        maximum_length=60,
    )
    night.save()

    assert (night.interval, night.minimum_length, night.maximum_length) == (
        720,
        720,
        720,
    )


@pytest.mark.django_db
def test_the_weekdays_are_kept_in_order_and_once_each() -> None:
    made = made_up.everything()
    room_hire = configuration(
        made, period_start_weekdays=[Weekday.FRIDAY, Weekday.MONDAY, Weekday.FRIDAY]
    )

    room_hire.save()

    assert room_hire.period_start_weekdays == [Weekday.MONDAY, Weekday.FRIDAY]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "faults"),
    [
        (
            {"period_end": 9 * 60},
            {"period_end": ["The period ends after it starts."]},
        ),
        (
            {"period_end": 10 * 60},
            {"period_end": ["The period ends after it starts."]},
        ),
        (
            {"interval": None, "maximum_length": None},
            {
                "interval": ["A flexible period needs it."],
                "maximum_length": ["A flexible period needs it."],
            },
        ),
        (
            # A minimum left out follows the interval, which is what is missing.
            {"interval": None, "minimum_length": None},
            {"interval": ["A flexible period needs it."]},
        ),
        (
            {"interval": 0},
            {"interval": ["It is above zero."]},
        ),
        (
            {"interval": 120},
            {
                "interval": ["The period is a whole number of intervals."],
                "minimum_length": ["It is a whole number of intervals."],
                "maximum_length": ["It is a whole number of intervals."],
            },
        ),
        (
            {"maximum_length": 8 * 60},
            {"maximum_length": ["It is at most the period's length."]},
        ),
        (
            {"minimum_length": 4 * 60},
            {"minimum_length": ["It is at most the maximum length."]},
        ),
        (
            {"period_end": 11 * 60, "end_day": 1, "maximum_length": 60},
            {"period_end": ["A flexible period is at most 24 hours."]},
        ),
        (
            {"start_date": date(2027, 6, 7), "stop_date": date(2027, 6, 6)},
            {"stop_date": ["The last calendar week is not before the first."]},
        ),
        (
            {"start_date": date(2027, 6, 1), "stop_date": date(2027, 5, 31)},
            {
                "start_date": ["It is the Monday of the first calendar week."],
                "stop_date": ["It is the Sunday of the last calendar week."],
            },
        ),
        (
            {"every_weeks": 2},
            {
                "start_date": [
                    "Every N weeks counts from a first calendar week, so it needs one."
                ]
            },
        ),
        (
            {"every_weeks": 0, "start_date": date(2027, 6, 7)},
            {"every_weeks": ["It is 1 to 52."]},
        ),
    ],
    ids=[
        "ends before it starts",
        "ends when it starts",
        "lengths missing",
        "interval and minimum missing",
        "no interval",
        "not whole intervals",
        "longer than the period",
        "minimum above maximum",
        "longer than a day",
        "stops before it starts",
        "not whole calendar weeks",
        "every other week from no first week",
        "every no week",
    ],
)
def test_validation_refuses_a_configuration_that_cannot_be(
    fields: dict[str, Any], faults: dict[str, list[str]]
) -> None:
    made = made_up.everything()
    room_hire = configuration(made, **fields)

    with pytest.raises(ValidationError) as refused:
        room_hire.full_clean(exclude=["number"])

    assert refused.value.message_dict == faults


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"period_end": 9 * 60}, "bookingconfiguration_ends_after_it_starts"),
        ({"period_end": 24 * 60}, "bookingconfiguration_times_are_on_the_clock"),
        ({"period_start_weekdays": []}, "bookingconfiguration_starts_on_a_weekday"),
        ({"period_start_weekdays": [7]}, "bookingconfiguration_starts_on_a_weekday"),
        ({"maximum_length": 8 * 60}, "bookingconfiguration_lengths_fit_the_period"),
        ({"minimum_length": 4 * 60}, "bookingconfiguration_lengths_fit_the_period"),
        (
            {"start_date": date(2027, 6, 7), "stop_date": date(2027, 6, 6)},
            "bookingconfiguration_stops_after_it_starts",
        ),
        ({"start_date": date(2027, 6, 1)}, "bookingconfiguration_starts_on_a_monday"),
        ({"stop_date": date(2027, 5, 31)}, "bookingconfiguration_stops_on_a_sunday"),
        ({"every_weeks": 2}, "bookingconfiguration_counts_weeks_from_the_first"),
        (
            {"every_weeks": 0, "start_date": date(2027, 6, 7)},
            "bookingconfiguration_counts_weeks_from_the_first",
        ),
    ],
)
def test_the_database_refuses_it_too(fields: dict[str, Any], constraint: str) -> None:
    # For code that saves without validating.
    made = made_up.everything()

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        configuration(made, **fields).save()

    cause = refused.value.__cause__
    assert cause is not None
    assert cause.diag.constraint_name == constraint  # type: ignore[attr-defined]
