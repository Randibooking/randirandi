# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0042 : the audit vocabulary gains the fifteen
actions of the Assets and Spaces screens, and the application's user may change
a spot's place. Reversing is refused while an event uses one of the actions,
keeps every action other parts added, and takes the permission back."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import asset_admin
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, Space
from tests import made_up
from tests.capacity_app import APP_UPDATES, RULE_MIGRATION, application_login

pytestmark = pytest.mark.django_db(transaction=True)
PREVIOUS = importlib.import_module("randirandi.core.migrations.0041_added_member")
THIS = importlib.import_module("randirandi.core.migrations.0042_asset_screens")
BEFORE = [("core", "0041_added_member")]
FIFTEEN = {
    f"{verb} {thing}"
    for thing in ("space", "asset", "floor plan", "spot", "spot group")
    for verb in ("made", "changed", "removed")
}


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


def may_change_a_spots_place() -> bool:
    with application_login(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_column_privilege('randirandi_app', 'core_spot', 'place', "
            "'UPDATE')"
        )
        (found,) = cursor.fetchone()
    return bool(found)


@contextmanager
def a_role_the_migration_grants() -> Iterator[None]:
    """The deployed application role, made on the operator's connection so the
    migrations can run: it holds the current grants, and a migration that
    reverses or applies changes them as it would on a deployed database."""
    with connection.cursor() as cursor:
        cursor.execute("CREATE ROLE randirandi_app NOLOGIN")
        cursor.execute("GRANT USAGE ON SCHEMA public TO randirandi_app")
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            "TO randirandi_app"
        )
    try:
        with connection.schema_editor() as editor:
            RULE_MIGRATION.grant(editor, APP_UPDATES)
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP OWNED BY randirandi_app")
            cursor.execute("DROP ROLE randirandi_app")


def place_privilege() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_column_privilege('randirandi_app', 'core_spot', 'place', "
            "'UPDATE')"
        )
        (found,) = cursor.fetchone()
    return bool(found)


def test_the_vocabulary_is_the_previous_one_and_the_fifteen_more() -> None:
    current = {a.value for a in AuditAction}
    earlier = {action for action, _ in PREVIOUS.NEW_ACTIONS}
    assert "added member" in earlier
    # Later parts add their own actions (7.96's containers): a set, never exact.
    assert current >= earlier | FIFTEEN
    assert {action for action, _ in THIS.NEW_ACTIONS} == earlier | FIFTEEN
    assert FIFTEEN.isdisjoint(earlier)
    assert len(FIFTEEN) == 15


def test_each_new_action_is_stored() -> None:
    made = made_up.everything()
    space = Space.objects.of(made.organization).get()
    for action in sorted(FIFTEEN):
        record_event(actor=made.administrator, action=AuditAction(action), target=space)
    stored = AuditEvent.objects.of(made.organization).filter(action__in=FIFTEEN)
    assert set(stored.values_list("action", flat=True)) == FIFTEEN


def test_the_application_may_change_a_spots_place() -> None:
    assert may_change_a_spots_place()


def test_the_migrations_grant_goes_and_comes_back_with_the_reverse() -> None:
    with a_role_the_migration_grants():
        assert place_privilege()
        reverse()
        assert not place_privilege()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        assert place_privilege()


def test_reversing_is_refused_while_an_event_uses_a_new_action() -> None:
    made = made_up.everything()
    asset_admin.make_space(made.administrator, {"name": "Loft"})
    with a_role_the_migration_grants():
        with pytest.raises(RuntimeError, match="assets-and-spaces audit event"):
            reverse()
        # The refusal changed nothing: the schema is still current.
        assert place_privilege()
    assert AuditEvent.objects.of(made.organization).filter(action="made space")
    AuditEvent.objects.of(made.organization).filter(action__in=FIFTEEN).delete()
    # An event of another part's action survives the reverse and the way back.
    record_event(
        actor=made.administrator, action=AuditAction.ADD_MEMBER, target=made.member
    )
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.MADE_SPACE,
            target=made.organization,
        )
    # The older actions still work after the reverse, "added member" included.
    record_event(
        actor=made.administrator,
        action=AuditAction.ADD_MEMBER,
        target=made.member,
    )
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_ROLE,
        target=made.standard,
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert (
        AuditEvent.objects.of(made.organization).filter(action="added member").count()
        == 2
    )
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_SPACE,
        target=Space.objects.of(made.organization).get(name="Loft"),
    )


def test_reversing_changes_no_table() -> None:
    made_up.everything()
    before = sorted(
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "core_auditevent"
        )
    )
    reverse()
    after = sorted(
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "core_auditevent"
        )
    )
    assert before == after
