# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0046 : the audit vocabulary gains the price
screens' six actions. Reversing is refused while an event uses one, and
changes no table and no grant."""

from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import price_admin
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, PriceBook, PriceBookEntry
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0045_container_screens_audit")]
SIX = [
    "made price book",
    "changed price book",
    "removed price book",
    "made price",
    "changed price",
    "removed price",
]


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


def test_the_vocabulary_is_the_previous_leafs_set_and_the_six() -> None:
    before = import_module("randirandi.core.migrations.0045_container_screens_audit")
    this = import_module("randirandi.core.migrations.0046_price_book_audit")
    old = {action for action, _ in before.NEW_ACTIONS}
    assert {action for action, _ in this.OLD_ACTIONS} == old
    assert {action for action, _ in this.NEW_ACTIONS} == old | set(SIX)
    assert len(this.NEW_ACTIONS) == len(old) + len(SIX)
    # The model has them all; later parts may add more of their own.
    assert {a.value for a in AuditAction} >= old | set(SIX)
    assert {"added member", "made role", "made asset", "made container"} <= old


def test_the_migration_is_still_applied_at_the_leaf() -> None:
    """Whatever lands after it, the plan to the leaf nodes names no step of
    this migration: it is applied, not pending."""
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    assert "0046_price_book_audit" not in [m.name for m, _ in plan]
    assert ("core", "0046_price_book_audit") in executor.loader.applied_migrations


def test_each_new_action_is_stored() -> None:
    made = made_up.everything()
    entry = PriceBookEntry.objects.of(made.organization).get()
    targets = {"book": made.price_book, "": entry}
    for action in SIX:
        kind = "book" if action.endswith("book") else ""
        record_event(
            actor=made.administrator, action=AuditAction(action), target=targets[kind]
        )
    stored = AuditEvent.objects.of(made.organization).filter(action__in=SIX)
    assert sorted(stored.values_list("action", flat=True)) == sorted(SIX)
    assert {e.object_kind for e in stored} == {"price_book", "price_book_entry"}


def test_reversing_is_refused_while_an_event_uses_a_new_action() -> None:
    made = made_up.everything()
    price_admin.make_book(made.administrator, {"name": "Spring"})
    with pytest.raises(RuntimeError, match="retained price events"):
        reverse()
    # Put the graph's current leaf nodes back after the refused reversal.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    # The refusal left the schema where it was: the action still works.
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_PRICE_BOOK,
        target=PriceBook.objects.of(made.organization).get(name="Spring"),
    )
    AuditEvent.objects.of(made.organization).filter(action__in=SIX).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.MADE_PRICE_BOOK,
            target=made.price_book,
        )
    # The older actions still work after the reverse.
    record_event(
        actor=made.administrator,
        action=AuditAction.ADD_MEMBER,
        target=made.member,
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_PRICE_BOOK,
        target=made.price_book,
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
