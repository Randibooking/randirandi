# SPDX-License-Identifier: AGPL-3.0-only
"""the order read and the locked cart's held order name the
spots each booking holds , for the member's own order only."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client as Browser

from randirandi.core.booking_views import choices_payload
from randirandi.core.models import Order, SpotGroup
from randirandi.core.ordering import create_order
from tests import made_up
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_booking_checkout import send
from tests.test_calendar import DAY, ZONE, Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import get, sign_in
from tests.test_ordering import NOW, request
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payment_return import another_member, outcome

pytestmark = pytest.mark.django_db(transaction=True)


def read(made: Example, order: Order) -> Any:
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    response = outcome(browser, order.public_id)
    assert response.status_code == 200
    return json.loads(response.content)


def order_of(made: Example, **changes: Any) -> Order:
    return create_order(
        member=made.member,
        requests=[replace(request(made), **changes)],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )


def named(made: Example, *, grouped: bool = True) -> None:
    """A shared container of four spots, two of them named, and a group of the
    last two (the lowest free spots are taken from a group with room first,
    W18, so tests of "the lowest" go without it)."""
    made.capacity(4)
    plan = made_up.floor_plan_of(made.asset)
    # The floor plan's own word for its spots.
    plan.unit = "seat"
    plan.save()
    first, second, third, fourth = made_up.spots_of(plan)
    first.name, second.name = "Window", "Aisle"
    first.save()
    second.save()
    if not grouped:
        return
    table = SpotGroup.objects.create(
        organization=made.org, floor_plan=plan, name="Corner table", word="table"
    )
    for spot in (third, fourth):
        spot.group = table
        spot.save()


def test_a_shared_booking_without_a_pick_holds_the_lowest_free_spots(
    made: Example,
) -> None:
    named(made, grouped=False)
    # Another member holds spot 1 at the same time.
    create_order(
        member=another_member(made),
        requests=[request(made, quantity=1)],
        now=NOW,
        accepted_version=made_up.accepted(made.org),
    )
    order = order_of(made, quantity=2)

    body = read(made, order)

    assert body["bookings"][0]["container"] == {
        "id": made.config.pk,
        "number": made.config.number,
    }
    assert body["bookings"][0]["spots"] == [
        {"number": 2, "name": "Aisle", "group": None},
        {"number": 3, "name": "", "group": None},
    ]
    # The floor plan's word for them comes with the booking.
    assert body["bookings"][0]["spot_word"] == "seat"


def test_the_default_floor_plan_word_is_sent_as_no_word(made: Example) -> None:
    made.capacity(4)  # The floor plan has the word it gets when none is written.

    body = read(made, order_of(made, quantity=2))

    assert body["bookings"][0]["spot_word"] is None
    assert container_words(made) == [None]


def test_the_choices_carry_the_floor_plans_word_per_container(made: Example) -> None:
    named(made)

    assert container_words(made) == ["seat"]


def container_words(made: Example) -> list[str | None]:
    found = choices_payload(made.member)
    return [
        container["spot_word"]
        for asset in found["assets"]  # type: ignore[attr-defined]
        for row in asset["types"]
        for container in row["containers"]
    ]


def test_a_pick_is_named_as_picked(made: Example) -> None:
    named(made)

    body = read(made, order_of(made, quantity=2, spots=(4, 1)))

    assert body["bookings"][0]["spots"] == [
        {"number": 1, "name": "Window", "group": None},
        {"number": 4, "name": "", "group": {"number": 1, "name": "Corner table"}},
    ]


def test_a_groups_number_and_name_come_through(made: Example) -> None:
    named(made)
    group = SpotGroup.objects.of(made.org).get()

    body = read(made, order_of(made, quantity=2, group_id=group.pk))

    assert [
        (spot["number"], spot["group"]) for spot in body["bookings"][0]["spots"]
    ] == [
        (3, {"number": 1, "name": "Corner table"}),
        (4, {"number": 1, "name": "Corner table"}),
    ]


def test_an_exclusive_booking_has_no_spots(made: Example) -> None:
    body = read(made, order_of(made))

    assert body["bookings"][0]["spots"] == []
    # A container without a floor plan has no word for spots.
    assert body["bookings"][0]["spot_word"] is None
    assert body["bookings"][0]["container"]["id"] == made.config.pk


def test_another_members_order_stays_refused(made: Example) -> None:
    named(made)
    theirs = create_order(
        member=another_member(made),
        requests=[request(made, starts_at=at(hour=12), ends_at=at(hour=14))],
        now=NOW,
        accepted_version=made_up.accepted(made.org),
    )
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302

    response = outcome(browser, theirs.public_id)

    assert response.status_code == 404
    assert json.loads(response.content) == {"error": "not-found"}


def test_the_held_order_in_the_cart_names_the_spots_and_the_rows_keep_the_pick(
    made: Example, wire: Wire
) -> None:
    named(made, grouped=False)
    client = Browser(enforce_csrf_checks=True)
    sign_in(client, made.member)
    start = at(DAY, 10).astimezone(ZONE)
    booking: dict[str, Any] = {
        "container": made.config.pk,
        "category": made.category.pk,
        "quantity": 2,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
        "total": 1,
    }
    # The page learns the total from the refusal, as it does.
    quoted = json.loads(send(client, "/booking/cart/add/", booking).content)
    assert send(client, "/booking/cart/add/", {**booking, "total": quoted["total"]})
    cart = json.loads(get(client, "/booking/cart/").content)
    assert cart["bookings"][0]["spots"] == []  # Not guessed before the order.
    assert cart["bookings"][0]["spot_word"] == "seat"
    assert cart["order"] is None

    assert (
        send(
            client, "/booking/cart/order/", made_up.order_body(quoted["total"])
        ).status_code
        == 200
    )

    cart = json.loads(get(client, "/booking/cart/").content)
    assert cart["locked"]
    assert cart["bookings"][0]["spots"] == []  # Only the member's pick.
    assert [spot["number"] for spot in cart["order"]["bookings"][0]["spots"]] == [1, 2]
    assert cart["order"]["bookings"][0]["spot_word"] == "seat"
    assert cart["order"]["bookings"][0]["container"] == {
        "id": made.config.pk,
        "number": made.config.number,
    }
