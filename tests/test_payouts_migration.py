# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0051 : the ledger, its entries and the payouts, the
fee and payout on a payment, the payout on a refund, the moments of an
account's report read, and the audit action "listed payouts". Reversing is
refused while any of them holds a record, keeps every action other parts
added, and drops it all when nothing is lost. The graph's leaf nodes are
restored after every test, refused reverse or not."""

import importlib
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    AuditEvent,
    LedgerEntry,
    Payment,
    PaymentLedger,
    PaymentProviderAccount,
    Payout,
    Refund,
)
from tests import made_up, payouts_made

pytestmark = pytest.mark.django_db(transaction=True)
THIS = importlib.import_module("randirandi.core.migrations.0051_payouts_and_fees")
NAME = "0051_payouts_and_fees"
BEFORE = [("core", "0050_payment_trouble")]
NEW = {"core_paymentledger", "core_ledgerentry", "core_payout"}


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


def still_applied() -> bool:
    """The migration is applied; it is not assumed to be the graph's leaf."""
    return ("core", NAME) in MigrationExecutor(connection).loader.applied_migrations


def tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(cursor, table)
        }


def entry(made: made_up.MadeUp) -> LedgerEntry:
    return LedgerEntry.objects.create(
        organization=made.organization,
        ledger=payouts_made.ledger(made),
        topic="funds",
        entry_type="capture",
        psp_reference="C1",
        reference="r",
        ledger_date=date(2027, 2, 20),
        at=datetime(2027, 2, 20, 12, tzinfo=UTC),
        amount=100,
        balance_before=0,
        balance_after=100,
    )


def test_the_vocabulary_gains_one_action_and_keeps_the_others() -> None:
    earlier = {a for a, _ in THIS.OLD_ACTIONS}
    assert earlier == {
        a
        for a, _ in importlib.import_module(
            "randirandi.core.migrations.0049_organization_audit"
        ).NEW_ACTIONS
    }
    assert {a for a, _ in THIS.NEW_ACTIONS} == earlier | {"listed payouts"}
    assert "listed payouts" not in earlier
    assert {a.value for a in AuditAction} >= earlier | {"listed payouts"}


def refuses(made: made_up.MadeUp, what: str, make: Callable[[], object]) -> None:
    make()
    with pytest.raises(RuntimeError, match=what):
        reverse()
    assert still_applied() and tables() >= NEW


def test_reversing_is_refused_while_any_record_of_money_exists() -> None:
    made = made_up.everything()
    # Each in turn, cleaned away before the next, so each one alone is enough.
    refuses(made, "ledger", lambda: payouts_made.ledger(made))
    refuses(made, "ledger entry", lambda: entry(made))
    LedgerEntry.objects.of(made.organization).all().delete()
    PaymentLedger.objects.of(made.organization).all().delete()
    refuses(made, "payout", lambda: payouts_made.payout(made))
    Payout.objects.of(made.organization).all().delete()
    PaymentLedger.objects.of(made.organization).all().delete()
    refuses(
        made,
        "payment fee",
        lambda: Payment.objects.of(made.organization).update(fee_amount=0),
    )
    Payment.objects.of(made.organization).update(fee_amount=None)
    stamped = PaymentProviderAccount.objects.of(made.organization)
    refuses(
        made,
        "account report moment",
        lambda: stamped.update(report_attempted_at=datetime(2027, 1, 1, tzinfo=UTC)),
    )
    stamped.update(report_attempted_at=None)
    refuses(
        made,
        "account report moment",
        lambda: stamped.update(report_completed_at=datetime(2027, 1, 1, tzinfo=UTC)),
    )
    stamped.update(report_completed_at=None)


def test_reversing_is_refused_while_a_payment_or_a_refund_is_in_a_payout() -> None:
    made = made_up.everything()
    payout = payouts_made.payout(made)
    Payment.objects.of(made.organization).update(payout=payout)
    with pytest.raises(RuntimeError, match="payment payout link"):
        reverse()
    Payment.objects.of(made.organization).update(payout=None)
    Refund.objects.of(made.organization).update(payout=payout)
    with pytest.raises(RuntimeError, match="refund payout link"):
        reverse()
    assert still_applied()
    # Nothing was dropped by the refusals.
    assert Payout.objects.of(made.organization).count() == 1
    assert Refund.objects.of(made.organization).get().payout_id == payout.pk


def test_reversing_is_refused_while_an_event_of_the_new_vocabulary_exists() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator,
        action=AuditAction.LISTED_PAYOUTS,
        target=made.organization,
    )
    with pytest.raises(RuntimeError, match="payout audit event"):
        reverse()
    assert still_applied()


def test_a_clean_reverse_drops_it_all_and_the_way_forward_is_whole() -> None:
    made = made_up.everything()
    record_event(
        actor=made.administrator, action=AuditAction.ADD_MEMBER, target=made.member
    )
    assert tables() >= NEW
    reverse()
    assert not still_applied()
    assert not NEW & tables()
    assert not {"fee_amount", "payout_id"} & columns("core_payment")
    assert "payout_id" not in columns("core_refund")
    assert not {"report_attempted_at", "report_completed_at"} & columns(
        "core_paymentprovideraccount"
    )
    with pytest.raises(Exception, match="audit_fixed_action"):
        record_event(
            actor=made.administrator,
            action=AuditAction.LISTED_PAYOUTS,
            target=made.organization,
        )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert still_applied()
    assert tables() >= NEW
    assert AuditEvent.objects.of(made.organization).filter(action="added member")
    assert not Payout.objects.of(made.organization).exists()
