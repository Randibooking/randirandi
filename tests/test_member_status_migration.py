# SPDX-License-Identifier: AGPL-3.0-only
"""The member status migration : access becomes a status, both
ways, with rows in it. Fictional people."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0024_vipps_description_and_receipt")]
AFTER = [("core", "0025_member_status")]
START = datetime(2027, 1, 1, 9, tzinfo=UTC)


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def migrate_to(target: list[tuple[str, str]]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def rows(query: str, *values: Any) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query, values)
        return list(cursor.fetchall())


def legacy_people(apps: Any, made: made_up.MadeUp) -> dict[str, int]:
    """Members as the schema before this part holds them: by email."""
    org, role = made.organization.pk, made.standard.pk
    administrator = made.administrator.pk
    members = apps.get_model("core", "Member")._default_manager
    events = apps.get_model("core", "AccessEvent")._default_manager

    def member(name: str, access: str, origin: str = "member import") -> int:
        # An erased member keeps no name and no email.
        erased = name == "erased"
        return int(
            members.create(
                organization_id=org,
                role_id=role,
                first_name="" if erased else name,
                email="" if erased else f"{name}@example.com",
                access=access,
                origin=origin,
            ).pk
        )

    def event(
        who: int, change: str, days: int, cause: str, *, admin: bool = False
    ) -> None:
        events.create(
            organization_id=org,
            member_id=who,
            change=change,
            cause=cause,
            administrator_id=administrator if admin else None,
            at=START + timedelta(days=days),
        )

    ids = {
        "granted": member("granted", "granted"),
        "disabled": member("disabled", "revoked"),
        "blocked": member("blocked", "revoked"),
        "returned": member("returned", "revoked"),
        "added": member("added", "revoked", origin="added by administrator"),
        "erased": member("erased", "revoked"),
    }
    event(ids["granted"], "granted", 0, "member import")
    event(ids["disabled"], "granted", 0, "member import")
    event(ids["disabled"], "revoked", 1, "member import")
    # Revoked by an administrator after the import had listed the member.
    event(ids["blocked"], "granted", 0, "member import")
    event(ids["blocked"], "revoked", 1, "administrator", admin=True)
    # Revoked by the import, then by an administrator: the import's last word
    # was that the member is not on the list.
    event(ids["returned"], "revoked", 1, "member import")
    event(ids["returned"], "revoked", 2, "administrator", admin=True)
    return ids


def state(ids: dict[str, int]) -> dict[str, tuple[str, bool]]:
    by_id = {pk: name for name, pk in ids.items()}
    return {
        by_id[pk]: (status, listed)
        for pk, status, listed in rows(
            "SELECT id, status, listed FROM core_member WHERE id = ANY(%s)",
            list(by_id),
        )
    }


def accesses(ids: dict[str, int]) -> dict[str, str]:
    by_id = {pk: name for name, pk in ids.items()}
    return {
        by_id[pk]: access
        for pk, access in rows(
            "SELECT id, access FROM core_member WHERE id = ANY(%s)", list(by_id)
        )
    }


def test_access_becomes_a_status_with_what_the_list_said_and_back() -> None:
    made = made_up.everything()
    apps = migrate_to(BEFORE)
    ids = legacy_people(apps, made)
    events_before = rows(
        "SELECT member_id, change, cause FROM core_accessevent ORDER BY id"
    )

    migrate_to(AFTER)

    assert state(ids) == {
        "granted": ("active", True),
        "disabled": ("disabled", False),
        "blocked": ("blocked", True),
        "returned": ("blocked", False),
        "added": ("disabled", True),
        "erased": ("disabled", False),
    }
    # Unlisted at: the time of the latest import-caused revoked event, else empty.
    unlisted = {
        name: rows("SELECT unlisted_at FROM core_member WHERE id = %s", pk)[0][0]
        for name, pk in ids.items()
    }
    assert unlisted == {
        "granted": None,
        "disabled": START + timedelta(days=1),
        "blocked": None,
        "returned": START + timedelta(days=1),
        "added": None,
        "erased": None,
    }
    # The made-up members were granted.
    assert rows(
        "SELECT status, listed FROM core_member WHERE id IN (%s, %s) ORDER BY id",
        made.member.pk,
        made.administrator.pk,
    ) == [("active", True), ("active", True)]
    mapped = {"granted": "active", "revoked": "disabled"}
    expected = [
        (
            member,
            "blocked"
            if change == "revoked" and cause == "administrator"
            else mapped[change],
            cause,
        )
        for member, change, cause in events_before
    ]
    assert rows(
        "SELECT member_id, change, cause FROM core_accessevent ORDER BY id"
    ) == (expected)

    migrate_to(BEFORE)

    assert accesses(ids) == {
        "granted": "granted",
        "disabled": "revoked",
        "blocked": "revoked",
        "returned": "revoked",
        "added": "revoked",
        "erased": "revoked",
    }
    assert rows(
        "SELECT member_id, change, cause FROM core_accessevent ORDER BY id"
    ) == (events_before)


def test_a_round_trip_keeps_who_can_log_in() -> None:
    made = made_up.everything()
    people = rows("SELECT id, status FROM core_member ORDER BY id")
    migrate_to(BEFORE)
    migrate_to(AFTER)
    assert rows("SELECT id, status FROM core_member ORDER BY id") == people
    assert rows("SELECT count(*) FROM core_accessevent WHERE change = 'active'") == [
        (1,)
    ]
    assert made.member.pk


def test_reversing_is_refused_while_a_block_or_unblock_is_on_record() -> None:
    made = made_up.everything()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core_auditevent "
            "(organization_id, administrator_id, action, object_kind, object_id, at) "
            "VALUES (%s, %s, 'blocked member', 'member', %s, now())",
            [made.organization.pk, made.administrator.pk, made.member.pk],
        )
    with pytest.raises(RuntimeError, match="block and unblock"):
        migrate_to(BEFORE)
    assert rows("SELECT status FROM core_member WHERE id = %s", made.member.pk) == [
        ("active",)
    ]
