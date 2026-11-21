# SPDX-License-Identifier: AGPL-3.0-only
"""Release local capacity independently; recover every saved provider intent."""

import uuid
from datetime import datetime

from randirandi.core import payment_capture, payment_refunds, payment_trouble
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    BookingState,
    Order,
    Organization,
    Payment,
    TakenTime,
    TroubleStep,
)
from randirandi.core.payments import (
    INTENT_ENDED,
    PaymentBusyError,
    client_for,
    create_at_provider,
    order_lock,
)
from randirandi.vipps import PaymentStatus, VippsError, VippsResponseError


def _fulfilled(org: Organization, payment: Payment) -> bool:
    order = Order.objects.of(org).get(pk=payment.order_id)
    return (
        order.hold_expires_at is None
        and order.bookings.exists()
        and not order.bookings.exclude(
            taken_time__state__in=[BookingState.CONFIRMED, BookingState.CANCELLED]
        ).exists()
    )


def release(org: Organization, order_id: int) -> None:
    """Free what the order still holds ."""
    TakenTime.objects.of(org).filter(
        booking__order_id=order_id, state=BookingState.HELD
    ).update(state=BookingState.EXPIRED)


def release_ineligible(
    org: Organization, payment: Payment, now: datetime | None
) -> None:
    if (
        not _fulfilled(org, payment)
        and payment_capture._held(org, payment, now) is None
    ):
        release(org, payment.order_id)


def expire_holds(*, now: datetime | None = None) -> None:
    """No provider dependency, including an order whose checkout never began."""
    moment = utc_now(now)
    for org in Organization.objects.order_by("pk").iterator():
        orders = (
            Order.objects.of(org)
            .filter(
                hold_expires_at__lte=moment,
                bookings__taken_time__state=BookingState.HELD,
            )
            .order_by("pk")
            .values_list("pk", flat=True)
            .distinct()
        )
        for order_id in orders.iterator():
            try:
                with order_lock(order_id):

                    def expire(
                        organization: Organization, order_id: int = order_id
                    ) -> None:
                        if (
                            Order.objects.of(organization)
                            .filter(pk=order_id, hold_expires_at__lte=utc_now(now))
                            .exists()
                        ):
                            release(organization, order_id)

                    organization_transaction(org.pk, expire)
            except PaymentBusyError:
                continue


def read(payment: Payment, *, now: datetime | None = None) -> PaymentStatus:
    with payment_trouble.step(TroubleStep.READ):
        return _read(payment, now=now)


def _read(payment: Payment, *, now: datetime | None = None) -> PaymentStatus:
    provider = client_for(payment)
    try:
        return provider.status(payment.provider_reference, payment.currency)
    except VippsError as error:
        if error.status != 404 or payment.create_completed_at is not None:
            raise
    # An accepted checkout may have died before create reached the provider.
    # Resolve the original intent, even if it now needs immediate cancellation.
    created = create_at_provider(provider, payment)
    organization_transaction(
        payment.organization_id,
        lambda org: (
            Payment.objects.of(org)
            .filter(pk=payment.pk)
            .update(
                create_completed_at=utc_now(now),
                redirect_url=created.redirect_url,
                **INTENT_ENDED,
            )
        ),
    )
    return provider.status(payment.provider_reference, payment.currency)


def advance(
    payment: Payment, status: PaymentStatus, *, now: datetime | None = None
) -> PaymentStatus:
    """Under the order lock, compensate an order that cannot be fulfilled."""

    def needs_cleanup(org: Organization) -> bool:
        if _fulfilled(org, payment):
            return False
        held = payment_capture._held(org, payment, now) is not None
        untouched = not (status.captured or status.cancelled or status.refunded)
        payable = untouched and (
            (status.state == "CREATED" and not status.authorized)
            or (status.state == "AUTHORIZED" and status.authorized == payment.amount)
        )
        if held and payable:
            return False
        release(org, payment.order_id)
        return True

    if not organization_transaction(payment.organization_id, needs_cleanup):
        return status
    refund = payment_refunds.automatic(payment, now=now)
    if refund is not None:
        status = payment_refunds.execute(payment, refund, status, now=now)
    with payment_trouble.step(TroubleStep.CANCEL):
        return _cancel(payment, status)


def _cancel(payment: Payment, status: PaymentStatus) -> PaymentStatus:
    if status.state == "AUTHORIZED" and not status.authorized:
        raise VippsResponseError()
    if status.state == "CREATED" or status.open_authorization > 0:

        def cancel_intent(org: Organization) -> uuid.UUID:
            current = Payment.objects.of(org).get(pk=payment.pk)
            if current.cancel_key is None:
                current.cancel_key = uuid.uuid4()
                current.save(update_fields=["cancel_key"])
            return current.cancel_key

        key = organization_transaction(payment.organization_id, cancel_intent)
        provider = client_for(payment)
        provider.cancel(payment.provider_reference, payment.currency, key=str(key))
        status = provider.status(payment.provider_reference, payment.currency)
        if status.state == "CREATED" or status.open_authorization > 0:
            raise VippsResponseError()
    return status


def mark_settled(
    org: Organization, payment_id: int, status: PaymentStatus, now: datetime | None
) -> None:
    payment = Payment.objects.of(org).get(pk=payment_id)
    covered = status.open_authorization == 0
    if _fulfilled(org, payment):
        settled = covered and status.captured == payment.amount
    else:
        terminal = status.state in {"ABORTED", "EXPIRED", "TERMINATED"} or (
            status.state == "AUTHORIZED" and status.authorized > 0
        )
        settled = terminal and covered and status.captured == status.refunded
    settled = settled and payment_refunds.pending(payment) is None
    Payment.objects.of(org).filter(pk=payment_id).update(
        settled_at=(payment.settled_at or utc_now(now)) if settled else None
    )
