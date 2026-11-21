# SPDX-License-Identifier: AGPL-3.0-only
"""Refunds the provider's report does not show .

A refund we marked completed should be in the daily report as a refund entry
(the read pairs the two and sets `Refund.reported_at`). One that is still not
there once the report has been read through two days past the day the refund
completed needs attention. The judge is `funds_through` of the ledger that holds
the payment's own capture, never the clock, so a read that lags does not raise
it; a payment whose capture the report has not shown is not judged.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta

from django.db.models import OuterRef, Subquery

from randirandi.core.models import LedgerEntry, LedgerTopic, Organization, Refund

GRACE = timedelta(days=2)


def attention(
    org: Organization | int, payment_ids: Iterable[int] | None = None
) -> set[int]:
    """The ids of the payments of the organization that have a completed refund
    the report has not shown, once the payment's ledger is read far enough to
    say so."""
    through = LedgerEntry.objects.of(org).filter(
        topic=LedgerTopic.FUNDS,
        entry_type="capture",
        reference=OuterRef("payment__provider_reference"),
    )
    found = (
        Refund.objects.of(org)
        .filter(completed_at__isnull=False, reported_at__isnull=True)
        .annotate(through=Subquery(through.values("ledger__funds_through")[:1]))
        .filter(through__isnull=False)
    )
    if payment_ids is not None:
        found = found.filter(payment_id__in=list(payment_ids))
    # Read through D means every day up to D is whole: a refund completed on day
    # C is judged once D is at least C + 2.
    return {
        row["payment_id"]
        for row in found.values("payment_id", "completed_at", "through")
        if row["completed_at"] is not None
        and row["completed_at"]
        < datetime.combine(row["through"] - GRACE + timedelta(days=1), time(), UTC)
    }
