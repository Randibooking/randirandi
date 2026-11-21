# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0041 : the audit vocabulary gains "added member".
Reversing is refused while an event uses the action; the graph's leaf nodes
are restored after each test, a refused reversal included."""

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
BEFORE = [("core", "0040_member_parsers")]


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


def record(made: made_up.MadeUp) -> None:
    record_event(
        actor=made.administrator, action=AuditAction.ADD_MEMBER, target=made.member
    )


def test_the_action_is_stored_and_the_vocabulary_stays_fixed() -> None:
    made = made_up.everything()
    record(made)
    assert (
        AuditEvent.objects.of(made.organization).filter(action="added member").count()
        == 1
    )
    with pytest.raises(Exception, match="audit_fixed_action"):
        AuditEvent.objects.create(
            organization=made.organization,
            administrator=made.administrator,
            action="added somebody",
            object_kind="member",
            object_id=made.member.pk,
        )


def test_reversing_is_refused_while_an_event_records_an_add() -> None:
    made = made_up.everything()
    record(made)
    with pytest.raises(RuntimeError, match="added-member"):
        reverse()
    # The refusal left the schema and the event as they were.
    assert (
        AuditEvent.objects.of(made.organization).filter(action="added member").count()
        == 1
    )
    record(made)
    assert (
        AuditEvent.objects.of(made.organization).filter(action="added member").count()
        == 2
    )


def test_reversing_goes_through_without_events_and_refuses_the_action_after() -> None:
    made = made_up.everything()
    record(made)
    AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.ADD_MEMBER
    ).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record(made)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record(made)
    assert (
        AuditEvent.objects.of(made.organization).filter(action="added member").count()
        == 1
    )


def test_the_actions_of_the_migration_before_stay_allowed() -> None:
    made = made_up.everything()
    for action in (AuditAction.MADE_ROLE, AuditAction.REMOVED_BOOKING_TYPE):
        record_event(actor=made.administrator, action=action, target=made.member)
    record(made)
    assert (
        AuditEvent.objects.of(made.organization)
        .filter(action__in=["made role", "removed booking type", "added member"])
        .count()
        == 3
    )
