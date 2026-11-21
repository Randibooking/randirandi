# SPDX-License-Identifier: AGPL-3.0-only
"""The application login's database rights, declared once.

The migrations give and take rights one at a time (the audit history takes
UPDATE and TRUNCATE away, three migrations take table UPDATE away for a
column list). A restore, or a reset of the permission at the platform, gives
the login more. This module is the whole of what the login holds, so a
deploy can bring any database to it and a check can compare without
changing. A test proves it equals what the migrations alone give a fresh
database; a migration that changes rights must change this file.
"""

from typing import Any

APP = "randirandi_app"

# Every table in the public schema carries these, as the operator's default
# privileges give them (grants.py), unless the next lines say otherwise.
TABLE_RIGHTS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
SEQUENCE_RIGHTS = frozenset({"USAGE", "SELECT"})
SCHEMA_RIGHTS = frozenset({"USAGE"})

# Tables that lose table-wide UPDATE, with the columns that keep it (an
# empty tuple: no UPDATE at all). From migrations 0002, 0004, 0016, 0017
# 0027, 0042, 0051 and 0053; the last migration to name a table sets its columns.
UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "core_asset": (
        "description",
        "maximum_occupancy",
        "name",
        "space_id",
        "time_zone",
        "whole_space",
    ),
    "core_assetinventory": (
        "capacity",
        "last_group_number",
        "last_spot_number",
        "name",
        "unit",
    ),
    "core_auditevent": (),
    "core_bookingconditions": (),
    "core_capacityallocation": (),
    "core_ledgerentry": (),
    "core_space": ("booking_page", "description", "name"),
    "core_spot": ("group_id", "name", "place"),
    "core_spotgroup": ("name", "word"),
    "core_takentime": ("ends_at", "mode", "quantity", "starts_at", "state"),
}

# What PostgreSQL can grant on each kind of object.
ALL_TABLE_RIGHTS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
ALL_COLUMN_RIGHTS = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
ALL_SEQUENCE_RIGHTS = ("USAGE", "SELECT", "UPDATE")
ALL_SCHEMA_RIGHTS = ("USAGE", "CREATE")

# One right of the login: (kind, object, column or "", right).
type Right = tuple[str, str, str, str]


def has_login(cursor: Any) -> bool:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [APP])
    return cursor.fetchone() is not None


def _names(cursor: Any, kinds: str) -> list[str]:
    cursor.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind::text = ANY(%s::text[]) ORDER BY 1",
        [list(kinds)],
    )
    return [name for (name,) in cursor.fetchall()]


def declared(cursor: Any) -> set[Right]:
    """The contract, spread over the tables and sequences the database has."""
    tables = _names(cursor, "rp")
    unknown = sorted(set(UPDATE_COLUMNS) - set(tables))
    if unknown:
        raise RuntimeError(f"The contract names tables that are not there: {unknown}")
    wanted: set[Right] = set()
    for table in tables:
        for right in TABLE_RIGHTS:
            if right == "UPDATE" and table in UPDATE_COLUMNS:
                continue
            wanted.add(("table", table, "", right))
        for column in UPDATE_COLUMNS.get(table, ()):
            wanted.add(("column", table, column, "UPDATE"))
    for sequence in _names(cursor, "S"):
        wanted |= {("sequence", sequence, "", right) for right in SEQUENCE_RIGHTS}
    wanted |= {("schema", "public", "", right) for right in SCHEMA_RIGHTS}
    return wanted


def held(cursor: Any) -> set[Right]:
    """What the login can do now, through any route PostgreSQL counts."""
    found: set[Right] = set()
    cursor.execute("SHOW server_version_num")
    modern = int(cursor.fetchone()[0]) >= 170000
    for table in _names(cursor, "rp"):
        for right in ALL_TABLE_RIGHTS:
            if right == "MAINTAIN" and not modern:
                continue
            cursor.execute(
                "SELECT has_table_privilege("
                "%s::name, quote_ident(%s::text)::regclass, %s::text)",
                [APP, table, right],
            )
            if cursor.fetchone()[0]:
                found.add(("table", table, "", right))
    # A column right is listed only where the table does not give it whole.
    cursor.execute(
        """SELECT c.relname, a.attname, r.priv,
            has_column_privilege(%s::name, c.oid, a.attnum, r.priv)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            CROSS JOIN unnest(%s::text[]) AS r(priv)
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            AND a.attnum > 0 AND NOT a.attisdropped""",
        [APP, list(ALL_COLUMN_RIGHTS)],
    )
    for table, column, right, allowed in cursor.fetchall():
        if allowed and ("table", table, "", right) not in found:
            found.add(("column", table, column, right))
    for sequence in _names(cursor, "S"):
        for right in ALL_SEQUENCE_RIGHTS:
            cursor.execute(
                "SELECT has_sequence_privilege("
                "%s::name, quote_ident(%s::text)::regclass, %s::text)",
                [APP, sequence, right],
            )
            if cursor.fetchone()[0]:
                found.add(("sequence", sequence, "", right))
    for right in ALL_SCHEMA_RIGHTS:
        cursor.execute(
            "SELECT has_schema_privilege(%s::name, 'public', %s::text)", [APP, right]
        )
        if cursor.fetchone()[0]:
            found.add(("schema", "public", "", right))
    return found


def difference(cursor: Any) -> tuple[set[Right], set[Right]]:
    """(missing, extra): what the contract has and the login lacks, and the reverse."""
    wanted, now = declared(cursor), held(cursor)
    return wanted - now, now - wanted


def counts(rights: set[Right]) -> dict[str, int]:
    """Rights per object, by name only."""
    per: dict[str, int] = {}
    for kind, name, _, _ in rights:
        key = f"{kind} {name}"
        per[key] = per.get(key, 0) + 1
    return dict(sorted(per.items()))


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _statement(verb: str, right: Right) -> str:
    kind, name, column, what = right
    target = {"table": "TABLE", "column": "TABLE", "sequence": "SEQUENCE"}.get(
        kind, "SCHEMA"
    )
    columns = f" ({_quote(column)})" if column else ""
    side = "TO" if verb == "GRANT" else "FROM"
    return f"{verb} {what}{columns} ON {target} {_quote(name)} {side} {APP}"


def bring_to_contract(cursor: Any) -> tuple[int, int]:
    """Takes away what is beyond the contract and gives what is missing.
    Returns (taken, given). The caller holds the transaction; run as the
    owner of the tables. Raises when a right remains that this cannot take
    away (one that comes through PUBLIC or another role)."""
    _, extra = difference(cursor)
    taken = len(extra)
    for right in sorted(extra):
        cursor.execute(_statement("REVOKE", right))
    # A table-wide revoke also takes the column rights under it: look again.
    missing, _ = difference(cursor)
    given = len(missing)
    for right in sorted(missing):
        cursor.execute(_statement("GRANT", right))
    missing, extra = difference(cursor)
    if missing or extra:
        raise RuntimeError(
            f"{len(missing)} rights missing and {len(extra)} beyond the contract "
            f"remain, on: {sorted(counts(missing | extra))}"
        )
    return taken, given
