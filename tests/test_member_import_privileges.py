# SPDX-License-Identifier: AGPL-3.0-only
"""The application login's privileges on core_memberimport : the
grants give it the same rights after migration 0040 forward and after a
lossless reverse and forward again."""

import secrets
from collections.abc import Iterator

import psycopg
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0039_setup_screens_audit")]
RIGHTS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def grant() -> None:
    config = connection.settings_dict
    with psycopg.connect(
        host=config["HOST"],
        port=config["PORT"],
        dbname=config["NAME"],
        user=config["USER"],
        password=config["PASSWORD"],
        autocommit=True,
    ) as owner:
        owner.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            "TO randirandi_app"
        )


def rights() -> dict[str, bool]:
    with connection.cursor() as cursor:
        found = {}
        for right in RIGHTS:
            cursor.execute(
                "SELECT has_table_privilege('randirandi_app','core_memberimport',%s)",
                [right],
            )
            found[right] = cursor.fetchone()[0]
        return found


def test_the_app_keeps_the_same_rights_on_member_imports_through_0040() -> None:
    config = connection.settings_dict
    assert config["NAME"].startswith("test_")
    with psycopg.connect(
        host=config["HOST"],
        port=config["PORT"],
        dbname=config["NAME"],
        user=config["USER"],
        password=config["PASSWORD"],
        autocommit=True,
    ) as owner:
        owner.execute(
            sql.SQL("CREATE ROLE randirandi_app LOGIN PASSWORD {}").format(
                sql.Literal(secrets.token_urlsafe(32))
            )
        )
        try:
            grant()
            forward = rights()
            assert forward == {
                "SELECT": True,
                "INSERT": True,
                "UPDATE": True,
                "DELETE": True,
                "TRUNCATE": False,
                "REFERENCES": False,
                "TRIGGER": False,
            }
            executor = MigrationExecutor(connection)
            executor.migrate(BEFORE)
            grant()
            assert rights() == forward
            current = MigrationExecutor(connection)
            current.migrate(current.loader.graph.leaf_nodes())
            grant()
            assert rights() == forward
        finally:
            owner.execute("DROP OWNED BY randirandi_app")
            owner.execute("DROP ROLE randirandi_app")
