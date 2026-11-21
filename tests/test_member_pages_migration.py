# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0036 : a security notice may name the address it
goes to, and the audit vocabulary gains "changed member email". Reversing is
refused while an event uses the action or a notice holds an address."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, SecurityNotice
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0035_booking_limits")]


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


def notice_columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'core_securitynotice'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_a_notice_names_no_address_until_one_is_set() -> None:
    made = made_up.everything()
    assert "address" in notice_columns()
    assert not SecurityNotice.objects.of(made.organization).exclude(address="").exists()


def test_reversing_is_refused_while_a_notice_holds_an_old_address() -> None:
    made = made_up.everything()
    SecurityNotice.objects.of(made.organization).update(address="old@example.test")
    with pytest.raises(RuntimeError, match="old address"):
        reverse()
    assert "address" in notice_columns()
    SecurityNotice.objects.of(made.organization).update(address="")
    reverse()
    assert "address" not in notice_columns()
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "address" in notice_columns()


def test_reversing_is_refused_while_an_event_records_an_email_change() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.CHANGE_MEMBER_EMAIL,
        target=made.member,
    )
    with pytest.raises(RuntimeError, match="email changes"):
        reverse()
    AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.CHANGE_MEMBER_EMAIL
    ).delete()
    reverse()
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
