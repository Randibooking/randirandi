# SPDX-License-Identifier: AGPL-3.0-only
"""Capture recovery and atomic booking confirmation on a real database."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from threading import Event
from unittest import mock

import pytest
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import payment_callbacks as callbacks
from randirandi.core import payment_capture, payment_recovery, payment_refunds, payments
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.models import (
    BookingLine,
    CapacityAllocation,
    Member,
    Order,
    Organization,
    Payment,
    TakenTime,
)
from randirandi.core.retention import purge_organization
from randirandi.vipps import Client, PaymentEvent, PaymentStatus
from tests.capacity_app import application_login
from tests.test_calendar import Example, at
from tests.test_calendar import made as made
from tests.test_ordering import NOW, checkout, request
from tests.test_payments import Provider
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass
class Money:
    snapshots: dict[str, PaymentStatus] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    captures: list[tuple[str, int, str, str]] = field(default_factory=list)
    cancellations: list[tuple[str, str]] = field(default_factory=list)
    refunds: list[tuple[str, int, str, str]] = field(default_factory=list)
    events: dict[str, list[PaymentEvent]] = field(default_factory=dict)
    before_capture: Callable[[], None] = lambda: None
    after_capture: Callable[[], None] = lambda: None
    before_read: Callable[[], None] = lambda: None
    before_cancel: Callable[[], None] = lambda: None
    after_cancel: Callable[[], None] = lambda: None
    before_refund: Callable[[], None] = lambda: None
    after_refund: Callable[[], None] = lambda: None


@pytest.fixture
def money(monkeypatch: pytest.MonkeyPatch) -> Money:
    money = Money()

    def read(self: Client, reference: str, currency: str) -> PaymentStatus:
        assert connection.get_autocommit() and not connection.in_atomic_block
        money.reads.append(reference)
        money.before_read()
        return money.snapshots[reference]

    def capture(
        self: Client, reference: str, amount: int, currency: str, *, key: str
    ) -> PaymentStatus:
        assert connection.get_autocommit() and not connection.in_atomic_block
        money.captures.append((reference, amount, currency, key))
        money.before_capture()
        money.snapshots[reference] = replace(
            money.snapshots[reference], captured=amount
        )
        money.after_capture()
        return money.snapshots[reference]

    monkeypatch.setattr(Client, "status", read)
    monkeypatch.setattr(Client, "capture", capture)

    def cancel(
        self: Client, reference: str, currency: str, *, key: str
    ) -> PaymentStatus:
        assert connection.get_autocommit() and not connection.in_atomic_block
        money.cancellations.append((reference, key))
        money.before_cancel()
        current = money.snapshots[reference]
        # A CREATED payment terminates with its whole amount cancelled and
        # nothing authorized, as observed in the Vipps test environment.
        money.snapshots[reference] = replace(
            current,
            state="TERMINATED" if current.state == "CREATED" else current.state,
            cancelled=current.amount
            if current.state == "CREATED"
            else current.authorized - current.captured,
        )
        money.after_cancel()
        return money.snapshots[reference]

    def refund(
        self: Client, reference: str, amount: int, currency: str, *, key: str
    ) -> PaymentStatus:
        assert connection.get_autocommit() and not connection.in_atomic_block
        money.refunds.append((reference, amount, currency, key))
        money.before_refund()
        events = money.events.setdefault(reference, [])
        if not any(row.idempotency_key == key for row in events):
            current = money.snapshots[reference]
            assert amount <= current.captured - current.refunded
            money.snapshots[reference] = replace(
                current, refunded=current.refunded + amount
            )
            events.append(
                PaymentEvent(reference, "event-" + key, "REFUNDED", amount, True, key)
            )
        money.after_refund()
        return money.snapshots[reference]

    def events(self: Client, reference: str, currency: str) -> tuple[PaymentEvent, ...]:
        return tuple(money.events.get(reference, ()))

    monkeypatch.setattr(Client, "cancel", cancel)
    monkeypatch.setattr(Client, "refund", refund)
    monkeypatch.setattr(Client, "events", events)
    return money


def rule_sees(made: Example, order: Order) -> set[str]:
    """The states of the order's claims as the rule against double booking
    reads them: its allocations' copies, which follow the taken time."""
    return set(
        CapacityAllocation.objects.of(made.org)
        .filter(taken_time__booking__order=order)
        .values_list("state", flat=True)
    )


def ready(made: Example, money: Money, *, multiple: bool = False) -> Payment:
    requests = [request(made)]
    if multiple:
        requests.append(request(made, starts_at=at(hour=12), ends_at=at(hour=14)))
    order = checkout(made, *requests)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", order.total, order.total, 0, 0, 0
    )
    callbacks.request_reconciliation(made.org, order.public_id, now=NOW)
    return payment


def test_full_capture_commits_intent_then_confirms_every_booking_as_application(
    made: Example, provider: Provider, money: Money
) -> None:
    made.capacity(3)
    payment = ready(made, money, multiple=True)

    def committed() -> None:
        try:
            saved = Payment.objects.of(made.org).get(pk=payment.pk)
            assert str(saved.capture_key) == money.captures[0][-1]
            assert saved.captured_amount == 0 and saved.captured_at is None
            assert set(
                saved.order.bookings.values_list("taken_time__state", flat=True)
            ) == {"held"}
        finally:
            connections.close_all()

    def before() -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(committed).result(timeout=10)

    money.before_capture = before
    with application_login():
        reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    payment.order.refresh_from_db()
    assert payment.state == "captured" and payment.captured_amount == 400
    # The first read that showed the money captured is the capture's moment.
    first_seen = payment.captured_at
    assert first_seen is not None
    assert payment.order.hold_expires_at is None
    # Its confirmation email is due from the moment it was confirmed.
    assert payment.order.confirmation_due_at == NOW
    assert set(TakenTime.objects.of(made.org).values_list("state", flat=True)) == {
        "confirmed"
    }
    assert rule_sees(made, payment.order) == {"confirmed"}
    assert len(money.reads) == 2 and len(money.captures) == 1
    assert money.captures[0][1:3] == (400, "NOK")
    callbacks.request_reconciliation(
        made.org, payment.order.public_id, now=NOW + timedelta(seconds=5)
    )
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    assert len(money.captures) == 1
    payment.refresh_from_db()
    assert payment.captured_at == first_seen


@pytest.mark.parametrize("accepted", [False, True])
def test_uncertain_capture_recovers_without_a_new_key_or_second_charge(
    made: Example, provider: Provider, money: Money, accepted: bool
) -> None:
    payment = ready(made, money)

    def fail() -> None:
        raise TimeoutError("fictional lost connection")

    if accepted:
        money.after_capture = fail
    else:
        money.before_capture = fail
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    key = payment.capture_key
    assert key is not None and payment.provider_checked_at is None
    assert payment.captured_at is None
    assert payment.order.bookings.get().taken_time.state == "held"
    money.before_capture = money.after_capture = lambda: None
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    payment.refresh_from_db()
    assert payment.capture_key == key
    assert payment.captured_amount == 200 and payment.captured_at is not None
    assert payment.order.bookings.get().taken_time.state == "confirmed"
    assert len(money.captures) == (1 if accepted else 2)
    assert {call[-1] for call in money.captures} == {str(key)}


def test_capture_reply_alone_cannot_confirm_and_failed_get_remains_pending(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)

    def fail_second_read() -> None:
        if len(money.reads) == 2:
            raise TimeoutError("fictional status timeout")

    money.before_read = fail_second_read
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.provider_checked_at is None and payment.captured_amount == 0
    assert payment.order.bookings.get().taken_time.state == "held"
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    payment.refresh_from_db()
    assert payment.captured_amount == 200
    assert payment.order.bookings.get().taken_time.state == "confirmed"
    assert len(money.captures) == 1


def test_stale_post_capture_read_keeps_work_pending_until_full_capture_is_read(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    authorized = money.snapshots[payment.provider_reference]

    def stale() -> None:
        money.snapshots[payment.provider_reference] = authorized

    money.after_capture = stale
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.provider_checked_at is None
    assert payment.order.bookings.get().taken_time.state == "held"
    money.snapshots[payment.provider_reference] = replace(authorized, captured=200)
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    assert payment.order.bookings.get().taken_time.state == "confirmed"
    assert len(money.captures) == 1


def test_failed_confirmation_commit_rolls_back_every_booking_and_recovers(
    made: Example, provider: Provider, money: Money, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = ready(made, money, multiple=True)
    confirm = payment_capture.confirm

    def fail(
        org: Organization,
        payment_id: int,
        status: PaymentStatus,
        now: datetime | None,
    ) -> None:
        confirm(org, payment_id, status, now)
        raise RuntimeError("fictional failure before commit")

    monkeypatch.setattr(payment_capture, "confirm", fail)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.captured_amount == 0 and payment.provider_checked_at is None
    assert payment.order.hold_expires_at is not None
    assert set(TakenTime.objects.of(made.org).values_list("state", flat=True)) == {
        "held"
    }
    monkeypatch.setattr(payment_capture, "confirm", confirm)
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    payment.refresh_from_db()
    assert payment.captured_amount == 400 and len(money.captures) == 1
    assert payment.order.hold_expires_at is None
    assert set(TakenTime.objects.of(made.org).values_list("state", flat=True)) == {
        "confirmed"
    }


@pytest.mark.parametrize(
    "state,authorized,captured,cancelled,refunded",
    [
        ("CREATED", 0, 0, 0, 0),
        ("EXPIRED", 0, 0, 0, 0),
        ("ABORTED", 0, 0, 0, 0),
        ("TERMINATED", 0, 0, 0, 0),
        ("AUTHORIZED", 200, 100, 0, 0),
        ("AUTHORIZED", 200, 0, 100, 0),
        ("AUTHORIZED", 200, 200, 0, 100),
    ],
)
def test_incomplete_or_reversed_payment_never_confirms_or_starts_capture(
    made: Example,
    provider: Provider,
    money: Money,
    state: str,
    authorized: int,
    captured: int,
    cancelled: int,
    refunded: int,
) -> None:
    payment = ready(made, money)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference,
        state,
        200,
        authorized,
        captured,
        cancelled,
        refunded,
    )
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.capture_key is None and money.captures == []
    assert payment.order.bookings.get().taken_time.state == (
        "held" if state == "CREATED" else "expired"
    )
    payment.order.refresh_from_db()
    assert payment.order.confirmation_due_at is None


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize(
    "fault", ["deadline", "released", "revoked", "line_total", "order_total", "started"]
)
def test_ineligible_booking_never_captures_or_confirms(
    made: Example, provider: Provider, money: Money, captured: bool, fault: str
) -> None:
    payment = ready(made, money, multiple=True)
    if captured:
        money.snapshots[payment.provider_reference] = replace(
            money.snapshots[payment.provider_reference], captured=400
        )
    if fault == "deadline":
        Order.objects.of(made.org).filter(pk=payment.order_id).update(
            hold_expires_at=NOW
        )
    elif fault == "released":
        taken = payment.order.bookings.earliest("pk").taken_time
        TakenTime.objects.of(made.org).filter(pk=taken.pk).update(state="expired")
    elif fault == "revoked":
        Member.objects.of(made.org).filter(pk=made.member.pk).update(status="disabled")
    elif fault == "line_total":
        BookingLine.objects.of(made.org).update(amount=1)
    elif fault == "order_total":
        Order.objects.of(made.org).filter(pk=payment.order_id).update(total=1)
    else:
        # Keep the hold live while observing the booking's start boundary.
        Order.objects.of(made.org).filter(pk=payment.order_id).update(
            hold_expires_at=at(hour=11)
        )
    reconciliation.reconcile_requested(now=at(hour=10) if fault == "started" else NOW)
    payment.refresh_from_db()
    assert money.captures == [] and payment.capture_key is None
    assert not payment.order.bookings.filter(taken_time__state="confirmed").exists()


@pytest.mark.parametrize("fault", ["deadline", "released", "revoked"])
def test_hold_and_member_are_rechecked_after_capture_latency(
    made: Example, provider: Provider, money: Money, fault: str
) -> None:
    payment = ready(made, money)
    with mock.patch("django.utils.timezone.now", return_value=NOW) as clock:

        def change() -> None:
            if fault == "deadline":
                clock.return_value = NOW + timedelta(minutes=10)
            elif fault == "released":
                TakenTime.objects.of(made.org).update(state="expired")
            else:
                Member.objects.of(made.org).filter(pk=made.member.pk).update(
                    status="disabled"
                )

        money.after_capture = change
        reconciliation.reconcile_requested()
    payment.refresh_from_db()
    assert len(money.captures) == 1 and payment.captured_amount == 200
    assert payment.order.bookings.get().taken_time.state != "confirmed"


def test_two_workers_capture_once_and_keep_the_order_lock_through_confirmation(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    entered, release = Event(), Event()

    def wait() -> None:
        entered.set()
        assert release.wait(timeout=10)

    def run() -> None:
        try:
            reconciliation.reconcile(made.org.pk, payment.order_id, now=NOW)
        finally:
            connections.close_all()

    money.before_capture = wait
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(payments.PaymentBusyError):
                reconciliation.reconcile(made.org.pk, payment.order_id, now=NOW)
        finally:
            release.set()
        first.result(timeout=10)
    assert len(money.captures) == 1
    assert payment.order.bookings.get().taken_time.state == "confirmed"


@pytest.mark.parametrize("fault", ["deadline", "released", "revoked", "none"])
def test_retention_keeps_unfulfilled_capture_until_the_money_is_returned(
    made: Example,
    provider: Provider,
    money: Money,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payment = ready(made, money)

    def change() -> None:
        if fault == "deadline":
            Order.objects.of(made.org).filter(pk=payment.order_id).update(
                hold_expires_at=NOW
            )
        elif fault == "released":
            TakenTime.objects.of(made.org).update(state="expired")
        elif fault == "revoked":
            Member.objects.of(made.org).filter(pk=made.member.pk).update(
                status="disabled"
            )

    money.after_capture = change
    advance = payment_recovery.advance

    def unavailable(
        payment: Payment, status: PaymentStatus, *, now: datetime | None = None
    ) -> PaymentStatus:
        raise TimeoutError("fictional cleanup outage")

    if fault != "none":
        monkeypatch.setattr(payment_recovery, "advance", unavailable)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.state == "captured" and payment.captured_amount == 200
    key = payment.capture_key
    purged = purge_organization(made.org, now=NOW + timedelta(days=365 * 5))
    if fault == "none":
        assert purged["orders"] == purged["bookings"] == 1
        return
    assert purged["orders"] == purged["bookings"] == 0
    payment.refresh_from_db()
    assert payment.capture_key == key
    # Recovery returns the money; pending alert delivery retains the graph too.
    monkeypatch.setattr(payment_recovery, "advance", advance)
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    purged = purge_organization(made.org, now=NOW + timedelta(days=365 * 5))
    assert purged["orders"] == purged["bookings"] == 0
    payment_refunds.alert_completed(now=NOW + timedelta(seconds=6))
    purged = purge_organization(made.org, now=NOW + timedelta(days=365 * 5))
    assert purged["orders"] == purged["bookings"] == 1


def test_populated_capture_key_refuses_lossy_reverse(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    key = payment.capture_key
    assert key is not None
    # Exercise this migration's guard independently of later settlement fields.
    Payment.objects.of(made.org).filter(pk=payment.pk).update(
        settled_at=None, captured_at=None
    )
    # And of the confirmation email's (0026), which a later reverse refuses.
    Order.objects.of(made.org).filter(pk=payment.order_id).update(
        confirmation_due_at=None
    )
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="retained capture intents"):
            MigrationExecutor(connection).migrate([("core", "0009_payment_callbacks")])
        MigrationExecutor(connection).migrate(latest)
        payment.refresh_from_db()
        assert payment.capture_key == key
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_populated_capture_moment_refuses_lossy_reverse(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.captured_at is not None
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="retained payment capture moments"):
            MigrationExecutor(connection).migrate([("core", "0029_audit_log_index")])
        MigrationExecutor(connection).migrate(latest)
        before = payment.captured_at
        payment.refresh_from_db()
        assert payment.captured_at == before
    finally:
        MigrationExecutor(connection).migrate(latest)
