# SPDX-License-Identifier: AGPL-3.0-only
"""Retained local lifecycle script through HTTP, real client and real database.

Only the provider wire is fictional. This does not attest a live Vipps run.
"""

import json
import uuid
from datetime import datetime, timedelta

import pytest
from django.test import Client as Browser

from randirandi.core import payment_adjustments, payment_refunds
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.models import Member, Payment, PaymentTrouble, Refund
from randirandi.core.payments import PaymentUnavailableError, client_for
from randirandi.vipps import VippsError
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_calendar import Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, post, sign_in
from tests.test_ordering import NOW, checkout, request
from tests.test_payment_adjustments import administrator as administrator
from tests.test_payment_callbacks import WEBHOOK, payload, send
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payment_testing import approve
from tests.test_pricing import tiers

pytestmark = pytest.mark.django_db(transaction=True)
EXPIRED = NOW + timedelta(minutes=10)


def start(made: Example, *, multiple: bool = True) -> tuple[Payment, Browser]:
    order = checkout(
        made,
        request(made),
        *(
            [request(made, starts_at=at(hour=12), ends_at=at(hour=14))]
            if multiple
            else []
        ),
    )
    browser = Browser(enforce_csrf_checks=True)
    assert sign_in(browser, made.member).status_code == 302
    response = post(browser, f"/payments/start/{order.public_id}/", amount="1")
    assert response.status_code == 200
    assert json.loads(response.content)["redirect"].startswith(
        "https://apitest.vipps.no/"
    )
    payment = Payment.objects.of(made.org).get(order=order)
    assert payment.amount == (400 if multiple else 200)
    return payment, browser


@pytest.mark.parametrize("first", ["return", "webhook"])
def test_http_to_capture_partial_and_whole_refund_lifecycle(
    made: Example,
    wire: Wire,
    administrator: Member,
    first: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with application_login():
        payment, _ = start(made)
        assert approve(payment) == "AUTHORIZED"
        returned = Browser(enforce_csrf_checks=True)

        def return_request() -> None:
            response = returned.get(
                "/payments/return/",
                {"order": str(payment.order.public_id)},
                HTTP_HOST=HOST,
                secure=True,
            )
            assert response.status_code == 302
            assert "_auth_user_id" not in returned.session

        if first == "return":
            return_request()
        assert send(payload(payment)).status_code == 202
        if first == "webhook":
            return_request()
        assert send(payload(payment)).status_code == 409
        assert (
            Browser()
            .post(
                WEBHOOK,
                json.dumps(payload(payment)),
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
            )
            .status_code
            == 403
        )
        reconciliation.reconcile_requested(now=NOW)
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=6))
        payment.refresh_from_db()
        assert payment.captured_amount == 400 and payment.capture_key is not None
        assert payment.order.hold_expires_at is None
        bookings = list(payment.order.bookings.order_by("pk"))
        assert [row.taken_time.state for row in bookings] == ["confirmed", "confirmed"]
        client = client_for(payment)
        client.capture(
            payment.provider_reference, 400, "NOK", key=str(payment.capture_key)
        )
        assert [row[0] for row in wire.mutations] == ["capture"]

        key = uuid.uuid4()

        def refund_booking(key: uuid.UUID) -> payment_adjustments.Adjustment:
            return payment_adjustments.request_refund(
                administrator=administrator,
                order_public_id=payment.order.public_id,
                booking_public_id=bookings[0].public_id,
                request_key=key,
                now=NOW + timedelta(seconds=7),
            )

        first_refund = refund_booking(key)
        assert first_refund.refund_id is not None
        assert refund_booking(key) == first_refund
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=7))
        payment.refresh_from_db()
        refund = Refund.objects.of(made.org).get(pk=first_refund.refund_id)
        assert payment.refunded_amount == refund.amount == 200
        assert refund.completed_at is not None and refund.refund_key == key
        client.refund(payment.provider_reference, 200, "NOK", key=str(key))
        assert [row[0] for row in wire.mutations] == ["capture", "refund"]
        with pytest.raises(VippsError) as excess:
            client.refund(payment.provider_reference, 201, "NOK", key=str(uuid.uuid4()))
        assert excess.value.status == 400
        with pytest.raises(PaymentUnavailableError, match="No refundable"):
            refund_booking(uuid.uuid4())
        bookings[1].refresh_from_db()
        assert bookings[1].taken_time.state == "confirmed"

        remaining = payment_adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=uuid.uuid4(),
            now=NOW + timedelta(seconds=8),
        )
        assert remaining.refund_id is not None
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=8))
        payment_refunds.alert_completed(now=NOW + timedelta(seconds=9))
        payment_refunds.alert_completed(now=NOW + timedelta(seconds=10))
        payment.refresh_from_db()
        assert payment.captured_amount == payment.refunded_amount == 400
        assert payment.settled_at is not None
        assert Refund.objects.of(made.org).get(pk=remaining.refund_id).amount == 200
        assert set(
            payment.order.bookings.values_list("taken_time__state", flat=True)
        ) == {"cancelled"}
        assert [row[0] for row in wire.mutations] == ["capture", "refund", "refund"]
        assert caplog.text.count("payment refund completed:") == 2
        assert len(payment.webhook_events) == 1
        assert "fictional-token" not in caplog.text


@pytest.mark.parametrize("late", ["abandoned", "authorized", "captured"])
def test_expiry_and_late_provider_money_leave_no_charge_or_capacity(
    made: Example,
    wire: Wire,
    late: str,
) -> None:
    with application_login():
        payment, _ = start(made, multiple=False)
        if late != "abandoned":
            assert approve(payment) == "AUTHORIZED"
        if late == "captured":
            # An external late capture is provider state, not product permission.
            wire.payments[payment.provider_reference].captured = payment.amount
        reconciliation.reconcile_requested(now=EXPIRED)
        reconciliation.reconcile_requested(now=EXPIRED + timedelta(seconds=6))
        payment.refresh_from_db()
        assert payment.order.bookings.get().taken_time.state == "expired"
        assert payment.captured_amount - payment.refunded_amount == 0
        assert payment.settled_at is not None
        assert checkout(made).pk != payment.order_id
        expected = ["refund"] if late == "captured" else ["cancel"]
        assert [row[0] for row in wire.mutations] == expected
        if late == "captured":
            refund = Refund.objects.of(made.org).get(payment=payment)
            assert refund.automatic and refund.made_by_id is None
            assert refund.completed_at is not None


@pytest.mark.parametrize("accepted", [False, True])
def test_abandoned_payment_cancelled_before_authorization_settles(
    made: Example,
    wire: Wire,
    caplog: pytest.LogCaptureFixture,
    accepted: bool,
) -> None:
    # Observed provider sandbox run, 2026-10-14: Vipps reported the worker-cancelled
    # CREATED payment as TERMINATED, nothing authorized and the whole amount
    # cancelled. The client refused that shape, so the worker failed and
    # alerted every minute. An accepted cancel whose reply is lost is the
    # same state; an unsent one must be retried with the original key.
    with application_login():
        payment, _ = start(made, multiple=False)
        (wire.lose_after if accepted else wire.fail_before).add("cancel")
        reconciliation.reconcile_requested(now=EXPIRED)
        payment.refresh_from_db()
        key = payment.cancel_key
        assert key is not None and payment.settled_at is None
        assert payment.order.bookings.get().taken_time.state == "expired"
        reconciliation.reconcile_requested(now=EXPIRED + timedelta(seconds=6))
        payment.refresh_from_db()
        remote = wire.payments[payment.provider_reference]
        assert (remote.state, remote.authorized, remote.cancelled) == (
            "TERMINATED",
            0,
            payment.amount,
        )
        assert payment.state == "cancelled" and payment.settled_at is not None
        assert payment.captured_amount == payment.refunded_amount == 0
        assert [row[0] for row in wire.mutations] == ["cancel"]
        calls = [row for row in wire.calls if row[1].endswith("/cancel")]
        assert len(calls) == (1 if accepted else 2)
        assert {row[2]["Idempotency-Key"] for row in calls} == {str(key)}
        assert checkout(made).pk != payment.order_id
        reads = len(wire.calls)
        reconciliation.reconcile_requested(now=EXPIRED + timedelta(minutes=1))
        assert len(wire.calls) == reads
    assert caplog.text.count("payment reconciliation failed:") == 1


@pytest.mark.parametrize("seen", [NOW, EXPIRED])
def test_abandoned_payment_expired_by_the_provider_settles_without_a_cancel(
    made: Example,
    wire: Wire,
    caplog: pytest.LogCaptureFixture,
    seen: datetime,
) -> None:
    # Observed provider sandbox run, 2026-10-15: with the real ten-minute hold, Vipps
    # expired the unapproved payment itself and reported EXPIRED, nothing
    # authorized and the whole amount cancelled. The client refused that
    # shape, so the worker's reconciliation failed. The expiry can be seen just
    # before the order's own hold has passed, or after it.
    with application_login():
        payment, _ = start(made, multiple=False)
        remote = wire.payments[payment.provider_reference]
        remote.state, remote.cancelled = "EXPIRED", remote.amount
        reconciliation.reconcile_requested(now=seen)
        payment.refresh_from_db()
        assert payment.state == "expired" and payment.settled_at is not None
        assert payment.captured_amount == payment.refunded_amount == 0
        assert payment.cancel_key is None and wire.mutations == []
        assert payment.order.bookings.get().taken_time.state == "expired"
        assert checkout(made).pk != payment.order_id
        reads = len(wire.calls)
        reconciliation.reconcile_requested(now=EXPIRED + timedelta(minutes=1))
        assert len(wire.calls) == reads
    assert "payment reconciliation failed:" not in caplog.text


@pytest.mark.parametrize("seen", [NOW + timedelta(seconds=6), NOW + timedelta(days=14)])
@pytest.mark.parametrize("previous_failure", [False, True])
def test_aborted_payment_settles_and_clears_trouble_without_moving_money(
    made: Example,
    wire: Wire,
    caplog: pytest.LogCaptureFixture,
    seen: datetime,
    previous_failure: bool,
) -> None:
    with application_login():
        payment, _ = start(made, multiple=False)
        remote = wire.payments[payment.provider_reference]
        remote.state, remote.cancelled = "ABORTED", remote.amount
        if previous_failure:
            # A refused wire reply leaves a real unresolved read failure.
            # Recover it with the full-cancelled response observed at Vipps.
            remote.cancelled = remote.amount - 1
            reconciliation.reconcile_requested(now=NOW)
            remote.cancelled = remote.amount
            trouble = PaymentTrouble.objects.of(made.org).get(payment=payment)
            assert (trouble.step, trouble.kind) == ("read", "unaccepted")
            assert trouble.ended_at is None
        caplog.clear()
        before = len(wire.calls)
        reconciliation.reconcile_requested(now=seen)
        payment.refresh_from_db()
        assert payment.state == "cancelled" and payment.settled_at == seen
        assert payment.captured_amount == payment.refunded_amount == 0
        assert payment.cancel_key is None and payment.capture_key is None
        assert payment.redirect_url == "" and wire.mutations == []
        assert not Refund.objects.of(made.org).filter(payment=payment).exists()
        assert payment.order.bookings.get().taken_time.state == "expired"
        assert checkout(made).pk != payment.order_id
        calls = wire.calls[before:]
        assert any(method == "GET" for method, _, _, _ in calls)
        assert all(
            method == "GET" or path == "/accesstoken/get"
            for method, path, _, _ in calls
        )
        if previous_failure:
            trouble.refresh_from_db()
            assert trouble.ended_at == seen and trouble.tries == 1
        assert (
            not PaymentTrouble.objects.of(made.org)
            .filter(payment=payment, ended_at__isnull=True)
            .exists()
        )
        reads = len(wire.calls)
        reconciliation.reconcile_requested(now=seen + timedelta(minutes=1))
        assert len(wire.calls) == reads
    assert "payment reconciliation failed:" not in caplog.text
    assert "payment trouble lasting:" not in caplog.text


@pytest.mark.parametrize("operation", ["capture", "refund"])
@pytest.mark.parametrize("accepted", [False, True])
def test_uncertain_money_calls_recover_one_effect_and_original_key(
    made: Example,
    wire: Wire,
    administrator: Member,
    operation: str,
    accepted: bool,
) -> None:
    with application_login():
        payment, _ = start(made, multiple=False)
        assert approve(payment) == "AUTHORIZED"
        if operation == "refund":
            reconciliation.reconcile_requested(now=NOW)
            payment_adjustments.request_refund(
                administrator=administrator,
                order_public_id=payment.order.public_id,
                request_key=uuid.uuid4(),
                now=NOW + timedelta(seconds=1),
            )
        (wire.lose_after if accepted else wire.fail_before).add(operation)
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=1))
        payment.refresh_from_db()
        key = (
            payment.capture_key
            if operation == "capture"
            else Refund.objects.of(made.org).get(payment=payment).refund_key
        )
        assert key is not None and payment.settled_at is None
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=7))
        payment.refresh_from_db()
        assert payment.settled_at is not None
        assert payment.captured_amount == 200
        assert payment.refunded_amount == (200 if operation == "refund" else 0)
        assert len([row for row in wire.mutations if row[0] == operation]) == 1
        calls = [row for row in wire.calls if row[1].endswith("/" + operation)]
        assert len(calls) == (1 if accepted else 2)
        assert {row[2]["Idempotency-Key"] for row in calls} == {str(key)}


def test_free_http_checkout_never_contacts_provider(made: Example, wire: Wire) -> None:
    tiers(made, [(240, 60, 0)])
    order = checkout(made)
    browser = Browser(enforce_csrf_checks=True)
    assert sign_in(browser, made.member).status_code == 302
    assert json.loads(post(browser, f"/payments/start/{order.public_id}/").content) == {
        "redirect": "/"
    }
    assert not wire.calls and not Payment.objects.of(made.org).exists()
    assert order.bookings.get().taken_time.state == "confirmed"
