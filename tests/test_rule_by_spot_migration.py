# SPDX-License-Identifier: AGPL-3.0-only
"""The rule by spot over stored rows : forward from the rule
by unit, back to it, and what stops it. Fictional data."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from randirandi.core.models import Asset
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BY_UNIT = [("core", "0016_floor_plans")]


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def migrate_to(target: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)


def latest() -> None:
    executor = MigrationExecutor(connection)
    migrate_to(executor.loader.graph.leaf_nodes())


def rows(query: str, *values: Any) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query, values)
        return list(cursor.fetchall())


CLAIMS = (
    "SELECT taken_time_id, ordinal, asset_id, inventory_id, spot_id, scope_key, "
    "whole_space, mode, quantity, state, starts_at, ends_at "
    "FROM core_capacityallocation ORDER BY taken_time_id, ordinal"
)
EVERY_COLUMN = "SELECT * FROM core_capacityallocation ORDER BY taken_time_id, ordinal"


def seeded() -> tuple[made_up.MadeUp, Any, Any]:
    """The organization, with a booking of spots 2 and 3 of a shared sauna
    beside its exclusive booking and its block."""
    made = made_up.everything()
    sauna = made.shared("Sauna", 4)
    assert sauna.floor_plan is not None
    benches = made.book(
        16, 18, configuration=sauna, units=made_up.spots_of(sauna.floor_plan, 2, 3)
    )
    return made, sauna, benches


def test_a_claim_keeps_its_spots_back_to_units_and_forward_again() -> None:
    made, sauna, benches = seeded()
    before = rows(CLAIMS)

    migrate_to(BY_UNIT)

    # The rule by unit: a shared claim's spots are its units, an exclusive
    # claim holds unit 0 of its asset's inventory.
    assert rows(
        "SELECT unit, inventory_id, named_unit, booking_start, booking_end "
        "FROM core_capacityallocation WHERE taken_time_id = %s ORDER BY ordinal",
        benches.taken_time_id,
    ) == [
        (
            number,
            sauna.floor_plan_id,
            0,
            made_up.at(made.day, 16),
            made_up.at(made.day, 18),
        )
        for number in (2, 3)
    ]
    assert rows(
        "SELECT c.unit, c.inventory_id = a.inventory_id FROM core_capacityallocation c "
        "JOIN core_takentime t ON t.id = c.taken_time_id "
        "JOIN core_asset a ON a.id = t.asset_id WHERE c.taken_time_id = %s",
        made.booking.taken_time_id,
    ) == [(0, True)]
    assert rows(
        "SELECT count(*) FROM core_asset a JOIN core_assetinventory i "
        "ON i.id = a.inventory_id AND i.asset_id = a.id"
    ) == [(2,)]

    latest()

    assert rows(CLAIMS) == before
    assert rows(
        "SELECT s.number FROM core_capacityallocation c JOIN core_spot s "
        "ON s.id = c.spot_id WHERE c.taken_time_id = %s ORDER BY c.ordinal",
        benches.taken_time_id,
    ) == [(2,), (3,)]


def test_what_does_not_map_stops_it_with_counts_and_changes_nothing() -> None:
    made, _sauna, _benches = seeded()
    spare = Asset.objects.create(organization=made.organization, name="Spare room")
    migrate_to(BY_UNIT)
    with connection.cursor() as cursor:
        # A named unit, the role the booking model retired, and a booking
        # without its container.
        cursor.execute("UPDATE core_asset SET named_unit = 1 WHERE id = %s", [spare.pk])
        cursor.execute(
            "UPDATE core_booking SET booking_configuration_id = NULL WHERE id = %s",
            [made.booking.pk],
        )
    before = rows(EVERY_COLUMN)

    with pytest.raises(RuntimeError) as refusal:
        latest()

    message = str(refusal.value)
    assert (
        f"named-unit assets (an asset that is one unit of an inventory): 1 "
        f"(organization {spare.organization_id}, asset {spare.number:03d})"
    ) in message
    assert f"bookings without a container: 1 (booking {made.booking.pk})" in message
    assert rows(EVERY_COLUMN) == before
    assert BY_UNIT[0] in MigrationExecutor(connection).loader.applied_migrations
    assert (
        "core",
        "0017_rule_by_spot",
    ) not in MigrationExecutor(connection).loader.applied_migrations
    # Put right, it goes on.
    with connection.cursor() as cursor:
        cursor.execute("UPDATE core_asset SET named_unit = 0 WHERE id = %s", [spare.pk])
        cursor.execute(
            "UPDATE core_booking SET booking_configuration_id = %s WHERE id = %s",
            [made.configuration.pk, made.booking.pk],
        )
    latest()


def test_slices_of_a_claim_on_one_unit_become_the_one_allocation_of_its_spot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    made, _sauna, benches = seeded()
    migrate_to(BY_UNIT)
    columns = [
        column
        for (column,) in rows(
            "SELECT column_name FROM information_schema.columns WHERE table_name = "
            "'core_capacityallocation' AND is_generated = 'NEVER' AND column_name "
            "<> 'id' ORDER BY ordinal_position"
        )
    ]
    following = {"starts_at": "ends_at", "ends_at": "booking_end"}
    # The rule by unit held unit 2 of the benches in two slices, 16-17 and
    # 17-18, the second first on unit 4.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        cursor.execute(
            "UPDATE core_capacityallocation SET ends_at = %s "
            "WHERE taken_time_id = %s AND ordinal = 1",
            [made_up.at(made.day, 17), benches.taken_time_id],
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO core_capacityallocation ({}) SELECT {} FROM "
                "core_capacityallocation WHERE taken_time_id = %s AND ordinal = 1"
            ).format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(
                    sql.Identifier(following.get(column, column)) for column in columns
                ),
            ),
            [benches.taken_time_id],
        )
        cursor.execute(
            "UPDATE core_capacityallocation SET unit = 4 "
            "WHERE taken_time_id = %s AND ordinal = 1 AND starts_at = %s",
            [benches.taken_time_id, made_up.at(made.day, 17)],
        )

    # A unit that changes during the booking is no one spot.
    with pytest.raises(RuntimeError) as refusal:
        latest()

    assert (
        f"claims whose unit changes during the booking: 1 "
        f"(taken time {benches.taken_time_id})"
    ) in str(refusal.value)

    # On one unit throughout, the slices are that spot over the whole booking.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_capacityallocation SET unit = 2 "
            "WHERE taken_time_id = %s AND unit = 4",
            [benches.taken_time_id],
        )
    with caplog.at_level(logging.INFO):
        latest()

    assert rows(
        "SELECT c.ordinal, s.number, c.starts_at, c.ends_at FROM "
        "core_capacityallocation c JOIN core_spot s ON s.id = c.spot_id "
        "WHERE c.taken_time_id = %s ORDER BY c.ordinal",
        benches.taken_time_id,
    ) == [
        (ordinal, number, made_up.at(made.day, 16), made_up.at(made.day, 18))
        for ordinal, number in ((1, 2), (2, 3))
    ]
    assert (
        "rule-by-spot migration: slices joined into the one allocation of their spot: 1"
    ) in caplog.messages
