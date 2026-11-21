# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0047 : the audit vocabulary gains the four list
look-ups. Reversing is refused while an event uses one, and changes no table
and no grant."""

from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0046_price_book_audit")]
FOUR = ["listed members", "listed bookings", "listed payments", "listed schedule"]


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


def test_the_vocabulary_is_the_previous_sets_and_the_four() -> None:
    before = import_module("randirandi.core.migrations.0046_price_book_audit")
    this = import_module("randirandi.core.migrations.0047_list_lookups")
    old = {action for action, _ in before.NEW_ACTIONS}
    assert {action for action, _ in this.OLD_ACTIONS} == old
    assert {action for action, _ in this.NEW_ACTIONS} == old | set(FOUR)
    assert {a.value for a in AuditAction} >= old | set(FOUR)


def test_the_migration_is_still_applied_at_the_leaf() -> None:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    assert "0047_list_lookups" not in [m.name for m, _ in plan]
    assert ("core", "0047_list_lookups") in executor.loader.applied_migrations


def test_each_new_action_is_stored_on_the_organization() -> None:
    made = made_up.everything()
    for action in FOUR:
        record_event(
            actor=made.administrator,
            action=AuditAction(action),
            target=made.organization,
        )
    stored = AuditEvent.objects.of(made.organization).filter(action__in=FOUR)
    assert {e.action for e in stored} == set(FOUR)
    assert {e.object_kind for e in stored} == {"organization"}


def test_reversing_is_refused_while_an_event_uses_a_new_action() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.LISTED_MEMBERS,
        target=made.organization,
    )
    with pytest.raises(RuntimeError, match="retained list look-up events"):
        reverse()
    # Put the graph's current leaf nodes back after the refused reversal.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record_event(
        actor=made.administrator,
        action=AuditAction.LISTED_BOOKINGS,
        target=made.organization,
    )
    AuditEvent.objects.of(made.organization).filter(action__in=FOUR).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.LISTED_MEMBERS,
            target=made.organization,
        )
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_PRICE_BOOK,
        target=made.price_book,
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record_event(
        actor=made.administrator,
        action=AuditAction.LISTED_MEMBERS,
        target=made.organization,
    )


def test_reversing_changes_no_table_and_no_grant() -> None:
    made_up.everything()

    def columns() -> list[str]:
        return sorted(
            c.name
            for c in connection.introspection.get_table_description(
                connection.cursor(), "core_auditevent"
            )
        )

    before = columns()
    reverse()
    assert before == columns()
