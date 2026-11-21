# SPDX-License-Identifier: AGPL-3.0-only
"""One provider-read path for browser returns and authenticated callbacks."""

from datetime import datetime, timedelta

from django.db.models import F, Q

from randirandi import vipps
from randirandi.core import (
    payment_capture,
    payment_recovery,
    payment_refunds,
    payment_trouble,
)
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import Organization, Payment, PaymentState, TroubleStep
from randirandi.core.payments import (
    INTENT_ENDED,
    PaymentBusyError,
    order_lock,
    send_details,
)
from randirandi.vipps import PaymentStatus, VippsResponseError

CHECK_INTERVAL = timedelta(seconds=5)


def _apply(
    org: Organization,
    payment_id: int,
    status: PaymentStatus,
    started: datetime,
    *,
    complete: bool = True,
) -> None:
    payment = Payment.objects.of(org).get(pk=payment_id)
    if (
        status.reference != payment.provider_reference
        or status.amount != payment.amount
        or status.captured < payment.captured_amount
        or status.refunded < payment.refunded_amount
    ):
        raise VippsResponseError()
    if status.captured:
        state = (
            PaymentState.REFUNDED
            if status.refunded == status.captured
            else PaymentState.CAPTURED
        )
    elif status.state == "EXPIRED":
        state = PaymentState.EXPIRED
    elif status.state in {"ABORTED", "TERMINATED"} or status.cancelled:
        state = PaymentState.CANCELLED
    elif status.state == "AUTHORIZED":
        state = PaymentState.AUTHORIZED
    else:
        state = PaymentState.CREATED
    Payment.objects.of(org).filter(pk=payment_id).update(
        state=state,
        captured_amount=status.captured,
        # The first read that shows money captured is the capture's moment.
        **(
            {"captured_at": started}
            if status.captured and not payment.captured_amount
            else {}
        ),
        refunded_amount=status.refunded,
        provider_checked_at=started if complete else payment.provider_checked_at,
        redirect_url=payment.redirect_url if state == PaymentState.CREATED else "",
        # Once the payment is no longer merely created, no create can follow.
        **({} if state == PaymentState.CREATED else INTENT_ENDED),
    )


def reconcile(
    organization_id: int,
    order_id: int,
    *,
    now: datetime | None = None,
    call_timeout: float | None = None,
) -> None:
    """Read the provider under the same order lock used by payment creation.
    `call_timeout` bounds each provider call (seconds; None: the worker's)."""
    with vipps.call_timeout(call_timeout):
        try:
            done = _reconcile(organization_id, order_id, now=now)
        except PaymentBusyError:
            raise
        except Exception as error:
            # The payment keeps its own record of the failure (payment_trouble.py).
            payment_trouble.failed(organization_id, order_id, error, now=now)
            raise
        if done:
            payment_trouble.cleared(organization_id, order_id, now=now)


def _reconcile(organization_id: int, order_id: int, *, now: datetime | None) -> bool:
    """True when a whole check went through; False when there was nothing to do."""
    with order_lock(order_id):
        payment = (
            Payment.objects.of(organization_id)
            .filter(order_id=order_id, create_key__isnull=False)
            .first()
        )
        if payment is None:
            return False
        started = utc_now(now)
        if (
            payment.provider_check_attempted_at is not None
            and payment.provider_check_attempted_at > started - CHECK_INTERVAL
        ):
            return False
        organization_transaction(
            organization_id,
            lambda org: (
                Payment.objects.of(org)
                .filter(pk=payment.pk)
                .update(provider_check_attempted_at=started)
            ),
        )
        organization_transaction(
            organization_id,
            lambda org: payment_recovery.release_ineligible(org, payment, now),
        )
        status = payment_recovery.read(payment, now=now)
        with payment_trouble.step(TroubleStep.READ):
            organization_transaction(
                organization_id,
                lambda org: _apply(org, payment.pk, status, started, complete=False),
            )
        refund = payment_refunds.pending(payment)
        if refund is not None:
            status = payment_refunds.execute(payment, refund, status, now=now)
        status = payment_capture.advance(payment, status, now=now)

        def confirm(org: Organization) -> None:
            _apply(org, payment.pk, status, started, complete=False)
            payment_capture.confirm(org, payment.pk, status, now)

        organization_transaction(organization_id, confirm)
        status = payment_recovery.advance(payment, status, now=now)

        def finish(org: Organization) -> None:
            _apply(org, payment.pk, status, started)
            payment_recovery.mark_settled(org, payment.pk, status, now)

        organization_transaction(organization_id, finish)
        # Last: a refused link in the Vipps app never holds back the money. An
        # unsent one is tried again with the next read.
        send_details(payment, now)
        return True


def reconcile_requested(*, now: datetime | None = None) -> None:
    """Drain requested work and periodically recover unsettled payment intents."""
    moment = utc_now(now)
    payment_recovery.expire_holds(now=now)
    for org in Organization.objects.order_by("pk").iterator():
        pending = (
            Payment.objects.of(org)
            .filter(create_key__isnull=False)
            .filter(
                Q(settled_at__isnull=True)
                | Q(
                    callback_received_at__isnull=False, provider_checked_at__isnull=True
                )
                | Q(callback_received_at__gt=F("provider_checked_at"))
            )
            .filter(
                Q(provider_check_attempted_at__isnull=True)
                | Q(provider_check_attempted_at__lte=moment - CHECK_INTERVAL)
            )
            .order_by(
                F("provider_check_attempted_at").asc(nulls_first=True),
                "callback_received_at",
                "pk",
            )
        )
        attempted = 0
        for payment in pending.iterator():
            try:
                reconcile(org.pk, payment.order_id, now=now)
            except PaymentBusyError:
                continue
            except Exception:  # noqa: BLE001, S110 - retain queue
                # `reconcile` recorded and logged it, in fixed words only: never a
                # provider body, redirect, signature or exception text. One
                # damaged order must not stop the rest of this durable queue.
                pass
            attempted += 1
            if attempted == 20:
                break
