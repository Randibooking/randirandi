# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0050 (a payment's spells of trouble with the provider check): one
table of fixed words, one open spell for each payment, no column on the
payment. Reversing is refused while a spell is held, and the graph's current
leaf nodes are put back after the refusal."""

from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import Payment, PaymentTrouble
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
NAME = "0050_payment_trouble"
BEFORE = [("core", "0049_organization_audit")]
TABLE = "core_paymenttrouble"
BEGAN = "2027-02-27T08:00:00Z"


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
    return set(connection.introspection.table_names())


def spell(made: made_up.MadeUp, **fields: Any) -> PaymentTrouble:
    held: dict[str, Any] = {
        "organization": made.organization,
        "payment": made.payment,
        "since": BEGAN,
        "step": "read",
        "kind": "unanswered",
        "first_step": "read",
        "first_kind": "unanswered",
        "tries": 1,
    }
    return PaymentTrouble.objects.create(**(held | fields))  # type: ignore[misc]


def test_it_follows_the_organization_audit_migration() -> None:
    this = import_module(f"randirandi.core.migrations.{NAME}")
    assert this.Migration.dependencies == [("core", "0049_organization_audit")]


def test_the_migration_is_applied_whatever_the_leaf() -> None:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    assert NAME not in [m.name for m, _ in plan]
    assert ("core", NAME) in executor.loader.applied_migrations
    assert TABLE in tables()


def test_a_payment_starts_with_no_spell_and_no_column() -> None:
    made = made_up.everything()
    assert not PaymentTrouble.objects.of(made.organization).exists()
    with connection.cursor() as cursor:
        described = connection.introspection.get_table_description(
            cursor, "core_payment"
        )
    assert not [c.name for c in described if c.name.startswith("trouble")]


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"step": "oops"}, "payment_trouble_is_fixed_words"),
        ({"kind": "a text"}, "payment_trouble_is_fixed_words"),
        ({"first_step": "oops"}, "payment_trouble_is_fixed_words"),
        ({"first_kind": "a text"}, "payment_trouble_is_fixed_words"),
        ({"tries": 0}, "payment_trouble_is_fixed_words"),
        ({"ended_at": "2027-02-27T07:59:59Z"}, "payment_trouble_ends_after_it_began"),
    ],
)
def test_a_spell_holds_fixed_words_and_ends_after_it_began(
    fields: dict[str, Any], constraint: str
) -> None:
    made = made_up.everything()
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        spell(made, **fields)


def test_a_payment_has_one_open_spell_and_any_number_of_ended_ones() -> None:
    made = made_up.everything()
    spell(made, ended_at="2027-02-27T08:05:00Z")
    spell(made, since="2027-02-27T09:00:00Z", ended_at="2027-02-27T09:05:00Z")
    spell(made, since="2027-02-27T10:00:00Z")
    with (
        pytest.raises(IntegrityError, match="payment_trouble_one_open"),
        transaction.atomic(),
    ):
        spell(made, since="2027-02-27T11:00:00Z")
    assert PaymentTrouble.objects.of(made.organization).count() == 3


def test_reversing_is_refused_while_a_spell_is_held() -> None:
    made = made_up.everything()
    spell(made, step="refund", kind="database", tries=4)
    with pytest.raises(RuntimeError, match="payment trouble spell"):
        reverse()
    # Put the graph's current leaf nodes back after the refused reversal.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    kept = PaymentTrouble.objects.of(made.organization).get()
    assert (kept.step, kept.kind, kept.tries) == ("refund", "database", 4)
    assert TABLE in tables()


def test_reversing_without_a_spell_drops_the_table_and_forward_restores_it() -> None:
    made = made_up.everything()
    reverse()
    assert TABLE not in tables()
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert TABLE in tables()
    assert Payment.objects.of(made.organization).filter(pk=made.payment.pk).exists()
