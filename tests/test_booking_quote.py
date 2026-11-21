# SPDX-License-Identifier: AGPL-3.0-only
"""the price of one choice, before it goes in the cart.

Made-up setups only: a sauna club (seats by the hour in two price tiers,
whole evenings at one price).
"""

import json
from datetime import date, datetime, timedelta
from typing import Any
from unittest import mock
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client

from randirandi.core.booking_quote import (
    NOT_OFFERED,
    STARTED,
    TAKEN,
    Quote,
    QuoteLine,
    quote,
)
from randirandi.core.models import (
    AvailabilityCalendar,
    AvailableSpan,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    BookingType,
    Organization,
    PriceBookEntry,
    RoleCategory,
)
from randirandi.core.ordering import BookingRequest, create_order
from randirandi.core.pricing import price_booking
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, ZONE, Example, at, scenario
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_member_calendar import ids, take

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))


def price(
    org: Organization,
    member: Any,
    asset: str,
    kind: str,
    start: datetime,
    end: datetime,
    *,
    category: str = "private",
    places: int = 1,
    number: int | None = None,
    spots: tuple[int, ...] = (),
    now: datetime = NOW,
) -> Quote:
    return quote(
        member=member,
        quantity=places,
        starts_at=start,
        ends_at=end,
        spots=spots,
        now=now,
        **ids(org, asset, kind, category, number),
    )


def test_seats_are_priced_in_parts_for_the_places() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    found = price(
        org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=13), places=3
    )
    assert (found.reason, found.period_type, found.consumption_mode) == (
        None,
        "flexible",
        "shared",
    )
    assert (found.quantity, found.tiers_go_by, found.currency) == (3, "length", "NOK")
    # Across a tier boundary the booking is one, priced in parts ().
    assert found.lines == (
        QuoteLine(60, 60, 1, 10000, 3, "place", "per_capacity_unit", 30000),
        QuoteLine(480, 60, 2, 5000, 3, "place", "per_capacity_unit", 30000),
    )
    # Amounts are in øre: the setup file says 100 and 50 kr.
    assert found.total == 60000
    config = BookingConfiguration.objects.of(org).get(number=1)
    assert (
        price_booking(
            configuration=config,
            booking_category=BookingCategory.objects.of(org).get(name="private"),
            starts_at=at(hour=10),
            ends_at=at(hour=13),
            quantity=3,
        ).total
        == 60000
    )


def test_a_whole_offer_is_priced_with_one_whatever_the_places() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    for places in (1, 3):
        found = price(
            org,
            member,
            "Sauna 1",
            "Private sauna",
            at(hour=18),
            at(hour=22),
            places=places,
        )
        assert (found.period_type, found.consumption_mode, found.quantity) == (
            "fixed",
            "exclusive",
            1,
        )
        assert found.lines == (
            QuoteLine(240, 240, 1, 80000, 1, "booking", "per_booking", 80000),
        )


def test_the_price_shown_is_the_price_ordered_and_then_taken() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    private = BookingCategory.objects.of(org).get(name="private")
    for number, start, end, places in (
        (1, at(hour=10), at(hour=13), 3),
        (2, at(hour=18), at(hour=22), 1),
    ):
        config = BookingConfiguration.objects.of(org).get(number=number)
        kind = BookingType.objects.of(org).get(pk=config.booking_type_id).name
        shown = price(org, member, "Sauna 1", kind, start, end, places=places)
        order = create_order(
            member=member,
            requests=[BookingRequest(config.pk, private.pk, start, end, places)],
            now=NOW,
            accepted_version=made_up.accepted(member),
        )
        assert order.total == shown.total
        lines = BookingLine.objects.of(org).filter(booking__order=order)
        assert sorted(
            (line.up_to, line.quantity, line.amount, line.capacity_quantity)
            for line in lines
        ) == sorted(
            (line.up_to, line.paid_units, line.unit_price, line.times)
            for line in shown.lines
        )
        if number == 2:
            # The whole evening is held for the order now.
            assert price(org, member, "Sauna 1", kind, start, end) == TAKEN
    # Five of eight places are left at 10:00-13:00: three more fit, six do not.
    assert (
        price(
            org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), places=5
        ).reason
        is None
    )
    assert price(
        org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), places=6
    ) == Quote("taken", places_left=5)


def test_times_outside_the_offers_are_not_offered() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    for start, end in (
        (at(hour=10, minute=30), at(hour=11, minute=30)),  # off the hourly steps
        (at(hour=17), at(hour=19)),  # across two offers
        (at(hour=9), at(hour=10)),  # before the offer
        (at(DAY + timedelta(days=5), 10), at(DAY + timedelta(days=5), 11)),  # Saturday
        (at(hour=10), at(hour=10, minute=59)),  # not whole steps
    ):
        assert price(org, member, "Sauna 1", "Sauna visit", start, end) == NOT_OFFERED
    assert (
        price(org, member, "Sauna 1", "Private sauna", at(hour=18), at(hour=21))
        == NOT_OFFERED
    )
    # More places than the sauna has.
    assert (
        price(org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), places=9)
        == NOT_OFFERED
    )
    work = BookingCategory.objects.create(organization=org, name="work", position=1)
    RoleCategory.objects.create(
        organization=org, role=member.role, booking_category=work
    )
    assert (
        price(
            org,
            member,
            "Sauna 1",
            "Sauna visit",
            at(hour=10),
            at(hour=11),
            category="work",
        )
        == NOT_OFFERED
    )
    other = made_up.everything("riverside")
    assert (
        quote(
            member=member,
            configuration_id=other.configuration.pk,
            booking_category_id=other.private.pk,
            quantity=1,
            starts_at=at(hour=10),
            ends_at=at(hour=11),
            now=NOW,
        )
        == NOT_OFFERED
    )
    BookingConfiguration.objects.of(org).filter(number=4).update(
        booking_type=BookingType.objects.of(org).get(name="Sauna visit")
    )
    BookingConfiguration.objects.of(org).filter(number=3).update(period_end=19 * 60)
    # The visits run into the evening (): the shared visits and the
    # exclusive evening stack, and each is quoted for the time they share.
    for number, start, end in (
        (3, at(hour=10), at(hour=11)),
        (3, at(hour=17), at(hour=19)),
        (4, at(hour=18), at(hour=22)),
    ):
        assert (
            price(
                org, member, "Sauna 2", "Sauna visit", start, end, number=number
            ).reason
            is None
        )
    member.status = "disabled"
    member.save()
    assert (
        price(org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11))
        == NOT_OFFERED
    )


def test_a_taken_time_says_taken_and_a_started_one_says_so() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=11), at(hour=12), units=tuple(range(1, 9)))
    # A seat booking that no longer fits says how many places are left (T6).
    assert price(
        org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=13)
    ) == Quote("taken", places_left=0)
    assert (
        price(org, member, "Sauna 1", "Sauna visit", at(hour=12), at(hour=13)).reason
        is None
    )
    assert (
        price(
            org,
            member,
            "Sauna 1",
            "Sauna visit",
            at(hour=12),
            at(hour=13),
            now=at(hour=12, minute=1),
        )
        == STARTED
    )
    take(org, "Sauna 1", at(hour=20), at(hour=21), state="blocked")
    assert (
        price(org, member, "Sauna 1", "Private sauna", at(hour=18), at(hour=22))
        == TAKEN
    )


def test_both_starts_of_a_repeated_hour_are_priced(made: Example) -> None:
    day = date(2027, 10, 31)
    made.configure(period_start=120, period_end=240, maximum_length=60)
    for fold in (0, 1):
        start = at(day, 2, fold=fold)
        found = quote(
            member=made.member,
            configuration_id=made.config.pk,
            booking_category_id=made.category.pk,
            quantity=1,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            now=at(day - timedelta(days=1)),
        )
        assert found.reason is None and found.total == 100
    assert at(day, 2, fold=0) != at(day, 2, fold=1)


def test_bad_input_and_own_transaction() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    with pytest.raises(ValueError):
        price(org, member, "Sauna 1", "Sauna visit", at(hour=11), at(hour=10))
    with pytest.raises(ValueError):
        price(
            org,
            member,
            "Sauna 1",
            "Sauna visit",
            at(hour=10).replace(tzinfo=None),
            at(hour=11),
        )
    with pytest.raises(ValueError):
        price(org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), places=0)
    with pytest.raises(ValueError):
        price(
            org,
            member,
            "Sauna 1",
            "Sauna visit",
            datetime.max.replace(tzinfo=ZONE) - timedelta(hours=1),
            datetime.max.replace(tzinfo=ZONE),
        )
    with transaction.atomic(), pytest.raises(RuntimeError):
        price(org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11))
    with application_login():
        assert (
            price(
                org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11)
            ).reason
            is None
        )


def price_page(client: Client, **query: Any) -> Any:
    return get(client, "/booking/price/?" + urlencode(query))


def test_the_page_gets_the_price_for_the_signed_in_member(made: Example) -> None:
    client = Client()
    start = at(DAY, 10).astimezone(ZONE)
    query = {
        "container": made.config.pk,
        "category": made.category.pk,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
    }
    assert price_page(client, **query).status_code == 302
    sign_in(client, made.member)
    response = price_page(client, **query)
    assert response.status_code == 200
    assert {"no-store", "private"} <= {
        part.strip() for part in response["Cache-Control"].split(",")
    }
    assert json.loads(response.content) == {
        "available": True,
        "container": made.config.pk,
        "period_type": "flexible",
        "consumption_mode": "exclusive",
        "quantity": 1,
        "tiers_go_by": "length",
        "currency": made.org.currency,
        "total": 200,
        "lines": [
            {
                "up_to": 240,
                "paid_per": 60,
                "times_paid": 2,
                "amount": 100,
                "capacity_quantity": 1,
                "capacity_unit": made_up.floor_plan_of(made.asset).unit,
                "billing_basis": "per_booking",
                "group": None,
                "total": 200,
            }
        ],
    }
    off_grid = {**query, "start": (start + timedelta(minutes=30)).isoformat()}
    assert json.loads(price_page(client, **off_grid).content) == {
        "available": False,
        "reason": "not-offered",
        "text": "not offered",
    }
    for change in (
        {"start": "2027-03-01T10:00:00"},
        {"start": "2027-03-01 10:00:00+01:00"},
        {"start": "2027-03-01T10:00+01:00"},
        {"end": query["start"]},
        {"quantity": "0"},
        {"container": "x"},
        {"spots": "1,1"},
    ):
        assert price_page(client, **{**query, **change}).status_code == 400, change
    response = client.post("/booking/price/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 405


def test_a_time_outside_opening_hours_is_not_offered() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    sauna = AvailabilityCalendar.objects.of(org).get(asset__name="Sauna 1")
    AvailableSpan.objects.of(org).filter(
        availability_calendar=sauna, weekday=DAY.weekday()
    ).update(start_time=10 * 60, end_time=12 * 60)
    # Inside the offer's own hours, outside the sauna's opening hours.
    assert (
        price(org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=13))
        == NOT_OFFERED
    )
    assert (
        price(org, member, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12)).reason
        is None
    )


def test_a_whole_offer_is_never_billed_per_place() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    entries = PriceBookEntry.objects.of(org).filter(booking_configuration__number=2)
    entry = entries.get()
    entry.billing_basis = "per_capacity_unit"
    with pytest.raises(ValidationError, match="priced per booking"):
        entry.full_clean()
    # Written past the model, the price is not one the offer can have.
    entries.update(billing_basis="per_capacity_unit")
    found = price(
        org, member, "Sauna 1", "Private sauna", at(hour=18), at(hour=22), places=3
    )
    assert found == NOT_OFFERED


def test_an_overnight_start_after_midnight_is_priced(made: Example) -> None:
    made.configure(period_start=22 * 60, period_end=2 * 60, end_day=1)
    after_midnight = at(DAY + timedelta(days=1), 1)
    found = quote(
        member=made.member,
        configuration_id=made.config.pk,
        booking_category_id=made.category.pk,
        quantity=1,
        starts_at=after_midnight,
        ends_at=after_midnight + timedelta(hours=1),
        now=at(DAY - timedelta(days=1)),
    )
    assert found.reason is None and found.total == 100


def test_the_page_says_taken(made: Example) -> None:
    client = Client()
    sign_in(client, made.member)
    start = at(DAY, 10)
    made.claim(start, start + timedelta(hours=1))
    query = {
        "container": made.config.pk,
        "category": made.category.pk,
        "start": start.astimezone(ZONE).isoformat(),
        "end": (start + timedelta(hours=2)).astimezone(ZONE).isoformat(),
    }
    assert json.loads(price_page(client, **query).content) == {
        "available": False,
        "reason": "taken",
        "text": "taken",
    }


def test_the_page_says_already_started_for_a_start_that_has_passed(
    made: Example,
) -> None:
    client = Client()
    sign_in(client, made.member)
    start = at(DAY, 10)
    query = {
        "container": made.config.pk,
        "category": made.category.pk,
        "start": start.astimezone(ZONE).isoformat(),
        "end": (start + timedelta(hours=2)).astimezone(ZONE).isoformat(),
    }
    assert json.loads(price_page(client, **query).content)["available"] is True
    # A minute past the start: its own key and text, not "taken".
    with mock.patch(
        "randirandi.core.booking_quote._now", return_value=start + timedelta(minutes=1)
    ):
        found = json.loads(price_page(client, **query).content)
    assert found == {
        "available": False,
        "reason": "already-started",
        "text": "already started",
    }
