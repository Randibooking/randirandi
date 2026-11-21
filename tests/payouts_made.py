# SPDX-License-Identifier: AGPL-3.0-only
"""Made-up ledgers and payouts for the tests : no report is read."""

from datetime import UTC, date, datetime
from typing import Any

from randirandi.core.models import PaymentLedger, PaymentProviderAccount, Payout
from tests import made_up


def account(
    made: made_up.MadeUp, *, test_mode: bool = False, credentials: str = "VIPPS"
) -> PaymentProviderAccount:
    """The organization's Vipps account (the made-up one has a test account),
    made a live or a test account with merchant number 123456."""
    found, _ = PaymentProviderAccount.objects.of(made.organization).update_or_create(
        payment_provider="vipps",
        defaults={
            "organization": made.organization,
            "merchant_number": "123456",
            "credentials": credentials,
            "test_mode": test_mode,
        },
    )
    return found


def ledger(
    made: made_up.MadeUp, *, ledger_id: str = "9001", test_mode: bool = False
) -> PaymentLedger:
    found, _ = PaymentLedger.objects.of(made.organization).get_or_create(
        ledger_id=ledger_id,
        defaults={
            "organization": made.organization,
            "account": account(made, test_mode=test_mode),
            "currency": "NOK",
        },
    )
    return found


def payout(made: made_up.MadeUp, **over: Any) -> Payout:
    """A payout whose totals add up: 300.00 captured, 100.00 refunded, 12.00
    in fees, 288.00 less the 100.00 and the fees is 188.00."""
    fields: dict[str, Any] = {
        "ledger": ledger(made),
        "psp_reference": "9001-2000023",
        "ledger_date": date(2027, 2, 20),
        "scheduled_at": datetime(2027, 2, 21, 0, 0, tzinfo=UTC),
        "currency": "NOK",
        "amount": 18800,
        "statement_text": "Vipps utbet. 2000023 Vippsnr 123456",
        "captured_total": 30000,
        "refunded_total": 10000,
        "fee_total": 1200,
        "other_total": 0,
        "captured_count": 3,
        "refunded_count": 1,
    } | over
    return Payout.objects.create(organization=made.organization, **fields)  # type: ignore[misc]
