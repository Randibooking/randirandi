# SPDX-License-Identifier: AGPL-3.0-only
"""The order from the booking page's cart .

The member presses pay with the total the page shows. The cart is priced
again, every booking beside the others, and becomes one order only when all
of them are still free at the prices the member saw and the cart's total is
the one sent; the order itself refuses a different total, so a price that
moves in between saves nothing. A refused order holds nothing and leaves
the cart as it was, with its bookings marked on the next look ([7]).

One press makes one order: a lock per member covers the check, the order
and the session's save, and each booking keeps the cart row it came from,
so a press whose session is an older copy still finds the order made from
its rows . A member who already holds an order (from this
browser or another) is refused until it is paid, rejected or past its hold,
or until the member lets it go .
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from django.contrib.sessions.backends.base import SessionBase
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from randirandi.core import (
    booking_cart,
    booking_words,
    ordering,
    payment_callbacks,
    payment_recovery,
    payments,
)
from randirandi.core.booking_cart import PRICE_CHANGED, Refusal
from randirandi.core.member_data import organization_transaction
from randirandi.core.models import BookingState, Member, Order, Organization, Payment

NOT_READY = booking_words.NOT_READY
CART_EMPTY = booking_words.CART_EMPTY
TOO_LARGE = booking_words.TOO_LARGE
BUSY = booking_words.BUSY


class CheckoutBusyError(RuntimeError):
    """Another press of this member is making an order now."""


@contextmanager
def member_lock(member: Member) -> Iterator[None]:
    """Positive bigint advisory keys are reserved for one checkout per member.

    Payment order locks use the negative keys (`payments.order_lock`). The
    lock is held by the session, across the order's own transaction.
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Checkout requires its own transaction.")
    if type(member.pk) is not int or not 0 < member.pk < 2**63:
        raise ValueError("A saved member is required.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [member.pk])
        if not cursor.fetchone()[0]:
            raise CheckoutBusyError("This member's order is being made.")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [member.pk])


@dataclass(frozen=True)
class Placed:
    public_id: uuid.UUID
    total: int


def _holding(member: Member, moment: datetime) -> bool:
    """The member holds an order the payment start would take, made in any
    browser: its hold runs and every booking is still held."""
    return (
        Order.objects.of(member.organization_id)
        .filter(
            member_id=member.pk,
            hold_expires_at__gt=moment,
            bookings__taken_time__state=BookingState.HELD,
        )
        .exclude(
            bookings__taken_time__state__in=[
                state for state in BookingState.values if state != BookingState.HELD
            ]
        )
        .exists()
    )


def order(
    session: SessionBase,
    member: Member,
    total: int,
    now: datetime | None = None,
    *,
    accepted_version: str | None,
) -> Placed | Refusal:
    """Make one order from the cart at the total the member saw, accepting the
    booking conditions the cart showed .

    Refusals: "busy" (another press, or other orders made at the same
    moment every time it tried), "order-held", "cart-empty",
    "not-ready" (a booking is marked), "price-changed" (with the total
    today when known), "day-limit" (more bookings than the organization lets
    a member make in a day, with the limit and how many are left),
    "too-far-ahead" (a start after the last date the organization lets a
    member book), "taken" and
    "too-large" (the order's own limits), "conditions-not-accepted" (no
    `accepted_version`: the server is the gate, also for an order with
    nothing to pay) and "conditions-changed" (another one than the text in
    force, which is read in the order's own transaction).
    """
    if type(total) is not int or total < 0:
        raise ValueError("The total shown must be a whole number, zero or more.")
    moment = now if now is not None else timezone.now()
    try:
        with member_lock(member):
            cart = booking_cart.look(session, member, now)
            if cart.locked or _holding(member, moment):
                return Refusal(booking_cart.ORDER_HELD)
            if not cart.items:
                return Refusal(CART_EMPTY)
            if not cart.ready:
                return Refusal(NOT_READY)
            if cart.total != total:
                return Refusal(PRICE_CHANGED, cart.total)
            requests = [
                ordering.BookingRequest(
                    item.row.choice.configuration_id,
                    item.row.choice.booking_category_id,
                    item.row.choice.starts_at,
                    item.row.choice.ends_at,
                    item.quote.quantity,
                    item.row.id,
                    item.row.choice.spots,
                    item.row.choice.group_id,
                )
                for item in cart.items
            ]
            try:
                made = ordering.create_order(
                    member=member,
                    requests=requests,
                    now=now,
                    total=total,
                    accepted_version=accepted_version or "",
                )
            except ordering.OrderConditionsError as refused:
                return Refusal(
                    booking_words.CONDITIONS_NOT_ACCEPTED
                    if refused.missing
                    else booking_words.CONDITIONS_CHANGED
                )
            except ordering.OrderTotalChangedError:
                return Refusal(PRICE_CHANGED)
            except ordering.OrderLimitError:
                return Refusal(TOO_LARGE)
            except ordering.OrderOverDayLimitError as over:
                return Refusal(
                    booking_words.DAY_LIMIT, left=over.left, limit=over.limit
                )
            except ordering.OrderTooFarAheadError:
                return Refusal(booking_words.TOO_FAR_AHEAD)
            except ordering.OrderContendedError:
                # Nothing is known to be taken: the member may try again.
                return Refusal(BUSY)
            except ordering.OrderUnavailableError:
                # Still "taken", never the cart's own: the cart was checked
                # whole a moment ago, so someone else got there first.
                return Refusal(booking_words.TAKEN)
            booking_cart.placed(session, member, made)
            # Saved before the lock is let go: the next press reads it, and
            # it keeps the order before Vipps is called.
            session.save()
            return Placed(made.public_id, made.total)
    except CheckoutBusyError:
        return Refusal(BUSY)


# How long a member's cancel waits for the payment worker, which holds an
# order's lock while it asks Vipps about the payment (every 5 seconds for one
# not yet settled), and how often it looks.
CANCEL_WAIT = 2.0
CANCEL_LOOK = 0.2


def cancel(
    member: Member, public_id: uuid.UUID, now: datetime | None = None
) -> bool | Refusal:
    """Let go of the member's order waiting for payment .

    Its times are free at once, and the cart has them again, priced anew. Vipps
    is told by the payment worker, asked here: it cancels what Vipps still
    holds, and an approval that comes in the meantime is cancelled, never
    captured, since the order is no longer held (`payment_capture._held`).
    The order's lock is the one payment capture takes, so a capture under way
    finishes first; one that reached Vipps without its bookings confirmed
    (the worker stopped in between) is left to the worker, which confirms
    them or pays the money back. True: let go, or nothing left to let go.
    False: no such order of this member. Refusals: "confirmed" (paid first),
    "settling" (a capture is being finished, or the payment worker kept the
    lock past the wait).
    """
    org_id = member.organization_id
    found = (
        Order.objects.of(org_id)
        .filter(public_id=public_id, member_id=member.pk)
        .values_list("pk", flat=True)
        .first()
    )
    if found is None:
        return False

    def let_go(org: Organization) -> str:
        order = Order.objects.of(org).get(pk=found)
        state = booking_cart.order_state(order, now)
        if state in (booking_words.CONFIRMED, booking_words.CANCELLED):
            return state
        capturing = (
            Payment.objects.of(org)
            .filter(order_id=found)
            .filter(Q(capture_key__isnull=False) | Q(captured_amount__gt=0))
            .exists()
        )
        if capturing:
            return booking_words.SETTLING
        # Past its hold, what is still held is freed now, not at the
        # worker's next expiry.
        payment_recovery.release(org, found)
        return state

    deadline = time.monotonic() + CANCEL_WAIT
    while True:
        try:
            with payments.order_lock(found):
                state = organization_transaction(org_id, let_go)
            break
        except payments.PaymentBusyError:
            if time.monotonic() >= deadline:
                return Refusal(booking_words.SETTLING)
            time.sleep(CANCEL_LOOK)
    if state == booking_words.CONFIRMED:
        return Refusal(state)
    # A capture being finished is looked at at once too.
    payment_callbacks.request_reconciliation(member.organization, public_id, now=now)
    return Refusal(state) if state == booking_words.SETTLING else True
