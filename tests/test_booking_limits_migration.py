# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0035 : an organization may limit a member's
bookings a day and how far ahead. Reversing is refused while an organization
sets either, and works when none does."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import Organization
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0034_order_reference")]


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


def columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'core_organization'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_an_organization_has_no_limit_until_one_is_set() -> None:
    made = made_up.everything()
    assert {"max_bookings_a_day", "book_ahead_months"} <= columns()
    assert made.organization.max_bookings_a_day is None
    assert made.organization.book_ahead_months is None


@pytest.mark.parametrize("field", ["max_bookings_a_day", "book_ahead_months"])
def test_reversing_is_refused_while_an_organization_sets_a_limit(field: str) -> None:
    made = made_up.everything()
    Organization.objects.filter(pk=made.organization.pk).update(**{field: 3})
    with pytest.raises(RuntimeError, match="retained booking limits"):
        reverse()
    assert field in columns()
    # The refused reversal left the schema at its current leaf.
    Organization.objects.filter(pk=made.organization.pk).update(**{field: None})
    reverse()
    assert not {"max_bookings_a_day", "book_ahead_months"} & columns()
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert {"max_bookings_a_day", "book_ahead_months"} <= columns()
