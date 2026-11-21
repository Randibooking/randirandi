# SPDX-License-Identifier: AGPL-3.0-only
"""Administrator adjustment authority, saved amounts and durable worker recovery."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from functools import partial
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import payment_adjustments as adjustments
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core import payment_refunds
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    AuditEvent,
    BookingLine,
    Member,
    Order,
    Payment,
    Refund,
    Role,
)
from randirandi.core.payments import PaymentBusyError, PaymentUnavailableError
from randirandi.core.retention import after_months, purge_organization
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_ordering import NOW
from tests.test_payment_capture import Money, ready, rule_sees
from tests.test_payment_capture import money as money
from tests.test_payments import Provider, saved_rows
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def administrator(made: Example) -> Member:
    role = Role.objects.create(
        organization=made.org, name="Administrators", admin_access=True
    )
    return Member.objects.create(
        organization=made.org,
        role=role,
        email="admin@fictional.example",
        status="active",
        origin="administrator",
    )


def paid(made: Example, money: Money, *, multiple: bool = True) -> Payment:
    payment = ready(made, money, multiple=multiple)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    return payment


def test_booking_then_whole_order_refund_uses_saved_amounts_and_real_actor(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    key = uuid.uuid4()
    with application_login():
        result = adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            booking_public_id=first.public_id,
            request_key=key,
            now=NOW,
        )
        assert result.refund_id is not None
        refund = Refund.objects.of(made.org).get(pk=result.refund_id)
        assert refund.amount == 200 and refund.made_by == administrator
        assert not refund.automatic and refund.completed_at is None
        assert refund.refund_key == key and refund.refunded_before == 0
        first.refresh_from_db()
        assert first.taken_time.state == "cancelled"
        # The rule sees the one cancelled booking, and its time is free.
        assert rule_sees(made, payment.order) == {"cancelled", "confirmed"}
        made.claim(first.taken_time.starts_at, first.taken_time.ends_at)
        assert first.cancelled_by == administrator and first.cancelled_at == NOW
        assert second.taken_time.state == "confirmed"
        assert money.refunds == []
        assert (
            adjustments.request_refund(
                administrator=administrator,
                order_public_id=payment.order.public_id,
                booking_public_id=first.public_id,
                request_key=key,
                now=NOW,
            )
            == result
        )
        assert (
            AuditEvent.objects.of(made.org)
            .filter(action=AuditAction.REQUEST_REFUND)
            .count()
            == 1
        )
        reconciliation.reconcile_requested(now=NOW)
        refund.refresh_from_db()
        assert refund.completed_at == NOW
        assert money.refunds[0][1:] == (200, "NOK", str(key))
        with pytest.raises(PaymentUnavailableError, match="No refundable"):
            adjustments.request_refund(
                administrator=administrator,
                order_public_id=payment.order.public_id,
                booking_public_id=first.public_id,
                request_key=uuid.uuid4(),
                now=NOW,
            )
        second_result = adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=uuid.uuid4(),
            now=NOW,
        )
        assert second_result.refund_id is not None
        assert Refund.objects.of(made.org).get(pk=second_result.refund_id).amount == 200
        reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    second.refresh_from_db()
    assert payment.refunded_amount == payment.captured_amount == 400
    assert payment.settled_at == NOW and second.taken_time.state == "cancelled"
    assert len(money.refunds) == 2
    assert (
        AuditEvent.objects.of(made.org)
        .filter(action=AuditAction.REQUEST_REFUND)
        .count()
        == 2
    )
    exported = export_member(
        actor=administrator, member_id=administrator.pk, **fresh(administrator)
    )
    assert len(exported["made_refunds"]) == 2
    assert "refund_key" not in str(exported)


@pytest.mark.parametrize("accepted", [False, True])
def test_worker_retries_uncertain_admin_refund_without_new_request(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    accepted: bool,
) -> None:
    payment = paid(made, money, multiple=False)
    key = uuid.uuid4()
    result = adjustments.request_refund(
        administrator=administrator,
        order_public_id=payment.order.public_id,
        request_key=key,
        now=NOW,
    )

    def fail() -> None:
        raise TimeoutError("fictional lost refund reply")

    if accepted:
        money.after_refund = fail
    else:
        money.before_refund = fail
    reconciliation.reconcile_requested(now=NOW)
    assert result.refund_id is not None
    refund = Refund.objects.of(made.org).get(pk=result.refund_id)
    assert refund.completed_at is None
    money.before_refund = money.after_refund = lambda: None
    Member.objects.of(made.org).filter(pk=administrator.pk).update(status="disabled")
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=5))
    refund.refresh_from_db()
    assert refund.completed_at is not None and refund.made_by_id == administrator.pk
    assert len(money.refunds) == (1 if accepted else 2)
    assert {call[-1] for call in money.refunds} == {str(key)}


@pytest.mark.parametrize("state", ["CREATED", "AUTHORIZED"])
def test_uncaptured_booking_request_cancels_complete_order_without_refund(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    state: str,
) -> None:
    payment = ready(made, money, multiple=True)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference],
        state=state,
        authorized=400 if state == "AUTHORIZED" else 0,
    )
    key = uuid.uuid4()
    booking = payment.order.bookings.order_by("pk")[0]
    request = partial(
        adjustments.request_refund,
        administrator=administrator,
        order_public_id=payment.order.public_id,
        booking_public_id=booking.public_id,
        request_key=key,
        now=NOW,
    )
    with application_login():
        result = request()
        assert result.refund_id is None and money.cancellations == []
        assert set(
            payment.order.bookings.values_list("taken_time__state", flat=True)
        ) == {"cancelled"}
        assert rule_sees(made, payment.order) == {"cancelled"}
        assert request() == result
        reconciliation.reconcile_requested(now=NOW)
        assert request() == result
    payment.refresh_from_db()
    assert payment.cancel_key == key and payment.settled_at == NOW
    assert money.cancellations == [(payment.provider_reference, str(key))]
    assert not Refund.objects.of(made.org).exists()
    assert money.captures == money.refunds == []
    assert AuditEvent.objects.of(made.org).get().action == AuditAction.CANCEL_PAYMENT


@pytest.mark.parametrize(
    "fault",
    [
        "member",
        "revoked",
        "role",
        "foreign_order",
        "foreign_booking",
        "unknown_booking",
    ],
)
def test_authority_and_scope_refused_before_provider_read(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    fault: str,
) -> None:
    payment = paid(made, money)
    order_id, booking_id, actor = payment.order.public_id, None, administrator
    if fault == "member":
        actor = made.member
    elif fault == "revoked":
        Member.objects.of(made.org).filter(pk=actor.pk).update(status="disabled")
    elif fault == "role":
        Role.objects.of(made.org).filter(pk=actor.role_id).update(admin_access=False)
    elif fault.startswith("foreign"):
        other = made_up.everything("other")
        if fault == "foreign_order":
            order_id = other.order.public_id
        else:
            booking_id = other.booking.public_id
    else:
        booking_id = uuid.uuid4()
    reads = len(money.reads)
    with pytest.raises((PermissionDenied, PaymentUnavailableError)):
        adjustments.request_refund(
            administrator=actor,
            order_public_id=order_id,
            booking_public_id=booking_id,
            request_key=uuid.uuid4(),
            now=NOW,
        )
    assert len(money.reads) == reads
    assert not Refund.objects.of(made.org).exists()


def test_revocation_during_read_rolls_back_intent_and_cancellation(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)

    def revoke() -> None:
        Member.objects.of(made.org).filter(pk=administrator.pk).update(
            status="disabled"
        )

    money.before_read = revoke
    with pytest.raises(PermissionDenied):
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=uuid.uuid4(),
            now=NOW,
        )
    assert not Refund.objects.of(made.org).exists()
    assert not AuditEvent.objects.of(made.org).exists()
    assert set(payment.order.bookings.values_list("taken_time__state", flat=True)) == {
        "confirmed"
    }


@pytest.mark.parametrize(
    "fault",
    [
        "pending",
        "different_target",
        "different_actor",
        "inconsistent_lines",
        "unattributed_refund",
        "excess",
        "unconfirmed",
    ],
)
def test_refund_request_refuses_conflicting_or_unverifiable_money(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    fault: str,
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    key = uuid.uuid4()
    target: uuid.UUID | None = first.public_id
    if fault in {"pending", "different_target", "different_actor"}:
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            booking_public_id=first.public_id,
            request_key=key,
            now=NOW,
        )
        if fault == "pending":
            key = uuid.uuid4()
        elif fault == "different_target":
            target = second.public_id
        else:
            administrator = Member.objects.create(
                organization=made.org,
                role=administrator.role,
                status="active",
                email="other-admin@fictional.example",
                origin="administrator",
            )
    elif fault == "inconsistent_lines":
        BookingLine.objects.of(made.org).filter(booking=first).update(amount=101)
    elif fault in {"unattributed_refund", "excess"}:
        money.snapshots[payment.provider_reference] = replace(
            money.snapshots[payment.provider_reference],
            refunded=100 if fault == "unattributed_refund" else 400,
        )
        if fault == "excess":
            target = None
    elif fault == "unconfirmed":
        Order.objects.of(made.org).filter(pk=payment.order_id).update(
            hold_expires_at=NOW
        )
    before = Refund.objects.of(made.org).count()
    with pytest.raises((PaymentUnavailableError, PaymentBusyError)):
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            booking_public_id=target,
            request_key=key,
            now=NOW,
        )
    assert Refund.objects.of(made.org).count() == before


def test_concurrent_requests_do_not_create_parallel_refunds(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    entered, release = Event(), Event()

    def pause() -> None:
        entered.set()
        assert release.wait(timeout=10)

    money.before_read = pause

    def request() -> adjustments.Adjustment:
        try:
            return adjustments.request_refund(
                administrator=administrator,
                order_public_id=payment.order.public_id,
                request_key=uuid.uuid4(),
                now=NOW,
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(request)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(PaymentBusyError):
                adjustments.request_refund(
                    administrator=administrator,
                    order_public_id=payment.order.public_id,
                    request_key=uuid.uuid4(),
                    now=NOW,
                )
        finally:
            release.set()
        worker.result(timeout=10)
    assert Refund.objects.of(made.org).count() == 1


@pytest.mark.parametrize("captured", [False, True])
def test_failed_audit_rolls_back_adjustment_and_booking_changes(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    monkeypatch: pytest.MonkeyPatch,
    captured: bool,
) -> None:
    payment = paid(made, money) if captured else ready(made, money)
    before = list(
        payment.order.bookings.values(
            "cancelled_at", "cancelled_by_id", "taken_time__state"
        )
    )

    def fail(**kwargs: object) -> None:
        raise RuntimeError("fictional audit failure")

    monkeypatch.setattr(adjustments, "record_event", fail)
    with pytest.raises(RuntimeError, match="fictional audit failure"):
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=uuid.uuid4(),
            now=NOW,
        )
    payment.refresh_from_db()
    assert payment.cancel_key is None
    assert not Refund.objects.of(made.org).exists()
    assert (
        list(
            payment.order.bookings.values(
                "cancelled_at", "cancelled_by_id", "taken_time__state"
            )
        )
        == before
    )


def test_manual_transaction_is_refused_without_losing_caller_writes(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    reads = len(money.reads)
    connection.set_autocommit(False)
    try:
        Member.objects.of(made.org).filter(pk=administrator.pk).update(
            first_name="Pending"
        )
        with pytest.raises(RuntimeError, match="own transaction"):
            adjustments.request_refund(
                administrator=administrator,
                order_public_id=payment.order.public_id,
                request_key=uuid.uuid4(),
                now=NOW,
            )
        assert not connection.get_autocommit()
        assert (
            Member.objects.of(made.org).get(pk=administrator.pk).first_name == "Pending"
        )
        assert len(money.reads) == reads
    finally:
        connection.rollback()
        connection.set_autocommit(True)


def test_partial_booking_refund_uses_remaining_attributed_amount(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    booking = payment.order.bookings.order_by("pk")[0]
    Refund.objects.create(
        organization=made.org,
        payment=payment,
        booking=booking,
        made_by=administrator,
        amount=50,
    )
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference],
        refunded=50,
    )
    result = adjustments.request_refund(
        administrator=administrator,
        order_public_id=payment.order.public_id,
        booking_public_id=booking.public_id,
        request_key=uuid.uuid4(),
        now=NOW,
    )
    assert result.refund_id is not None
    refund = Refund.objects.of(made.org).get(pk=result.refund_id)
    assert refund.amount == 150 and refund.refunded_before == 50
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.refunded_amount == 200


def test_foreign_request_key_collision_rolls_back_without_foreign_access(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    other = made_up.everything("foreign-key")
    key = uuid.uuid4()
    Refund.objects.of(other.organization).update(refund_key=key, completed_at=NOW)
    with pytest.raises(PaymentUnavailableError, match="could not be accepted"):
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=key,
            now=NOW,
        )
    assert not Refund.objects.of(made.org).exists()
    assert set(payment.order.bookings.values_list("taken_time__state", flat=True)) == {
        "confirmed"
    }


def test_audit_migration_refuses_retained_adjustment_history(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    adjustments.request_refund(
        administrator=administrator,
        order_public_id=payment.order.public_id,
        request_key=uuid.uuid4(),
        now=NOW,
    )
    # Exercise this guard independently of the confirmation email's (0026),
    # which a later reverse refuses first.
    Order.objects.of(made.org).filter(pk=payment.order_id).update(
        confirmation_due_at=None
    )
    # And of the capture moment's (0030).
    Payment.objects.of(made.org).filter(pk=payment.pk).update(captured_at=None)
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    before = list(AuditEvent.objects.of(made.org).values())
    try:
        with pytest.raises(RuntimeError, match="retained payment adjustment"):
            MigrationExecutor(connection).migrate(
                [("core", "0011_payment_recovery_intents")]
            )
        MigrationExecutor(connection).migrate(latest)
        assert list(AuditEvent.objects.of(made.org).values()) == before
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.parametrize("whole", [False, True])
@pytest.mark.parametrize("already_refunded", [False, True])
def test_remaining_order_can_be_refunded_after_older_booking_retention(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    whole: bool,
    already_refunded: bool,
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    if already_refunded:
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            booking_public_id=first.public_id,
            request_key=uuid.uuid4(),
            now=NOW,
        )
        reconciliation.reconcile_requested(now=NOW)
        payment_refunds.alert_completed(now=NOW)
    moment = after_months(first.taken_time.ends_at, made.org.booking_retention_months)
    with application_login():
        assert purge_organization(made.org, now=moment)["bookings"] == 1
        assert purge_organization(made.org, now=moment)["bookings"] == 0
        payment.refresh_from_db()
        assert payment.retired_booking_amount == 200
        assert payment.retired_refund_amount == (200 if already_refunded else 0)
        assert payment.order.bookings.count() == 1
        assert not Refund.objects.of(made.org).exists()
        result = adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            booking_public_id=None if whole else second.public_id,
            request_key=uuid.uuid4(),
            now=moment,
        )
        assert result.refund_id is not None
        refund = Refund.objects.of(made.org).get(pk=result.refund_id)
        assert refund.amount == (400 if whole and not already_refunded else 200)
        reconciliation.reconcile_requested(now=moment)
        refund.refresh_from_db()
        assert refund.completed_at == moment
        exported = export_member(
            actor=administrator, member_id=made.member.pk, **fresh(administrator)
        )
        assert exported["payments"][0]["retired_booking_amount"] == 200
        assert exported["payments"][0]["retired_refund_amount"] == (
            200 if already_refunded else 0
        )


@pytest.mark.parametrize("field", ["retired_booking_amount", "retired_refund_amount"])
def test_retired_totals_refuse_lossy_reverse_without_audit_rows(
    made: Example,
    provider: Provider,
    money: Money,
    field: str,
) -> None:
    payment = paid(made, money)
    # Nor by the capture moment's (0030).
    Payment.objects.of(made.org).filter(pk=payment.pk).update(
        refunded_amount=100, captured_at=None, **{field: 100}
    )
    # Exercise this guard independently of the confirmation email's (0026),
    # which a later reverse refuses first.
    Order.objects.of(made.org).filter(pk=payment.order_id).update(
        confirmation_due_at=None
    )
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    before = saved_rows(made.org)
    try:
        with pytest.raises(RuntimeError, match="retained payment adjustment"):
            MigrationExecutor(connection).migrate(
                [("core", "0011_payment_recovery_intents")]
            )
        MigrationExecutor(connection).migrate(latest)
        assert saved_rows(made.org) == before
    finally:
        MigrationExecutor(connection).migrate(latest)
