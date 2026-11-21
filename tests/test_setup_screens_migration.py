# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0039 : the audit vocabulary gains the nine actions
of the setup screens. Reversing is refused while an event uses one."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import setup_admin
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, BookingType
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0038_category_icon")]
NINE = [
    "made booking type",
    "changed booking type",
    "removed booking type",
    "made booking category",
    "changed booking category",
    "removed booking category",
    "made role",
    "changed role",
    "removed role",
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


def test_the_vocabulary_has_the_nine_more() -> None:
    # Later parts add their own actions; the set is checked, never a position.
    vocabulary = {a.value for a in AuditAction}
    assert set(NINE) <= vocabulary
    assert len(NINE) == 9


def test_each_new_action_is_stored() -> None:
    made = made_up.everything()
    targets = {
        "booking_type": made.booking_type,
        "booking_category": made.private,
        "role": made.standard,
    }
    for action in NINE:
        kind = action.split(" ", 1)[1].replace(" ", "_")
        record_event(
            actor=made.administrator,
            action=AuditAction(action),
            target=targets[kind],
        )
    stored = AuditEvent.objects.of(made.organization).filter(action__in=NINE)
    assert sorted(stored.values_list("action", flat=True)) == sorted(NINE)


def test_reversing_is_refused_while_an_event_uses_a_new_action() -> None:
    made = made_up.everything()
    setup_admin.make_item(made.administrator, BookingType, {"name": "Sauna"})
    with pytest.raises(RuntimeError, match="retained setup-screen events"):
        reverse()
    AuditEvent.objects.of(made.organization).filter(action__in=NINE).delete()
    reverse()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.MADE_ROLE,
            target=made.standard,
        )
    # The older actions still work after the reverse.
    record_event(
        actor=made.administrator,
        action=AuditAction.GAVE_ADMIN_ACCESS,
        target=made.member,
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    record_event(
        actor=made.administrator, action=AuditAction.MADE_ROLE, target=made.standard
    )


def test_reversing_changes_no_table_and_no_grant() -> None:
    made = made_up.everything()
    before = sorted(
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "core_auditevent"
        )
    )
    reverse()
    middle = sorted(
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "core_auditevent"
        )
    )
    assert before == middle
    del made
