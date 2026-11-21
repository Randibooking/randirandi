# SPDX-License-Identifier: AGPL-3.0-only
"""Production configuration through HTTP and the database; provider wire is fake."""

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.test import Client as Browser
from django.test import override_settings

from randirandi.core import payment_adjustments, payment_reconciliation, payments
from randirandi.core.models import Member, Payment, PaymentProviderAccount, Refund
from randirandi.core.payments import PaymentUnavailableError
from randirandi.environment import Environment
from randirandi.payment_configuration import PaymentConfigurationError, Secrets
from randirandi.vipps import Client
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, post, sign_in
from tests.test_ordering import NOW, checkout
from tests.test_payment_accounts import values
from tests.test_payment_adjustments import administrator as administrator
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payment_callbacks import payload, send
from tests.test_payment_testing import approve

pytestmark = pytest.mark.django_db(transaction=True)
GATE = "public-fictional-production-gate"


@pytest.fixture
def production(made: Example) -> Iterator[Wire]:
    PaymentProviderAccount.objects.create(
        organization=made.org,
        payment_provider="vipps",
        merchant_number="123456",
        credentials="VIPPS",
        test_mode=False,
    )
    wire = Wire(host="api.vipps.no")
    secrets = Secrets(values(), Environment.PRODUCTION)
    credentials = secrets.credentials(
        prefix="VIPPS", merchant_number="123456", test_mode=False
    )
    secrets._clients["VIPPS"] = Client(
        Environment.PRODUCTION, credentials, transport=wire
    )
    with override_settings(
        ENVIRONMENT="production", VIPPS_SECRETS=secrets, ACCESS_GATE_PASSWORD=GATE
    ):
        yield wire


def test_gated_production_booking_capture_and_refund(
    made: Example, production: Wire, administrator: Member
) -> None:
    browser = Browser(enforce_csrf_checks=True)
    assert browser.get("/login/", HTTP_HOST=HOST, secure=True).status_code == 401
    browser.defaults["HTTP_AUTHORIZATION"] = (
        "Basic " + base64.b64encode(f"client:{GATE}".encode()).decode()
    )
    with application_login():
        assert sign_in(browser, made.member).status_code == 302
        order = checkout(made)
        response = post(browser, f"/payments/start/{order.public_id}/", amount="1")
        assert response.status_code == 200
        assert json.loads(response.content)["redirect"].startswith(
            "https://api.vipps.no/"
        )
        payment = Payment.objects.of(made.org).get(order=order)
        assert payment.amount == 200
        assert payment.test_mode is False
        # A later edit of the agreement cannot retarget this existing intent.
        PaymentProviderAccount.objects.of(made.org).update(
            test_mode=True, credentials="VIPPS_OTHER", merchant_number="654321"
        )

        # Production never uses the test-approval endpoint. This changes only
        # the fictional provider's state, standing in for approval in the app.
        calls_before = len(production.calls)
        with pytest.raises(PaymentUnavailableError):
            approve(payment)
        assert len(production.calls) == calls_before
        remote = production.payments[payment.provider_reference]
        remote.state, remote.authorized = "AUTHORIZED", remote.amount

        returned = Browser()
        returned_response = returned.get(
            "/payments/return/",
            {"order": str(order.public_id)},
            HTTP_HOST=HOST,
            secure=True,
        )
        assert returned_response.status_code == 302
        assert "_auth_user_id" not in returned.session
        assert (
            returned.get(
                returned_response.headers["Location"], HTTP_HOST=HOST, secure=True
            ).status_code
            == 401
        )
        assert send(payload(payment)).status_code == 202
        assert send(payload(payment)).status_code == 409
        payment_reconciliation.reconcile_requested(now=NOW)
        payment_reconciliation.reconcile_requested(now=NOW + timedelta(seconds=6))
        payment.refresh_from_db()
        assert payment.captured_amount == 200
        assert payment.order.bookings.get().taken_time.state == "confirmed"

        refund_key = uuid.uuid4()
        result = payment_adjustments.request_refund(
            administrator=administrator,
            order_public_id=order.public_id,
            request_key=refund_key,
            now=NOW + timedelta(seconds=7),
        )
        payment_reconciliation.reconcile_requested(now=NOW + timedelta(seconds=7))
        payment.refresh_from_db()
        assert result.refund_id is not None
        refund = Refund.objects.of(made.org).get(pk=result.refund_id)
        assert payment.refunded_amount == refund.amount == 200
        assert refund.completed_at is not None and refund.refund_key == refund_key
        assert payment.order.bookings.get().taken_time.state == "cancelled"
        assert [row[0] for row in production.mutations] == ["capture", "refund"]
        assert not any("/test/" in path for _, path, _, _ in production.calls)


def test_saved_production_intent_refuses_sandbox_retry_and_callback(
    made: Example, production: Wire
) -> None:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    calls_before = len(production.calls)
    with override_settings(VIPPS_SECRETS=Secrets(values(), Environment.SANDBOX)):
        with pytest.raises(PaymentConfigurationError, match="deployment"):
            payments.checkout(
                member=made.member, order_public_id=order.public_id, now=NOW
            )
        # A deployment mismatch is unavailable server configuration, not an
        # invalid provider signature; the callback remains unprocessed.
        assert send(payload(payment)).status_code == 503
    assert len(production.calls) == calls_before
    payment.refresh_from_db()
    assert payment.test_mode is False
    assert payment.webhook_events == []
