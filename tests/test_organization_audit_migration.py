# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0049 : the audit vocabulary gains "changed
organization". Reversing is refused while an event uses it, keeps every action
other parts added, and changes no table and no grant."""

from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import organization_admin
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, Organization
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
NAME = "0049_organization_audit"
BEFORE = [("core", "0048_availability_closures")]
NEW = "changed organization"


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


def test_the_vocabulary_is_the_previous_sets_and_the_one() -> None:
    before = import_module("randirandi.core.migrations.0048_availability_closures")
    this = import_module(f"randirandi.core.migrations.{NAME}")
    old = {action for action, _ in before.NEW_ACTIONS}
    assert {action for action, _ in this.OLD_ACTIONS} == old
    assert {action for action, _ in this.NEW_ACTIONS} == old | {NEW}
    assert len(this.NEW_ACTIONS) == len(old) + 1
    assert {a.value for a in AuditAction} >= old | {NEW}
    assert {"added member", "made closure", "left refund by hand"} <= old
    assert this.Migration.dependencies == [("core", "0048_availability_closures")]


def test_the_migration_is_applied_whatever_the_leaf() -> None:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    assert NAME not in [m.name for m, _ in plan]
    assert ("core", NAME) in executor.loader.applied_migrations


def test_the_new_action_is_stored_on_the_organization() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.CHANGED_ORGANIZATION,
        target=made.organization,
    )
    [stored] = AuditEvent.objects.of(made.organization).filter(action=NEW)
    assert (stored.object_kind, stored.object_id) == (
        "organization",
        made.organization.pk,
    )


def test_reversing_is_refused_while_an_event_uses_the_action() -> None:
    made = made_up.everything()
    organization_admin.change(
        made.administrator,
        {
            "version": organization_admin.version_of(made.organization),
            "tax_rate": 500,
        },
    )
    with pytest.raises(RuntimeError, match="retained organization events"):
        reverse()
    # Put the graph's current leaf nodes back after the refused reversal.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert Organization.objects.get(pk=made.organization.pk).tax_rate == 500
    # The refusal left the schema where it was: the action still works.
    record_event(
        actor=made.administrator,
        action=AuditAction.CHANGED_ORGANIZATION,
        target=made.organization,
    )
    AuditEvent.objects.of(made.organization).filter(action=NEW).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.CHANGED_ORGANIZATION,
            target=made.organization,
        )
    # The older actions still work after the reverse.
    record_event(
        actor=made.administrator, action=AuditAction.ADD_MEMBER, target=made.member
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record_event(
        actor=made.administrator,
        action=AuditAction.CHANGED_ORGANIZATION,
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
