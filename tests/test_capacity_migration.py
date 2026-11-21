# SPDX-License-Identifier: AGPL-3.0-only
"""Populated backfill, atomic failure and restore of the installed model."""

import os
import secrets
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import StateApps
from psycopg import sql

from randirandi import setup_file
from randirandi.core import setup as setups
from randirandi.core.models import Asset, AssetInventory
from randirandi.member_file import MemberTable
from tests import made_up
from tests.capacity_app import APP_UPDATES, CAPACITY_MIGRATION, application_login
from tests.made_up import import_members
from tests.restore_queries import QUERIES, canonical_sql, compare, read_database
from tests.test_setup_capacity import SCENARIOS, TODAY

pytestmark = pytest.mark.django_db(transaction=True)
OLD = [("core", "0001_the_objects")]
NEW = [("core", "0002_capacity")]


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    # A refused multi-migration downgrade may already have unapplied a newer
    # reversible migration. Every test returns to the actual current schema.
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.fixture
def legacy_schema() -> Iterator[StateApps]:
    executor = MigrationExecutor(connection)
    executor.migrate(OLD)
    try:
        yield executor.loader.project_state(OLD).apps
    finally:
        latest = MigrationExecutor(connection)
        latest.migrate(latest.loader.graph.leaf_nodes())


def seed_legacy(apps: StateApps) -> None:
    def make(model_name: str, **values: Any) -> Any:
        return apps.get_model("core", model_name)._default_manager.create(**values)

    org = make(
        "Organization",
        name="Migration example",
        subdomain="migration",
        time_zone="Europe/Oslo",
        currency="NOK",
        contact_email="board@migration.example",
        member_retention_months=12,
        booking_retention_months=36,
        last_asset_number=7,
        last_order_number=11,
        last_price_book_number=3,
        last_booking_configuration_number=9,
    )
    asset = make("Asset", organization=org, number=7, name="Guest room")
    times = []
    for index, state in enumerate(
        ("confirmed", "held", "cancelled", "expired", "blocked")
    ):
        start = datetime(2027, 1, 3, 17, tzinfo=UTC) + timedelta(days=index)
        times.append(
            make(
                "TakenTime",
                organization=org,
                asset=asset,
                starts_at=start,
                ends_at=start + timedelta(hours=15),
                state=state,
            )
        )
    role = make("Role", organization=org, name="Standard")
    member = make(
        "Member",
        organization=org,
        role=role,
        first_name="Alex",
        last_name="Example",
        email="alex@migration.example",
        phone="+4790000011",
        access="granted",
        origin="member import",
    )
    category = make("BookingCategory", organization=org, name="Private")
    kind = make("BookingType", organization=org, name="Overnight")
    price = make("PriceBook", organization=org, name="Original prices", number=3)
    config = make(
        "BookingConfiguration",
        organization=org,
        asset=asset,
        booking_type=kind,
        number=9,
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        period_start_weekdays=[0, 1, 2, 3, 4, 5, 6],
        interval=900,
        minimum_length=900,
        maximum_length=900,
        status="enabled",
        price_book=price,
    )
    make(
        "PriceBookEntry",
        organization=org,
        price_book=price,
        booking_configuration=config,
        booking_category=category,
        tiers_go_by="length",
    )
    order = make("Order", organization=org, number=11, member=member, total=45000)
    booking = make(
        "Booking",
        organization=org,
        taken_time=times[0],
        order=order,
        member=member,
        booking_type=kind,
        booking_category=category,
        booking_type_name="Overnight",
        booking_category_name="Private",
    )
    make(
        "BookingLine",
        organization=org,
        booking=booking,
        booking_configuration=config,
        price_book=price,
        up_to=900,
        quantity=3,
        amount=15000,
        configuration_number=9,
        price_book_number=3,
    )
    make(
        "Block",
        organization=org,
        taken_time=times[-1],
        made_by=member,
        note="Maintenance",
    )


def old_rows(apps: StateApps) -> dict[str, list[tuple[Any, ...]]]:
    result = {}
    with connection.cursor() as cursor:
        for model in apps.get_app_config("core").get_models():
            columns = [
                field.column
                for field in model._meta.concrete_fields
                if field.column is not None
            ]
            query = sql.SQL("SELECT {} FROM {} ORDER BY id").format(
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.Identifier(model._meta.db_table),
            )
            cursor.execute(query)
            result[model._meta.db_table] = cursor.fetchall()
    return result


def test_populated_migration_preserves_every_original_value(
    legacy_schema: StateApps,
) -> None:
    seed_legacy(legacy_schema)
    before = old_rows(legacy_schema)
    MigrationExecutor(connection).migrate(NEW)
    assert old_rows(legacy_schema) == before
    # IDs need not begin at one after earlier tests; read through the asset.
    with connection.cursor() as cursor:
        cursor.execute("SELECT organization_id FROM core_asset")
        organization_id = cursor.fetchone()[0]
    # The models as this migration leaves them; later ones change them.
    apps = MigrationExecutor(connection).loader.project_state(NEW).apps
    owned = {
        name: apps.get_model("core", name)._default_manager.filter(
            organization_id=organization_id
        )
        for name in ("AssetInventory", "Space", "TakenTime")
    }
    inventory = owned["AssetInventory"].get()
    assert (inventory.capacity, inventory.unit, inventory.space_id) == (
        1,
        "booking",
        None,
    )
    assert not owned["Space"].exists()
    for taken in owned["TakenTime"]:
        allocation = taken.allocations.get()
        assert (taken.mode, taken.quantity, taken.represented_space) == (
            "exclusive",
            1,
            0,
        )
        assert (
            allocation.starts_at,
            allocation.ends_at,
            allocation.state,
            allocation.unit,
        ) == (taken.starts_at, taken.ends_at, taken.state, 0)
    # Legacy-only downgrade is supported, and keeps the original values too.
    MigrationExecutor(connection).migrate(OLD)
    assert old_rows(legacy_schema) == before


def test_failed_migration_rolls_back_data_schema_and_old_guard(
    legacy_schema: StateApps, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_legacy(legacy_schema)
    before = old_rows(legacy_schema)

    def fail(apps: Any, editor: Any) -> None:
        raise RuntimeError("injected migration failure")

    operations = list(CAPACITY_MIGRATION.Migration.operations)
    switch = next(
        i
        for i, operation in enumerate(operations)
        if isinstance(operation, migrations.RemoveConstraint)
    )
    operations.insert(switch, migrations.RunPython(fail, migrations.RunPython.noop))
    with monkeypatch.context() as scoped:
        scoped.setattr(CAPACITY_MIGRATION.Migration, "operations", operations)
        with pytest.raises(RuntimeError, match="injected migration failure"):
            MigrationExecutor(connection).migrate(NEW)
    assert old_rows(legacy_schema) == before
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('core_assetinventory')")
        assert cursor.fetchone() == (None,)
        with pytest.raises(IntegrityError), transaction.atomic():
            cursor.execute(
                "INSERT INTO core_takentime "
                "(organization_id,asset_id,starts_at,ends_at,state) SELECT "
                "organization_id,asset_id,starts_at,ends_at,state FROM "
                "core_takentime WHERE state='confirmed'"
            )


def test_new_capacity_data_refuses_a_lossy_downgrade() -> None:
    made = made_up.everything()
    # The floor plans migration would refuse first .
    made_up.plain_floor_plans(made.organization)
    with pytest.raises(RuntimeError, match="cannot be represented"):
        MigrationExecutor(connection).migrate(OLD)
    # The rule by unit again: the exclusive booking holds unit 0.
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM core_space")
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT unit FROM core_capacityallocation WHERE taken_time_id = %s",
            [made.booking.taken_time_id],
        )
        assert cursor.fetchall() == [(0,)]


def test_a_second_floor_plan_refuses_the_downgrade_and_survives_it() -> None:
    organization = made_up.organization("second")
    room = Asset.objects.create(organization=organization, name="Studio")
    AssetInventory.objects.create(organization=organization, asset=room, capacity=2)
    # The rule by unit has one inventory for each asset.
    with pytest.raises(RuntimeError, match="without exactly one floor plan: 1"):
        MigrationExecutor(connection).migrate([("core", "0016_floor_plans")])
    assert room.floor_plans.count() == 2
    assert (
        "core",
        "0017_rule_by_spot",
    ) in MigrationExecutor(connection).loader.applied_migrations


def test_migration_runs_as_the_non_superuser_owner_and_installs_app_privileges() -> (
    None
):
    config = connection.settings_dict
    target = "capacity_owner_" + uuid.uuid4().hex
    assert config["HOST"] in ("localhost", "127.0.0.1")
    assert config["NAME"].startswith("test_")
    graph = MigrationExecutor(connection).loader.graph
    expected_migrations = {
        name
        for target_node in graph.leaf_nodes("core")
        for app, name in graph.forwards_plan(target_node)
        if app == "core"
    }
    passwords = {
        role: secrets.token_urlsafe(32)
        for role in ("randirandi_owner", "randirandi_app")
    }
    parameters = {
        "host": config["HOST"],
        "port": config["PORT"],
        "user": config["USER"],
        "password": config["PASSWORD"],
    }
    with psycopg.connect(**parameters, dbname="postgres", autocommit=True) as control:
        for role, password in passwords.items():
            control.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        try:
            control.execute(
                sql.SQL("CREATE DATABASE {} OWNER randirandi_owner").format(
                    sql.Identifier(target)
                )
            )
            environment = os.environ | {
                "RANDIRANDI_DB_NAME": target,
                "RANDIRANDI_DB_USER": "randirandi_owner",
                "RANDIRANDI_DB_PASSWORD": passwords["randirandi_owner"],
            }
            for arguments in (("grants",), ("migrate", "--noinput"), ("grants",)):
                subprocess.run(  # noqa: S603 -- fixed command on disposable database
                    [sys.executable, "manage.py", *arguments],
                    env=environment,
                    check=True,
                    capture_output=True,
                )
            with psycopg.connect(
                host=config["HOST"],
                port=config["PORT"],
                dbname=target,
                user="randirandi_app",
                password=passwords["randirandi_app"],
                autocommit=True,
            ) as app:
                assert {
                    name
                    for (name,) in app.execute(
                        "SELECT name FROM django_migrations WHERE app='core'"
                    ).fetchall()
                } == expected_migrations
                for table, allowed in APP_UPDATES.items():
                    rows = app.execute(
                        "SELECT attname, "
                        "has_column_privilege(current_user,attrelid,attname,'UPDATE') "
                        "FROM pg_attribute WHERE attrelid=%s::regclass "
                        "AND attnum>0 AND NOT attisdropped",
                        (table,),
                    ).fetchall()
                    assert all(
                        writable == (column in allowed) for column, writable in rows
                    )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    app.execute(
                        "ALTER TABLE core_capacityallocation "
                        "DROP CONSTRAINT capacity_no_double_booking"
                    )
        finally:
            control.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(target)
                )
            )
            for role in passwords:
                control.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_dump_restore_preserves_data_constraints_and_effective_column_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = os.environ["RANDIRANDI_TEST_DB_CONTAINER"]
    owner_config = connection.settings_dict.copy()
    target = "capacity_restore_" + uuid.uuid4().hex
    owner_parameters = {
        "host": owner_config["HOST"],
        "port": owner_config["PORT"],
        "user": owner_config["USER"],
        "password": owner_config["PASSWORD"],
    }
    # Database-level deployed roles are outside this isolated local test.
    monkeypatch.setitem(QUERIES, "database_privileges", "SELECT current_user")
    with psycopg.connect(
        **owner_parameters, dbname="postgres", autocommit=True
    ) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        try:
            with application_login():
                try:
                    made = made_up.everything()
                    report = import_members(
                        made_up.Source(made.organization), MemberTable((), ())
                    )
                    assert report.report["reason"] == "invalid_columns"
                    # Restore real format-2 setup data too: spaces, floor
                    # plans, mode schedules and per-place price entries.
                    for path in SCENARIOS:
                        setups.load(setup_file.read(path), today=TODAY)
                    app_config = connection.settings_dict
                    app_parameters = {
                        "host": app_config["HOST"],
                        "port": app_config["PORT"],
                        "user": app_config["USER"],
                        "password": app_config["PASSWORD"],
                    }
                    source = read_database(app_parameters, app_config["NAME"])
                    environment = os.environ | {"PGPASSWORD": owner_config["PASSWORD"]}
                    client = [
                        "docker",
                        "exec",
                        "--interactive",
                        "--env",
                        "PGPASSWORD",
                        container,
                    ]
                    dump = subprocess.run(  # noqa: S603 -- local test container and argv
                        [
                            *client,
                            "pg_dump",
                            "--host=127.0.0.1",
                            f"--username={owner_config['USER']}",
                            "--format=custom",
                            owner_config["NAME"],
                        ],
                        env=environment,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(  # noqa: S603 -- local test container and argv
                        [
                            *client,
                            "pg_restore",
                            "--host=127.0.0.1",
                            f"--username={owner_config['USER']}",
                            f"--dbname={target}",
                            "--exit-on-error",
                        ],
                        env=environment,
                        input=dump.stdout,
                        check=True,
                        capture_output=True,
                    )
                    restored = read_database(app_parameters, target)
                    comparison = compare(source, restored)
                    changes = [
                        (one, two)
                        for one, two in zip(
                            source["constraints"], restored["constraints"], strict=True
                        )
                        if (*one[:-1], canonical_sql(one[-1]))
                        != (*two[:-1], canonical_sql(two[-1]))
                    ]
                    for one, two in changes:
                        assert canonical_sql(one[-1]) == canonical_sql(two[-1])
                    assert comparison["ok"], (comparison, changes)
                    with psycopg.connect(
                        **app_parameters, dbname=target, autocommit=True
                    ) as app:
                        assert app.execute(
                            "SELECT report FROM core_memberimport WHERE id=%s",
                            (report.pk,),
                        ).fetchone() == (report.report,)
                        with pytest.raises(psycopg.errors.InsufficientPrivilege):
                            app.execute("UPDATE core_auditevent SET action='inspected'")
                        assert (
                            app.execute(
                                "DELETE FROM core_auditevent RETURNING id"
                            ).fetchall()
                            == []
                        )
                        with pytest.raises(psycopg.errors.InsufficientPrivilege):
                            app.execute(
                                "UPDATE core_takentime SET scope_key=1 WHERE id=%s",
                                (made.booking.taken_time_id,),
                            )
                        with pytest.raises(psycopg.errors.ForeignKeyViolation):
                            app.execute(
                                "DELETE FROM core_capacityallocation "
                                "WHERE taken_time_id=%s",
                                (made.booking.taken_time_id,),
                            )
                finally:
                    # Drop the restored database before the helper drops its role.
                    control.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(target)
                        )
                    )
        finally:
            control.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(target)
                )
            )
