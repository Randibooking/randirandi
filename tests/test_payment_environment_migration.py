# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy sandbox payments keep their mode; production rows prevent rollback."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import Payment
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0055_closure_vacation_reason")]
AFTER = ("core", "0056_payment_environment")


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_legacy_payments_are_backfilled_as_sandbox_and_can_reverse() -> None:
    made = made_up.everything()
    MigrationExecutor(connection).migrate(BEFORE)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert (
        Payment.objects.of(made.organization).get(pk=made.payment.pk).test_mode is True
    )
    MigrationExecutor(connection).migrate(BEFORE)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT amount FROM core_payment WHERE id = %s", [made.payment.pk]
        )
        assert cursor.fetchone() == (made.payment.amount,)


def test_reversing_with_a_production_payment_refuses_and_keeps_the_row() -> None:
    made = made_up.everything()
    Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
        test_mode=False
    )
    with pytest.raises(RuntimeError, match="production payments"):
        MigrationExecutor(connection).migrate(BEFORE)
    assert (
        Payment.objects.of(made.organization).get(pk=made.payment.pk).test_mode is False
    )
    # The refusal keeps 0056; later migrations, reversed before it, are put
    # back by the fixture.
    assert AFTER in MigrationExecutor(connection).loader.applied_migrations


def test_reversal_waits_for_a_concurrent_production_insert_then_refuses() -> None:
    ready = Event()
    reverse_pid = 0

    def reverse() -> str:
        nonlocal reverse_pid
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET statement_timeout = '20s'")
                cursor.execute("SELECT pg_backend_pid()")
                reverse_pid = cursor.fetchone()[0]
            ready.set()
            try:
                MigrationExecutor(connection).migrate(BEFORE)
            except RuntimeError as error:
                return str(error)
            return "reversed"
        finally:
            connection.close()
            ready.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            made = made_up.everything()
            Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
                test_mode=False
            )
            result = pool.submit(reverse)
            assert ready.wait(timeout=15)
            # Observe the real migration waiting for this uncommitted insert.
            waiting = False
            with connection.cursor() as cursor:
                for _ in range(500):
                    cursor.execute(
                        "SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))",
                        [reverse_pid],
                    )
                    if cursor.fetchone()[0]:
                        waiting = True
                        break
                    Event().wait(0.01)
            assert waiting
        assert "production payments" in result.result(timeout=15)
    assert (
        Payment.objects.of(made.organization).get(pk=made.payment.pk).test_mode is False
    )
    # The refusal keeps 0056; later migrations, reversed before it, are put
    # back by the fixture.
    assert AFTER in MigrationExecutor(connection).loader.applied_migrations
