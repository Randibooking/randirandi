# SPDX-License-Identifier: AGPL-3.0-only
"""Administrator requests; the durable worker performs the saved money operation.

`request_refund` queues a refund or cancels an unpaid order. `cancel`  is the
administrator's cancel of a whole booking or one of its lines, with
or without the refund, built from the same locks, checks and money rules.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection

from randirandi.core import payment_reconciliation, payment_refunds
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    AuditEvent,
    Booking,
    BookingState,
    Member,
    Order,
    Organization,
    Payment,
    Refund,
    TakenTime,
)
from randirandi.core.payments import (
    PaymentBusyError,
    PaymentUnavailableError,
    client_for,
    order_lock,
)
from randirandi.vipps import PaymentStatus


@dataclass(frozen=True)
class Adjustment:
    """An accepted request, not proof that its money operation completed."""

    payment_id: int
    refund_id: int | None


@dataclass(frozen=True)
class Cancelled:
    """An accepted cancel: the booking lines it took (empty for a repeat of a
    request `request_refund` accepted) and the refund it queued, if any. Not
    proof that the refund completed."""

    order_id: int
    booking_ids: tuple[int, ...]
    refund_id: int | None


class CancelRefusedError(PaymentUnavailableError):
    """A cancel the rules refuse. `code` is one of CODES; the page words it."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# Why a cancel is refused, besides "busy" (PaymentBusyError: another worker
# has the order, or a refund of it is unfinished).
CODES = (
    "not-found",  # the line is not in this booking
    "past",  # every line left has ended
    "already-cancelled",  # nothing left to cancel
    "not-cancellable",  # the lines are not held or paid (they ran out)
    "whole-only",  # a booking being paid is cancelled whole
    "recovery",  # the order and the provider disagree; payment recovery first
    "payment-unavailable",  # there is no payment to act on
    "no-refund-left",  # nothing refundable remains
    "key-in-use",  # the request key belongs to another request
    "unavailable",  # any other refusal
)


def _actor(org: Organization, actor_id: int) -> Member:
    actor = Member.objects.of(org).select_related("role").filter(pk=actor_id).first()
    if actor is None or not actor.is_staff:
        raise PermissionDenied("An active administrator is required.")
    return actor


def _context(
    org: Organization,
    actor_id: int,
    order_id: int,
    booking_public_id: uuid.UUID | None,
) -> tuple[Member, Payment, Booking | None]:
    actor = _actor(org, actor_id)
    payment = (
        Payment.objects.of(org)
        .select_related("order")
        .filter(order_id=order_id, create_key__isnull=False)
        .first()
    )
    if payment is None:
        raise PaymentUnavailableError("The payment is unavailable.")
    booking = None
    if booking_public_id is not None:
        booking = payment.order.bookings.filter(public_id=booking_public_id).first()
        if booking is None:
            raise PaymentUnavailableError("The booking is unavailable.")
    return actor, payment, booking


def _existing(
    actor: Member, payment: Payment, booking: Booking | None, key: uuid.UUID
) -> Adjustment | None:
    existing = Refund.objects.of(payment.organization_id).filter(refund_key=key).first()
    if existing is not None:
        if (
            existing.payment_id != payment.pk
            or existing.booking_id != (booking.pk if booking else None)
            or existing.made_by_id != actor.pk
            or existing.automatic
        ):
            raise PaymentUnavailableError("The request identity is already in use.")
        return Adjustment(payment.pk, existing.pk)
    if payment.cancel_key is not None:
        if (
            payment.cancel_key != key
            or not AuditEvent.objects.of(payment.organization_id)
            .filter(
                action=AuditAction.CANCEL_PAYMENT,
                object_kind="payment",
                object_id=payment.pk,
                administrator=actor,
            )
            .exists()
        ):
            raise PaymentUnavailableError("A cancellation is already in progress.")
        return Adjustment(payment.pk, None)
    if payment_refunds.pending(payment) is not None:
        raise PaymentBusyError("A refund is already in progress.")
    return None


def _cancel_bookings(
    org: Organization,
    actor: Member,
    order: Order,
    booking: Booking | None,
    now: datetime | None,
    *,
    only: list[int] | None = None,
    key: uuid.UUID | None = None,
) -> list[int]:
    """Cancel the order's held and confirmed bookings, or `booking` alone, or
    those in `only`. With a `key` (an administrator's cancel, `cancel`) each
    records it and that the cancellation email is due."""
    rows = order.bookings.filter(
        taken_time__state__in=[BookingState.HELD, BookingState.CONFIRMED]
    )
    if booking is not None:
        rows = rows.filter(pk=booking.pk)
    if only is not None:
        rows = rows.filter(pk__in=only)
    ids = list(rows.order_by("pk").values_list("pk", flat=True))
    moment = utc_now(now)
    TakenTime.objects.of(org).filter(booking__pk__in=ids).update(
        state=BookingState.CANCELLED
    )
    Booking.objects.of(org).filter(pk__in=ids).update(
        cancelled_at=moment,
        cancelled_by=actor,
        cancellation_key=key,
        cancel_mail_due_at=None if key is None else moment,
    )
    return ids


def _captured_bookings(current: Payment, status: PaymentStatus) -> list[Booking]:
    """The bookings of an order whose money is captured, after the checks that
    the order and the provider agree."""
    bookings = list(
        current.order.bookings.select_related("taken_time").prefetch_related("lines")
    )
    if (
        current.order.hold_expires_at is not None
        or not bookings
        or any(row.member_id != current.order.member_id for row in bookings)
        or any(
            row.taken_time.state not in {BookingState.CONFIRMED, BookingState.CANCELLED}
            for row in bookings
        )
        or current.retired_booking_amount
        + sum(line.total for row in bookings for line in row.lines.all())
        != current.amount
        or current.order.total != current.amount
        or status.captured != current.amount
    ):
        raise CancelRefusedError("recovery", "The order requires payment recovery.")
    return bookings


def _refund_amount(
    org: Organization,
    current: Payment,
    status: PaymentStatus,
    bookings: list[Booking],
    chosen: list[Booking] | None,
) -> int:
    """What a refund returns: the payment's remaining captured amount for the
    whole order (`chosen` None), or the saved lines of the `chosen` bookings
    less what was refunded for them."""
    amount = status.captured - status.refunded
    if chosen is not None:
        refunds = list(Refund.objects.of(org).filter(payment=current))
        if (
            any(row.booking_id not in {item.pk for item in bookings} for row in refunds)
            or current.retired_refund_amount + sum(row.amount for row in refunds)
            != status.refunded
        ):
            raise CancelRefusedError("recovery", "Prior refunds are not attributable.")
        picked = {item.pk for item in chosen}
        amount = sum(line.total for item in chosen for line in item.lines.all()) - sum(
            row.amount for row in refunds if row.booking_id in picked
        )
    if not 0 < amount <= status.captured - status.refunded:
        raise CancelRefusedError("no-refund-left", "No refundable amount remains.")
    return amount


def _queue_refund(
    org: Organization,
    actor: Member,
    current: Payment,
    status: PaymentStatus,
    amount: int,
    booking: Booking | None,
    key: uuid.UUID,
    now: datetime | None,
) -> Refund:
    return Refund.objects.create(
        organization=org,
        payment=current,
        booking=booking,
        amount=amount,
        made_by=actor,
        refund_key=key,
        refunded_before=status.refunded,
        made_at=utc_now(now),
    )


def _wake(org: Organization, payment: Payment) -> None:
    """Have the worker look at the payment's money again."""
    Payment.objects.of(org).filter(pk=payment.pk).update(
        settled_at=None, provider_check_attempted_at=None
    )


def _guard(
    administrator: Member,
    order_public_id: uuid.UUID,
    request_key: uuid.UUID,
    booking_public_id: uuid.UUID | None,
    now: datetime | None,
) -> datetime | None:
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Payment operations require their own transaction.")
    if (
        not administrator.pk
        or not isinstance(order_public_id, uuid.UUID)
        or not isinstance(request_key, uuid.UUID)
        or (
            booking_public_id is not None
            and not isinstance(booking_public_id, uuid.UUID)
        )
    ):
        raise PaymentUnavailableError("A saved administrator and UUIDs are required.")
    return None if now is None else utc_now(now)


def request_refund(
    *,
    administrator: Member,
    order_public_id: uuid.UUID,
    request_key: uuid.UUID,
    booking_public_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Adjustment:
    """Queue a booking/whole-order refund, or cancel the whole unpaid order.

    Amounts come from saved lines and a current provider read. The order lock
    covers that read; authority and intent are committed in one short transaction.
    The worker uses the saved intent even if the requesting admin later leaves.
    """
    now = _guard(administrator, order_public_id, request_key, booking_public_id, now)
    order = (
        Order.objects.of(administrator.organization_id)
        .filter(public_id=order_public_id)
        .first()
    )
    if order is None:
        raise PaymentUnavailableError("The order is unavailable.")
    with order_lock(order.pk):

        def initial(org: Organization) -> tuple[Payment, Adjustment | None]:
            actor, payment, booking = _context(
                org, administrator.pk, order.pk, booking_public_id
            )
            return payment, _existing(actor, payment, booking, request_key)

        payment, existing = organization_transaction(order.organization_id, initial)
        if existing is not None:
            return existing
        status = client_for(payment).status(
            payment.provider_reference, payment.currency
        )

        def intent(org: Organization) -> Adjustment:
            actor, current, booking = _context(
                org, administrator.pk, order.pk, booking_public_id
            )
            existing = _existing(actor, current, booking, request_key)
            if existing is not None:
                return existing
            payment_reconciliation._apply(
                org, current.pk, status, utc_now(now), complete=False
            )
            current.refresh_from_db()
            if not status.captured:
                if status.state not in {"CREATED", "AUTHORIZED"}:
                    raise PaymentUnavailableError("There is no payment to cancel.")
                current.cancel_key = request_key
                current.save(update_fields=["cancel_key"])
                _cancel_bookings(org, actor, current.order, None, now)
                record_event(
                    actor=actor, action=AuditAction.CANCEL_PAYMENT, target=current
                )
                result = Adjustment(current.pk, None)
            else:
                bookings = _captured_bookings(current, status)
                amount = _refund_amount(
                    org,
                    current,
                    status,
                    bookings,
                    None if booking is None else [booking],
                )
                refund = _queue_refund(
                    org, actor, current, status, amount, booking, request_key, now
                )
                _cancel_bookings(org, actor, current.order, booking, now)
                record_event(
                    actor=actor, action=AuditAction.REQUEST_REFUND, target=refund
                )
                result = Adjustment(current.pk, refund.pk)
            _wake(org, current)
            return result

        try:
            return organization_transaction(order.organization_id, intent)
        except IntegrityError:
            raise PaymentUnavailableError(
                "The adjustment could not be accepted."
            ) from None


@dataclass(frozen=True)
class _Plan:
    payment: Payment | None
    # The bookings this cancel takes, and the one line asked for, if any.
    chosen: list[Booking]
    line: Booking | None
    # The order is still being paid: only the unpaid-order cancel applies.
    held: bool
    # Paid money would be returned for them: they hold a price above 0.
    money: bool
    # They hold a price but the order has no payment row (only when the caller
    # accepts that: a change to availability cancels such an order anyway).
    unpaid_price: bool = False


def _plan(
    org: Organization,
    order_id: int,
    actor: Member,
    booking_public_id: uuid.UUID | None,
    key: uuid.UUID,
    moment: datetime,
    lines: Sequence[uuid.UUID] = (),
    *,
    without_payment: bool = False,
) -> Cancelled | _Plan:
    """What a cancel would take, or the answer to a repeat of the request.
    Refuses what the rules refuse; reads only. `lines` are several lines of the
    order asked for together , as the one `booking_public_id` is."""
    if lines:
        wanted = list(dict.fromkeys(lines))
        booking_public_id = wanted[0] if len(wanted) == 1 else None
    else:
        wanted = [] if booking_public_id is None else [booking_public_id]
    order = Order.objects.of(org).get(pk=order_id)
    rows = list(
        Booking.objects.of(org)
        .filter(order=order)
        .select_related("taken_time")
        .prefetch_related("lines")
        .order_by("taken_time__starts_at", "pk")
    )
    earlier = Refund.objects.of(org).filter(refund_key=key).first()
    done = [row for row in rows if row.cancellation_key == key]
    if done:
        if any(row.cancelled_by_id != actor.pk for row in done) or (
            wanted and {row.public_id for row in done} != set(wanted)
        ):
            raise CancelRefusedError(
                "key-in-use", "The request identity is already in use."
            )
        return Cancelled(
            order.pk,
            tuple(row.pk for row in done),
            None if earlier is None else earlier.pk,
        )
    if earlier is not None:
        raise CancelRefusedError(
            "key-in-use", "The request identity is already in use."
        )
    live = [
        row
        for row in rows
        if row.taken_time.state in {BookingState.HELD, BookingState.CONFIRMED}
    ]
    held = any(row.taken_time.state == BookingState.HELD for row in live)
    line = None
    if wanted:
        chosen = []
        for public_id in wanted:
            found = next((row for row in rows if row.public_id == public_id), None)
            if found is None:
                raise CancelRefusedError(
                    "not-found", "The booking line is unavailable."
                )
            if found.taken_time.state == BookingState.CANCELLED:
                raise CancelRefusedError("already-cancelled")
            if found not in live:
                raise CancelRefusedError("not-cancellable")
            if held:
                raise CancelRefusedError("whole-only")
            if found.taken_time.ends_at <= moment:
                raise CancelRefusedError("past")
            chosen.append(found)
        line = chosen[0] if len(chosen) == 1 else None
    else:
        coming = [row for row in live if row.taken_time.ends_at > moment]
        if not coming:
            raise CancelRefusedError(
                "past"
                if live
                else "already-cancelled"
                if any(row.taken_time.state == BookingState.CANCELLED for row in rows)
                else "not-cancellable"
            )
        # An order being paid is cancelled whole, past lines too.
        chosen = live if held else coming
    payment = Payment.objects.of(org).filter(order=order).first()
    money = any(price.total for row in chosen for price in row.lines.all())
    if payment is None and money:
        if not without_payment:
            raise CancelRefusedError(
                "payment-unavailable", "The payment is unavailable."
            )
        return _Plan(None, chosen, line, held, False, True)
    return _Plan(payment, chosen, line, held, money and not held)


def cancel(
    *,
    administrator: Member,
    order_public_id: uuid.UUID,
    request_key: uuid.UUID,
    booking_public_id: uuid.UUID | None = None,
    booking_public_ids: Sequence[uuid.UUID] = (),
    refund: bool = True,
    now: datetime | None = None,
) -> Cancelled:
    """Cancel a whole booking (the lines still to come) or one line, with or
    without the refund .

    A line still to come, also one under way, is refunded by its whole saved
    amount; an order still being paid is cancelled whole through the
    unpaid-order cancel (`request_refund`'s), with nothing to refund. A refund
    goes to the existing queue; without it nothing is sent to the provider.
    The lock and the busy refusals are `request_refund`'s, and the request key
    makes a repeat answer as the first did. Each cancelled line gets an audit
    event, and its member a cancellation email (`booking_emails`).

    `booking_public_ids` name several lines of the order to cancel together
    (not with `booking_public_id`): one refund returns what they cost, and the
    order's other lines stay. A change to availability cancels this way .
    """
    if not isinstance(refund, bool):
        raise PaymentUnavailableError("The refund choice must be yes or no.")
    if booking_public_ids and booking_public_id is not None:
        raise PaymentUnavailableError("Name one line or several, not both.")
    now = _guard(administrator, order_public_id, request_key, booking_public_id, now)
    if any(not isinstance(item, uuid.UUID) for item in booking_public_ids):
        raise PaymentUnavailableError("The booking lines must be UUIDs.")
    moment = utc_now(now)
    order = (
        Order.objects.of(administrator.organization_id)
        .filter(public_id=order_public_id)
        .first()
    )
    if order is None:
        raise CancelRefusedError("not-found", "The order is unavailable.")
    with order_lock(order.pk):

        def initial(org: Organization) -> Cancelled | _Plan:
            actor = _actor(org, administrator.pk)
            return _plan(
                org,
                order.pk,
                actor,
                booking_public_id,
                request_key,
                moment,
                booking_public_ids,
            )

        plan = organization_transaction(order.organization_id, initial)
        if isinstance(plan, Cancelled):
            return plan
        status = None
        if plan.payment is not None and (plan.held or (refund and plan.money)):
            if plan.payment.create_key is None:
                raise CancelRefusedError("payment-unavailable", "No request intent.")
            status = client_for(plan.payment).status(
                plan.payment.provider_reference, plan.payment.currency
            )

        def intent(org: Organization) -> Cancelled:
            actor = _actor(org, administrator.pk)
            current = _plan(
                org,
                order.pk,
                actor,
                booking_public_id,
                request_key,
                moment,
                booking_public_ids,
            )
            if isinstance(current, Cancelled):
                return current
            payment = None
            queued = None
            if current.payment is not None and (
                current.held or (refund and current.money)
            ):
                if status is None:
                    raise PaymentBusyError("The order changed; try again.")
                payment = (
                    Payment.objects.of(org)
                    .select_related("order")
                    .get(pk=current.payment.pk)
                )
                existing = _existing(actor, payment, current.line, request_key)
                if existing is not None:
                    return Cancelled(order.pk, (), existing.refund_id)
                payment_reconciliation._apply(
                    org, payment.pk, status, moment, complete=False
                )
                payment.refresh_from_db()
                if current.held:
                    if status.captured:
                        raise CancelRefusedError(
                            "recovery", "The order requires payment recovery."
                        )
                    if status.state not in {"CREATED", "AUTHORIZED"}:
                        raise CancelRefusedError(
                            "payment-unavailable", "There is no payment to cancel."
                        )
                    payment.cancel_key = request_key
                    payment.save(update_fields=["cancel_key"])
                else:
                    amount = _refund_amount(
                        org,
                        payment,
                        status,
                        _captured_bookings(payment, status),
                        current.chosen,
                    )
                    queued = _queue_refund(
                        org,
                        actor,
                        payment,
                        status,
                        amount,
                        current.chosen[0] if len(current.chosen) == 1 else None,
                        request_key,
                        now,
                    )
            ids = _cancel_bookings(
                org,
                actor,
                order,
                None,
                now,
                only=[row.pk for row in current.chosen],
                key=request_key,
            )
            for row in Booking.objects.of(org).filter(pk__in=ids).order_by("pk"):
                record_event(
                    actor=actor, action=AuditAction.CANCEL_BOOKING_LINE, target=row
                )
            if payment is not None and current.held:
                record_event(
                    actor=actor, action=AuditAction.CANCEL_PAYMENT, target=payment
                )
            if queued is not None:
                record_event(
                    actor=actor, action=AuditAction.REQUEST_REFUND, target=queued
                )
            if payment is not None:
                _wake(org, payment)
            return Cancelled(
                order.pk, tuple(ids), None if queued is None else queued.pk
            )

        try:
            return organization_transaction(order.organization_id, intent)
        except IntegrityError:
            raise CancelRefusedError(
                "unavailable", "The cancel could not be accepted."
            ) from None


@dataclass(frozen=True)
class CancelledHere:
    """What `cancel_in_transaction` did for one order."""

    booking_ids: tuple[int, ...]
    refund_id: int | None
    # Why no refund was recorded for money paid: "recovery" (the order's money
    # cannot be attributed from the saved records), "no-refund-left" or
    # "no-payment" (a price, and no payment row to refund).
    unrecorded: str | None


def cancel_in_transaction(
    org: Organization,
    administrator: Member,
    order_id: int,
    booking_public_ids: Sequence[uuid.UUID],
    key: uuid.UUID,
    now: datetime | None,
) -> CancelledHere:
    """Cancel lines of an order inside the caller's organization transaction,
    from the saved records alone: no provider call, no order lock .

    A change to availability uses it, so that the bookings it strands are
    cancelled in the transaction that commits the change, whatever the provider
    would answer. An order still being paid (`booking_public_ids` empty) is
    cancelled whole and its payment is given the cancel intent, which the worker
    carries out (`payment_recovery.advance`). Paid money is queued as a `Refund`
    (key, `completed_at` empty), the existing queue the worker works through
    again until the provider takes it (`payment_refunds.execute`), from the
    amounts the payment saved at its last read. The refusals of `_plan` are
    raised: an order with nothing left to cancel is not an error to the caller.
    """
    moment = utc_now(now)
    actor = _actor(org, administrator.pk)
    plan = _plan(
        org,
        order_id,
        actor,
        None,
        key,
        moment,
        booking_public_ids,
        without_payment=True,
    )
    if isinstance(plan, Cancelled):
        return CancelledHere(plan.booking_ids, plan.refund_id, None)
    order = Order.objects.of(org).get(pk=order_id)
    payment = None
    queued = None
    unrecorded = "no-payment" if plan.unpaid_price else None
    if plan.payment is not None and plan.held:
        payment = (
            Payment.objects.of(org).select_related("order").get(pk=plan.payment.pk)
        )
        payment.cancel_key = key
        payment.save(update_fields=["cancel_key"])
    elif plan.payment is not None and plan.money:
        payment = (
            Payment.objects.of(org).select_related("order").get(pk=plan.payment.pk)
        )
        saved = PaymentStatus(
            payment.provider_reference,
            "CAPTURED",
            payment.amount,
            payment.amount,
            payment.captured_amount,
            0,
            payment.refunded_amount,
        )
        try:
            amount = _refund_amount(
                org, payment, saved, _captured_bookings(payment, saved), plan.chosen
            )
        except CancelRefusedError as refused:
            # The bookings are cancelled all the same; the money needs a person.
            unrecorded = refused.code
        else:
            queued = _queue_refund(
                org,
                actor,
                payment,
                saved,
                amount,
                plan.chosen[0] if len(plan.chosen) == 1 else None,
                key,
                now,
            )
    ids = _cancel_bookings(
        org,
        actor,
        order,
        None,
        now,
        only=[row.pk for row in plan.chosen],
        key=key,
    )
    for row in Booking.objects.of(org).filter(pk__in=ids).order_by("pk"):
        record_event(actor=actor, action=AuditAction.CANCEL_BOOKING_LINE, target=row)
    if payment is not None and plan.held:
        record_event(actor=actor, action=AuditAction.CANCEL_PAYMENT, target=payment)
    if queued is not None:
        record_event(actor=actor, action=AuditAction.REQUEST_REFUND, target=queued)
    if unrecorded is not None and not plan.unpaid_price:
        # Money was captured but cannot be attributed: saved, so that the page
        # lists the order after a reload. An order with no payment row has
        # nothing to refund and is only in the save's unfinished list.
        record_event(actor=actor, action=AuditAction.LEFT_REFUND_BY_HAND, target=order)
    if payment is not None:
        _wake(org, payment)
    return CancelledHere(tuple(ids), None if queued is None else queued.pk, unrecorded)
