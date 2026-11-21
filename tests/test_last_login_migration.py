# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0033 : a member keeps the moment of their last
sign-in. Reversing is refused while a member holds one, and works when none
does."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import LoginAttempt, Member
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0032_blocks_admin")]


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


def columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'core_member'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_a_member_has_no_last_login_until_one_is_set() -> None:
    made = made_up.everything()
    assert "last_login_at" in columns()
    assert made.member.last_login_at is None


def test_reversing_is_refused_while_a_member_holds_a_last_login() -> None:
    made = made_up.everything()
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        last_login_at=datetime(2027, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(RuntimeError, match="retained last logins"):
        reverse()
    assert "last_login_at" in columns()
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        last_login_at=None
    )
    reverse()
    assert "last_login_at" not in columns()


BACKFILL = [("core", "0033_member_last_login_at")]
FIRST = datetime(2027, 1, 10, 8, 0, tzinfo=UTC)


def attempt(
    made: made_up.MadeUp, who: Member, kind: str, outcome: str, days: int
) -> None:
    LoginAttempt.objects.create(
        organization=made.organization,
        member=who,
        at=FIRST + timedelta(days=days),
        network="192.0.2.1",
        kind=kind,
        outcome=outcome,
    )


def test_the_backfill_reads_the_kept_attempts_that_granted_a_session() -> None:
    made = made_up.everything()
    refused = Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Robin Lake",
        email="robin@harbourview.example",
        status="active",
        origin="member import",
    )
    nothing = Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Blake Gray",
        email="blake@harbourview.example",
        status="active",
        origin="member import",
    )
    # A member: the newest accepted login code; a refused or later request is no login.
    attempt(made, made.member, "verify", "accepted", 1)
    attempt(made, made.member, "verify", "accepted", 3)
    attempt(made, made.member, "verify", "refused", 5)
    attempt(made, made.member, "request", "issued", 6)
    # An administrator: the accepted authenticator code; a verify without it is none.
    attempt(made, made.administrator, "verify", "accepted", 2)
    attempt(made, made.administrator, "mfa", "accepted", 2)
    attempt(made, made.administrator, "verify", "accepted", 4)
    attempt(made, made.administrator, "mfa", "refused", 4)
    attempt(made, refused, "verify", "refused", 1)
    Member.objects.of(made.organization).update(last_login_at=None)
    reverse()
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BACKFILL)
    found = {m.full_name: m.last_login_at for m in Member.objects.of(made.organization)}
    assert found["Alex Rivers"] == FIRST + timedelta(days=3)
    assert found["Sam Field"] == FIRST + timedelta(days=2)
    assert found["Robin Lake"] is None
    assert found[nothing.full_name] is None


def test_an_administrator_with_only_a_verify_has_no_last_login() -> None:
    made = made_up.everything()
    attempt(made, made.administrator, "verify", "accepted", 1)
    reverse()
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BACKFILL)
    assert (
        Member.objects.of(made.organization).get(pk=made.administrator.pk).last_login_at
        is None
    )
