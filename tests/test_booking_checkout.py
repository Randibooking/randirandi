# SPDX-License-Identifier: AGPL-3.0-only
"""the order from the cart, once per press, at the total seen.

Made-up setups only: a sauna club with seats by the hour and whole evenings.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from typing import Any
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import connections
from django.test import Client

from randirandi.core import booking_cart, booking_checkout, ordering
from randirandi.core.booking_cart import CartRow, Refusal
from randirandi.core.booking_checkout import Placed
from randirandi.core.booking_quote import cart_quotes
from randirandi.core.models import (
    Booking,
    BookingState,
    Member,
    Order,
    Organization,
    Payment,
    PriceTier,
    TakenTime,
)
from randirandi.core.payment_recovery import expire_holds
from randirandi.vipps import VippsError
from tests import made_up
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_booking_cart import choice
from tests.test_calendar import DAY, ZONE, Example, at, scenario
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, sign_in
from tests.test_member_calendar import take
from tests.test_payment_callbacks import page_settings as page_settings

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))
SEATS = 30000  # two places 10:00-12:00: 2 x 100 kr + 2 x 50 kr, in øre
EVENING = 80000  # the whole sauna 18:00-22:00


def filled(org: Organization, member: Member) -> SessionStore:
    session = SessionStore()
    for wanted, seen in (
        (choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 2), SEATS),
        (choice(org, "Sauna 1", "Private sauna", at(hour=18), at(hour=22)), EVENING),
    ):
        assert isinstance(booking_cart.add(session, member, wanted, seen, NOW), CartRow)
    return session


def states(org: Organization) -> list[str]:
    return sorted(TakenTime.objects.of(org).values_list("state", flat=True))


def test_a_paid_cart_becomes_one_held_order_and_locks_the_cart() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)
    placed = booking_checkout.order(
        session, member, SEATS + EVENING, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, Placed) and placed.total == SEATS + EVENING
    made = Order.objects.of(org).get()
    assert (made.public_id, made.total) == (placed.public_id, SEATS + EVENING)
    assert made.hold_expires_at == NOW + timedelta(minutes=10)
    assert states(org) == ["held", "held"]
    # Proposal (c): while held, the cart shows that order and cannot change.
    cart = booking_cart.look(session, member, NOW)
    assert cart.locked and not cart.ready
    assert cart.order is not None and cart.order.state == "held"
    assert (cart.total, [item.state for item in cart.items]) == (
        SEATS + EVENING,
        ["held", "held"],
    )
    row = cart.items[0].row
    held = Refusal("order-held")
    assert booking_cart.add(session, member, row.choice, SEATS, NOW) == held
    assert booking_cart.change(session, member, row.id, row.choice, SEATS, NOW) == held
    assert booking_cart.remove(session, member, row.id, NOW) == held
    assert booking_cart.empty(session, member, NOW) == held
    # A second press makes no second order.
    assert (
        booking_checkout.order(
            session,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        )
        == held
    )
    assert Order.objects.of(org).count() == 1
    # Proposal (a): past its hold the cart is the member's again, with the
    # same bookings. Until the held times are freed, the whole evening is
    # taken; the two seats still fit beside the two held ones.
    later = NOW + timedelta(minutes=11)
    cart = booking_cart.look(session, member, later)
    assert cart.order is not None and cart.order.state == "not-completed"
    assert not cart.locked
    assert [item.state for item in cart.items] == ["ok", "taken"]
    expire_holds(now=later)
    cart = booking_cart.look(session, member, later)
    assert ([item.state for item in cart.items], cart.ready) == (["ok", "ok"], True)
    again = booking_checkout.order(
        session,
        member,
        SEATS + EVENING,
        later,
        accepted_version=made_up.accepted(member),
    )
    assert isinstance(again, Placed) and again.public_id != placed.public_id
    latest = booking_cart.look(session, member, later).order
    assert latest is not None and latest.public_id == again.public_id


def test_a_free_cart_is_confirmed_at_once_and_emptied() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    PriceTier.objects.of(org).update(amount=0)
    session = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 2)
    assert isinstance(booking_cart.add(session, member, seats, 0, NOW), CartRow)
    placed = booking_checkout.order(
        session, member, 0, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, Placed) and placed.total == 0
    assert states(org) == ["confirmed"]
    assert Order.objects.of(org).get().hold_expires_at is None
    cart = booking_cart.look(session, member, NOW)
    assert cart.items == () and not cart.locked
    assert cart.order is not None and cart.order.state == "confirmed"
    # The member books again: the new booking stays in the cart.
    later = choice(org, "Sauna 1", "Sauna visit", at(hour=14), at(hour=15))
    assert isinstance(booking_cart.add(session, member, later, 0, NOW), CartRow)
    for _ in range(2):
        cart = booking_cart.look(session, member, NOW)
        assert [item.row.choice for item in cart.items] == [later]


def confirm(org: Organization, made: Order) -> None:
    """What capturing the payment does to the order (`payment_capture`)."""
    TakenTime.objects.of(org).filter(booking__order=made).update(
        state=BookingState.CONFIRMED
    )
    Order.objects.of(org).filter(pk=made.pk).update(hold_expires_at=None)


def test_a_paid_order_empties_its_bookings_from_the_cart_once() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)
    assert isinstance(
        booking_checkout.order(
            session,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        ),
        Placed,
    )
    confirm(org, Order.objects.of(org).get())
    cart = booking_cart.look(session, member, NOW)
    assert cart.items == () and cart.order is not None
    assert cart.order.state == "confirmed" and not cart.locked
    later = choice(org, "Sauna 2", "Sauna visit", at(hour=10), at(hour=11))
    seen = cart_quotes(member=member, cart=[later], now=NOW)[0].total
    row = booking_cart.add(session, member, later, seen, NOW)
    assert isinstance(row, CartRow)
    for _ in range(2):
        cart = booking_cart.look(session, member, NOW)
        assert [item.row for item in cart.items] == [row]
        assert cart.ready and cart.order is not None
        assert cart.order.state == "confirmed"


def test_an_administrators_cancellation_is_read_from_the_bookings() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)
    assert isinstance(
        booking_checkout.order(
            session,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        ),
        Placed,
    )
    made = Order.objects.of(org).get()
    times = TakenTime.objects.of(org).filter(booking__order=made)
    # The evening of two held bookings cancelled: the payment start refuses
    # the order, so the cart is the member's again and can be ordered.
    times.filter(starts_at=at(hour=18)).update(state=BookingState.CANCELLED)
    cart = booking_cart.look(session, member, NOW)
    assert cart.order is not None and cart.order.state == "not-completed"
    assert not cart.locked and cart.ready
    again = booking_checkout.order(
        session, member, SEATS + EVENING, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(again, Placed) and again.public_id != made.public_id


def test_a_paid_order_cancelled_whole_leaves_the_cart() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)
    assert isinstance(
        booking_checkout.order(
            session,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        ),
        Placed,
    )
    made = Order.objects.of(org).get()
    confirm(org, made)
    TakenTime.objects.of(org).filter(booking__order=made).update(
        state=BookingState.CANCELLED
    )
    cart = booking_cart.look(session, member, NOW)
    assert cart.order is not None and cart.order.state == "cancelled"
    assert cart.items == ()


def test_the_order_refuses_what_the_member_did_not_see() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = SessionStore()
    assert booking_checkout.order(
        session, member, 0, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("cart-empty")
    session = filled(org, member)
    total = SEATS + EVENING
    # A different total, say from a change lost between two requests.
    assert booking_checkout.order(
        session, member, SEATS, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("price-changed", total)
    # A price that moves between the look and the order saves nothing.
    real = ordering.create_order

    def moved(**arguments: Any) -> Order:
        PriceTier.objects.of(org).filter(amount=10000).update(amount=12000)
        return real(**arguments)

    with mock.patch.object(ordering, "create_order", moved):
        assert booking_checkout.order(
            session, member, total, NOW, accepted_version=made_up.accepted(member)
        ) == Refusal("price-changed")
    assert not Order.objects.of(org).exists() and states(org) == []
    cart = booking_cart.look(session, member, NOW)
    assert [item.state for item in cart.items] == ["price-changed", "ok"]
    assert booking_checkout.order(
        session, member, total, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("not-ready")
    PriceTier.objects.of(org).filter(amount=12000).update(amount=10000)
    # A booking taken meanwhile is marked with the places left (T6).
    take(org, "Sauna 1", at(hour=11), at(hour=12), units=(1, 2, 3, 4, 5, 6, 7))
    assert booking_checkout.order(
        session, member, total, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("not-ready")
    cart = booking_cart.look(session, member, NOW)
    assert [(item.state, item.quote.places_left) for item in cart.items] == [
        ("taken", 1),
        ("ok", None),
    ]
    assert not Order.objects.of(org).exists()
    assert len(booking_cart.rows(session, member)) == 2
    # The member takes the marked line out and orders the rest.
    assert booking_cart.remove(session, member, cart.items[0].row.id, NOW) is True
    placed = booking_checkout.order(
        session, member, EVENING, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, Placed) and placed.total == EVENING
    assert Order.objects.of(org).count() == 1
    assert booking_cart.look(session, member, NOW).order is not None


def test_orders_made_at_the_same_moment_every_try_are_busy_not_taken() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)

    def contended(**arguments: Any) -> Order:
        raise ordering.OrderContendedError("other orders at the same moment")

    with mock.patch.object(ordering, "create_order", contended):
        assert booking_checkout.order(
            session,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        ) == (Refusal("busy"))
    # Nothing is marked: the same press works a moment later.
    cart = booking_cart.look(session, member, NOW)
    assert [item.state for item in cart.items] == ["ok", "ok"]
    assert isinstance(
        booking_checkout.order(
            session,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        ),
        Placed,
    )


def test_the_order_itself_refuses_a_different_total() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)
    cart = booking_cart.look(session, member, NOW)
    requests = [
        ordering.BookingRequest(
            item.quote.configuration_id,
            item.row.choice.booking_category_id,
            item.row.choice.starts_at,
            item.row.choice.ends_at,
            item.quote.quantity,
        )
        for item in cart.items
    ]
    with pytest.raises(ordering.OrderTotalChangedError):
        ordering.create_order(
            member=member,
            requests=requests,
            now=NOW,
            total=1,
            accepted_version=made_up.accepted(member),
        )
    assert not Order.objects.of(org).exists() and states(org) == []
    with pytest.raises(ValueError):
        ordering.create_order(
            member=member,
            requests=requests,
            now=NOW,
            total=-1,
            accepted_version=made_up.accepted(member),
        )
    made = ordering.create_order(
        member=member,
        requests=requests,
        now=NOW,
        total=SEATS + EVENING,
        accepted_version=made_up.accepted(member),
    )
    assert made.total == SEATS + EVENING


def test_the_orders_own_limits_leave_the_cart_as_it_was() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    PriceTier.objects.of(org).filter(amount=10000).update(amount=2_000_000_000)
    session = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 2)
    assert isinstance(
        booking_cart.add(session, member, seats, 4_000_000_000, NOW), CartRow
    )
    assert booking_checkout.order(
        session, member, 4_000_000_000, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("too-large")
    assert not Order.objects.of(org).exists()
    assert len(booking_cart.rows(session, member)) == 1


def test_one_press_makes_one_order() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    first = filled(org, member)
    second = SessionStore()
    second[booking_cart.SESSION_KEY] = first[booking_cart.SESSION_KEY]
    barrier = Barrier(2)
    real = ordering.create_order

    def slow(**arguments: Any) -> Order:
        time.sleep(0.5)
        return real(**arguments)

    def press(session: SessionStore) -> Placed | Refusal:
        try:
            barrier.wait(timeout=10)
            return booking_checkout.order(
                session,
                member,
                SEATS + EVENING,
                NOW,
                accepted_version=made_up.accepted(member),
            )
        finally:
            connections.close_all()

    with (
        mock.patch.object(ordering, "create_order", slow),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = list(pool.map(press, (first, second)))
    assert sorted(type(result).__name__ for result in results) == [
        "Placed",
        "Refusal",
    ]
    assert Refusal("busy") in results
    assert Order.objects.of(org).count() == 1


def test_a_press_loaded_before_the_last_press_saved_orders_nothing() -> None:
    # a free order holds nothing. The second request loaded the
    # session before the first press saved, and presses after it let go.
    org, member = scenario("scenarios/sauna-palace.toml")
    PriceTier.objects.of(org).update(amount=0)
    first = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 2)
    assert isinstance(booking_cart.add(first, member, seats, 0, NOW), CartRow)
    first.save()
    second = SessionStore(first.session_key)
    assert len(booking_cart.rows(second, member)) == 1
    assert isinstance(
        booking_checkout.order(
            first, member, 0, NOW, accepted_version=made_up.accepted(member)
        ),
        Placed,
    )
    assert booking_checkout.order(
        second, member, 0, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("cart-empty")
    assert Order.objects.of(org).count() == 1
    assert states(org) == ["confirmed"]


def test_an_older_session_saved_after_a_free_order_orders_nothing() -> None:
    # Every signed-in request saves the session. One that loaded it before
    # the order and ends after it writes the cart back without the order.
    org, member = scenario("scenarios/sauna-palace.toml")
    PriceTier.objects.of(org).update(amount=0)
    first = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 2)
    assert isinstance(booking_cart.add(first, member, seats, 0, NOW), CartRow)
    first.save()
    older = SessionStore(first.session_key)
    older["active_at"] = 1.0  # what the sign-in check writes; loads the session
    row = booking_cart.rows(first, member)[0]
    placed = booking_checkout.order(
        first, member, 0, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, Placed)
    assert list(Booking.objects.of(org).values_list("cart_row", flat=True)) == [row.id]
    older.save()
    later = SessionStore(first.session_key)
    assert len(booking_cart.rows(later, member)) == 1
    cart = booking_cart.look(later, member, NOW)
    assert cart.items == ()
    assert cart.order is not None and cart.order.public_id == placed.public_id
    assert cart.order.state == "confirmed"
    again = SessionStore(first.session_key)
    older.save()
    assert booking_checkout.order(
        again, member, 0, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("cart-empty")
    assert Order.objects.of(org).count() == 1
    # A booking added later is the member's own and can be ordered.
    other = choice(org, "Sauna 1", "Sauna visit", at(hour=14), at(hour=15))
    assert isinstance(booking_cart.add(again, member, other, 0, NOW), CartRow)
    assert isinstance(
        booking_checkout.order(
            again, member, 0, NOW, accepted_version=made_up.accepted(member)
        ),
        Placed,
    )
    assert Order.objects.of(org).count() == 2


def test_an_older_session_saved_after_a_paid_order_shows_it_held() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    first = filled(org, member)
    first.save()
    older = SessionStore(first.session_key)
    older["active_at"] = 1.0  # what the sign-in check writes; loads the session
    placed = booking_checkout.order(
        first, member, SEATS + EVENING, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, Placed)
    older.save()
    later = SessionStore(first.session_key)
    cart = booking_cart.look(later, member, NOW)
    assert cart.locked and cart.order is not None
    assert cart.order.public_id == placed.public_id
    assert booking_checkout.order(
        later, member, SEATS + EVENING, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("order-held")
    # Past its hold the same rows can be ordered again.
    after = NOW + timedelta(minutes=11)
    expire_holds(now=after)
    again = booking_checkout.order(
        later, member, SEATS + EVENING, after, accepted_version=made_up.accepted(member)
    )
    assert isinstance(again, Placed) and again.public_id != placed.public_id
    assert booking_cart.look(later, member, after).order == booking_cart.order(
        later, member, after
    )
    assert Order.objects.of(org).count() == 2


def test_the_cart_finds_only_its_own_members_orders() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="other@example.com",
        status="active",
        origin="member import",
    )
    session = filled(org, member)
    item = booking_cart.look(session, member, NOW).items[0]
    row, wanted = item.row, item.row.choice
    request = ordering.BookingRequest(
        item.quote.configuration_id,
        wanted.booking_category_id,
        wanted.starts_at,
        wanted.ends_at,
        wanted.quantity,
        row.id,
    )
    ordering.create_order(
        member=other,
        requests=[request],
        now=NOW,
        accepted_version=made_up.accepted(other),
    )
    cart = booking_cart.look(session, member, NOW)
    assert cart.order is None and len(cart.items) == 2
    bad: Any
    for bad in (1, "0" * 17):
        with pytest.raises(ValueError):
            ordering.create_order(
                member=member,
                requests=[replace(request, cart_row=bad)],
                now=NOW,
                accepted_version=made_up.accepted(member),
            )


def test_an_order_held_in_another_browser_refuses_this_one() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    here = filled(org, member)
    assert isinstance(
        booking_checkout.order(
            here,
            member,
            SEATS + EVENING,
            NOW,
            accepted_version=made_up.accepted(member),
        ),
        Placed,
    )
    there = SessionStore()
    later = choice(org, "Sauna 2", "Sauna visit", at(hour=10), at(hour=11))
    assert isinstance(booking_cart.add(there, member, later, 10000, NOW), CartRow)
    assert booking_checkout.order(
        there, member, 10000, NOW, accepted_version=made_up.accepted(member)
    ) == Refusal("order-held")
    assert booking_cart.look(there, member, NOW).order is None
    assert Order.objects.of(org).count() == 1


def send(client: Client, path: str, body: Any = None) -> Any:
    return client.post(
        path,
        json.dumps(body) if body is not None else "",
        content_type="application/json",
        HTTP_HOST=HOST,
        HTTP_ORIGIN="https://" + HOST,
        HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
        secure=True,
    )


def fill_page(client: Client, made: Example, total: int = 200) -> None:
    start = at(DAY, 10).astimezone(ZONE)
    booking = {
        "container": made.config.pk,
        "category": made.category.pk,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
        "total": total,
    }
    assert send(client, "/booking/cart/add/", booking).status_code == 200


def test_the_held_orders_bookings_carry_their_prices_by_start(
    made: Example, wire: Wire
) -> None:
    # The payment page lists them: the cart's own rows are not priced while
    # their order holds them.
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    for day in (DAY + timedelta(days=1), DAY):
        start = at(day, 10).astimezone(ZONE)
        booking = {
            "container": made.config.pk,
            "category": made.category.pk,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(),
            "total": 200,
        }
        assert send(client, "/booking/cart/add/", booking).status_code == 200
    assert (
        send(client, "/booking/cart/order/", made_up.order_body(400)).status_code == 200
    )
    cart = json.loads(get(client, "/booking/cart/").content)
    held = cart["order"]
    assert [booking["start"] for booking in held["bookings"]] == [
        at(DAY, 10).astimezone(ZONE).isoformat(),
        at(DAY + timedelta(days=1), 10).astimezone(ZONE).isoformat(),
    ]
    assert [booking["total"] for booking in held["bookings"]] == [200, 200]
    assert sum(booking["total"] for booking in held["bookings"]) == held["total"]
    assert [row["total"] for row in cart["bookings"]] == [None, None]


def test_the_page_orders_the_cart_and_goes_to_vipps(made: Example, wire: Wire) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    fill_page(client, made)
    for bad in ({"total": "200"}, {"total": -1}, [200], None):
        assert send(client, "/booking/cart/order/", bad).status_code == 400, bad
    assert get(client, "/booking/cart/order/").status_code == 405
    assert json.loads(
        send(client, "/booking/cart/order/", made_up.order_body(100)).content
    ) == {
        "reason": "price-changed",
        "text": "the price has changed",
        "total": 200,
    }
    response = send(client, "/booking/cart/order/", made_up.order_body(200))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    body = json.loads(response.content)
    made_order = Order.objects.of(made.org).get()
    assert body == {
        "order": str(made_order.public_id),
        "redirect": Payment.objects.of(made.org).get(order=made_order).redirect_url,
    }
    assert body["redirect"].startswith("https://apitest.vipps.no/")
    cart = json.loads(get(client, "/booking/cart/").content)
    assert made_order.hold_expires_at is not None
    assert (cart["locked"], cart["ready"], cart["total"]) == (True, False, 200)
    held = made_order.bookings.get()
    assert cart["order"] == {
        "id": str(made_order.public_id),
        "reference": made_order.reference,
        "state": "held",
        "total": 200,
        "hold_expires_at": made_order.hold_expires_at.astimezone(ZONE).isoformat(),
        "bookings": [
            {
                "container": {"id": made.config.pk, "number": made.config.number},
                "spots": [],
                "spot_word": None,
                "asset": made.asset.name,
                "type": held.booking_type_name,
                "category": held.booking_category_name,
                "places": 1,
                "start": at(DAY, 10).astimezone(ZONE).isoformat(),
                "end": at(DAY, 12).astimezone(ZONE).isoformat(),
                # Priced, where the cart's own row is not while it is held.
                "total": 200,
                "currency": "NOK",
                "state": "held",
            }
        ],
    }
    assert [(row["state"], row["total"]) for row in cart["bookings"]] == [
        ("held", None)
    ]
    assert json.loads(send(client, "/booking/cart/empty/").content) == {
        "reason": "order-held",
        "text": "a booking is waiting for payment",
    }
    row_id = cart["bookings"][0]["id"]
    assert send(client, f"/booking/cart/{row_id}/remove/").status_code == 409
    again = send(client, "/booking/cart/order/", made_up.order_body(200))
    assert (again.status_code, json.loads(again.content)) == (
        409,
        {"reason": "order-held", "text": "a booking is waiting for payment"},
    )
    # "Continue to Vipps" is the payment start for the same order.
    resumed = send(client, f"/payments/start/{made_order.public_id}/")
    assert json.loads(resumed.content) == {"redirect": body["redirect"]}
    assert Order.objects.of(made.org).count() == 1


def test_the_order_stays_held_when_vipps_cannot_be_reached(
    made: Example, wire: Wire
) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    fill_page(client, made)

    def unreachable(*, member: Member, order_public_id: Any) -> None:
        # The session already keeps the order when Vipps is called.
        stored = SessionStore(client.session.session_key).load()
        assert stored[booking_cart.SESSION_KEY]["order"] == str(order_public_id)
        raise VippsError(503)

    with mock.patch("randirandi.core.payments.checkout", unreachable):
        response = send(client, "/booking/cart/order/", made_up.order_body(200))
    made_order = Order.objects.of(made.org).get()
    assert (response.status_code, json.loads(response.content)) == (
        503,
        {"order": str(made_order.public_id), "error": "payment-unavailable"},
    )
    assert states(made.org) == [BookingState.HELD]
    retried = send(client, f"/payments/start/{made_order.public_id}/")
    assert json.loads(retried.content)["redirect"].startswith(
        "https://apitest.vipps.no/"
    )


def test_the_page_confirms_a_free_cart(made: Example, wire: Wire) -> None:
    PriceTier.objects.of(made.org).update(amount=0)
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    fill_page(client, made, total=0)
    response = send(client, "/booking/cart/order/", made_up.order_body(0))
    made_order = Order.objects.of(made.org).get()
    assert json.loads(response.content) == {
        "order": str(made_order.public_id),
        "confirmed": True,
    }
    assert not Payment.objects.of(made.org).exists()
    cart = json.loads(get(client, "/booking/cart/").content)
    assert (cart["count"], cart["order"]["state"]) == (0, "confirmed")


def test_past_its_hold_the_order_is_refused_and_the_cart_unlocked(
    made: Example, wire: Wire
) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    fill_page(client, made)
    assert (
        send(client, "/booking/cart/order/", made_up.order_body(200)).status_code == 200
    )
    made_order = Order.objects.of(made.org).get()
    assert made_order.hold_expires_at is not None
    later = made_order.hold_expires_at + timedelta(minutes=1)
    with mock.patch("django.utils.timezone.now", return_value=later):
        resumed = send(client, f"/payments/start/{made_order.public_id}/")
        cart = json.loads(get(client, "/booking/cart/").content)
    assert resumed.status_code == 409
    assert (cart["locked"], cart["order"]["state"]) == (False, "not-completed")
    assert [row["state"] for row in cart["bookings"]] == ["taken"]


def test_the_last_instant_refusal_stays_taken() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = filled(org, member)
    total = SEATS + EVENING
    real = ordering.create_order

    def rival(**arguments: Any) -> Order:
        # Someone else takes the whole sauna between the cart's look and the
        # order: no longer the cart's own doing.
        take(org, "Sauna 1", at(hour=18), at(hour=22))
        return real(**arguments)

    with mock.patch.object(ordering, "create_order", rival):
        refused = booking_checkout.order(
            session, member, total, NOW, accepted_version=made_up.accepted(member)
        )
    assert refused == Refusal("taken")
    assert not Order.objects.of(org).exists()
    assert len(booking_cart.rows(session, member)) == 2
