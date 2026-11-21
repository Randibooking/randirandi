# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0032 : a block records when it was made, and the
audit vocabulary gains "removed block". Reversing is refused while an event
uses the action, and works when none does."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, Block
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0031_cancel_booking_line")]


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


def has_created_at() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'core_block' AND column_name = 'created_at'"
        )
        return cursor.fetchone() is not None


def event(made: made_up.MadeUp, action: str) -> None:
    AuditEvent.objects.create(
        organization=made.organization,
        administrator=made.administrator,
        action=action,
        object_kind="block",
        object_id=made.rows[Block].pk,
    )


def test_the_database_knows_the_new_action_and_no_other() -> None:
    made = made_up.everything()
    event(made, "removed block")
    with pytest.raises(IntegrityError), transaction.atomic():
        event(made, "removed blocks")


def test_a_block_from_before_has_no_creation_time() -> None:
    made = made_up.everything()
    assert Block.objects.of(made.organization).get().created_at is None


def test_reversing_is_refused_while_an_event_uses_the_action() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.REMOVED_BLOCK,
        target=made.rows[Block],
    )
    with pytest.raises(RuntimeError, match="retained block removals"):
        reverse()
    assert has_created_at()
    AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.REMOVED_BLOCK
    ).delete()
    reverse()
    assert not has_created_at()
    # The block and the audit vocabulary are as they were.
    assert Block.objects.of(made.organization).count() == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        event(made, "removed block")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert has_created_at()
    event(made, "removed block")
