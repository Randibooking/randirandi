# SPDX-License-Identifier: AGPL-3.0-only
"""the member lets go of an order waiting for payment.


Made-up setups only.
"""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import mock

import pytest
from django.db import connections
from django.test import Client

from randirandi.core import booking_checkout, payments
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.booking_cart import Refusal
from randirandi.core.models import BookingState, Member, Order, Payment
from randirandi.core.ordering import create_order
from randirandi.core.payments import order_lock
from randirandi.vipps import PaymentStatus
from tests import made_up
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_booking_checkout import fill_page, send
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import get, sign_in
from tests.test_ordering import NOW, request
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payment_capture import Money, ready
from tests.test_payment_capture import money as money
from tests.test_payments import Provider
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)
LATER = NOW + timedelta(minutes=2)


def held_state(payment: Payment) -> str:
    return payment.order.bookings.get().taken_time.state


def waiting(made: Example, money: Money, state: str = "CREATED") -> Payment:
    """A held order whose payment Vipps holds as `state`, the worker caught up."""
    payment = ready(made, money)
    if state == "CREATED":
        money.snapshots[payment.provider_reference] = PaymentStatus(
            payment.provider_reference, "CREATED", 200, 0, 0, 0, 0
        )
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    return payment


def test_the_times_are_free_at_once_and_vipps_cancels_the_payment(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = waiting(made, money)
    assert held_state(payment) == BookingState.HELD
    assert booking_checkout.cancel(made.member, payment.order.public_id, LATER)
    payment.refresh_from_db()
    assert held_state(payment) == BookingState.EXPIRED
    # The worker is asked at once, not at its next periodic look.
    assert payment.callback_received_at is not None
    reconciliation.reconcile_requested(now=LATER)
    payment.refresh_from_db()
    assert len(money.cancellations) == 1 and money.captures == []
    assert payment.settled_at is not None and payment.captured_amount == 0
    # A second press: nothing left to let go, nothing more at Vipps.
    assert booking_checkout.cancel(made.member, payment.order.public_id, LATER)
    reconciliation.reconcile_requested(now=LATER + timedelta(minutes=1))
    assert len(money.cancellations) == 1


@pytest.mark.parametrize("taken", ["capture_key", "captured_amount"])
def test_a_capture_that_reached_vipps_is_left_to_the_worker(
    made: Example, provider: Provider, money: Money, taken: str
) -> None:
    payment = waiting(made, money)
    # The worker stopped between Vipps taking the money and the bookings
    # being confirmed: it confirms them or pays back, never the member.
    change = (
        {"capture_key": uuid.uuid4()}
        if taken == "capture_key"
        else {"captured_amount": 200}
    )
    Payment.objects.of(made.org).filter(pk=payment.pk).update(**change)
    done = booking_checkout.cancel(made.member, payment.order.public_id, LATER)
    assert done == Refusal("settling")
    assert held_state(payment) == BookingState.HELD
    assert money.cancellations == []


def test_paying_again_after_a_cancel_leaves_the_new_order_alone(
    made: Example, provider: Provider, money: Money
) -> None:
    old = waiting(made, money)
    assert booking_checkout.cancel(made.member, old.order.public_id, LATER)
    # The same times, ordered and sent to Vipps again; the old payment is
    # then approved late in the Vipps app.
    order = create_order(
        member=made.member,
        requests=[request(made)],
        now=LATER,
        accepted_version=made_up.accepted(made.member),
    )
    payments.checkout(member=made.member, order_public_id=order.public_id, now=LATER)
    new = Payment.objects.of(made.org).get(order=order)
    money.snapshots[new.provider_reference] = PaymentStatus(
        new.provider_reference, "CREATED", 200, 0, 0, 0, 0
    )
    money.snapshots[old.provider_reference] = PaymentStatus(
        old.provider_reference, "AUTHORIZED", 200, 200, 0, 0, 0
    )
    reconciliation.reconcile_requested(now=LATER)
    assert money.captures == []
    assert [reference for reference, _ in money.cancellations] == [
        old.provider_reference
    ]
    assert held_state(new) == BookingState.HELD
    new.refresh_from_db()
    assert new.cancel_key is None and new.settled_at is None


def test_an_approval_after_the_cancel_is_cancelled_never_captured(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = waiting(made, money)
    assert booking_checkout.cancel(made.member, payment.order.public_id, LATER)
    # The member approves in the Vipps app before the worker looks.
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", 200, 200, 0, 0, 0
    )
    reconciliation.reconcile_requested(now=LATER)
    payment.refresh_from_db()
    assert money.captures == [] and len(money.cancellations) == 1
    assert held_state(payment) == BookingState.EXPIRED
    assert payment.settled_at is not None and payment.captured_amount == 0


def test_a_paid_order_is_not_let_go(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = waiting(made, money, "AUTHORIZED")
    assert held_state(payment) == BookingState.CONFIRMED
    calls = len(money.reads)
    done = booking_checkout.cancel(made.member, payment.order.public_id, LATER)
    assert done == Refusal("confirmed")
    payment.refresh_from_db()
    assert held_state(payment) == BookingState.CONFIRMED
    assert money.cancellations == [] and len(money.reads) == calls


def test_only_the_members_own_order(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = waiting(made, money)
    other = Member.objects.create(
        organization=made.org,
        role_id=made.member.role_id,
        email="other@example.com",
        status="active",
        origin="member import",
    )
    order = payment.order
    assert booking_checkout.cancel(other, order.public_id, LATER) is False
    assert booking_checkout.cancel(made.member, uuid.uuid4(), LATER) is False
    assert held_state(payment) == BookingState.HELD


def test_past_its_hold_the_times_are_freed_without_the_worker(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = waiting(made, money)
    held_until = payment.order.hold_expires_at
    assert held_until is not None
    expired = held_until + timedelta(seconds=1)
    assert booking_checkout.cancel(made.member, payment.order.public_id, expired)
    assert held_state(payment) == BookingState.EXPIRED
    # Again: nothing left to let go.
    assert booking_checkout.cancel(made.member, payment.order.public_id, expired)


def test_the_cancel_waits_for_the_payment_worker_then_refuses(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = waiting(made, money)
    order_id = payment.order_id
    taken, done = Event(), Event()

    def worker() -> None:
        try:
            with order_lock(order_id):
                taken.set()
                done.wait(10)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(worker)
        assert taken.wait(10)
        with mock.patch.object(booking_checkout, "CANCEL_WAIT", 0.3):
            started = time.monotonic()
            refused = booking_checkout.cancel(
                made.member, payment.order.public_id, LATER
            )
            waited = time.monotonic() - started
        assert refused == Refusal("settling") and waited >= 0.3
        assert held_state(payment) == BookingState.HELD

        # Let go while the cancel waits: it takes the lock and goes on.
        def finish_soon() -> None:
            time.sleep(0.3)
            done.set()

        with mock.patch.object(booking_checkout, "CANCEL_WAIT", 5.0):
            pool.submit(finish_soon)
            assert booking_checkout.cancel(made.member, payment.order.public_id, LATER)
        future.result()
    assert held_state(payment) == BookingState.EXPIRED


def test_the_payment_page_lets_go_and_the_cart_has_the_times_again(
    made: Example, wire: Wire
) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    fill_page(client, made)
    assert (
        send(client, "/booking/cart/order/", made_up.order_body(200)).status_code == 200
    )
    order = Order.objects.of(made.org).get()
    path = f"/booking/order/{order.public_id}/cancel/"
    assert get(client, path).status_code == 405
    response = send(client, path)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    cart = json.loads(response.content)
    assert (cart["locked"], cart["ready"], cart["total"]) == (False, True, 200)
    assert cart["order"]["state"] == "not-completed"
    assert [row["state"] for row in cart["bookings"]] == ["ok"]
    # Pay again: a new order from the same times.
    assert (
        send(client, "/booking/cart/order/", made_up.order_body(200)).status_code == 200
    )
    assert Order.objects.of(made.org).count() == 2
    unknown = send(client, f"/booking/order/{uuid.uuid4()}/cancel/")
    assert (unknown.status_code, json.loads(unknown.content)) == (
        404,
        {"error": "not-found"},
    )
    # Signed out, with the login page's CSRF cookie.
    signed_out = Client(enforce_csrf_checks=True)
    assert get(signed_out, "/login/").status_code == 200
    away = send(signed_out, path)
    assert (away.status_code, away["Location"]) == (302, f"/login/?next={path}")
    assert Order.objects.of(made.org).count() == 2
