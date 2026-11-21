# SPDX-License-Identifier: AGPL-3.0-only
"""The rule against double booking by spot  as an executable
storage-design proof, not the product schema.

The SQL is installed in a separate, disposable database. All reservation
writes use an actual unprivileged login. Product migrations remain unchanged.
"""

import re
import secrets
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from psycopg import sql

SCHEMA = Path(__file__).parent / "proofs" / "capacity.sql"
APP_GRANTS = SCHEMA.with_name("capacity_app_grants.sql")
START = datetime(2027, 1, 3, 10, tzinfo=UTC)


def at(hour: int) -> datetime:
    return START + timedelta(hours=hour - 10)


@dataclass
class ProofDatabase:
    owner: psycopg.Connection[Any]
    parameters: dict[str, Any] = field(repr=False)

    def connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            **self.parameters,
            autocommit=True,
            options="-c statement_timeout=10000 -c lock_timeout=8000",
        )


@pytest.fixture(scope="module")
def proof_database() -> Iterator[ProofDatabase]:
    configured = settings.DATABASES["default"]
    assert configured["HOST"] in ("127.0.0.1", "localhost")
    owner_parameters: dict[str, Any] = {
        "host": configured["HOST"],
        "port": configured["PORT"],
        "user": configured["USER"],
        "password": configured["PASSWORD"],
        "dbname": "postgres",
    }
    name = "capacity_proof_" + uuid.uuid4().hex
    role = name + "_app"
    # Only in memory; this login and the database are destroyed below.
    password = secrets.token_urlsafe(32)
    with psycopg.connect(**owner_parameters, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            control.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            with psycopg.connect(
                **(owner_parameters | {"dbname": name}), autocommit=True
            ) as owner:
                owner.execute(SCHEMA.read_text())
                owner.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                owner.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        sql.Identifier(role)
                    )
                )
                owner.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(sql.Identifier(role))
                )
                owner.execute(
                    sql.SQL(APP_GRANTS.read_text()).format(app=sql.Identifier(role))
                )
                yield ProofDatabase(
                    owner,
                    owner_parameters
                    | {"dbname": name, "user": role, "password": password},
                )
        finally:
            control.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            control.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
            )


@pytest.fixture
def db(proof_database: ProofDatabase) -> Iterator[psycopg.Connection[Any]]:
    owner = proof_database.owner
    owner.execute(
        "TRUNCATE allocation, taken_time, spot, floor_plan, asset, space, organization"
    )
    with proof_database.connect() as app:
        app.execute("INSERT INTO organization VALUES (1), (2)")
        app.execute("INSERT INTO space VALUES (1, 1), (2, 1), (3, 2)")
        # Two washers and the whole laundry in space 1; a guest room that
        # covers its wing with four beds, and a reading nook, in space 2; a
        # sauna with eight places and an item without a space; another
        # organization's item.
        app.execute("""
            INSERT INTO asset (id, organization_id, space_id, whole_space)
            VALUES (1,1,1,false), (2,1,1,false), (3,1,1,true), (4,1,2,true),
                   (5,1,NULL,false), (6,1,NULL,false), (7,2,3,false),
                   (8,1,2,false)
        """)
        app.execute("INSERT INTO floor_plan SELECT id, organization_id, id FROM asset")
        # Spot n of floor plan f has the id 10 f + n.
        app.execute("""
            INSERT INTO spot (id, organization_id, floor_plan_id, number)
            SELECT 10 * f.id + n, f.organization_id, f.id, n FROM floor_plan f,
            generate_series(1, CASE f.id WHEN 4 THEN 4 WHEN 5 THEN 8 ELSE 1 END) n
        """)
        yield app


def header(
    db: psycopg.Connection[Any],
    booking: int,
    asset: int = 5,
    *,
    quantity: int = 1,
    mode: str = "shared",
    state: str = "held",
    start: int = 10,
    end: int = 12,
) -> None:
    db.execute(
        """
        INSERT INTO taken_time
            (id, organization_id, asset_id, scope_key, whole_space, mode,
             quantity, state, starts_at, ends_at)
        SELECT %s, organization_id, id, scope_key, whole_space, %s, %s, %s, %s, %s
        FROM asset WHERE id = %s
        """,
        (booking, mode, quantity, state, at(start), at(end), asset),
    )


def hold(
    db: psycopg.Connection[Any],
    booking: int,
    ordinal: int,
    spot: int | None,
    *,
    floor_plan: int | None = None,
) -> None:
    """One row of the claim: the spot by its id; none for the whole asset."""
    db.execute(
        """
        INSERT INTO allocation
            (taken_time_id, ordinal, organization_id, asset_id, floor_plan_id,
             spot_id, scope_key, whole_space, mode, quantity, state, starts_at,
             ends_at)
        SELECT id, %s, organization_id, asset_id, %s, %s, scope_key, whole_space,
               mode, quantity, state, starts_at, ends_at
        FROM taken_time WHERE id = %s
        """,
        (ordinal, floor_plan, spot, booking),
    )


def take(
    db: psycopg.Connection[Any],
    booking: int,
    spots: tuple[int, ...] = (1,),
    *,
    asset: int = 5,
    mode: str = "shared",
    state: str = "held",
    start: int = 10,
    end: int = 12,
) -> None:
    """A claim on the asset holding its spots by number, or the whole asset."""
    with db.transaction():
        header(
            db,
            booking,
            asset,
            quantity=1 if mode == "exclusive" else len(spots),
            mode=mode,
            state=state,
            start=start,
            end=end,
        )
        if mode == "exclusive":
            hold(db, booking, 1, None)
        for ordinal, number in enumerate(spots if mode == "shared" else (), 1):
            hold(db, booking, ordinal, 10 * asset + number, floor_plan=asset)


def test_laundry_siblings_are_independent_and_whole_space_excludes_both(
    db: psycopg.Connection[Any],
) -> None:
    take(db, 1, asset=1)
    take(db, 2, asset=2)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        take(db, 3, asset=1)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        take(db, 4, asset=3, mode="exclusive")


def test_whole_space_first_blocks_every_asset_of_its_space(
    db: psycopg.Connection[Any],
) -> None:
    take(db, 1, asset=3, mode="exclusive")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        take(db, 2, asset=2)
    take(db, 3, asset=6)  # Without a space, not in the laundry.


@pytest.mark.parametrize("marked_first", [True, False])
def test_a_shared_claim_on_a_marked_asset_keeps_its_space_and_its_spots(
    db: psycopg.Connection[Any], marked_first: bool
) -> None:
    if marked_first:
        take(db, 1, (1, 2), asset=4)
        take(db, 2, (3,), asset=4)  # Its own other spots stay free.
        with pytest.raises(psycopg.errors.ExclusionViolation):
            take(db, 3, (2,), asset=4)
        with pytest.raises(psycopg.errors.ExclusionViolation):
            take(db, 4, asset=8)
    else:
        take(db, 1, asset=8)
        with pytest.raises(psycopg.errors.ExclusionViolation):
            take(db, 2, (4,), asset=4)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        take(db, 5, asset=4, mode="exclusive")


def test_a_second_marked_asset_in_a_space_is_refused(
    db: psycopg.Connection[Any],
) -> None:
    # One asset per space carries the mark: no native key could keep two
    # marked assets' shared claims apart.
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute("UPDATE asset SET whole_space = true WHERE id = 1")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(
            "INSERT INTO asset (id, organization_id, space_id, whole_space) "
            "VALUES (9, 1, 1, true)"
        )


@pytest.mark.parametrize("mode", ["shared", "exclusive"])
@pytest.mark.parametrize("marked_first", [True, False])
def test_a_marked_claim_is_refused_beside_every_claim_in_its_space(
    db: psycopg.Connection[Any], mode: str, marked_first: bool
) -> None:
    # The laundry: washers 1 and 2 and the whole laundry, asset 3, marked.
    others = [(1, "shared"), (2, "shared"), (1, "exclusive"), (2, "exclusive")]
    for booking, (asset, other_mode) in enumerate(others, 1):
        first, second = (
            ((3, mode), (asset, other_mode))
            if marked_first
            else ((asset, other_mode), (3, mode))
        )
        with db.transaction(force_rollback=True):
            take(db, 10 * booking, asset=first[0], mode=first[1])
            with pytest.raises(psycopg.errors.ExclusionViolation):
                take(db, 10 * booking + 1, asset=second[0], mode=second[1])
    # Its own other spots stay free beside a shared claim on it.
    take(db, 1, (1, 2), asset=4)
    take(db, 2, (3,), asset=4)


@pytest.mark.parametrize("exclusive_first", [False, True])
def test_shared_and_exclusive_conflict_in_either_order(
    db: psycopg.Connection[Any], exclusive_first: bool
) -> None:
    if exclusive_first:
        take(db, 1, mode="exclusive")
        with pytest.raises(psycopg.errors.ExclusionViolation):
            take(db, 2, (7,))
    else:
        take(db, 1, (7,))
        with pytest.raises(psycopg.errors.ExclusionViolation):
            take(db, 2, mode="exclusive")


def test_one_spot_sold_twice_and_a_touching_boundary(
    db: psycopg.Connection[Any],
) -> None:
    take(db, 1, (1, 2, 3), start=12, end=14)
    take(db, 2, (4, 5, 6, 7, 8), start=13, end=14)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        take(db, 3, (1,), start=13, end=15)
    take(db, 4, (1, 2, 3), start=14, end=18)
    take(db, 5, mode="exclusive", start=18, end=20)


def test_full_interval_across_midnight(db: psycopg.Connection[Any]) -> None:
    take(db, 1, asset=6, mode="exclusive", start=17, end=32)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        take(db, 2, asset=6, start=30, end=31)
    take(db, 3, asset=6, start=32, end=33)


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_release_reaches_every_spot_and_reactivation_rechecks(
    db: psycopg.Connection[Any], state: str
) -> None:
    take(db, 1, (1, 2))
    db.execute("UPDATE taken_time SET state = %s WHERE id = 1", (state,))
    assert db.execute(
        "SELECT array_agg(state) FROM allocation WHERE taken_time_id = 1"
    ).fetchone() == ([state, state],)
    take(db, 2, (1,))
    with pytest.raises(psycopg.errors.ExclusionViolation):
        db.execute("UPDATE taken_time SET state = 'held' WHERE id = 1")


def test_several_claims_go_in_together_or_not_at_all(
    db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(psycopg.errors.ExclusionViolation), db.transaction():
        take(db, 1, asset=1)
        take(db, 2, asset=6)
        take(db, 3, asset=1)
    assert db.execute("SELECT count(*) FROM taken_time").fetchone() == (0,)


@pytest.mark.parametrize("case", ["none", "missing-first", "missing-last"])
def test_incomplete_allocations_cannot_commit(
    db: psycopg.Connection[Any], case: str
) -> None:
    held = {"none": (), "missing-first": (2,), "missing-last": (1,)}[case]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        header(db, 1, quantity=2)
        for ordinal in held:
            hold(db, 1, ordinal, 50 + ordinal, floor_plan=5)


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("extra-ordinal", psycopg.errors.CheckViolation),
        ("same-spot-twice", psycopg.errors.ExclusionViolation),
        ("spot-of-another-asset", psycopg.errors.ForeignKeyViolation),
        ("spot-off-its-floor-plan", psycopg.errors.ForeignKeyViolation),
        ("exclusive-with-a-spot", psycopg.errors.CheckViolation),
        ("shared-without-a-spot", psycopg.errors.CheckViolation),
    ],
)
def test_a_forged_quantity_or_spot_is_refused(
    db: psycopg.Connection[Any], case: str, error: type[Exception]
) -> None:
    with pytest.raises(error), db.transaction():
        if case == "exclusive-with-a-spot":
            header(db, 1, mode="exclusive")
            hold(db, 1, 1, 51, floor_plan=5)
            return
        header(db, 1, quantity=2 if case == "same-spot-twice" else 1)
        if case == "extra-ordinal":
            hold(db, 1, 1, 51, floor_plan=5)
            hold(db, 1, 2, 52, floor_plan=5)
        elif case == "same-spot-twice":
            hold(db, 1, 1, 51, floor_plan=5)
            hold(db, 1, 2, 51, floor_plan=5)
        elif case == "spot-of-another-asset":
            hold(db, 1, 1, 61, floor_plan=6)
        elif case == "spot-off-its-floor-plan":
            hold(db, 1, 1, 61, floor_plan=5)
        else:
            hold(db, 1, 1, None)


@pytest.mark.parametrize(
    ("column", "value"), [("scope_key", -6), ("scope_key", 1), ("whole_space", True)]
)
def test_a_claim_with_a_stale_copy_of_its_asset_is_refused(
    db: psycopg.Connection[Any], column: str, value: Any
) -> None:
    copies = [
        sql.Literal(value) if name == column else sql.Identifier(name)
        for name in ("scope_key", "whole_space")
    ]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            sql.SQL(
                """
                INSERT INTO taken_time
                    (id, organization_id, asset_id, scope_key, whole_space, mode,
                     quantity, state, starts_at, ends_at)
                SELECT 1, organization_id, id, {}, {}, 'exclusive', 1, 'held',
                       %s, %s
                FROM asset WHERE id = 5
                """
            ).format(*copies),
            (at(10), at(12)),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("scope_key", -6),
        ("whole_space", True),
        ("state", "cancelled"),
        ("asset_id", 6),
        ("starts_at", at(11)),
    ],
)
def test_an_allocation_must_copy_its_claim(
    db: psycopg.Connection[Any], column: str, value: Any
) -> None:
    columns = (
        "organization_id",
        "asset_id",
        "scope_key",
        "whole_space",
        "mode",
        "quantity",
        "state",
        "starts_at",
        "ends_at",
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        header(db, 1)
        db.execute(
            sql.SQL(
                """
                INSERT INTO allocation
                    (taken_time_id, ordinal, floor_plan_id, spot_id, {})
                SELECT id, 1, 5, 51, {} FROM taken_time WHERE id = 1
                """
            ).format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(
                    sql.Literal(value) if name == column else sql.Identifier(name)
                    for name in columns
                ),
            )
        )


@pytest.mark.parametrize("change", ["move", "mark"])
def test_a_change_to_the_asset_carries_its_claims_and_is_rechecked(
    db: psycopg.Connection[Any], change: str
) -> None:
    if change == "move":
        take(db, 1, asset=8)
        take(db, 2, asset=3, mode="exclusive")
        statement = "UPDATE asset SET space_id = 1 WHERE id = 8"
    else:
        # The whole laundry gives up the mark, which washer 1 then takes.
        db.execute("UPDATE asset SET whole_space = false WHERE id = 3")
        take(db, 1, asset=1)
        take(db, 2, asset=2)
        statement = "UPDATE asset SET whole_space = true WHERE id = 1"
    with pytest.raises(psycopg.errors.ExclusionViolation):
        db.execute(statement)
    # Without a clash the copies follow the asset.
    take(db, 3, asset=6, start=14, end=16)
    db.execute("UPDATE asset SET space_id = 2 WHERE id = 6")
    assert db.execute(
        "SELECT t.scope_key, a.scope_key FROM taken_time t "
        "JOIN allocation a ON a.taken_time_id = t.id WHERE t.id = 3"
    ).fetchone() == (2, 2)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE allocation SET state = 'cancelled'",
        "UPDATE allocation SET spot_id = 52",
        "UPDATE taken_time SET scope_key = -6",
        "UPDATE taken_time SET whole_space = true",
        "UPDATE taken_time SET asset_id = 6",
        "UPDATE spot SET number = 9",
        "UPDATE floor_plan SET asset_id = 6",
    ],
)
def test_the_application_cannot_change_a_copy_or_a_spot(
    db: psycopg.Connection[Any], statement: str
) -> None:
    take(db, 1)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


@pytest.mark.parametrize("state", ["held", "cancelled"])
def test_a_spot_or_an_asset_with_a_claim_stays(
    db: psycopg.Connection[Any], state: str
) -> None:
    take(db, 1, state=state)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM spot WHERE id = 51")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM asset WHERE id = 5")


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE allocation DROP CONSTRAINT capacity_no_double_booking",
        "ALTER TABLE allocation DISABLE TRIGGER ALL",
        "TRUNCATE allocation CASCADE",
        "CREATE TABLE bypass (id integer)",
        "SET session_replication_role = replica",
    ],
)
def test_application_login_cannot_remove_the_guard(
    db: psycopg.Connection[Any], statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


def test_no_extension_or_user_trigger_is_needed(
    db: psycopg.Connection[Any], proof_database: ProofDatabase
) -> None:
    owner = proof_database.owner
    assert owner.execute(
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
    ).fetchone() == (0,)
    owner.execute("DROP EXTENSION plpgsql CASCADE")
    try:
        # The removal is committed before a separate login writes.
        assert db.execute("SELECT count(*) FROM pg_extension").fetchone() == (0,)
        take(db, 1)
        with pytest.raises(psycopg.errors.ExclusionViolation):
            take(db, 2)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute("DELETE FROM allocation WHERE taken_time_id = 1")
    finally:
        owner.execute("CREATE EXTENSION plpgsql")


@pytest.mark.parametrize("second_spot", [1, 2])
def test_simultaneous_claims_on_one_spot_and_on_two(
    db: psycopg.Connection[Any], proof_database: ProofDatabase, second_spot: int
) -> None:
    ready = threading.Event()
    finished = threading.Event()
    outcomes: list[str] = []

    def second() -> None:
        with proof_database.connect() as other:
            ready.set()
            try:
                take(other, 2, (second_spot,))
                outcomes.append("committed")
            except psycopg.errors.ExclusionViolation:
                outcomes.append("refused")
            finally:
                finished.set()

    with db.transaction():
        take(db, 1)
        worker = threading.Thread(target=second)
        worker.start()
        assert ready.wait(timeout=5)
        # The same spot waits for the first to end; another does not.
        assert finished.wait(timeout=0.25) is (second_spot == 2)
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcomes == ["refused" if second_spot == 1 else "committed"]
    count = db.execute("SELECT count(*) FROM taken_time").fetchone()
    assert count == (1 if second_spot == 1 else 2,)


def test_organizations_are_kept_apart(db: psycopg.Connection[Any]) -> None:
    take(db, 1, asset=7)
    # Another organization's asset with the same space key is not in the way.
    db.execute("INSERT INTO space VALUES (4, 1)")
    db.execute("UPDATE asset SET space_id = 4 WHERE id = 6")
    take(db, 2, asset=6)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        header(db, 3, asset=6)
        hold(db, 3, 1, 71, floor_plan=7)


def test_effective_update_permissions_exclude_every_copy(
    db: psycopg.Connection[Any],
) -> None:
    writable = set(
        db.execute("""SELECT c.relname, a.attname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND a.attnum > 0 AND NOT a.attisdropped
              AND has_column_privilege(current_user, c.oid, a.attname, 'UPDATE')
        """).fetchall()
    )
    assert writable == {
        (table, column)
        for table, columns in {
            "asset": ("space_id", "whole_space"),
            "taken_time": ("state", "mode", "quantity", "starts_at", "ends_at"),
        }.items()
        for column in columns
    }


def test_complete_reservation_deletion_remains_an_explicit_lifecycle_operation(
    db: psycopg.Connection[Any],
) -> None:
    take(db, 1, asset=3, mode="exclusive")
    with db.transaction():
        db.execute("DELETE FROM allocation WHERE taken_time_id = 1")
        db.execute("DELETE FROM taken_time WHERE id = 1")
    take(db, 2, asset=1)


RULE = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE conname = 'capacity_no_double_booking'"
)


def normalised(definition: str) -> str:
    """The rule as PostgreSQL gives it back, without the casts that differ
    between the proof's text columns and the product's character varying,
    the brackets they bring, and spaces. Every other bracket stays, so a
    regrouped condition is told apart."""
    definition = re.sub(r"\((\w+)\)::(?:text|character varying)\b", r"\1", definition)
    definition = re.sub(
        r"\((ARRAY\[[^\]]*\])\)::(?:text|character varying)\[\]", r"\1", definition
    )
    for cast in ("::character varying[]", "::character varying", "::text[]", "::text"):
        definition = definition.replace(cast, "")
    return re.sub(r"\s", "", definition)


@pytest.mark.django_db
def test_the_proofs_rule_is_the_migrated_databases(
    db: psycopg.Connection[Any],
) -> None:
    proof = db.execute(RULE).fetchone()
    with connection.cursor() as cursor:
        cursor.execute(RULE)
        product = cursor.fetchone()
    assert proof is not None and product is not None
    assert normalised(proof[0]) == normalised(product[0])
