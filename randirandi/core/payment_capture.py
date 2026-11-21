# SPDX-License-Identifier: AGPL-3.0-only
"""Persist capture before calling; confirm the complete order after a fresh read."""

import uuid
from datetime import datetime

from randirandi.core import payment_trouble
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    BookingState,
    Order,
    Organization,
    Payment,
    TakenTime,
    TroubleStep,
)
from randirandi.core.payments import PaymentUnavailableError, _eligible, client_for
from randirandi.vipps import PaymentStatus, VippsResponseError


def _held(org: Organization, payment: Payment, now: datetime | None) -> Order | None:
    order = Order.objects.of(org).get(pk=payment.order_id)
    if not order.total or order.total != payment.amount:
        return None
    try:
        order = _eligible(org, order.member_id, order.pk, now)
    except PaymentUnavailableError:
        return None
    if order.bookings.filter(taken_time__starts_at__lte=utc_now(now)).exists():
        return None
    return order


def _intent(
    org: Organization, payment_id: int, now: datetime | None
) -> uuid.UUID | None:
    payment = Payment.objects.of(org).get(pk=payment_id)
    if _held(org, payment, now) is None:
        return None
    if payment.capture_key is None:
        payment.capture_key = uuid.uuid4()
        payment.save(update_fields=["capture_key"])
    return payment.capture_key


def advance(
    payment: Payment, status: PaymentStatus, *, now: datetime | None = None
) -> PaymentStatus:
    with payment_trouble.step(TroubleStep.CAPTURE):
        return _advance(payment, status, now=now)


def _advance(
    payment: Payment, status: PaymentStatus, *, now: datetime | None = None
) -> PaymentStatus:
    """Called under the order lock, after applying the initial provider read.

    Only an untouched full authorization can initiate capture. Never try to
    fill an unexplained partial capture or cancelled authorization. Expiry
    recovery handles money on orders that can no longer be confirmed.
    """
    if (
        status.state != "AUTHORIZED"
        or status.authorized != payment.amount
        or status.captured
        or status.cancelled
        or status.refunded
    ):
        return status
    key = organization_transaction(
        payment.organization_id, lambda org: _intent(org, payment.pk, now)
    )
    if key is None:
        return status
    provider = client_for(payment)
    provider.capture(
        payment.provider_reference, payment.amount, payment.currency, key=str(key)
    )
    # A mutation response is not the confirmation authority. Read again even
    # when it reports capture; uncertain/stale reads leave reconciliation pending.
    status = provider.status(payment.provider_reference, payment.currency)
    if status.captured != payment.amount:
        raise VippsResponseError()
    return status


def confirm(
    org: Organization, payment_id: int, status: PaymentStatus, now: datetime | None
) -> None:
    """Apply together with the validated provider snapshot in one transaction."""
    payment = Payment.objects.of(org).get(pk=payment_id)
    if (
        status.state != "AUTHORIZED"
        or status.authorized != payment.amount
        or status.captured != payment.amount
        or status.cancelled
        or status.refunded
    ):
        return
    order = _held(org, payment, now)
    if order is None:
        return
    TakenTime.objects.of(org).filter(
        booking__order=order, state=BookingState.HELD
    ).update(state=BookingState.CONFIRMED)
    # Confirmed: its confirmation email is due .
    Order.objects.of(org).filter(pk=order.pk).update(
        hold_expires_at=None, confirmation_due_at=utc_now(now)
    )
