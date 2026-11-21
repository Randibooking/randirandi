# SPDX-License-Identifier: AGPL-3.0-only
"""What became of one order: the outcome read behind the return from Vipps
.

The member's own order only. The state is the cart's (`booking_cart.order_state`:
held, confirmed, cancelled, not-completed) plus "settling": the order is held
and the payment is being read at the provider (a callback or a return came
after the last read, or none was made yet) or is authorized or captured and
the bookings are not yet confirmed. A held order with no read pending means
the member came back without deciding; the cart shows it and the page offers
"continue to Vipps". A confirmed order's bookings carry their address in
Google Calendar; the calendar file is `booking_calendar`'s .
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from randirandi.core import (
    booking_calendar,
    booking_cart,
    booking_words,
    payment_reconciliation,
)
from randirandi.core.models import (
    Booking,
    BookingConfiguration,
    BookingState,
    CapacityAllocation,
    Member,
    Order,
    Organization,
    Payment,
    PaymentState,
)
from randirandi.core.payments import PaymentBusyError

SETTLING = booking_words.SETTLING
CONFIRMED = booking_words.CONFIRMED
logger = logging.getLogger(__name__)
# Seconds each provider call may take inside the member's request; the worker
# keeps `vipps.TIMEOUT`. Several calls in a row must fit the platform's 30 s.
READ_TIMEOUT = 3


def _payment(org: Organization, found: Order) -> Payment | None:
    return Payment.objects.of(org).filter(order=found, create_key__isnull=False).first()


def _read_pending(payment: Payment) -> bool:
    received, checked = payment.callback_received_at, payment.provider_checked_at
    # The same test `payment_reconciliation.reconcile_requested` queues by.
    return received is not None and (checked is None or received > checked)


def _settling(org: Organization, found: Order) -> bool:
    payment = _payment(org, found)
    if payment is None:
        return False
    paid = payment.state in (PaymentState.AUTHORIZED, PaymentState.CAPTURED)
    return _read_pending(payment) or paid


def _read_now(org: Organization, found: Order, now: datetime | None) -> None:
    """Run the pending provider read here instead of waiting for the worker
    : the same `reconcile`, under the same order lock and its
    own throttle. A busy lock or a provider failure changes nothing: the
    order stays settling and the worker reads it as before. Never raises and
    never logs a provider word."""
    payment = _payment(org, found)
    if payment is None or not _read_pending(payment):
        return
    try:
        payment_reconciliation.reconcile(
            org.pk, found.pk, now=now, call_timeout=READ_TIMEOUT
        )
    except PaymentBusyError:
        return
    except Exception:  # noqa: BLE001 - the worker retries; sanitize provider errors
        logger.error(
            "outcome read failed: organization=%d payment=%d", org.pk, payment.pk
        )


def spots_held(org: Organization, taken_time_ids: list[int]) -> dict[int, list[Any]]:
    """The spots each claim holds, by claim, lowest number first, from its
    allocations: `{"number", "name", "group": {"number", "name"} | None}`. An
    exclusive claim holds the whole asset and names none. One query."""
    held: dict[int, list[Any]] = {pk: [] for pk in taken_time_ids}
    rows = (
        CapacityAllocation.objects.of(org)
        .filter(taken_time_id__in=taken_time_ids, spot__isnull=False)
        .select_related("spot__group")
        .order_by("spot__number")
    )
    for row in rows:
        spot = row.spot
        if spot is None:
            continue
        held[row.taken_time_id].append(
            {
                "number": spot.number,
                "name": spot.name,
                "group": None
                if spot.group is None
                else {"number": spot.group.number, "name": spot.group.name},
            }
        )
    return held


def spot_word(configuration: BookingConfiguration) -> str | None:
    """The word for the spots a container books, from its floor plan."""
    plan = configuration.floor_plan
    return booking_words.spot_word(None if plan is None else plan.unit)


def container_and_spots(
    org: Organization, bookings: list[Booking]
) -> list[dict[str, Any]]:
    """For each booking, in order, its container and the spots it holds, and
    the word for them (`spot_word`, the floor plan's: "seat", "bed"; None for
    a container without a floor plan). What the order read and the locked
    cart's held order both answer. Only the member's own order is ever passed
    in: nothing about other members. The booking's container and its floor
    plan must be loaded with it."""
    held = spots_held(org, [booking.taken_time_id for booking in bookings])
    configurations = {
        booking.booking_configuration_id: booking.booking_configuration
        for booking in bookings
    }
    return [
        {
            "container": {
                "id": booking.booking_configuration_id,
                "number": configurations[booking.booking_configuration_id].number,
            },
            "spots": held[booking.taken_time_id],
            "spot_word": spot_word(configurations[booking.booking_configuration_id]),
        }
        for booking in bookings
    ]


def ordered_bookings(found: Order) -> list[Booking]:
    """The order's bookings as the member sees them: by start, then entry."""
    return list(
        found.bookings.select_related(
            "taken_time__asset", "booking_configuration__floor_plan"
        )
        .prefetch_related("lines__price_book")
        .order_by("taken_time__starts_at", "pk")
    )


def currency_of(org: Organization, bookings: Sequence[Booking]) -> str:
    """The currency of what was bought: that of its first booking in a price
    book, even at price 0 (an order has one), else the organization's, for
    bookings without payment. The lines must be loaded with their books."""
    for booking in bookings:
        for line in booking.lines.all():
            return line.price_book.currency
    return org.currency


def booking_item(
    org: Organization, booking: Booking, placed: dict[str, Any]
) -> dict[str, Any]:
    """One booking as the member sees it, with its container and spots
    (`placed`, from `container_and_spots`): its times in its own asset's zone
    (the claim keeps a copy) and its amount in its own price book's currency.
    The booking, its asset and its lines, with their price books, must be
    loaded with it."""
    zone = ZoneInfo(booking.taken_time.time_zone)
    lines = list(booking.lines.all())
    return {
        **placed,
        "asset": booking.taken_time.asset.name,
        "type": booking.booking_type_name,
        "category": booking.booking_category_name,
        "places": booking.taken_time.quantity,
        "start": booking.taken_time.starts_at.astimezone(zone).isoformat(),
        "end": booking.taken_time.ends_at.astimezone(zone).isoformat(),
        "total": sum(line.total for line in lines),
        "currency": lines[0].price_book.currency if lines else org.currency,
        "state": booking.taken_time.state,
    }


def read(
    org: Organization, member: Member, public_id: uuid.UUID, now: datetime | None = None
) -> dict[str, Any] | None:
    """The order as the member sees it; None for an order that is not theirs
    or does not exist (the two are not told apart)."""
    found = (
        Order.objects.of(org).filter(public_id=public_id, member_id=member.pk).first()
    )
    if found is None:
        return None
    moment = now if now is not None else timezone.now()
    state = booking_cart.order_state(found, moment)
    if state == booking_cart.HELD:
        _read_now(org, found, now)
        found.refresh_from_db()
        state = booking_cart.order_state(found, moment)
    if state == booking_cart.HELD and _settling(org, found):
        state = SETTLING
    bookings = ordered_bookings(found)
    named = container_and_spots(org, bookings)
    items = [
        booking_item(org, booking, placed)
        for booking, placed in zip(bookings, named, strict=True)
    ]
    # A confirmed order's bookings carry their Google Calendar address (step
    # 5, ): None for one cancelled since.
    links = booking_calendar.google_links(org, found) if state == CONFIRMED else None

    def google(booking: Booking) -> dict[str, str | None]:
        return {} if links is None else {"google": links.get(booking.public_id)}

    return {
        "id": str(found.public_id),
        "reference": found.reference,
        "state": state,
        "text": booking_words.text(state, org=org),
        "total": found.total,
        "currency": currency_of(org, bookings),
        # An instant, shown on the clock of the first booking's asset.
        "hold_expires_at": found.hold_expires_at.astimezone(
            ZoneInfo(bookings[0].taken_time.time_zone if bookings else org.time_zone)
        ).isoformat()
        if found.hold_expires_at is not None
        else None,
        "contact_email": org.contact_email,
        # Whether the order recorded the booking conditions it accepted : its text is at
        # `/book/order/<id>/
        # conditions/`.
        "conditions": found.conditions_id is not None,
        "bookings": [
            item | google(booking)
            for booking, item in zip(bookings, items, strict=True)
        ],
    }


def bookings(
    org: Organization, member: Member, now: datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The member's own bookings: the coming ones (ending after `now`) by
    start, then the earlier ones, latest first. Each is the booking as the
    order read gives it, with its order's id and number. Confirmed and
    cancelled bookings are listed; a held or not-completed order's are not
    (the cart shows the held one). Own bookings only; a fixed number of
    queries."""
    moment = now if now is not None else timezone.now()
    own = Order.objects.of(org).filter(member_id=member.pk)
    found = list(
        Booking.objects.of(org)
        .filter(
            order__in=own,
            taken_time__state__in=(BookingState.CONFIRMED, BookingState.CANCELLED),
        )
        .select_related(
            "taken_time__asset", "booking_configuration__floor_plan", "order"
        )
        .prefetch_related("lines__price_book")
        .order_by("taken_time__starts_at", "pk")
    )
    named = container_and_spots(org, found)
    coming: list[dict[str, Any]] = []
    earlier: list[dict[str, Any]] = []
    for booking, placed in zip(found, named, strict=True):
        item = {
            **booking_item(org, booking, placed),
            "order": {
                "id": str(booking.order.public_id),
                "reference": booking.order.reference,
                "conditions": booking.order.conditions_id is not None,
            },
        }
        (coming if booking.taken_time.ends_at > moment else earlier).append(item)
    earlier.reverse()
    return {"coming": coming, "earlier": earlier}
