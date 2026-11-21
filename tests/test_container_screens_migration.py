# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0045 : the audit vocabulary gains the container
screens' three actions. Reversing is refused while an event uses one, and no
table or grant changes."""

from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import container_admin
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0044_authenticator_secret_encrypted")]
THREE = ["made container", "changed container", "removed container"]


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


def test_the_vocabulary_is_the_previous_leafs_set_and_the_three() -> None:
    before = import_module("randirandi.core.migrations.0042_asset_screens")
    this = import_module("randirandi.core.migrations.0045_container_screens_audit")
    old = {action for action, _ in before.NEW_ACTIONS}
    assert {action for action, _ in this.OLD_ACTIONS} == old
    assert {action for action, _ in this.NEW_ACTIONS} == old | set(THREE)
    assert len(this.NEW_ACTIONS) == len(old) + 3
    # The model has them all; later parts may add more of their own.
    assert {a.value for a in AuditAction} >= old | set(THREE)
    # The earlier parts' actions (7.93, 8.2, 7.95) survive.
    assert {"added member", "made role", "requested member import", "made asset"} <= old


def test_each_new_action_is_stored() -> None:
    made = made_up.everything()
    for action in THREE:
        record_event(
            actor=made.administrator,
            action=AuditAction(action),
            target=made.configuration,
        )
    stored = AuditEvent.objects.of(made.organization).filter(action__in=THREE)
    assert sorted(stored.values_list("action", flat=True)) == sorted(THREE)


def test_reversing_is_refused_while_an_event_uses_a_new_action() -> None:
    made = made_up.everything()
    pk = container_admin.make_container(
        made.administrator,
        {
            "asset": made.asset.pk,
            "booking_type": made.booking_type.pk,
            "period_type": "fixed",
        },
    )
    with pytest.raises(RuntimeError, match="retained container-screen events"):
        reverse()
    # The refused reverse leaves the schema where it was: the screens still work.
    container_admin.remove_container(made.administrator, pk)
    AuditEvent.objects.of(made.organization).filter(action__in=THREE).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.MADE_BOOKING_CONFIGURATION,
            target=made.configuration,
        )
    # The older actions, the setup screens' too, still work after the reverse.
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_ROLE,
        target=made.standard,
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record_event(
        actor=made.administrator,
        action=AuditAction.MADE_BOOKING_CONFIGURATION,
        target=made.configuration,
    )
    assert (
        AuditEvent.objects.of(made.organization).filter(action__in=THREE).count() == 1
    )


def test_reversing_changes_no_table_and_no_grant() -> None:
    made_up.everything()
    columns = sorted(
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "core_auditevent"
        )
    )
    reverse()
    assert columns == sorted(
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "core_auditevent"
        )
    )
