# SPDX-License-Identifier: AGPL-3.0-only
"""Durable checkout intents; provider calls run outside database transactions."""

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection

from randirandi.core import payment_accounts, payment_receipt
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    BookingLine,
    BookingState,
    Member,
    Order,
    Organization,
    Payment,
    PaymentProvider,
    PaymentState,
)
from randirandi.payment_configuration import Secrets
from randirandi.vipps import Client, CreatedPayment, VippsError, vipps_phone

logger = logging.getLogger(__name__)
# What a create intent made before the description was saved has always sent.
LEGACY_DESCRIPTION = "RandiRandi booking"
# What a create intent holds only until it ends: the phone, and the description
# and receipt that a retry must repeat.
INTENT_ENDED = {"create_phone": "", "create_description": "", "create_receipt": ""}
# The states in which the Vipps app still shows the payment.
LINKABLE = (PaymentState.CREATED, PaymentState.AUTHORIZED, PaymentState.CAPTURED)


class PaymentUnavailableError(ValueError):
    """This member's order is not eligible for checkout."""


class PaymentBusyError(ValueError):
    """Another worker owns this order; a later attempt can recover it."""


@dataclass(frozen=True, repr=False)
class Checkout:
    payment_id: int
    redirect_url: str


@contextmanager
def order_lock(order_id: int) -> Iterator[None]:
    """Negative bigint advisory keys are reserved for payment order locks.

    A session lock covers the network call while each database write has a
    short, independently committed transaction. A dead worker loses the lock;
    the next worker uses the intent already committed before that call.
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Payment operations require their own transaction.")
    if type(order_id) is not int or not 0 < order_id < 2**63:
        raise ValueError("A saved order is required.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [-order_id])
        if not cursor.fetchone()[0]:
            raise PaymentBusyError("This order is already being processed.")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [-order_id])


def client_for(payment: Payment) -> Client:
    """A saved intent keeps its merchant and environment after account edits."""
    if payment.create_key is None:
        raise PaymentUnavailableError("This historical payment has no request intent.")
    secrets: Secrets = settings.VIPPS_SECRETS
    return secrets.client(
        prefix=payment.credential_name,
        merchant_number=payment.merchant_number,
        test_mode=payment.test_mode,
    )


# A refusal by Vipps that repeating the same request cannot cure. 401 is a
# credential problem that can be fixed, 408 and 429 are transient and 409 means
# the same key is still in flight; those keep the intent, and its phone.
RETRYABLE_REFUSALS = {401, 408, 409, 429}


def create_at_provider(provider: Client, payment: Payment) -> CreatedPayment:
    """Send the saved create intent. Its copies go when the intent ends.

    A refusal that no retry can cure ends the intent, so the phone, description
    and receipt are emptied then. Any other failure keeps them for the
    identical retry.
    """
    try:
        return provider.create(
            payment.provider_reference,
            payment.amount,
            payment.currency,
            return_url=payment.return_url,
            key=str(payment.create_key),
            description=payment.create_description or LEGACY_DESCRIPTION,
            receipt=json.loads(payment.create_receipt)
            if payment.create_receipt
            else None,
            phone=payment.create_phone or None,
        )
    except VippsError as error:
        if (
            type(error) is VippsError
            and 400 <= error.status < 500
            and error.status not in RETRYABLE_REFUSALS
        ):
            organization_transaction(
                payment.organization_id,
                lambda org: (
                    Payment.objects.of(org).filter(pk=payment.pk).update(**INTENT_ENDED)
                ),
            )
        raise


def send_details(payment: Payment, now: datetime | None = None) -> None:
    """Link the payment in the Vipps app to the organization's site.

    The site is the saved return address's scheme and host. This never raises
    and never changes the payment but for the moment Vipps accepted the link:
    a failure leaves it unsent, and reconciliation tries again while the
    payment is still shown.
    """
    current = Payment.objects.of(payment.organization_id).get(pk=payment.pk)
    if (
        current.create_key is None
        or current.create_completed_at is None
        or current.details_sent_at is not None
        or current.state not in LINKABLE
    ):
        return
    site = urlsplit(current.return_url)
    try:
        client_for(current).send_details(
            current.provider_reference, f"{site.scheme}://{site.netloc}/"
        )
    except VippsError, ValueError:
        # Neither the provider's reply nor the address goes in the log.
        logger.error(
            "payment details failed: organization=%d payment=%d",
            current.organization_id,
            current.pk,
        )
        return
    organization_transaction(
        current.organization_id,
        lambda org: (
            Payment.objects.of(org)
            .filter(pk=current.pk, details_sent_at__isnull=True)
            .update(details_sent_at=utc_now(now))
        ),
    )


def _eligible(
    organization: Organization,
    member_id: int,
    order_id: int,
    now: datetime | None,
) -> Order:
    member = Member.objects.of(organization).filter(pk=member_id).first()
    order = (
        Order.objects.of(organization).filter(pk=order_id, member_id=member_id).first()
    )
    if member is None or not member.is_active or order is None:
        raise PaymentUnavailableError("The order is unavailable.")
    bookings = list(
        order.bookings.select_related("taken_time").prefetch_related("lines")
    )
    if (
        not bookings
        or any(row.member_id != member_id for row in bookings)
        or sum(line.total for row in bookings for line in row.lines.all())
        != order.total
    ):
        raise PaymentUnavailableError("The saved order is inconsistent.")
    if order.total == 0:
        if order.hold_expires_at is not None or any(
            row.taken_time.state != BookingState.CONFIRMED for row in bookings
        ):
            raise PaymentUnavailableError("The free order is not confirmed.")
    elif (
        order.hold_expires_at is None
        or order.hold_expires_at <= utc_now(now)
        or any(row.taken_time.state != BookingState.HELD for row in bookings)
    ):
        raise PaymentUnavailableError("The payment hold is no longer available.")
    return order


def _intent(
    organization: Organization,
    member_id: int,
    order_id: int,
    now: datetime | None,
) -> Payment | None:
    order = _eligible(organization, member_id, order_id, now)
    payment = Payment.objects.of(organization).filter(order=order).first()
    if not order.total:
        if payment is not None:
            raise PaymentUnavailableError("A free order cannot have a payment.")
        return None
    if payment is not None:
        if (
            payment.create_key is None
            or payment.amount != order.total
            or payment.payment_provider != PaymentProvider.VIPPS
            or payment.state != PaymentState.CREATED
        ):
            raise PaymentUnavailableError("This payment cannot start checkout.")
        return payment
    # An order is one payment in one currency: its price books' (7.3).
    currencies = set(
        BookingLine.objects.of(organization)
        .filter(booking__order=order)
        .values_list("price_book__currency", flat=True)
    )
    if len(currencies) != 1:
        raise PaymentUnavailableError("The saved order is inconsistent.")
    account = payment_accounts.account_for(organization.pk)
    phone = (
        Member.objects.of(organization)
        .filter(pk=member_id)
        .values_list("phone", flat=True)
        .get()
    )
    description, receipt = payment_receipt.build(organization, order)
    return Payment.objects.create(
        organization=organization,
        order=order,
        payment_provider=PaymentProvider.VIPPS,
        provider_reference=uuid.uuid4().hex,
        amount=order.total,
        state=PaymentState.CREATED,
        create_key=uuid.uuid4(),
        credential_name=account.credentials,
        merchant_number=account.merchant_number,
        test_mode=account.test_mode,
        currency=currencies.pop(),
        create_phone=vipps_phone(phone),
        create_description=description,
        create_receipt=receipt,
        return_url=(
            f"https://{organization.subdomain}.{settings.ORGANIZATION_DOMAIN}"
            f"/payments/return/?order={order.public_id}"
        ),
    )


def checkout(
    *, member: Member, order_public_id: uuid.UUID, now: datetime | None = None
) -> Checkout | None:
    """Return one provider redirect, or None for an already-confirmed free order.

    No amount, merchant, destination URL or phone comes from the caller. The
    phone comes from the member's saved record on the server and is frozen in
    the committed intent with the create key; a provider failure leaves that
    intent for an identical retry, even if the member's phone changed since.
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Payment operations require their own transaction.")
    if not isinstance(order_public_id, uuid.UUID) or not member.pk:
        raise PaymentUnavailableError("A saved member and order are required.")
    if now is not None:
        now = utc_now(now)
    order = (
        Order.objects.of(member.organization_id)
        .filter(public_id=order_public_id, member_id=member.pk)
        .first()
    )
    if order is None:
        raise PaymentUnavailableError("The order is unavailable.")
    with order_lock(order.pk):
        payment = organization_transaction(
            member.organization_id,
            lambda org: _intent(org, member.pk, order.pk, now),
        )
        if payment is None:
            return None
        provider = client_for(payment)
        if not payment.redirect_url:
            created = create_at_provider(provider, payment)

            def save_result(org: Organization) -> None:
                Payment.objects.of(org).filter(pk=payment.pk).update(
                    redirect_url=created.redirect_url,
                    create_completed_at=utc_now(now),
                    **INTENT_ENDED,
                )

            organization_transaction(member.organization_id, save_result)
            payment.redirect_url = created.redirect_url
            payment.create_phone = ""
            payment.create_description = payment.create_receipt = ""
        # Network latency or a concurrent revocation must not extend access.
        organization_transaction(
            member.organization_id,
            lambda org: _eligible(org, member.pk, order.pk, now),
        )
        send_details(payment, now)
        return Checkout(payment.pk, payment.redirect_url)
