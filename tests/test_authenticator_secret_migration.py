# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0044 : plain authenticator secrets are encrypted
forward under the environment's key; reversing is refused while a row exists.
The graph's leaf nodes are restored after each test, a refused reversal included."""

import re
from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from randirandi.core import authenticator_secret
from randirandi.core.models import Authenticator
from tests import made_up

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.keeps_authenticators,
]
BEFORE = [("core", "0043_session_limits")]
PLAIN = "1f" * 20


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def reverse() -> MigrationExecutor:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BEFORE)
    return executor


def plain_row(made: made_up.MadeUp) -> Any:
    """Reverse to before 0044 and keep one plain row, through the old model."""
    Authenticator._base_manager.all().delete()
    executor = reverse()
    old = executor.loader.project_state(BEFORE).apps.get_model("core", "Authenticator")
    return old._base_manager.create(
        organization_id=made.organization.pk, member_id=made.member.pk, secret=PLAIN
    )


def forward() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def raw(pk: int) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT secret FROM core_authenticator WHERE id = %s", [pk])
        return str(cursor.fetchone()[0])


def test_forward_encrypts_an_existing_plain_row_and_it_opens() -> None:
    made = made_up.everything()
    row = plain_row(made)
    assert raw(row.pk) == PLAIN
    forward()
    stored = raw(row.pk)
    assert re.fullmatch(r"v1:[A-Za-z0-9_-]{64}", stored) and PLAIN not in stored
    assert Authenticator._base_manager.get(pk=row.pk).plain_secret() == PLAIN


def test_forward_without_the_key_is_refused_and_changes_nothing() -> None:
    made = made_up.everything()
    row = plain_row(made)
    with (
        override_settings(AUTHENTICATOR_KEY=""),
        pytest.raises(RuntimeError, match="RANDIRANDI_AUTHENTICATOR_KEY is not set"),
    ):
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
    assert raw(row.pk) == PLAIN
    assert MigrationExecutor(connection).migration_plan(
        MigrationExecutor(connection).loader.graph.leaf_nodes()
    )


def test_reversing_is_refused_while_a_row_exists_and_the_row_survives() -> None:
    made = made_up.everything()
    row = plain_row(made)
    forward()
    sealed = raw(row.pk)
    with pytest.raises(RuntimeError, match="Reversing 0044 is refused"):
        reverse()
    assert raw(row.pk) == sealed
    assert Authenticator._base_manager.get(pk=row.pk).plain_secret() == PLAIN
    # The refusal leaves 0044 applied; migrations after it may have been
    # reversed on the way down and are not this test's concern.
    plan = MigrationExecutor(connection).migration_plan(
        MigrationExecutor(connection).loader.graph.leaf_nodes()
    )
    assert not [m for m, _ in plan if m.name == "0044_authenticator_secret_encrypted"]


def test_reversing_goes_through_without_rows_and_forward_again_works() -> None:
    made = made_up.everything()
    Authenticator._base_manager.all().delete()
    reverse()
    forward()
    device = Authenticator._base_manager.create(
        organization=made.organization,
        member=made.member,
        secret=authenticator_secret.seal(
            PLAIN,
            key=made_up_key(),
            organization_id=made.organization.pk,
            member_id=made.member.pk,
        ),
    )
    assert device.plain_secret() == PLAIN


def made_up_key() -> str:
    from django.conf import settings

    return str(settings.AUTHENTICATOR_KEY)
