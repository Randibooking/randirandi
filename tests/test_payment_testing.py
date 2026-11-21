# SPDX-License-Identifier: AGPL-3.0-only
"""Sandbox-only approval through actual client parsing and persisted intents."""

import io
import json
import os
import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import override_settings

from randirandi.core import payment_testing, payments
from randirandi.core.models import Payment
from randirandi.core.payments import PaymentUnavailableError
from randirandi.vipps import MAX_RESPONSE, VippsError
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_ordering import NOW, checkout

pytestmark = pytest.mark.django_db(transaction=True)
PHONE = "4799999999"
KEY = uuid.UUID("11111111-2222-3333-4444-555555555555")


def created(made: Example) -> Payment:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    return Payment.objects.of(made.org).get(order=order)


def approve(payment: Payment) -> str:
    return payment_testing.approve(
        organization_id=payment.organization_id,
        order_public_id=payment.order.public_id,
        phone=PHONE,
        request_key=KEY,
    ).state


def test_empty_response_approval_and_retry_observe_provider_state(
    made: Example, wire: Wire
) -> None:
    payment = created(made)
    with application_login():
        assert approve(payment) == "AUTHORIZED"
        assert approve(payment) == "AUTHORIZED"
    calls = [row for row in wire.calls if row[1].endswith("/approve")]
    assert len(calls) == 1 and calls[0][2]["Idempotency-Key"] == str(KEY)
    assert calls[0][3] is not None
    assert json.loads(calls[0][3])["customer"] == {"phoneNumber": PHONE}
    payment.refresh_from_db()
    # Only authoritative product reconciliation may confirm or capture.
    assert payment.state == "created" and payment.captured_amount == 0
    assert payment.order.bookings.get().taken_time.state == "held"
    assert PHONE not in str(payment.__dict__)


def test_uncertain_approval_retry_reads_state_without_second_mutation(
    made: Example, wire: Wire
) -> None:
    payment = created(made)
    wire.lose_approval_reply = True
    with pytest.raises(VippsError) as caught:
        approve(payment)
    assert "private" not in str(caught.value)
    wire.lose_approval_reply = False
    assert approve(payment) == "AUTHORIZED"
    assert sum(row[1].endswith("/approve") for row in wire.calls) == 1


@pytest.mark.parametrize("code", [301, 400, 401, 403, 429, 500])
def test_provider_failure_is_bounded_and_does_not_confirm(
    made: Example, wire: Wire, code: int
) -> None:
    payment = created(made)
    wire.approval_code, wire.approval_body = code, b"fictional-private-response"
    with pytest.raises(VippsError) as caught:
        approve(payment)
    assert caught.value.status == code and "private" not in str(caught.value)
    payment.refresh_from_db()
    assert payment.state == "created"


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/?token=fictional-token",
        "http://apitest.vipps.no/?token=fictional-token",
        "https://u:p@apitest.vipps.no/?token=fictional-token",
        "https://apitest.vipps.no:8443/?token=fictional-token",
        "https://apitest.vipps.no/?token=a&token=b",
        "https://apitest.vipps.no/?other=fictional-token",
        "https://apitest.vipps.no/?token=bad%0Atoken",
        "https://apitest.vipps.no/?token=fictional-token#fragment",
    ],
)
def test_invalid_saved_redirect_never_approves(
    made: Example, wire: Wire, redirect: str
) -> None:
    payment = created(made)
    Payment.objects.of(made.org).filter(pk=payment.pk).update(redirect_url=redirect)
    with pytest.raises(PaymentUnavailableError):
        approve(payment)
    assert not any(row[1].endswith("/approve") for row in wire.calls)


@pytest.mark.parametrize("environment", ["production", "invalid"])
def test_other_environment_refuses_before_provider_access(
    made: Example, wire: Wire, environment: str
) -> None:
    payment = created(made)
    wire.calls.clear()
    with (
        override_settings(ENVIRONMENT=environment),
        pytest.raises(PaymentUnavailableError),
    ):
        approve(payment)
    assert not wire.calls


def test_tenant_and_transaction_guards(made: Example, wire: Wire) -> None:
    payment = created(made)
    wire.calls.clear()
    with pytest.raises(PaymentUnavailableError):
        payment_testing.approve(
            organization_id=made.org.pk + 1,
            order_public_id=payment.order.public_id,
            phone=PHONE,
            request_key=KEY,
        )
    with transaction.atomic(), pytest.raises(RuntimeError, match="own transaction"):
        approve(payment)
    assert not wire.calls


def test_oversized_reply_is_refused(made: Example, wire: Wire) -> None:
    payment = created(made)
    wire.approval_body = b"x" * (MAX_RESPONSE + 1)
    with pytest.raises(VippsError):
        approve(payment)


def test_operator_command_withholds_identity_and_removes_inherited_phone(
    made: Example, wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = created(made)
    monkeypatch.setenv("RANDIRANDI_TEST_PHONE", PHONE)
    out = io.StringIO()
    call_command(
        "approve_test_payment",
        str(made.org.pk),
        str(payment.order.public_id),
        "--request-key",
        str(KEY),
        stdout=out,
    )
    assert out.getvalue() == "Test payment provider state: AUTHORIZED.\n"
    assert "RANDIRANDI_TEST_PHONE" not in os.environ
    with pytest.raises(CommandError, match="unresolved"):
        call_command(
            "approve_test_payment",
            str(made.org.pk),
            str(payment.order.public_id),
            "--request-key",
            str(KEY),
            stdout=out,
        )
