# SPDX-License-Identifier: AGPL-3.0-only
"""The Payouts as the admin pages read them : what Vipps paid to
the organization's bank, by day, and one payout with the payments and refunds
in it. Every answer is read only, from the records: the amounts are the
report's, never worked out from a rate. The list carries amounts, days and
counts, no member; a payout's page names who booked each payment.
"""

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from randirandi.core import audit_log
from randirandi.core.booking_admin import _paging, _stamp
from randirandi.core.models import Organization, Payment, Payout, Refund

PER_PAGE = audit_log.PER_PAGE


@dataclass(frozen=True)
class PayoutsQuery:
    """`first` and `last` are the days paid out, both ends included."""

    first: date | None = None
    last: date | None = None
    newest_first: bool = True
    page: int = 1


def _row(payout: Payout) -> dict[str, object]:
    return {
        "id": payout.pk,
        "day": payout.ledger_date.isoformat(),
        "payments": payout.captured_count,
        "refunds": payout.refunded_count,
        "captured": payout.captured_total,
        "refunded": payout.refunded_total,
        "fees": payout.fee_total,
        "other": payout.other_total,
        "amount": payout.amount,
        "currency": payout.currency,
        "reference": payout.psp_reference,
    }


def payouts(org: Organization, query: PayoutsQuery) -> dict[str, object]:
    """One page of the payouts, newest first or oldest."""
    found = Payout.objects.of(org).filter(aborted_at__isnull=True)
    if query.first is not None:
        found = found.filter(ledger_date__gte=query.first)
    if query.last is not None:
        found = found.filter(ledger_date__lte=query.last)
    key = ("ledger_date", "pk")
    order = tuple(f"-{name}" for name in key) if query.newest_first else key
    start = (query.page - 1) * PER_PAGE
    return {
        **_paging(found.count(), query.page),
        "payouts": [
            _row(row) for row in found.order_by(*order)[start : start + PER_PAGE]
        ],
    }


def payout(
    org: Organization, found: Payout, *, payments_page: int = 1, refunds_page: int = 1
) -> dict[str, object]:
    """One payout's page: its totals, and the payments and refunds of it that
    are still on record, 25 of each at a time (booking retention erases old
    ones; the totals and counts stay)."""
    zone = ZoneInfo(org.time_zone)
    payments = Payment.objects.of(org).filter(payout=found)
    refunds = Refund.objects.of(org).filter(payout=found)
    paid = payments.annotate(who=audit_log.member_shown("order__member__"))
    start = (payments_page - 1) * PER_PAGE
    taken = (refunds_page - 1) * PER_PAGE
    by = refunds.annotate(
        who=audit_log.member_shown("payment__order__member__"),
        by_name=audit_log.member_shown("made_by__"),
    )
    return {
        **_row(found),
        "statement": found.statement_text,
        "scheduled": _stamp(found.scheduled_at, zone),
        "payments_on_record": _paging(payments.count(), payments_page),
        "refunds_on_record": _paging(refunds.count(), refunds_page),
        "payment_rows": [
            {
                "number": row["order__number"],
                "order_reference": row["order__reference"],
                "member": row["who"],
                "captured_at": None
                if row["captured_at"] is None
                else _stamp(row["captured_at"], zone),
                "captured": row["captured_amount"],
                "fee": row["fee_amount"],
                "reference": row["provider_reference"],
            }
            for row in paid.order_by("captured_at", "pk").values(
                "order__number",
                "order__reference",
                "who",
                "captured_at",
                "captured_amount",
                "fee_amount",
                "provider_reference",
            )[start : start + PER_PAGE]
        ],
        "refund_rows": [
            {
                "number": row["payment__order__number"],
                "order_reference": row["payment__order__reference"],
                "member": row["who"],
                "requested": _stamp(row["made_at"], zone),
                "amount": row["amount"],
                "by": None if row["automatic"] else row["by_name"],
                "reference": row["provider_reference"],
            }
            for row in by.order_by("made_at", "pk").values(
                "payment__order__number",
                "payment__order__reference",
                "who",
                "made_at",
                "amount",
                "automatic",
                "by_name",
                "provider_reference",
            )[taken : taken + PER_PAGE]
        ],
    }
