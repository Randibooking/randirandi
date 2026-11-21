# SPDX-License-Identifier: AGPL-3.0-only
"""The examples on the price page  are calculated by
`pricing.py`, the function Book and the order price with: each example equals
what an order for the same time costs, by length and by time of day, per
capacity unit, for a group, over midnight and in the asset's time zone. A
time-of-day tier up to 24:00 is read and written by the setup file."""

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from django.db import connection, transaction

from randirandi import setup_file
from randirandi.core import price_admin
from randirandi.core import setup as setups
from randirandi.core.calendar import _instants
from randirandi.core.models import (
    Asset,
    Booking,
    BookingLine,
    CapacityAllocation,
    Order,
    PriceBookEntry,
    PriceTier,
    Spot,
    SpotGroup,
    TakenTime,
)
from randirandi.core.ordering import BookingRequest, create_order
from tests import made_up
from tests.test_admin_prices import H, administrator_of, normal, tier
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import page_settings  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
MINUTE = timedelta(minutes=1)


def container_in(place: Mapping[str, Any], pk: int) -> dict[str, Any]:
    return next(c for c in place["containers"] if c["id"] == pk)


def container_of(example: Example) -> dict[str, Any]:
    return container_in(price_admin.prices_place(example.org), example.config.pk)


def examples_of(example: Example, group: int | None = None) -> list[dict[str, Any]]:
    entry = next(e for e in container_of(example)["entries"] if e["group"] == group)
    found: list[dict[str, Any]] = entry["examples"]
    return found


def moments(example: Example, given: dict[str, Any]) -> tuple[datetime, datetime]:
    zone = example.asset.zone
    midnight = datetime.combine(date.fromisoformat(given["day"]), time())
    (start,) = _instants(midnight + given["start"] * MINUTE, zone)
    (end,) = _instants(midnight + (given["start"] + given["length"]) * MINUTE, zone)
    return start, end


def forget_the_orders(example: Example) -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        for model in (BookingLine, Booking, CapacityAllocation, TakenTime, Order):
            model.objects.of(example.org).all().delete()


def cost_of_an_order(
    example: Example,
    given: dict[str, Any],
    *,
    spots: tuple[int, ...] = (),
    quantity: int = 1,
) -> int:
    forget_the_orders(example)
    start, end = moments(example, given)
    order = create_order(
        member=example.member,
        requests=[
            BookingRequest(
                example.config.pk,
                example.category.pk,
                start,
                end,
                quantity=quantity,
                spots=spots,
            )
        ],
        now=start - timedelta(days=1),
        accepted_version=made_up.accepted(example.member),
    )
    return order.total


def price_it(example: Example, *entries: dict[str, Any]) -> None:
    price_admin.save_prices(
        administrator_of(example),
        example.config.pk,
        example.category.pk,
        {"entries": list(entries)},
    )


def assert_each_equals_an_order(
    example: Example, given: list[dict[str, Any]], **order: Any
) -> None:
    assert given
    for each in given:
        assert each["total"] == cost_of_an_order(example, each, **order), each


def test_examples_by_length_equal_what_an_order_costs(made: Example) -> None:
    price_it(made, normal(tier(2 * H, H, 100), tier(4 * H, 2 * H, 150)))
    given = examples_of(made)
    assert [(g["start"], g["length"]) for g in given] == [
        (10 * H, H),
        (10 * H, 3 * H),
        (10 * H, 4 * H),
    ]
    assert [g["total"] for g in given] == [100, 350, 350]
    assert_each_equals_an_order(made, given)


def test_examples_by_time_of_day_equal_what_an_order_costs(made: Example) -> None:
    price_it(
        made, normal(tier(12 * H, H, 100), tier(14 * H, 2 * H, 300), by="time of day")
    )
    given = examples_of(made)
    assert [(g["start"], g["length"]) for g in given] == [
        (10 * H, 2 * H),
        (10 * H, 4 * H),
        (13 * H, H),
    ]
    assert [g["total"] for g in given] == [200, 500, 300]
    assert_each_equals_an_order(made, given)


def test_examples_follow_the_assets_time_zone(made: Example) -> None:
    Asset.objects.of(made.org).filter(pk=made.asset.pk).update(
        time_zone="America/New_York"
    )
    made.asset.refresh_from_db()
    price_it(made, normal(tier(12 * H, H, 100), tier(14 * H, H, 300), by="time of day"))
    given = examples_of(made)
    assert [g["total"] for g in given] == [200, 800, 300]
    start, _ = moments(made, given[0])
    assert start.astimezone(made.asset.zone).hour == 10
    assert start.hour != 10
    assert_each_equals_an_order(made, given)


def test_examples_per_capacity_unit_are_for_one_spot(made: Example) -> None:
    made.capacity(5)
    price_it(made, normal(tier(4 * H, H, 100), basis="per_capacity_unit"))
    given = examples_of(made)
    assert [g["total"] for g in given] == [100, 400]
    assert_each_equals_an_order(made, given)
    assert cost_of_an_order(made, given[1], quantity=3) == 3 * given[1]["total"]


def test_examples_for_a_group_use_the_groups_price(made: Example) -> None:
    made.capacity(5)
    plan = made_up.floor_plan_of(made.asset)
    group = SpotGroup.objects.create(
        organization=made.org, floor_plan=plan, name="Front", word="front"
    )
    Spot.objects.of(made.org).filter(floor_plan=plan, number=1).update(group=group)
    per_spot = {"basis": "per_capacity_unit"}
    price_it(
        made,
        normal(tier(4 * H, H, 100), **per_spot),
        {**normal(tier(4 * H, H, 700), **per_spot), "group": group.pk},
    )
    ordinary, front = examples_of(made), examples_of(made, group.pk)
    assert [g["total"] for g in ordinary] == [100, 400]
    assert [g["total"] for g in front] == [700, 2800]
    assert_each_equals_an_order(made, ordinary, spots=(2,))
    assert_each_equals_an_order(made, front, spots=(1,))


def test_examples_over_midnight_equal_what_an_order_costs(made: Example) -> None:
    made.configure(
        period_start=22 * H, period_end=2 * H, end_day=1, maximum_length=4 * H
    )
    price_it(made, normal(tier(H, H, 100), tier(4 * H, 2 * H, 150)))
    given = examples_of(made)
    assert [(g["start"], g["length"]) for g in given] == [
        (22 * H, H),
        (22 * H, 2 * H),
        (22 * H, 4 * H),
    ]
    assert [g["total"] for g in given] == [100, 250, 400]
    assert_each_equals_an_order(made, given)


def test_a_fixed_period_over_midnight_is_one_example_at_the_period_price(
    made: Example,
) -> None:
    made.configure(
        period_type="fixed",
        period_start=22 * H,
        period_end=8 * H,
        end_day=1,
        interval=10 * H,
        minimum_length=10 * H,
        maximum_length=10 * H,
    )
    price_it(made, normal(tier(10 * H, 10 * H, 9000)))
    given = examples_of(made)
    assert [(g["start"], g["length"], g["total"]) for g in given] == [
        (22 * H, 10 * H, 9000)
    ]
    assert_each_equals_an_order(made, given)


def test_time_of_day_over_midnight_is_refused_on_the_screen_and_by_book(
    made: Example,
) -> None:
    from randirandi.core.setup_admin import SetupRefusedError

    made.configure(
        period_start=22 * H, period_end=2 * H, end_day=1, maximum_length=4 * H
    )
    with pytest.raises(SetupRefusedError):
        price_it(made, normal(tier(24 * H, H, 1), by="time of day"))
    # Written past the screens, Book offers nothing and there is no example.
    PriceBookEntry.objects.of(made.org).filter(pk=made.price.pk).update(
        tiers_go_by="time of day"
    )
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(up_to=H)
    assert made.offers() == ()
    assert examples_of(made) == []


def test_a_tier_up_to_24_00_made_on_the_screen_exports_and_reloads() -> None:
    from randirandi.core.models import BookingCategory, BookingConfiguration
    from tests.test_admin_prices import fjordside_with_admin, roundtrip

    org, admin = fjordside_with_admin()
    hire = BookingConfiguration.objects.of(org).get(number=2)
    work = BookingCategory.objects.of(org).get(name="Work")
    price_admin.save_prices(
        admin,
        hire.pk,
        work.pk,
        {
            "entries": [
                normal(
                    tier(17 * H, 4 * H, 500),
                    tier(24 * H, 5 * H, 1000),
                    by="time of day",
                )
            ]
        },
    )
    assert PriceTier.objects.of(org).filter(up_to=24 * H).exists()
    exported, again = roundtrip(org, "second")
    assert "24:00" in setup_file.write(exported)
    assert exported.price_books == again.price_books
    place = price_admin.prices_place(org)
    container = container_in(place, hire.pk)
    entry = next(
        e
        for e in container["entries"]
        if e["category"] == work.pk and e["group"] is None
    )
    assert [t["up_to"] for t in entry["tiers"]] == [17 * H, 24 * H]
    assert entry["examples"]
    assert setups.export(org).price_books == exported.price_books


def test_a_disabled_container_has_examples_and_book_offers_nothing(
    made: Example,
) -> None:
    price_it(made, normal(tier(4 * H, H, 100)))
    enabled = examples_of(made)
    assert enabled and made.offers()
    made.config.status = "disabled"
    made.config.save()
    assert made.offers() == ()
    assert examples_of(made) == enabled
