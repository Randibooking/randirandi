# SPDX-License-Identifier: AGPL-3.0-only
"""A real restricted application login on the disposable test database."""

import importlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from django.db import connection
from psycopg import sql

CAPACITY_MIGRATION = importlib.import_module("randirandi.core.migrations.0002_capacity")


AUDIT_MIGRATION = importlib.import_module(
    "randirandi.core.migrations.0004_audit_history"
)
FLOOR_PLAN_MIGRATION = importlib.import_module(
    "randirandi.core.migrations.0016_floor_plans"
)
RULE_MIGRATION = importlib.import_module("randirandi.core.migrations.0017_rule_by_spot")
TIME_ZONE_MIGRATION = importlib.import_module(
    "randirandi.core.migrations.0027_time_zone_and_currency"
)
ASSET_SCREENS_MIGRATION = importlib.import_module(
    "randirandi.core.migrations.0042_asset_screens"
)

# The columns the app may change, for each table that limits them: 0002's
# lists, and the later migrations' for the tables they change.
APP_UPDATES = (
    CAPACITY_MIGRATION.APP_UPDATES
    | FLOOR_PLAN_MIGRATION.APP_UPDATES
    | RULE_MIGRATION.APP_UPDATES
    | TIME_ZONE_MIGRATION.APP_UPDATES
    | ASSET_SCREENS_MIGRATION.APP_UPDATES
    # 0051: a ledger entry is inserted and read, never updated.
    | {"core_ledgerentry": ()}
    | {"core_bookingconditions": ()}
)


@contextmanager
def application_login() -> Iterator[None]:
    """Tests using this helper must use django_db(transaction=True).

    The role and its grants are cleaned up. It has no ownership, role
    membership or schema rights; ORM calls open an actual separate login.
    """
    config = connection.settings_dict
    assert config["HOST"] in ("localhost", "127.0.0.1")
    assert config["NAME"].startswith("test_")
    assert not connection.in_atomic_block
    original = {key: config[key] for key in ("USER", "PASSWORD")}
    password = secrets.token_urlsafe(32)
    parameters = {
        "host": config["HOST"],
        "port": config["PORT"],
        "dbname": config["NAME"],
        "user": original["USER"],
        "password": original["PASSWORD"],
    }
    with psycopg.connect(**parameters, autocommit=True) as owner:
        owner.execute(
            sql.SQL("CREATE ROLE randirandi_app LOGIN PASSWORD {}").format(
                sql.Literal(password)
            )
        )
        try:
            owner.execute("GRANT USAGE ON SCHEMA public TO randirandi_app")
            owner.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN "
                "SCHEMA public TO randirandi_app"
            )
            owner.execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "
                "randirandi_app"
            )
            with connection.schema_editor() as editor:
                RULE_MIGRATION.grant(editor, APP_UPDATES)
                AUDIT_MIGRATION.install_permissions(None, editor)
            connection.close()
            config.update(USER="randirandi_app", PASSWORD=password)
            yield
        finally:
            connection.close()
            config.update(original)
            owner.execute("DROP OWNED BY randirandi_app")
            owner.execute("DROP ROLE randirandi_app")
