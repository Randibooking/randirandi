# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0043 : a login code keeps the network it was asked
for from, and a login attempt may be a payment return. Reversing drops the
return counters and the column; the leaf nodes are restored after."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import LoginAttempt
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0042_asset_screens")]
NOW = datetime(2028, 3, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def code_columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'core_logincode'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_forward_has_the_column_and_the_return_kind() -> None:
    made = made_up.everything()
    assert "network" in code_columns()
    LoginAttempt.objects.create(
        organization=made.organization,
        at=NOW,
        network="192.0.2.1",
        kind="return",
        outcome="unknown",
    )


def test_reversed_drops_return_counters_and_the_column_then_forward_again() -> None:
    made = made_up.everything()
    LoginAttempt.objects.create(
        organization=made.organization,
        at=NOW,
        network="192.0.2.1",
        kind="return",
        outcome="unknown",
    )
    kept = LoginAttempt.objects.of(made.organization).exclude(kind="return").count()
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BEFORE)
    assert "network" not in code_columns()
    assert LoginAttempt.objects.of(made.organization).count() == kept
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "network" in code_columns()
