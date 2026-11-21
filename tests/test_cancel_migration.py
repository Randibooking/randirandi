# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0031 : a booking records the cancel that took it and
the email about it, and the audit vocabulary gains "cancelled booking line".
Reversing is refused while either is used, and works when neither is."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, Booking
from tests import made_up
from tests.test_admin_bookings import book, order_of

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0030_payment_captured_at")]


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def reverse() -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BEFORE)
    return executor


def columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'core_booking'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_a_cancel_mail_needs_a_cancel() -> None:
    made = made_up.everything()
    line = book(made, order_of(made), day=5)
    with pytest.raises(IntegrityError), transaction.atomic():
        Booking.objects.of(made.organization).filter(pk=line.pk).update(
            cancel_mail_due_at=line.order.created_at
        )
    Booking.objects.of(made.organization).filter(pk=line.pk).update(
        cancellation_key=uuid.uuid4(), cancel_mail_due_at=line.order.created_at
    )


def test_reversing_is_refused_while_a_booking_records_a_cancel() -> None:
    made = made_up.everything()
    line = book(made, order_of(made), day=5)
    Booking.objects.of(made.organization).filter(pk=line.pk).update(
        cancellation_key=uuid.uuid4()
    )
    with pytest.raises(RuntimeError, match="would send it again"):
        reverse()
    assert "cancellation_key" in columns()
    Booking.objects.of(made.organization).filter(pk=line.pk).update(
        cancellation_key=None
    )
    reverse()
    assert "cancellation_key" not in columns()


def test_reversing_is_refused_while_an_event_uses_the_action() -> None:
    made = made_up.everything()
    line = book(made, order_of(made), day=5)
    record_event(
        actor=made.administrator,
        action=AuditAction.CANCEL_BOOKING_LINE,
        target=line,
    )
    with pytest.raises(RuntimeError, match="retained booking line cancellations"):
        reverse()
    assert "cancel_mailed_at" in columns()
    AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.CANCEL_BOOKING_LINE
    ).delete()
    reverse()
    assert "cancel_mailed_at" not in columns()
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "cancel_mailed_at" in columns()
