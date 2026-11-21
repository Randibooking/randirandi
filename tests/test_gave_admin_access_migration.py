# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0037 : the audit vocabulary gains "gave admin
access". Reversing is refused while an event uses the action."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0036_member_data_pages")]


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


def test_the_action_is_stored_and_a_fixed_action_check_refuses_others() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.GAVE_ADMIN_ACCESS,
        target=made.member,
    )
    assert (
        AuditEvent.objects.of(made.organization)
        .filter(action="gave admin access")
        .count()
        == 1
    )


def test_reversing_is_refused_while_an_event_records_a_gain_of_admin_access() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.GAVE_ADMIN_ACCESS,
        target=made.member,
    )
    with pytest.raises(RuntimeError, match="gave-admin-access"):
        reverse()
    AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.GAVE_ADMIN_ACCESS
    ).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.GAVE_ADMIN_ACCESS,
            target=made.member,
        )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
