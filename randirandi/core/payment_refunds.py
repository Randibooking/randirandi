# SPDX-License-Identifier: AGPL-3.0-only
"""Durable refund intents and provider-event attribution; no invented actor."""

import logging
import uuid
from datetime import datetime

from randirandi.core import payment_trouble
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    Organization,
    Payment,
    PaymentState,
    Refund,
    TroubleStep,
)
from randirandi.core.payments import PaymentBusyError, client_for, order_lock
from randirandi.vipps import PaymentEvent, PaymentStatus, VippsResponseError

logger = logging.getLogger(__name__)


def pending(payment: Payment) -> Refund | None:
    return (
        Refund.objects.of(payment.organization_id)
        .filter(payment=payment, refund_key__isnull=False, completed_at__isnull=True)
        .first()
    )


def automatic(payment: Payment, *, now: datetime | None = None) -> Refund | None:
    """Called under the order lock after the provider snapshot was persisted."""

    def create(org: Organization) -> Refund | None:
        current = Payment.objects.of(org).get(pk=payment.pk)
        existing = pending(current)
        if existing is not None:
            return existing
        amount = current.captured_amount - current.refunded_amount
        if not amount:
            return None
        row = Refund.objects.create(
            organization=org,
            payment=current,
            amount=amount,
            automatic=True,
            refund_key=uuid.uuid4(),
            refunded_before=current.refunded_amount,
            made_at=utc_now(now),
        )
        Payment.objects.of(org).filter(pk=current.pk).update(settled_at=None)
        return row

    return organization_transaction(payment.organization_id, create)


def _event(events: tuple[PaymentEvent, ...], refund: Refund) -> PaymentEvent | None:
    matching = [
        row
        for row in events
        if row.idempotency_key == str(refund.refund_key)
        and row.name == "REFUNDED"
        and row.success
    ]
    if len(matching) > 1 or (matching and matching[0].amount != refund.amount):
        raise VippsResponseError()
    return matching[0] if matching else None


def execute(
    payment: Payment,
    refund: Refund,
    status: PaymentStatus,
    *,
    now: datetime | None = None,
) -> PaymentStatus:
    with payment_trouble.step(TroubleStep.REFUND):
        return _execute(payment, refund, status, now=now)


def _execute(
    payment: Payment,
    refund: Refund,
    status: PaymentStatus,
    *,
    now: datetime | None = None,
) -> PaymentStatus:
    """Resolve the saved operation, never infer its identity from aggregates alone.

    A changed aggregate without a matching event is uncertain. Keep the intent
    instead of guessing that an unrelated refund completed this operation.
    """
    if (
        refund.organization_id != payment.organization_id
        or refund.payment_id != payment.pk
        or refund.refund_key is None
        or refund.completed_at is not None
    ):
        raise ValueError("An unfinished refund for this payment is required.")
    provider = client_for(payment)
    event = _event(
        provider.events(payment.provider_reference, payment.currency), refund
    )
    if event is None:
        if (
            status.refunded != refund.refunded_before
            or status.captured - status.refunded < refund.amount
        ):
            raise VippsResponseError()
        provider.refund(
            payment.provider_reference,
            refund.amount,
            payment.currency,
            key=str(refund.refund_key),
        )
        event = _event(
            provider.events(payment.provider_reference, payment.currency), refund
        )
    status = provider.status(payment.provider_reference, payment.currency)
    if (
        event is None
        or event.reference != payment.provider_reference
        or status.reference != payment.provider_reference
        or status.amount != payment.amount
        or status.refunded < refund.refunded_before + refund.amount
    ):
        raise VippsResponseError()
    reference = event.provider_reference

    def complete(org: Organization) -> None:
        current = Payment.objects.of(org).get(pk=payment.pk)
        if (
            status.captured < current.captured_amount
            or status.refunded < current.refunded_amount
        ):
            raise VippsResponseError()
        Refund.objects.of(org).filter(pk=refund.pk, completed_at__isnull=True).update(
            provider_reference=reference, completed_at=utc_now(now)
        )
        Payment.objects.of(org).filter(pk=payment.pk).update(
            captured_amount=status.captured,
            **(
                {"captured_at": utc_now(now)}
                if status.captured and not current.captured_amount
                else {}
            ),
            refunded_amount=status.refunded,
            state=(
                PaymentState.REFUNDED
                if status.refunded == status.captured
                else PaymentState.CAPTURED
            ),
            redirect_url="",
        )

    organization_transaction(payment.organization_id, complete)
    return status


def alert_completed(*, now: datetime | None = None) -> None:
    """Durable at-least-once log delivery; the alert rule is a deployment concern."""
    for org in Organization.objects.order_by("pk").iterator():
        rows = (
            Refund.objects.of(org)
            .filter(
                refund_key__isnull=False,
                completed_at__isnull=False,
                alerted_at__isnull=True,
            )
            .select_related("payment__order")
            .order_by("made_at", "pk")
        )
        delivered = 0
        for row in rows.iterator():
            try:
                with order_lock(row.payment.order_id):
                    row.refresh_from_db()
                    if row.alerted_at is not None:
                        continue
                    logger.error(
                        "payment refund completed: organization=org-%d "
                        "refund=refund-%d payment=payment-%d automatic=%s "
                        "name=%s reference=%s link=%s/admin/bookings/%d/",
                        org.pk,
                        row.pk,
                        row.payment_id,
                        row.automatic,
                        org.subdomain.capitalize(),
                        row.payment.order.reference,
                        payment_trouble.site(org),
                        row.payment.order.number,
                        extra={"require_write": True},
                    )

                    def mark(
                        organization: Organization, refund_id: int = row.pk
                    ) -> None:
                        Refund.objects.of(organization).filter(
                            pk=refund_id, alerted_at__isnull=True
                        ).update(alerted_at=utc_now(now))

                    organization_transaction(org.pk, mark)
            except PaymentBusyError:
                continue
            delivered += 1
            if delivered == 50:
                break
