# SPDX-License-Identifier: AGPL-3.0-only
"""The database comparison the capacity migration test uses to prove a dump
restored into a second database equals its source: privileges, constraints,
indexes, and every row as JSON text.

This copy is the product's own: its tests check it against the product's
migrations. The restore probe's queries (`tools/restore_probe.py`) are the
service's, and nothing here keeps the two equal."""

import re
from typing import Any

import psycopg
from psycopg import sql

QUERIES = {
    "database_privileges": """
        SELECT pg_get_userbyid(datdba),
               has_database_privilege('randirandi_owner',datname,'CONNECT'),
               has_database_privilege('randirandi_owner',datname,'CREATE'),
               has_database_privilege('randirandi_owner',datname,'TEMP'),
               has_database_privilege('randirandi_app',datname,'CONNECT'),
               has_database_privilege('randirandi_app',datname,'CREATE'),
               has_database_privilege('randirandi_app',datname,'TEMP')
        FROM pg_database WHERE datname=current_database()
    """,
    "tables_and_owners": """
        SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m')
        ORDER BY 1,2
    """,
    "row_security": """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY 1
    """,
    "row_policies": """
        SELECT tablename,policyname,permissive,roles,cmd,qual,with_check
        FROM pg_policies WHERE schemaname='public' ORDER BY 1,2
    """,
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod),
               a.attnotnull, a.attidentity, a.attgenerated,
               pg_get_expr(d.adbin,d.adrelid)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum
    """,
    "constraints": """
        SELECT c.relname, k.conname, k.contype, k.condeferrable,
               k.condeferred, k.convalidated, pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY 1,2
    """,
    "indexes": """
        SELECT tablename,indexname,indexdef FROM pg_indexes
        WHERE schemaname='public' ORDER BY 1,2
    """,
    "triggers": """
        SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY 1,2
    """,
    "extensions": """
        SELECT extname,extversion FROM pg_extension ORDER BY 1
    """,
    "table_privileges": """
        SELECT c.relname,
               has_table_privilege(current_user,c.oid,'SELECT'),
               has_table_privilege(current_user,c.oid,'INSERT'),
               has_table_privilege(current_user,c.oid,'UPDATE'),
               has_table_privilege(current_user,c.oid,'DELETE'),
               has_table_privilege(current_user,c.oid,'TRUNCATE'),
               pg_get_userbyid(c.relowner)=current_user
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY 1
    """,
    "schema_privileges": """
        SELECT has_schema_privilege(current_user,'public','USAGE'),
               has_schema_privilege(current_user,'public','CREATE')
    """,
    "reachable_role_privileges": """
        SELECT r.rolname,r.rolsuper,r.rolbypassrls,c.relname,
               pg_has_role(r.oid,c.relowner,'USAGE'),
               has_any_column_privilege(r.oid,c.oid,'UPDATE'),
               has_table_privilege(r.oid,c.oid,'TRUNCATE')
        FROM pg_roles r CROSS JOIN pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE pg_has_role(current_user,r.oid,'SET')
          AND n.nspname='public' AND c.relkind IN ('r','p') ORDER BY 1,4
    """,
    "column_privileges": """
        SELECT c.relname, a.attname,
               has_column_privilege(current_user,c.oid,a.attname,'SELECT'),
               has_column_privilege(current_user,c.oid,a.attname,'INSERT'),
               has_column_privilege(current_user,c.oid,a.attname,'UPDATE')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2
    """,
    "sequence_privileges": """
        SELECT c.relname,
               has_sequence_privilege(current_user,c.oid,'USAGE'),
               has_sequence_privilege(current_user,c.oid,'SELECT'),
               has_sequence_privilege(current_user,c.oid,'UPDATE')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S' ORDER BY 1
    """,
}


def read_database(parameters: dict[str, Any], name: str) -> dict[str, Any]:
    """One consistent read-only snapshot; nothing from it is logged."""
    result: dict[str, Any] = {}
    with psycopg.connect(
        **parameters,
        dbname=name,
        options="-c default_transaction_read_only=on -c statement_timeout=30000",
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        for category, query in QUERIES.items():
            result[category] = connection.execute(query).fetchall()
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "ORDER BY tablename"
        ).fetchall()
        result["rows"] = {}
        for (table,) in tables:
            # JSON text gives a deterministic representation even for UUID,
            # range and timestamp fields. Sort rows independently of heap order.
            rows_query = sql.SQL(
                "SELECT row_to_json(t)::text FROM {} t ORDER BY 1"
            ).format(sql.Identifier("public", table))
            result["rows"][table] = connection.execute(rows_query).fetchall()
    return result


def canonical_sql(definition: str) -> str:
    """Normalize only PostgreSQL's two spellings of constant varchar→text arrays.

    A dump/restore pushes the text[] cast onto each unbounded varchar
    constant. Other expressions and all constant values remain significant.
    """
    constant = r"'[a-z_ ]+'::character varying"
    definition = re.sub(
        rf"\(ARRAY\[({constant}(?:, {constant})*)\]\)::text\[\]",
        lambda match: (
            "ARRAY[" + match[1].replace("::character varying", "::text") + "]"
        ),
        definition,
    )
    return re.sub(r"\(('[a-z_ ]+')::character varying\)::text", r"\1::text", definition)


def compare(source: dict[str, Any], restored: dict[str, Any]) -> dict[str, Any]:
    source, restored = source.copy(), restored.copy()
    for snapshot in (source, restored):
        for category in ("constraints", "indexes"):
            snapshot[category] = [
                (*row[:-1], canonical_sql(row[-1])) for row in snapshot[category]
            ]
    checks = {category: source[category] == restored[category] for category in QUERIES}
    checks["table_contents"] = source["rows"] == restored["rows"]
    checks["has_tables"] = bool(source["rows"])
    checks["has_migration_history"] = bool(source["rows"].get("django_migrations"))
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "tables": len(source["rows"]),
        "rows": sum(len(rows) for rows in source["rows"].values()),
        "migrations": len(source["rows"].get("django_migrations", [])),
        "constraints": len(source["constraints"]),
    }
