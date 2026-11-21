# SPDX-License-Identifier: AGPL-3.0-only
"""The application login's rights: one declared contract (randirandi/ops/
privileges.py), the deploy step that brings a database to it, and the
read-only check. A fresh database is built by the migrations alone, in a
database of its own, and the contract must equal what it gives the login."""

import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from randirandi.ops import privileges
from randirandi.ops.management.commands.grants import GRANTS

ROOT = Path(__file__).resolve().parent.parent
FRESH = "contract_fresh"


def connect(name: str) -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["RANDIRANDI_DB_HOST"],
        port=os.environ["RANDIRANDI_DB_PORT"],
        dbname=name,
        user=os.environ["RANDIRANDI_DB_USER"],
        password=os.environ["RANDIRANDI_DB_PASSWORD"],
        autocommit=True,
    )


def manage(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed command on a disposable database
        [sys.executable, "manage.py", *arguments],
        cwd=ROOT,
        env=os.environ | {"RANDIRANDI_DB_NAME": FRESH},
        capture_output=True,
        text=True,
        check=False,
    )


def acls(owner: psycopg.Connection) -> list[tuple[object, ...]]:
    return owner.execute("""SELECT c.relname, c.relacl::text, (SELECT string_agg(
        a.attname || a.attacl::text, ',' ORDER BY a.attname) FROM pg_attribute a
        WHERE a.attrelid = c.oid AND a.attacl IS NOT NULL)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' ORDER BY 1""").fetchall()


def rights(owner: psycopg.Connection) -> set[privileges.Right]:
    with owner.cursor() as cursor:
        return privileges.held(cursor)


@pytest.fixture(scope="module")
def pristine() -> Iterator[set[privileges.Right]]:
    """A database the login and the operator's default rights exist in before the
    migrations, as at a deploy: the grants, then the migrations. Yields what
    the login held after the migrations alone."""
    user = os.environ["RANDIRANDI_DB_USER"]
    with connect(os.environ["RANDIRANDI_DB_NAME"]) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE randirandi_app LOGIN PASSWORD {}").format(
                sql.Literal(secrets.token_urlsafe(32))
            )
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(FRESH)))
        try:
            with connect(FRESH) as owner:
                for grant in GRANTS:
                    owner.execute(grant.replace("randirandi_owner", user))
                done = manage("migrate", "--no-input")
                assert done.returncode == 0, done.stdout[-500:] + done.stderr[-500:]
                yield rights(owner)
                owner.execute("DROP OWNED BY randirandi_app")
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(FRESH)
                )
            )
            admin.execute("DROP OWNED BY randirandi_app")
            admin.execute("DROP ROLE randirandi_app")


@pytest.fixture
def owner(pristine: set[privileges.Right]) -> Iterator[psycopg.Connection]:
    """The fresh database, brought back to what the migrations gave it."""
    with connect(FRESH) as owner:
        owner.execute("SELECT 1")
        yield owner
        with owner.transaction(), owner.cursor() as cursor:
            privileges.bring_to_contract(cursor)


def test_the_contract_is_what_the_migrations_alone_give_a_fresh_database(
    pristine: set[privileges.Right], owner: psycopg.Connection
) -> None:
    with owner.cursor() as cursor:
        wanted = privileges.declared(cursor)
    assert privileges.counts(pristine - wanted) == {}
    assert privileges.counts(wanted - pristine) == {}
    tables = {name for kind, name, _, _ in wanted if kind == "table"}
    columns = [right for right in wanted if right[0] == "column"]
    assert len(tables) > 30
    assert set(privileges.UPDATE_COLUMNS) <= tables
    assert len(columns) == sum(len(c) for c in privileges.UPDATE_COLUMNS.values())


def test_the_audit_history_has_no_update_or_truncate_in_the_contract() -> None:
    assert privileges.UPDATE_COLUMNS["core_auditevent"] == ()


def test_a_database_with_everything_granted_ends_at_the_contract(
    owner: psycopg.Connection, pristine: set[privileges.Right]
) -> None:
    owner.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO randirandi_app")
    owner.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO randirandi_app")
    owner.execute("GRANT CREATE ON SCHEMA public TO randirandi_app")
    owner.execute("GRANT UPDATE (id) ON core_capacityallocation TO randirandi_app")
    assert rights(owner) != pristine

    done = manage("apply_privileges")

    assert done.returncode == 0, done.stderr[-500:]
    assert rights(owner) == pristine
    with owner.cursor() as cursor:
        assert privileges.difference(cursor) == (set(), set())


def test_a_second_run_changes_nothing(
    owner: psycopg.Connection, pristine: set[privileges.Right]
) -> None:
    owner.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO randirandi_app")
    assert manage("apply_privileges").returncode == 0
    after_first = acls(owner)

    assert manage("apply_privileges").returncode == 0

    assert acls(owner) == after_first
    with owner.cursor() as cursor, owner.transaction():
        assert privileges.bring_to_contract(cursor) == (0, 0)
    assert rights(owner) == pristine


def test_the_audit_history_refuses_update_and_truncate_after_the_step(
    owner: psycopg.Connection,
) -> None:
    owner.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO randirandi_app")
    assert manage("apply_privileges").returncode == 0

    for statement in (
        "UPDATE core_auditevent SET action = action",
        "TRUNCATE core_auditevent",
    ):
        with owner.transaction(), pytest.raises(psycopg.errors.InsufficientPrivilege):
            owner.execute("SET LOCAL ROLE randirandi_app")
            owner.execute(statement)


def test_the_step_leaves_a_right_it_cannot_take_and_changes_nothing(
    owner: psycopg.Connection,
) -> None:
    owner.execute("GRANT TRUNCATE ON core_auditevent TO PUBLIC")
    before = acls(owner)
    try:
        done = manage("apply_privileges")
        assert done.returncode != 0
        assert acls(owner) == before
    finally:
        owner.execute("REVOKE TRUNCATE ON core_auditevent FROM PUBLIC")


def test_the_step_refuses_a_right_that_comes_through_a_role_and_changes_nothing(
    owner: psycopg.Connection,
) -> None:
    owner.execute("CREATE ROLE contract_truncator")
    owner.execute("GRANT TRUNCATE ON core_auditevent TO contract_truncator")
    owner.execute("GRANT contract_truncator TO randirandi_app")
    before = acls(owner)
    try:
        assert manage("check_privileges").returncode == 1
        assert manage("apply_privileges").returncode != 0
        assert acls(owner) == before
    finally:
        owner.execute("REVOKE contract_truncator FROM randirandi_app")
        owner.execute("DROP OWNED BY contract_truncator")
        owner.execute("DROP ROLE contract_truncator")


def test_the_check_exits_zero_on_the_contract_and_prints_only_counts(
    owner: psycopg.Connection,
) -> None:
    done = manage("check_privileges")

    assert done.returncode == 0, done.stderr[-500:]
    assert "as the contract" in done.stdout


def test_the_check_exits_one_on_extra_or_missing_rights_and_changes_nothing(
    owner: psycopg.Connection,
) -> None:
    owner.execute("GRANT TRUNCATE ON core_auditevent TO randirandi_app")
    owner.execute("GRANT UPDATE (id) ON core_capacityallocation TO randirandi_app")
    owner.execute("REVOKE SELECT ON core_organization FROM randirandi_app")
    before = acls(owner)

    done = manage("check_privileges")

    assert done.returncode == 1
    assert "beyond the contract: table core_auditevent: 1" in done.stdout
    assert "beyond the contract: column core_capacityallocation: 1" in done.stdout
    assert "missing: table core_organization: 1" in done.stdout
    assert "(id)" not in done.stdout + done.stderr
    assert acls(owner) == before
