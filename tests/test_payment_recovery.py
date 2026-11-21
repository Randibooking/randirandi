# SPDX-License-Identifier: AGPL-3.0-only
"""Expiry, missed callbacks and automatic money recovery with durable intents."""

import io
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from django.conf import settings
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core import payment_refunds
from randirandi.core.models import Order, Payment, Refund
from randirandi.core.payments import order_lock
from randirandi.core.retention import purge_organization
from randirandi.logs import IdsOnlyFormatter, RequiredLogError
from randirandi.vipps import PaymentStatus, VippsError
from tests.alert_queries import REFUND_DONE
from tests.capacity_app import application_login
from tests.test_calendar import Example, at
from tests.test_calendar import made as made
from tests.test_logs import _app_handlers
from tests.test_ordering import NOW, checkout
from tests.test_payment_capture import Money, ready, rule_sees
from tests.test_payment_capture import money as money
from tests.test_payments import Provider, saved_rows
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)
EXPIRED = NOW + timedelta(minutes=10)


def test_order_without_payment_releases_capacity_at_exact_deadline(
    made: Example,
) -> None:
    order = checkout(made)
    reconciliation.reconcile_requested(now=EXPIRED - timedelta(microseconds=1))
    assert order.bookings.get().taken_time.state == "held"
    assert rule_sees(made, order) == {"held"}
    with application_login():
        reconciliation.reconcile_requested(now=EXPIRED)
    assert order.bookings.get().taken_time.state == "expired"
    assert rule_sees(made, order) == {"expired"}
    assert not Payment.objects.of(made.org).exists()
    assert checkout(made).pk != order.pk


def test_lost_callback_is_recovered_and_confirmed_by_periodic_work(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    Payment.objects.of(made.org).filter(pk=payment.pk).update(callback_received_at=None)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.order.bookings.get().taken_time.state == "confirmed"
    assert payment.settled_at == NOW
    calls = len(money.reads)
    reconciliation.reconcile_requested(now=NOW + timedelta(minutes=1))
    assert len(money.reads) == calls


def test_provider_outage_never_keeps_expired_capacity_or_loses_intent(
    made: Example, provider: Provider, money: Money, caplog: pytest.LogCaptureFixture
) -> None:
    payment = ready(made, money)

    def fail() -> None:
        raise TimeoutError("private-provider-response")

    money.before_read = fail
    reconciliation.reconcile_requested(now=EXPIRED)
    payment.refresh_from_db()
    assert payment.order.bookings.get().taken_time.state == "expired"
    assert payment.settled_at is None and payment.create_key is not None
    assert "private-provider-response" not in caplog.text
    assert checkout(made).pk != payment.order_id
    assert (
        purge_organization(made.org, now=NOW + timedelta(days=365 * 5))["orders"] == 1
    )
    assert Payment.objects.of(made.org).filter(pk=payment.pk).exists()


@pytest.mark.parametrize("state", ["CREATED", "AUTHORIZED"])
@pytest.mark.parametrize("accepted", [False, True])
def test_abandoned_or_late_authorization_cancels_once_across_lost_response(
    made: Example, provider: Provider, money: Money, state: str, accepted: bool
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference,
        state,
        200,
        200 if state == "AUTHORIZED" else 0,
        0,
        0,
        0,
    )

    def fail() -> None:
        raise TimeoutError("fictional cancel failure")

    if accepted:
        money.after_cancel = fail
    else:
        money.before_cancel = fail
    reconciliation.reconcile_requested(now=EXPIRED)
    payment.refresh_from_db()
    key = payment.cancel_key
    assert key is not None and payment.settled_at is None
    assert payment.order.bookings.get().taken_time.state == "expired"
    assert rule_sees(made, payment.order) == {"expired"}
    made.claim(at(hour=10), at(hour=12))
    money.before_cancel = money.after_cancel = lambda: None
    reconciliation.reconcile_requested(now=EXPIRED + timedelta(seconds=5))
    payment.refresh_from_db()
    assert payment.settled_at is not None and payment.captured_amount == 0
    assert money.captures == money.refunds == []
    assert len(money.cancellations) == (1 if accepted else 2)
    assert {call[-1] for call in money.cancellations} == {str(key)}


@pytest.mark.parametrize("captured", [100, 200])
def test_late_capture_returns_money_and_releases_remaining_authorization(
    made: Example, provider: Provider, money: Money, captured: int
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], captured=captured
    )

    def committed() -> None:
        try:
            row = Refund.objects.of(made.org).get(payment=payment)
            assert row.automatic and row.made_by_id is None
            assert row.amount == captured and row.refunded_before == 0
            assert str(row.refund_key) == money.refunds[0][-1]
            assert row.completed_at is None
        finally:
            connections.close_all()

    def before() -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(committed).result(timeout=10)

    money.before_refund = before
    with application_login():
        reconciliation.reconcile_requested(now=EXPIRED)
    payment.refresh_from_db()
    row = Refund.objects.of(made.org).get(payment=payment)
    assert payment.captured_amount == payment.refunded_amount == captured
    assert payment.captured_at is not None
    assert payment.settled_at is not None
    assert row.completed_at is not None and row.provider_reference.startswith("event-")
    assert payment.order.bookings.get().taken_time.state == "expired"
    assert len(money.cancellations) == (1 if captured == 100 else 0)
    assert len(money.refunds) == 1 and money.captures == []


@pytest.mark.parametrize("accepted", [False, True])
def test_uncertain_refund_resolves_matching_event_and_never_uses_a_new_key(
    made: Example, provider: Provider, money: Money, accepted: bool
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], captured=200
    )

    def fail() -> None:
        raise TimeoutError("fictional refund failure")

    if accepted:
        money.after_refund = fail
    else:
        money.before_refund = fail
    reconciliation.reconcile_requested(now=EXPIRED)
    refund = Refund.objects.of(made.org).get(payment=payment)
    key = refund.refund_key
    assert key is not None and refund.completed_at is None
    money.before_refund = money.after_refund = lambda: None
    reconciliation.reconcile_requested(now=EXPIRED + timedelta(seconds=5))
    refund.refresh_from_db()
    payment.refresh_from_db()
    assert refund.completed_at is not None and payment.refunded_amount == 200
    assert {call[-1] for call in money.refunds} == {str(key)}
    assert len(money.refunds) == (1 if accepted else 2)
    assert len(money.events[payment.provider_reference]) == 1


def test_refunded_aggregate_without_matching_event_stays_uncertain(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], captured=200
    )

    def hide_event() -> None:
        money.events[payment.provider_reference] = []

    money.after_refund = hide_event
    reconciliation.reconcile_requested(now=EXPIRED)
    reconciliation.reconcile_requested(now=EXPIRED + timedelta(seconds=5))
    refund = Refund.objects.of(made.org).get(payment=payment)
    payment.refresh_from_db()
    assert refund.completed_at is None and payment.settled_at is None
    assert len(money.refunds) == 1


def test_ambiguous_create_is_resolved_with_original_inputs_then_cancelled(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    Payment.objects.of(made.org).filter(pk=payment.pk).update(create_completed_at=None)

    def missing() -> None:
        raise VippsError(404)

    def created() -> None:
        money.before_read = lambda: None
        money.snapshots[payment.provider_reference] = PaymentStatus(
            payment.provider_reference, "CREATED", 200, 0, 0, 0, 0
        )

    money.before_read = missing
    provider.during = created
    reconciliation.reconcile_requested(now=EXPIRED)
    payment.refresh_from_db()
    assert len(provider.calls) == 2 and provider.calls[0] == provider.calls[1]
    assert payment.settled_at is not None and payment.redirect_url == ""
    assert len(money.cancellations) == 1 and money.captures == []


def test_known_created_reference_not_found_does_not_create_another_payment(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)

    def missing() -> None:
        raise VippsError(404)

    money.before_read = missing
    reconciliation.reconcile_requested(now=EXPIRED)
    payment.refresh_from_db()
    assert len(provider.calls) == 1 and payment.settled_at is None
    assert payment.order.bookings.get().taken_time.state == "expired"


def test_refund_alert_is_durable_sanitized_and_not_repeated_after_delivery(
    made: Example,
    provider: Provider,
    money: Money,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], captured=200
    )
    reconciliation.reconcile_requested(now=EXPIRED)
    refund = Refund.objects.of(made.org).get(payment=payment)
    original = payment_refunds.logger.error

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("fictional unavailable logger")

    monkeypatch.setattr(payment_refunds.logger, "error", fail)
    with pytest.raises(RuntimeError, match="unavailable logger"):
        payment_refunds.alert_completed(now=EXPIRED)
    refund.refresh_from_db()
    assert refund.alerted_at is None
    assert (
        purge_organization(made.org, now=NOW + timedelta(days=365 * 5))["orders"] == 0
    )
    monkeypatch.setattr(payment_refunds.logger, "error", original)
    payment_refunds.alert_completed(now=EXPIRED)
    payment_refunds.alert_completed(now=EXPIRED)
    assert caplog.text.count("payment refund completed:") == 1
    assert made.member.email not in caplog.text
    record = next(
        row for row in caplog.records if "payment refund completed:" in row.msg
    )
    query = REFUND_DONE
    pattern = re.search(r"\| regexp `([^`]+)`", query)
    assert pattern is not None
    formatter = IdsOnlyFormatter("%(levelname)s %(name)s %(message)s")
    match = re.search(pattern[1], formatter.format(record))
    assert match is not None
    order = payment.order
    shown = (
        made.org.subdomain.capitalize(),
        order.reference,
        f"https://{made.org.subdomain}.{settings.ORGANIZATION_DOMAIN}"
        f"/admin/bookings/{order.number}/",
    )
    assert match.groups() == (
        str(made.org.pk),
        str(refund.pk),
        str(payment.pk),
        "True",
        *shown,
    )
    # Exercise the real emitter's template through the real privacy formatter
    # at each numeric-ID boundary; bare eight-digit numbers are phone-shaped.
    for position in range(3):
        for identifier in (9_999_999, 10_000_000, 99_999_999, 100_000_000):
            ids = [made.org.pk, refund.pk, payment.pk]
            ids[position] = identifier
            args = (*ids, True, *tuple(record.args)[4:])  # type: ignore[arg-type]
            boundary = logging.makeLogRecord(record.__dict__ | {"args": args})
            match = re.search(pattern[1], formatter.format(boundary))
            assert match is not None
            assert match.groups() == (*map(str, ids), "True", *shown)
    refund.refresh_from_db()
    assert refund.alerted_at == EXPIRED
    assert (
        purge_organization(made.org, now=NOW + timedelta(days=365 * 5))["orders"] == 1
    )


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_refund_log_stream_failure_remains_pending_until_successful_retry(
    made: Example, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    order = Order.objects.create(organization=made.org, member=made.member, total=200)
    payment = Payment.objects.create(
        currency="NOK", organization=made.org, order=order, amount=200, state="refunded"
    )
    refund = Refund.objects.create(
        organization=made.org,
        payment=payment,
        amount=200,
        automatic=True,
        refund_key=uuid.uuid4(),
        made_at=NOW,
        completed_at=NOW,
    )

    class BrokenStream(io.StringIO):
        def write(self, value: str) -> int:
            if failure == "write":
                raise OSError("fictional-private-stream-error")
            return super().write(value)

        def flush(self) -> None:
            if failure == "flush":
                raise OSError("fictional-private-stream-error")
            super().flush()

    handlers = _app_handlers()
    assert handlers
    with monkeypatch.context() as local:
        local.setattr(logging, "raiseExceptions", False)
        for handler in handlers:
            local.setattr(handler, "stream", BrokenStream())
        # Ordinary logging retains its standard best-effort behavior.
        logging.getLogger("randirandi.ops.jobs").error("fictional ordinary record")
        with pytest.raises(RequiredLogError) as caught:
            payment_refunds.alert_completed(now=NOW)
    assert str(caught.value) == "Required alert log could not be written."
    refund.refresh_from_db()
    assert refund.alerted_at is None

    written = io.StringIO()
    with monkeypatch.context() as local:
        for handler in handlers:
            local.setattr(handler, "stream", written)
        payment_refunds.alert_completed(now=NOW)
        payment_refunds.alert_completed(now=NOW)
    refund.refresh_from_db()
    assert refund.alerted_at == NOW
    assert written.getvalue().count("payment refund completed:") == 1


def test_database_refuses_fabricated_actor_and_parallel_pending_refunds(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    with application_login():
        with (
            pytest.raises(IntegrityError, match="refund_real_actor"),
            transaction.atomic(),
        ):
            Refund.objects.create(organization=made.org, payment=payment, amount=1)
        Refund.objects.create(
            organization=made.org,
            payment=payment,
            amount=1,
            automatic=True,
            refund_key=uuid.uuid4(),
        )
        with (
            pytest.raises(IntegrityError, match="refund_one_pending"),
            transaction.atomic(),
        ):
            Refund.objects.create(
                organization=made.org,
                payment=payment,
                amount=1,
                automatic=True,
                refund_key=uuid.uuid4(),
            )


def test_busy_refunds_do_not_block_later_alerts(
    made: Example, caplog: pytest.LogCaptureFixture
) -> None:
    payments = []
    for count in (50, 1):
        order = Order.objects.create(
            organization=made.org, member=made.member, total=200
        )
        payment = Payment.objects.create(
            currency="NOK",
            organization=made.org,
            order=order,
            amount=200,
            state="refunded",
            payment_provider="vipps",
            provider_reference=uuid.uuid4().hex,
        )
        Refund.objects.of(made.org).bulk_create(
            [
                Refund(
                    organization=made.org,
                    payment=payment,
                    amount=1,
                    automatic=True,
                    refund_key=uuid.uuid4(),
                    made_at=NOW,
                    completed_at=NOW,
                )
                for _ in range(count)
            ]
        )
        payments.append(payment)
    held, release = Event(), Event()

    def occupy() -> None:
        try:
            with order_lock(payments[0].order_id):
                held.set()
                assert release.wait(timeout=10)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(occupy)
        try:
            assert held.wait(timeout=10)
            payment_refunds.alert_completed(now=NOW)
            assert payments[1].refunds.get().alerted_at == NOW
            assert payments[0].refunds.filter(alerted_at__isnull=True).count() == 50
        finally:
            release.set()
        worker.result(timeout=10)
    payment_refunds.alert_completed(now=NOW)
    payment_refunds.alert_completed(now=NOW)
    assert caplog.text.count("payment refund completed:") == 51
    assert not Refund.objects.of(made.org).filter(alerted_at__isnull=True).exists()


def test_recovery_fields_refuse_lossy_reverse_and_preserve_current_graph(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], captured=100
    )
    reconciliation.reconcile_requested(now=EXPIRED)
    payment_refunds.alert_completed(now=EXPIRED)
    # Exercise this guard independently of the capture moment's (0030).
    Payment.objects.of(made.org).filter(pk=payment.pk).update(captured_at=None)
    before = (
        saved_rows(made.org),
        list(Refund.objects.of(made.org).values()),
    )
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="retained payment recovery"):
            MigrationExecutor(connection).migrate(
                [("core", "0010_payment_capture_intent")]
            )
        MigrationExecutor(connection).migrate(latest)
        assert before == (
            saved_rows(made.org),
            list(Refund.objects.of(made.org).values()),
        )
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_busy_orders_do_not_use_up_the_entire_recovery_batch(
    made: Example, provider: Provider, money: Money
) -> None:
    rows = []
    for _ in range(21):
        order = Order.objects.create(
            organization=made.org, member=made.member, total=200
        )
        row = Payment.objects.create(
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
        )
        money.snapshots[row.provider_reference] = PaymentStatus(
            row.provider_reference, "EXPIRED", 200, 0, 0, 0, 0
        )
        rows.append(row)
    held, release = Event(), Event()

    def occupy() -> None:
        try:
            with ExitStack() as stack:
                for row in rows[:20]:
                    stack.enter_context(order_lock(row.order_id))
                held.set()
                assert release.wait(timeout=10)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(occupy)
        try:
            assert held.wait(timeout=10)
            reconciliation.reconcile_requested(now=NOW)
            rows[-1].refresh_from_db()
            assert rows[-1].settled_at == NOW
            assert money.reads == [rows[-1].provider_reference]
        finally:
            release.set()
        worker.result(timeout=10)


def test_a_refund_read_that_first_shows_the_capture_sets_its_moment(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], captured=200
    )

    def unseen() -> None:
        # The payment row has not yet recorded the capture the refund read shows.
        Payment.objects.of(made.org).filter(pk=payment.pk).update(
            captured_amount=0, captured_at=None
        )

    money.before_refund = unseen
    reconciliation.reconcile_requested(now=EXPIRED)
    payment.refresh_from_db()
    assert payment.captured_amount == payment.refunded_amount == 200
    assert payment.captured_at is not None
