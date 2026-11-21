# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0052 : the event tables, the claim state "event",
the closure reason "event" gone, five audit actions and two audit kinds.
Reversing is refused while any event record exists, keeps every action other
parts added, and drops it all when nothing is lost. The graph's leaf nodes are
restored after every test, refused reverse or not."""

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    AuditEvent,
    AvailabilityClosure,
    Event,
    TakenTime,
)
from tests import made_up
from tests.test_events import NOW, SATURDAY, make, one_off, open_all_week

pytestmark = pytest.mark.django_db(transaction=True)
THIS = importlib.import_module("randirandi.core.migrations.0052_events")
NAME = "0052_events"
BEFORE = [("core", "0051_payouts_and_fees")]
NEW = {
    "core_event",
    "core_eventrepeat",
    "core_eventday",
    "core_eventskippedday",
    "core_eventnotice",
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


def still_applied() -> bool:
    return ("core", NAME) in MigrationExecutor(connection).loader.applied_migrations


def tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_the_vocabulary_gains_five_actions_and_two_kinds_and_keeps_the_others() -> None:
    earlier = {a for a, _ in THIS.OLD_ACTIONS}
    assert {a for a, _ in THIS.NEW_ACTIONS} == earlier | {
        "made event",
        "changed event",
        "removed event",
        "removed event day",
        "changed event day",
    }
    assert not earlier & {"made event", "changed event day"}
    assert {a.value for a in AuditAction} >= earlier | {"made event"}
    assert set(THIS.NEW_KINDS) - set(THIS.OLD_KINDS) == {"event", "event_day"}


def test_reversing_is_refused_while_an_event_exists_and_keeps_it() -> None:
    made = made_up.everything()
    open_all_week(made)
    event = make(made, one_off(SATURDAY, 10, 12))
    with pytest.raises(RuntimeError, match="event"):
        reverse()
    assert still_applied() and tables() >= NEW
    assert Event.objects.of(made.organization).filter(pk=event.pk).exists()
    assert TakenTime.objects.of(made.organization).filter(state="event").count() == 1


def test_reversing_is_refused_for_each_kind_of_record_alone() -> None:
    made = made_up.everything()
    open_all_week(made)
    event = make(made, one_off(SATURDAY, 10, 12), asset_id=None)
    # The audit event stays after the event is gone.
    Event.objects.of(made.organization).filter(pk=event.pk).delete()
    with pytest.raises(RuntimeError, match="event audit event"):
        reverse()
    assert still_applied()


def test_a_clean_reverse_drops_it_all_and_the_way_forward_is_whole() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator, action=AuditAction.ADD_MEMBER, target=made.member
    )
    assert tables() >= NEW
    reverse()
    assert not still_applied()
    assert not NEW & tables()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.MADE_EVENT,
            target=made.organization,
        )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert still_applied() and tables() >= NEW
    assert AuditEvent.objects.of(made.organization).filter(action="added member")


def test_a_closure_that_had_the_reason_event_keeps_no_reason_going_forward() -> None:
    made = made_up.everything()
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SATURDAY,
        last_date=SATURDAY,
    )
    reverse()
    with connection.cursor() as cursor:
        cursor.execute("UPDATE core_availabilityclosure SET reason = 'event'")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert AvailabilityClosure.objects.of(made.organization).get().reason == ""
    assert NOW
