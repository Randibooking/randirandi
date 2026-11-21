# SPDX-License-Identifier: AGPL-3.0-only
"""Real HTTP guards, durable event identities and the shared provider-read queue."""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from email.utils import format_datetime
from threading import Barrier
from typing import Any
from unittest import mock

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as Browser
from django.test import override_settings

from randirandi.core import payment_callbacks as callbacks
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core import payments
from randirandi.core.member_data import organization_transaction
from randirandi.core.models import Order, Payment
from randirandi.core.retention import purge_organization
from randirandi.vipps import AUTH_PREFIX, Client, PaymentStatus
from tests import made_up
from tests.test_access_gate import PASSWORD as GATE_PASSWORD
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, post, sign_in
from tests.test_ordering import NOW, checkout
from tests.test_payments import REDIRECT, Provider, saved_rows
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)
WEBHOOK = "/payments/webhook/"
RETURN = "/payments/return/"
SECRET = "invented-webhook-secret"  # noqa: S105 - fictional fixture


@pytest.fixture(autouse=True)
def page_settings() -> Iterator[None]:
    with (
        override_settings(
            ALLOWED_HOSTS=[".sandbox.example.org"],
            ORGANIZATION_DOMAIN="sandbox.example.org",
            ACCESS_GATE_PASSWORD="",
        ),
        mock.patch("django.utils.timezone.now", return_value=NOW),
        mock.patch("randirandi.core.authentication.utc_now", return_value=NOW),
        mock.patch("randirandi.core.login.wake_delivery"),
    ):
        yield


@dataclass
class Reader:
    calls: list[str] = field(default_factory=list)
    during: Callable[[], None] = lambda: None
    transform: Callable[[PaymentStatus], PaymentStatus] = lambda value: value


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> Reader:
    reader = Reader()

    def read(self: Client, reference: str, currency: str) -> PaymentStatus:
        assert not connection.in_atomic_block and connection.get_autocommit()
        reader.calls.append(reference)
        reader.during()
        return reader.transform(PaymentStatus(reference, "CREATED", 200, 0, 0, 0, 0))

    monkeypatch.setattr(Client, "status", read)
    return reader


def payment_for(made: Example) -> Payment:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    return Payment.objects.of(made.org).get(order=order)


def payload(payment: Payment, **changes: Any) -> dict[str, Any]:
    return {
        "msn": payment.merchant_number,
        "reference": payment.provider_reference,
        "pspReference": "fictional-operation-reference",
        "name": "AUTHORIZED",
        "amount": {"value": payment.amount, "currency": payment.currency},
        "timestamp": NOW.isoformat(),
        "success": True,
        "userDetails": {"email": "private-payer@example.test"},
    } | changes


def signed(
    body: bytes, *, host: str = HOST, target: str = WEBHOOK, secret: str = SECRET
) -> dict[str, str]:
    date = format_datetime(NOW, usegmt=True)
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    message = f"POST\n{target}\n{date};{host};{digest}".encode()
    signature = base64.b64encode(
        hmac.digest(secret.encode(), message, "sha256")
    ).decode()
    return {
        "Authorization": AUTH_PREFIX + signature,
        "x-ms-date": date,
        "x-ms-content-sha256": digest,
    }


def send(
    data: dict[str, Any], *, host: str = HOST, target: str = WEBHOOK, **headers: str
) -> Any:
    body = json.dumps(data).encode()
    return Browser(enforce_csrf_checks=True).post(
        target,
        data=body,
        content_type="application/json",
        secure=True,
        HTTP_HOST=host,
        headers=signed(body, host=host, target=target) | headers,
    )


def test_start_is_scoped_post_with_csrf_and_uses_server_amount(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)
    browser = Browser(enforce_csrf_checks=True)
    path = f"/payments/start/{order.public_id}/"
    assert browser.get(path, HTTP_HOST=HOST, secure=True).status_code == 302
    assert browser.post(path, HTTP_HOST=HOST, secure=True).status_code == 403
    assert sign_in(browser, made.member).status_code == 302
    assert browser.get(path, HTTP_HOST=HOST, secure=True).status_code == 405
    assert browser.post(path, HTTP_HOST=HOST, secure=True).status_code == 403
    assert not Payment.objects.of(made.org).exists()
    response = post(
        browser, path, amount="1", return_url="https://untrusted.example/", msn="999999"
    )
    assert response.status_code == 200 and json.loads(response.content) == {
        "redirect": REDIRECT
    }
    assert "no-store" in response["Cache-Control"]
    assert "form-action 'self'" in response["Content-Security-Policy"]
    assert provider.calls[0]["amount"] == order.total == 200


@pytest.mark.parametrize("first", ["return", "webhook"])
def test_return_and_webhook_use_one_provider_queue_in_either_order(
    made: Example, provider: Provider, reader: Reader, first: str
) -> None:
    payment = payment_for(made)
    browser = Browser(enforce_csrf_checks=True)
    with override_settings(ACCESS_GATE_PASSWORD=GATE_PASSWORD):
        actions: dict[str, Callable[[], Any]] = {
            "webhook": lambda: send(payload(payment)),
            "return": lambda: browser.get(
                RETURN,
                {"order": str(payment.order.public_id), "status": "CAPTURED"},
                HTTP_HOST=HOST,
                secure=True,
            ),
        }
        assert actions[first]().status_code in {202, 302}
        assert actions["webhook" if first == "return" else "return"]().status_code in {
            202,
            302,
        }
        assert not browser.session.get("_auth_user_id")
        # Exact callback/return exemptions do not open their neighbors.
        for path in (
            "/payments/",
            WEBHOOK + "extra/",
            "/payments/webhook",
            RETURN + "extra/",
        ):
            assert browser.get(path, HTTP_HOST=HOST, secure=True).status_code == 401
    assert reader.calls == []
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=1))
    assert reader.calls == [payment.provider_reference]
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=10))
    payment.refresh_from_db()
    assert reader.calls == [payment.provider_reference] * 2  # Periodic recovery too.
    assert payment.state == "created"  # Signed/browser claims never establish this.
    assert payment.order.bookings.get().taken_time.state == "held"
    assert len(payment.webhook_events) == 1 and len(payment.webhook_events[0]) == 64
    assert "private-payer" not in str(payment.__dict__)


def test_semantic_redelivery_is_refused_even_with_reordered_json(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    data = payload(payment)
    assert send(data).status_code == 202
    before = Payment.objects.of(made.org).values().get()
    assert send(dict(reversed(list(data.items())))).status_code == 409
    assert send(data | {"userDetails": {"new": "ignored"}}).status_code == 409
    assert Payment.objects.of(made.org).values().get() == before


@pytest.mark.parametrize(
    "fault",
    [
        "unsigned",
        "wrong_signature",
        "wrong_body_hash",
        "old_date",
        "wrong_host",
        "wrong_target",
        "wrong_merchant",
        "wrong_reference",
        "wrong_currency",
        "boolean_amount",
        "too_large_amount",
        "naive_timestamp",
        "unknown_event",
        "non_boolean_success",
    ],
)
def test_forged_or_malformed_callbacks_leave_no_queue_entry(
    made: Example, provider: Provider, reader: Reader, fault: str
) -> None:
    payment = payment_for(made)
    data = payload(payment)
    headers: dict[str, str] = {}
    if fault == "wrong_merchant":
        data["msn"] = "654321"
    elif fault == "wrong_reference":
        data["reference"] = uuid.uuid4().hex
    elif fault == "wrong_currency":
        data["amount"]["currency"] = "DKK"
    elif fault == "boolean_amount":
        data["amount"]["value"] = True
    elif fault == "too_large_amount":
        data["amount"]["value"] = 201
    elif fault == "naive_timestamp":
        data["timestamp"] = "2027-03-01T12:00:00"
    elif fault == "unknown_event":
        data["name"] = "MADE_UP"
    elif fault == "non_boolean_success":
        data["success"] = "true"
    body = json.dumps(data).encode()
    headers = signed(body)
    if fault == "unsigned":
        headers = {}
    elif fault == "wrong_signature":
        headers["Authorization"] += "a"
    elif fault == "wrong_body_hash":
        headers["x-ms-content-sha256"] = "wrong"
    elif fault == "old_date":
        headers["x-ms-date"] = format_datetime(NOW - timedelta(minutes=6), usegmt=True)
    elif fault == "wrong_host":
        headers = signed(body, host="another.sandbox.example.org")
    elif fault == "wrong_target":
        headers = signed(body, target=WEBHOOK + "?changed=1")
    response = Browser(enforce_csrf_checks=True).post(
        WEBHOOK,
        data=body,
        content_type="application/json",
        HTTP_HOST=HOST,
        headers=headers,
        secure=True,
    )
    assert response.status_code == 403
    payment.refresh_from_db()
    assert payment.webhook_events == [] and payment.callback_received_at is None
    reconciliation.reconcile_requested(now=NOW)
    # Invalid callbacks do not enqueue; ordinary recovery still checks the intent.
    assert reader.calls == [payment.provider_reference]


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{",
        b"\xff",
        b"{" + b" " * 17000,
        b"[" * 2000 + b"]" * 2000,
        b'{"reference":"a","reference":"b"}',
    ],
)
def test_invalid_wire_payload_is_bounded_and_refused(
    made: Example, provider: Provider, body: bytes
) -> None:
    payment = payment_for(made)
    response = Browser().post(
        WEBHOOK,
        data=body,
        content_type="application/json",
        HTTP_HOST=HOST,
        headers=signed(body),
        secure=True,
    )
    assert response.status_code == 403
    payment.refresh_from_db()
    assert payment.webhook_events == []


def test_foreign_return_and_callback_cannot_select_another_tenant(
    made: Example, provider: Provider, reader: Reader
) -> None:
    payment = payment_for(made)
    other = made_up.everything("pinehill")
    host = other.organization.subdomain + ".sandbox.example.org"
    assert send(payload(payment), host=host).status_code == 403
    response = Browser().get(
        RETURN,
        {"order": str(payment.order.public_id), "next": "https://untrusted.example/"},
        HTTP_HOST=host,
        secure=True,
    )
    assert response.status_code == 302 and response["Location"] == "/login/"
    payment.refresh_from_db()
    assert payment.callback_received_at is None
    assert reader.calls == []


def test_callback_during_provider_read_survives_same_clock_tick(
    made: Example, provider: Provider, reader: Reader
) -> None:
    payment = payment_for(made)
    callbacks.request_reconciliation(made.org, payment.order.public_id, now=NOW)
    reader.during = lambda: callbacks.request_reconciliation(
        made.org, payment.order.public_id, now=NOW
    )
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert (
        payment.callback_received_at is not None
        and payment.provider_checked_at is not None
    )
    assert payment.callback_received_at > payment.provider_checked_at
    reader.during = lambda: None
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    payment.refresh_from_db()
    assert (
        payment.callback_received_at is not None
        and payment.provider_checked_at is not None
    )
    assert payment.provider_checked_at >= payment.callback_received_at
    assert len(reader.calls) == 2


def test_provider_failure_keeps_queue_and_throttles_retry_without_error_text(
    made: Example, provider: Provider, reader: Reader, caplog: pytest.LogCaptureFixture
) -> None:
    payment = payment_for(made)
    callbacks.request_reconciliation(made.org, payment.order.public_id, now=NOW)

    def fail() -> None:
        raise RuntimeError("private-provider-reply")

    reader.during = fail
    reconciliation.reconcile_requested(now=NOW)
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=4))
    payment.refresh_from_db()
    assert payment.provider_checked_at is None and len(reader.calls) == 1
    assert "private-provider-reply" not in caplog.text
    reader.during = lambda: None
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    payment.refresh_from_db()
    assert payment.provider_checked_at is not None and len(reader.calls) == 2


def test_provider_amount_mismatch_cannot_update_payment(
    made: Example, provider: Provider, reader: Reader
) -> None:
    payment = payment_for(made)
    callbacks.request_reconciliation(made.org, payment.order.public_id, now=NOW)
    reader.transform = lambda value: replace(value, amount=201)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.provider_checked_at is None and payment.state == "created"


@pytest.mark.parametrize("previously_attempted", [False, True])
def test_failing_batch_cannot_starve_newer_payment_or_older_retry(
    made: Example,
    provider: Provider,
    monkeypatch: pytest.MonkeyPatch,
    previously_attempted: bool,
) -> None:
    rows = []
    for index in range(21):
        order = Order.objects.create(
            organization=made.org, member=made.member, total=200
        )
        rows.append(
            Payment.objects.create(
                currency="NOK",
                organization=made.org,
                order=order,
                amount=200,
                state="created",
                payment_provider="vipps",
                provider_reference=uuid.uuid4().hex,
                create_key=uuid.uuid4(),
                credential_name="VIPPS",
                merchant_number="123456",
                callback_received_at=NOW + timedelta(microseconds=index),
                provider_check_attempted_at=(
                    NOW - timedelta(minutes=1) if previously_attempted else None
                ),
            )
        )
    calls: list[str] = []
    available = {rows[-1].provider_reference}

    def read(self: Client, reference: str, currency: str) -> PaymentStatus:
        calls.append(reference)
        if reference not in available:
            raise RuntimeError("unavailable test payment")
        return PaymentStatus(reference, "EXPIRED", 200, 0, 0, 0, 0)

    monkeypatch.setattr(Client, "status", read)
    for tick in (1, 6):
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=tick))
    rows[-1].refresh_from_db()
    assert rows[-1].provider_checked_at is not None
    assert len(set(calls)) == 21
    # An item omitted from the second batch also gets its next attempt.
    available.add(rows[-2].provider_reference)
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=11))
    rows[-2].refresh_from_db()
    assert rows[-2].provider_checked_at is not None


@pytest.mark.parametrize(
    "state,authorized,captured,cancelled,refunded,local",
    [
        ("CREATED", 0, 0, 0, 0, "created"),
        ("AUTHORIZED", 200, 0, 0, 0, "authorized"),
        ("AUTHORIZED", 200, 200, 0, 0, "captured"),
        ("AUTHORIZED", 200, 200, 0, 100, "captured"),
        ("AUTHORIZED", 200, 200, 0, 200, "refunded"),
        ("AUTHORIZED", 200, 0, 200, 0, "cancelled"),
        ("ABORTED", 0, 0, 0, 0, "cancelled"),
        ("TERMINATED", 0, 0, 0, 0, "cancelled"),
        ("EXPIRED", 0, 0, 0, 0, "expired"),
    ],
)
def test_provider_snapshot_mapping(
    made: Example,
    provider: Provider,
    state: str,
    authorized: int,
    captured: int,
    cancelled: int,
    refunded: int,
    local: str,
) -> None:
    payment = payment_for(made)
    status = PaymentStatus(
        payment.provider_reference,
        state,
        200,
        authorized,
        captured,
        cancelled,
        refunded,
    )
    organization_transaction(
        made.org.pk, lambda org: reconciliation._apply(org, payment.pk, status, NOW)
    )
    payment.refresh_from_db()
    assert payment.state == local
    assert payment.captured_amount == captured and payment.refunded_amount == refunded


def test_simultaneous_identical_callbacks_are_accepted_once(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    body = json.dumps(payload(payment)).encode()
    barrier = Barrier(2)

    def accept() -> str:
        try:
            barrier.wait(timeout=10)
            callbacks.receive_webhook(
                organization=made.org,
                method="POST",
                target=WEBHOOK,
                host=HOST,
                headers=signed(body),
                body=body,
                now=NOW,
            )
            return "accepted"
        except callbacks.WebhookRepeatedError:
            return "repeated"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept(), range(2)))
    assert sorted(results) == ["accepted", "repeated"]
    payment.refresh_from_db()
    assert len(payment.webhook_events) == 1


def test_event_storage_bound_and_populated_reverse_preserve_history(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    assert send(payload(payment)).status_code == 202
    before = saved_rows(made.org)
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.of(made.org).filter(pk=payment.pk).update(
            webhook_events=["a" * 64] * 1025
        )
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="retained payment callbacks"):
            MigrationExecutor(connection).migrate(
                [("core", "0008_payment_create_intent")]
            )
        MigrationExecutor(connection).migrate(latest)
        assert saved_rows(made.org) == before
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_unprocessed_callback_protects_otherwise_settled_retention(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    Payment.objects.of(made.org).filter(pk=payment.pk).update(
        state="captured", captured_amount=200
    )
    callbacks.request_reconciliation(made.org, payment.order.public_id, now=NOW)
    result = purge_organization(made.org, now=NOW + timedelta(days=365 * 5))
    assert result["orders"] == result["bookings"] == 0
