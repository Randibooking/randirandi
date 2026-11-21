# SPDX-License-Identifier: AGPL-3.0-only
"""prices in the booking calendar's answer.

Each container with payment answers, per booking category, the price book
entry the price call uses, as numbers: the page words them ("from NOK 150 an
hour"). Made-up setups only: the four-bed room, the laundry, the sauna palace,
the cinema and the supper club.
"""

import json
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from randirandi.core import member_calendar as calendar_module
from randirandi.core.member_calendar import (
    ContainerCalendar,
    ContainerPrices,
    member_calendar,
)
from randirandi.core.models import (
    Asset,
    BookingCategory,
    BookingConfiguration,
    ConfigurationCategory,
    Member,
    Organization,
    PriceBookEntry,
    PriceTier,
    Space,
    SpotGroup,
)
from randirandi.core.pricing import PriceUnavailableError, price_booking
from tests.test_calendar import DAY, at, scenario
from tests.test_login_sessions import page_settings as page_settings
from tests.test_login_sessions import sign_in
from tests.test_member_calendar import at_home, calendar_page

pytestmark = pytest.mark.django_db(transaction=True)


def answered(filename: str, asset: str, length: int = 60) -> dict[int, Any]:
    """The containers of the asset the page answers for a week, by number."""
    org, member = at_home(filename)
    client = Client()
    sign_in(client, member)
    query = {
        "asset": Asset.objects.of(org).get(name=asset).pk,
        "start": DAY.isoformat(),
        "days": 7,
        "length": length,
    }
    body = json.loads(calendar_page(client, **query).content)
    return {row["number"]: row for row in body["containers"]}


def category(org: Organization, name: str) -> int:
    return BookingCategory.objects.of(org).get(name=name).pk


def test_one_fixed_period_has_one_tier_and_a_price_for_the_whole_booking() -> None:
    rooms = answered("scenarios/four-beds.toml", "Sunshine")
    org = Organization.objects.get(subdomain="harbourview")
    assert rooms[2]["prices"] == {
        "currency": "NOK",
        "categories": [
            {
                "category": category(org, "private"),
                "billing_basis": "per_booking",
                "tiers_go_by": "length",
                "tiers": [{"up_to": 900, "paid_per": 900, "amount": 40000}],
                "groups": [],
            }
        ],
    }


def test_a_flexible_container_has_one_tier_in_minutes() -> None:
    laundry = answered("scenarios/laundry.toml", "Washer A")
    (price,) = laundry[1]["prices"]["categories"]
    assert price["tiers"] == [{"up_to": 480, "paid_per": 60, "amount": 2000}]


def test_a_flexible_container_with_several_tiers_has_them_all() -> None:
    sauna = answered("scenarios/sauna-palace.toml", "Sauna 1")
    (price,) = sauna[1]["prices"]["categories"]
    assert (price["billing_basis"], price["tiers"]) == (
        "per_capacity_unit",
        [
            {"up_to": 60, "paid_per": 60, "amount": 10000},
            {"up_to": 480, "paid_per": 60, "amount": 5000},
        ],
    )


def test_a_price_per_spot_has_no_group_prices_where_the_book_has_none() -> None:
    cinema = answered("scenarios/cinema.toml", "Cinema", 120)
    (price,) = cinema[1]["prices"]["categories"]
    assert price["billing_basis"] == "per_capacity_unit"
    assert price["tiers"] == [{"up_to": 120, "paid_per": 120, "amount": 12000}]
    assert price["groups"] == []


def test_the_entries_for_groups_come_with_their_tiers_by_group() -> None:
    club = answered("scenarios/supper-club.toml", "Dining room", 180)
    org = Organization.objects.get(subdomain="harbourview")
    dinner, lunch = club[1]["prices"], club[2]["prices"]
    (price,) = dinner["categories"]
    assert price["tiers"] == [{"up_to": 180, "paid_per": 180, "amount": 35000}]
    named = {
        group.name: group.pk for group in SpotGroup.objects.of(org).order_by("number")
    }
    assert price["groups"] == [
        {
            "group": named[name],
            "tiers_go_by": "length",
            "tiers": [{"up_to": 180, "paid_per": 180, "amount": amount}],
        }
        for name, amount in (("Window", 50000), ("Long", 40000))
    ]
    (price,) = lunch["categories"]
    assert (price["tiers"][0]["amount"], price["groups"]) == (20000, [])


def test_a_container_without_payment_has_no_prices() -> None:
    org, member = at_home("scenarios/sauna-palace.toml")
    free = BookingConfiguration.objects.of(org).get(number=2)
    PriceTier.objects.of(org).filter(
        price_book_entry__booking_configuration=free
    ).delete()
    PriceBookEntry.objects.of(org).filter(booking_configuration=free).delete()
    BookingConfiguration.objects.of(org).filter(pk=free.pk).update(price_book=None)
    client = Client()
    sign_in(client, member)
    sauna = Asset.objects.of(org).get(name="Sauna 1").pk
    body = json.loads(
        calendar_page(
            client, asset=sauna, start=DAY.isoformat(), days=1, length=60
        ).content
    )
    prices = {row["number"]: row["prices"] for row in body["containers"]}
    assert prices[2] is None and prices[1] is not None


def test_the_answer_is_the_price_the_price_call_charges() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    found = member_calendar(
        member=member,
        asset_id=Asset.objects.of(org).get(name="Sauna 1").pk,
        start_date=DAY,
        end_date=DAY + timedelta(days=1),
        length=120,
        now=at(DAY - timedelta(days=1)),
    )
    visit = found.containers[0]
    assert visit.prices is not None
    (read,) = visit.prices.categories
    booked = price_booking(
        configuration=BookingConfiguration.objects.of(org).get(pk=visit.id),
        booking_category=BookingCategory.objects.of(org).get(pk=read.category_id),
        starts_at=at(hour=10),
        ends_at=at(hour=12),
    )
    assert [(line.up_to, line.paid_per, line.amount) for line in booked.lines] == [
        (tier.up_to, tier.paid_per, tier.amount) for tier in read.tiers
    ]


def test_prices_cost_one_more_query_for_the_whole_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    ask: dict[str, Any] = {
        "member": member,
        "space_id": Space.objects.of(org).get().pk,
        "start_date": DAY,
        "end_date": DAY + timedelta(days=7),
        "length": 60,
        "now": at(DAY - timedelta(days=1)),
    }
    with CaptureQueriesContext(connection) as with_prices:
        found = member_calendar(**ask)
    assert len(found.containers) > 1
    assert all(row.prices is not None for row in found.containers)
    monkeypatch.setattr(calendar_module, "_prices", lambda org_id, found: {})
    with CaptureQueriesContext(connection) as without:
        member_calendar(**ask)
    assert len(with_prices) - len(without) == 1


def sauna_visit(org: Organization, member: Member) -> ContainerCalendar:
    found = member_calendar(
        member=member,
        asset_id=Asset.objects.of(org).get(name="Sauna 1").pk,
        start_date=DAY,
        end_date=DAY + timedelta(days=1),
        length=60,
        now=at(DAY - timedelta(days=1)),
    )
    return found.containers[0]


def test_a_paid_container_with_no_category_priced_still_has_a_prices_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, member = scenario("scenarios/sauna-palace.toml")

    def refused(*args: Any) -> Any:
        raise PriceUnavailableError("No supported price.")

    monkeypatch.setattr(calendar_module, "_priced_by_book", refused)
    visit = sauna_visit(org, member)
    assert visit.with_payment
    assert visit.prices == ContainerPrices("NOK", ())


def test_only_the_categories_the_member_may_book_are_answered() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    visit = BookingConfiguration.objects.of(org).get(number=1)
    staff = BookingCategory.objects.create(organization=org, name="staff")
    ConfigurationCategory.objects.create(
        organization=org, booking_configuration=visit, booking_category=staff
    )
    assert visit.price_book is not None
    entry = PriceBookEntry.objects.create(
        organization=org,
        price_book=visit.price_book,
        booking_configuration=visit,
        booking_category=staff,
        tiers_go_by="length",
        billing_basis="per_capacity_unit",
    )
    PriceTier.objects.create(
        organization=org, price_book_entry=entry, up_to=480, paid_per=60, amount=1
    )
    found = sauna_visit(org, member)
    assert found.prices is not None
    assert [row.category_id for row in found.prices.categories] == [
        category(org, "private")
    ]
