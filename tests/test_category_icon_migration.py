# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0038 : a booking category's icon. Blank on every
existing category; reversing is refused while a category has an icon."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import BookingCategory
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0037_gave_admin_access")]


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
            "WHERE table_name = 'core_bookingcategory'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_existing_categories_are_blank_going_forward() -> None:
    made = made_up.everything()
    executor = reverse()
    assert "icon" not in columns()
    # A category made before the migration, through the earlier model.
    old = executor.loader.project_state(BEFORE).apps.get_model(
        "core", "BookingCategory"
    )
    old._default_manager.create(organization_id=made.organization.pk, name="older")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "icon" in columns()
    icons = set(
        BookingCategory.objects.of(made.organization).values_list("icon", flat=True)
    )
    assert icons == {""}
    assert BookingCategory.objects.of(made.organization).filter(name="older").exists()


def test_reversing_is_refused_while_a_category_has_an_icon() -> None:
    made = made_up.everything()
    BookingCategory.objects.of(made.organization).filter(pk=made.private.pk).update(
        icon="work"
    )
    with pytest.raises(RuntimeError, match="retained category icons"):
        reverse()
    assert "icon" in columns()
    assert (
        BookingCategory.objects.of(made.organization).get(pk=made.private.pk).icon
        == "work"
    )


def test_reversing_is_allowed_without_an_icon_and_the_leaves_come_back() -> None:
    made = made_up.everything()
    BookingCategory.objects.of(made.organization).filter(pk=made.private.pk).update(
        icon="work"
    )
    BookingCategory.objects.of(made.organization).filter(pk=made.private.pk).update(
        icon=""
    )
    reverse()
    assert "icon" not in columns()
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    # Whatever the graph's leaves are today, they all come after 0038.
    graph = executor.loader.graph
    for leaf in leaves:
        if leaf[0] == "core":
            assert ("core", "0038_category_icon") in graph.forwards_plan(leaf)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "icon" in columns()
