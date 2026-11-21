# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0040 : the member source becomes a parser choice. A
run keeps its parser's id, the file's name and its size; the sources go.
Reversing is refused while a run exists."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import MemberImport
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0039_setup_screens_audit")]


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


def tables() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        return {row[0] for row in cursor.fetchall()}


def columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'core_memberimport'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_an_earlier_run_keeps_its_place_and_the_sources_go() -> None:
    org = made_up.organization()
    executor = reverse()
    assert "core_membersource" in tables()
    assert "member_source_id" in columns() and "parser" not in columns()
    apps = executor.loader.project_state(BEFORE).apps
    sources = apps.get_model("core", "MemberSource")._default_manager
    runs = apps.get_model("core", "MemberImport")._default_manager
    bundled = sources.create(
        organization_id=org.pk, kind="file upload", location="bundled:dummy-members.csv"
    )
    other = sources.create(organization_id=org.pk, kind="spreadsheet", location="x")
    first = runs.create(
        organization_id=org.pk,
        member_source_id=bundled.pk,
        outcome="applied",
        created=3,
    )
    second = runs.create(
        organization_id=org.pk, member_source_id=other.pk, outcome="stopped"
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "core_membersource" not in tables()
    assert {"parser", "file_name", "file_size"} <= columns()
    assert "member_source_id" not in columns()
    kept = MemberImport.objects.of(org).order_by("pk")
    assert [(r.pk, r.parser, r.file_name, r.file_size) for r in kept] == [
        (first.pk, "made-up-members", "", 0),
        (second.pk, "", "", 0),
    ]
    assert kept[0].created == 3 and kept[1].outcome == "stopped"


def test_reversing_is_refused_while_a_run_holds_a_parser_or_a_file() -> None:
    org = made_up.organization()
    attempts: list[dict[str, Any]] = [
        {"parser": "unit-members-xlsx"},
        {"file_name": "a.xlsx"},
        {"file_size": 3},
    ]
    for details in attempts:
        run = MemberImport.objects.create(
            organization=org,
            outcome="applied",
            parser=details.get("parser", ""),
            file_name=details.get("file_name", ""),
            file_size=details.get("file_size", 0),
        )
        with pytest.raises(RuntimeError, match="retained parsers and file names"):
            reverse()
        assert "core_membersource" not in tables() and "parser" in columns()
        run.delete()
    # The refusal changed nothing: the leaves are still today's.
    assert "file_size" in columns()


def test_reversing_gives_each_run_a_bare_source_and_forward_keeps_them() -> None:
    made = made_up.everything()
    other = made_up.organization("other")
    plain = MemberImport.objects.create(organization=other, outcome="stopped")
    current = MigrationExecutor(connection).loader.graph.leaf_nodes()
    executor = reverse()
    apps = executor.loader.project_state(BEFORE).apps
    sources = apps.get_model("core", "MemberSource")._default_manager
    runs = apps.get_model("core", "MemberImport")._default_manager
    assert "parser" not in columns() and "member_source_id" in columns()
    made_run = runs.get(organization_id=made.organization.pk)
    plain_run = runs.get(pk=plain.pk)
    assert (
        sources.get(pk=made_run.member_source_id).location
        == "bundled:dummy-members.csv"
    )
    assert sources.get(pk=plain_run.member_source_id).location == ""
    assert sources.count() == 2
    forward = MigrationExecutor(connection)
    assert forward.loader.graph.leaf_nodes() == current
    forward.migrate(current)
    assert "core_membersource" not in tables()
    kept = {r.pk: r.parser for r in MemberImport.objects.of(made.organization)}
    assert set(kept.values()) == {"made-up-members"}
    assert MemberImport.objects.of(other).get().parser == ""


def test_reversing_is_allowed_without_a_run_and_the_leaves_come_back() -> None:
    made_up.organization()
    current = MigrationExecutor(connection).loader.graph.leaf_nodes()
    reverse()
    assert "core_membersource" in tables()
    assert "parser" not in columns() and "member_source_id" in columns()
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    # The graph's leaves are today's, not a remembered number.
    assert leaves == current
    executor.migrate(leaves)
    assert "core_membersource" not in tables() and "parser" in columns()
