# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0048 : the closure table and seven audit actions with
one kind. Reversing is refused while a closure or an event of the new
vocabulary exists, keeps every action other parts added, and drops the table
when nothing is lost."""

import importlib
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import availability_admin as admin
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, AvailabilityClosure
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
THIS = importlib.import_module("randirandi.core.migrations.0048_availability_closures")
NAME = "0048_availability_closures"
BEFORE = [("core", "0047_list_lookups")]
SIX = {
    f"{verb} {thing}"
    for thing in ("availability", "closure")
    for verb in ("made", "changed", "removed")
} | {"left refund by hand"}


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
    """The migration is applied; it is not assumed to be the graph's leaf."""
    return ("core", NAME) in MigrationExecutor(connection).loader.applied_migrations


def tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_the_vocabulary_gains_six_actions_and_one_kind() -> None:
    current = {a.value for a in AuditAction}
    earlier = {action for action, _ in THIS.OLD_ACTIONS}
    assert SIX.isdisjoint(earlier)
    assert current >= earlier | SIX
    assert {action for action, _ in THIS.NEW_ACTIONS} == earlier | SIX
    assert "availability_closure" in THIS.NEW_KINDS
    assert "availability_closure" not in THIS.OLD_KINDS
    assert set(THIS.NEW_KINDS) == set(THIS.OLD_KINDS) | {"availability_closure"}


def test_reversing_is_refused_while_a_closure_exists() -> None:
    made = made_up.everything()
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=date(2027, 3, 1),
        last_date=date(2027, 3, 1),
    )
    with pytest.raises(RuntimeError, match="closure by date exists"):
        reverse()
    assert "core_availabilityclosure" in tables()
    assert still_applied()


def test_reversing_is_refused_while_an_event_of_the_new_vocabulary_exists() -> None:
    made = made_up.everything()
    saved = admin.apply(
        made.administrator,
        made.asset.pk,
        {
            "kind": "closure-make",
            "first_date": "2027-04-01",
            "last_date": "2027-04-01",
            "reason": "",
        },
    )
    assert isinstance(saved, admin.Saved)
    AvailabilityClosure.objects.of(made.organization).all().delete()
    with pytest.raises(RuntimeError, match="audit event"):
        reverse()
    assert still_applied()
    AuditEvent.objects.of(made.organization).filter(action__in=SIX).delete()
    # An event of another part's action survives the reverse and the way back.
    record_event(
        actor=made.administrator, action=AuditAction.ADD_MEMBER, target=made.member
    )
    reverse()
    assert not still_applied()
    assert "core_availabilityclosure" not in tables()
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.MADE_CLOSURE,
            target=made.organization,
        )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert still_applied()
    assert "core_availabilityclosure" in tables()
    assert AuditEvent.objects.of(made.organization).filter(action="added member")
