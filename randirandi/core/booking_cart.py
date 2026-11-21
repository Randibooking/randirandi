# SPDX-License-Identifier: AGPL-3.0-only
"""The member's cart on the booking page .

The cart lives in the member's own server-side session. It holds choices and
the total the member saw for each, never a reservation . Every look
prices its bookings again, together, as one order of them all would take
them (T4): a booking that no longer fits is taken, or does not fit beside
the cart when only the cart is in the way; one whose price moved says so. A
booking goes in, or is changed, only when it fits beside the others
without pushing one out and costs what the member saw.

The session is saved whole, so the page sends one change at a time: of two
sent at once from one browser, the last saved wins. The order checks the
total the member saw (step 2.5), so a lost change is never ordered unseen.

The session also keeps the one order made from the cart  and which of its rows went into
it; each booking
keeps its cart row too, so an older copy of the session saved by another
request still finds that order . While that
order is held for payment the cart shows it and cannot be changed (proposal
c); once it is confirmed, or paid and then cancelled by an administrator,
those rows leave the cart; when it is not completed the cart is the
member's again, with the same bookings (proposal a).
"""

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.sessions.backends.base import SessionBase
from django.db.models import Q
from django.utils import timezone

from randirandi.core import booking_limits, booking_words, ordering
from randirandi.core.booking_quote import (
    Choice,
    Quote,
    QuoteInputError,
    _check,
    cart_quotes,
    quote,
)
from randirandi.core.models import BookingState, Member, Order, Organization

SESSION_KEY = "booking_cart"
MAX_ROWS = ordering.MAX_BOOKINGS
CART_FULL = booking_words.CART_FULL
PRICE_CHANGED = booking_words.PRICE_CHANGED
ORDER_HELD = booking_words.ORDER_HELD
HELD, CONFIRMED = booking_words.HELD, booking_words.CONFIRMED
CANCELLED, NOT_COMPLETED = booking_words.CANCELLED, booking_words.NOT_COMPLETED


@dataclass(frozen=True)
class CartOrder:
    """The order made from the cart, as its bookings stand.

    "held" while all its times are held for payment and the hold runs;
    "confirmed" once paid (or free); "cancelled" when an administrator
    cancelled all of it after that; "not-completed" when rejected, expired,
    past its hold or partly cancelled before payment (the payment start then
    refuses it).
    """

    public_id: uuid.UUID
    reference: str
    state: str
    total: int
    hold_expires_at: datetime | None


@dataclass(frozen=True)
class CartRow:
    """One booking in the cart: the choice and the total the member saw."""

    id: str
    choice: Choice
    seen: int


@dataclass(frozen=True)
class CartItem:
    row: CartRow
    quote: Quote

    @property
    def state(self) -> str:
        """ "ok", or a refusal key: "taken", "does-not-fit-beside-cart",
        "other-currency",
        "already-started", "not-offered" or "price-changed"."""
        if self.quote.reason is not None:
            return self.quote.reason
        return "ok" if self.quote.total == self.row.seen else PRICE_CHANGED


@dataclass(frozen=True)
class Cart:
    items: tuple[CartItem, ...]
    order: CartOrder | None = None

    @property
    def locked(self) -> bool:
        """An order from the cart is held for payment."""
        return self.order is not None and self.order.state == HELD

    @property
    def total(self) -> int:
        """What the bookings cost today, a taken booking nothing; while
        locked, the held order's total."""
        if self.order is not None and self.locked:
            return self.order.total
        return sum(item.quote.total for item in self.items)

    @property
    def currency(self) -> str:
        """The currency of the cart's paid bookings; empty when none is paid."""
        return next(
            (item.quote.currency for item in self.items if item.quote.lines), ""
        )

    @property
    def ready(self) -> bool:
        """Every booking is still free at the price the member saw."""
        return (
            not self.locked
            and bool(self.items)
            and all(item.state == "ok" for item in self.items)
        )


@dataclass(frozen=True)
class Refusal:
    """Why a booking did not go in; with the price today when it moved."""

    reason: str
    total: int | None = None
    # For "day-limit": how many bookings the member may still make today, and
    # the organization's limit for one day; `in_cart` when the cart's own
    # lines are what leave none (some are left without them).
    left: int | None = None
    limit: int | None = None
    in_cart: bool = False


def _row(stored: Any) -> CartRow:
    """One stored row. A row saved before the container was named  has no "container"
    and reads as malformed."""
    spots = stored.get("spots", []) if isinstance(stored, dict) else None
    if not isinstance(spots, list):
        raise QuoteInputError("A stored cart row is malformed.")
    choice = _check(
        Choice(
            stored["container"],
            stored["category"],
            stored["quantity"],
            datetime.fromisoformat(stored["start"]),
            datetime.fromisoformat(stored["end"]),
            tuple(spots),
            # Absent in a row saved before groups of spots .
            stored.get("group"),
        )
    )
    seen, row_id = stored["seen"], stored["id"]
    if type(seen) is not int or seen < 0 or type(row_id) is not str:
        raise QuoteInputError("A stored cart row is malformed.")
    return CartRow(row_id, choice, seen)


def _stored(session: SessionBase, member: Member) -> dict[str, Any]:
    stored = session.get(SESSION_KEY)
    if not isinstance(stored, dict) or stored.get("member") != member.pk:
        return {}
    return stored


def rows(session: SessionBase, member: Member) -> list[CartRow]:
    """The member's cart; anything else in the session reads as empty."""
    try:
        return [_row(item) for item in _stored(session, member).get("rows", [])]
    except KeyError, TypeError, ValueError:
        return []


def _order_id(session: SessionBase, member: Member) -> uuid.UUID | None:
    try:
        return uuid.UUID(_stored(session, member)["order"])
    except KeyError, TypeError, ValueError:
        return None


def _ordered(session: SessionBase, member: Member) -> set[str]:
    """The rows that went into the order and have not yet left the cart."""
    found = _stored(session, member).get("ordered")
    if not isinstance(found, list):
        return set()
    return {item for item in found if isinstance(item, str)}


def _save(
    session: SessionBase,
    member: Member,
    cart: Sequence[CartRow],
    order: uuid.UUID | None = None,
    ordered: Sequence[str] | None = None,
) -> None:
    """Store the rows; the order kept so far, and its rows, stay unless
    given."""
    order = order or _order_id(session, member)
    kept = list(ordered) if ordered is not None else sorted(_ordered(session, member))
    session[SESSION_KEY] = {
        "member": member.pk,
        **({"order": str(order), "ordered": kept} if order is not None else {}),
        "rows": [
            {
                "id": row.id,
                "container": row.choice.configuration_id,
                "category": row.choice.booking_category_id,
                "quantity": row.choice.quantity,
                "spots": list(row.choice.spots),
                "group": row.choice.group_id,
                "start": row.choice.starts_at.isoformat(),
                "end": row.choice.ends_at.isoformat(),
                "seen": row.seen,
            }
            for row in cart
        ],
    }


def order_state(found: Order, now: datetime | None = None) -> str:
    """One order's state, read from its bookings: held, confirmed, cancelled
    or not-completed (the cart and the outcome read it with this one rule)."""
    states = set(found.bookings.values_list("taken_time__state", flat=True))
    moment = now if now is not None else timezone.now()
    expires = found.hold_expires_at
    if states == {BookingState.HELD} and expires is not None and expires > moment:
        return HELD
    if (
        expires is None
        and states
        and states
        <= {
            BookingState.CONFIRMED,
            BookingState.CANCELLED,
        }
    ):
        return CANCELLED if states == {BookingState.CANCELLED} else CONFIRMED
    return NOT_COMPLETED


def settle(session: SessionBase, member: Member, public_id: uuid.UUID) -> None:
    """Apply the cart's clean-up when the cart's order is this one, so a
    confirmed order's rows have left the cart ."""
    if _order_id(session, member) == public_id:
        order(session, member)


def order(
    session: SessionBase, member: Member, now: datetime | None = None
) -> CartOrder | None:
    """The order made from the cart, as it stands; a confirmed one empties
    the cart.

    It is the member's latest order that holds one of the cart's rows, or the
    one the session keeps. Each booking keeps the cart row it was ordered
    from, because another request of the same browser (every signed-in
    request saves the session) can save an older copy of the session over
    the one the order saved .
    """
    public_id = _order_id(session, member)
    ids = [row.id for row in rows(session, member)]
    if public_id is None and not ids:
        return None
    which = Q(bookings__cart_row__in=ids)
    if public_id is not None:
        which |= Q(public_id=public_id)
    found = (
        Order.objects.of(member.organization_id)
        .filter(which, member_id=member.pk)
        .order_by("-pk")
        .first()
    )
    if found is None:
        return None
    moment = now if now is not None else timezone.now()
    expires = found.hold_expires_at
    state = order_state(found, moment)
    if state in (CONFIRMED, CANCELLED):
        ordered = _ordered(session, member) | set(
            found.bookings.values_list("cart_row", flat=True)
        )
        cart = rows(session, member)
        kept = [row for row in cart if row.id not in ordered]
        if (
            len(kept) != len(cart)
            or _ordered(session, member)
            or public_id != found.public_id
        ):
            _save(session, member, kept, found.public_id, ordered=[])
    return CartOrder(found.public_id, found.reference, state, found.total, expires)


def placed(session: SessionBase, member: Member, made: Order) -> None:
    """Keep the order just made from the cart and which rows went into it;
    a free one is confirmed and they leave the cart at once."""
    cart = rows(session, member)
    if made.total:
        _save(session, member, cart, made.public_id, [row.id for row in cart])
    else:
        _save(session, member, [], made.public_id, [])


def look(session: SessionBase, member: Member, now: datetime | None = None) -> Cart:
    """The cart priced today, every booking beside the others; while its
    order is held, that order and the bookings as they were sent."""
    made = order(session, member, now)
    cart = rows(session, member)
    if made is not None and made.state == HELD:
        held = Quote(HELD)
        return Cart(tuple(CartItem(row, held) for row in cart), made)
    found = cart_quotes(member=member, cart=[row.choice for row in cart], now=now)
    return Cart(
        tuple(CartItem(row, item) for row, item in zip(cart, found, strict=True)),
        made,
    )


def _locked(session: SessionBase, member: Member, now: datetime | None) -> bool:
    made = order(session, member, now)
    return made is not None and made.state == HELD


def _admit(
    member: Member,
    others: Sequence[CartRow],
    choice: Choice,
    seen: int,
    now: datetime | None,
) -> Quote | Refusal:
    # Early, for the booking page; the order's own count is the one that holds
    # (`ordering._attempt`).
    organization = Organization.objects.get(pk=member.organization_id)
    moment = now if now is not None else timezone.now()
    # Counted once: none left with the cart's lines, and whether some were left
    # without them, the cart then what fills the day.
    left = booking_limits.left_today(member, organization, moment)
    if left is not None and left <= len(others):
        return Refusal(
            booking_words.DAY_LIMIT,
            left=0,
            limit=organization.max_bookings_a_day,
            in_cart=left > 0,
        )
    found = quote(
        member=member,
        configuration_id=choice.configuration_id,
        booking_category_id=choice.booking_category_id,
        quantity=choice.quantity,
        starts_at=choice.starts_at,
        ends_at=choice.ends_at,
        now=now,
        cart=[row.choice for row in others],
        spots=choice.spots,
        group_id=choice.group_id,
    )
    if found.reason is not None:
        return Refusal(found.reason)
    if found.total != seen:
        return Refusal(PRICE_CHANGED, found.total)
    return found


def add(
    session: SessionBase,
    member: Member,
    choice: Choice,
    seen: int,
    now: datetime | None = None,
) -> CartRow | Refusal:
    """Put one booking in the cart, at the total the member saw."""
    choice = _check(choice)
    if _locked(session, member, now):
        return Refusal(ORDER_HELD)
    cart = rows(session, member)
    if len(cart) >= MAX_ROWS:
        return Refusal(CART_FULL)
    found = _admit(member, cart, choice, seen, now)
    if isinstance(found, Refusal):
        return found
    taken = {row.id for row in cart}
    # Long enough never to meet a row of the member's earlier orders, which
    # the cart's order is found by .
    row_id = secrets.token_hex(8)
    while row_id in taken:
        row_id = secrets.token_hex(8)
    row = CartRow(row_id, choice, found.total)
    _save(session, member, [*cart, row])
    return row


def change(
    session: SessionBase,
    member: Member,
    row_id: str,
    choice: Choice,
    seen: int,
    now: datetime | None = None,
) -> CartRow | Refusal | None:
    """Replace one booking, keeping its place; None when it is not in the cart.

    A refused change leaves the booking as it was.
    """
    choice = _check(choice)
    if _locked(session, member, now):
        return Refusal(ORDER_HELD)
    cart = rows(session, member)
    index = next((i for i, row in enumerate(cart) if row.id == row_id), None)
    if index is None:
        return None
    found = _admit(member, cart[:index] + cart[index + 1 :], choice, seen, now)
    if isinstance(found, Refusal):
        return found
    row = CartRow(row_id, choice, found.total)
    _save(session, member, [*cart[:index], row, *cart[index + 1 :]])
    return row


def remove(
    session: SessionBase,
    member: Member,
    row_id: str,
    now: datetime | None = None,
) -> bool | Refusal:
    if _locked(session, member, now):
        return Refusal(ORDER_HELD)
    cart = rows(session, member)
    kept = [row for row in cart if row.id != row_id]
    if len(kept) == len(cart):
        return False
    _save(session, member, kept)
    return True


def empty(
    session: SessionBase, member: Member, now: datetime | None = None
) -> Refusal | None:
    if _locked(session, member, now):
        return Refusal(ORDER_HELD)
    _save(session, member, [])
    return None


def without(
    session: SessionBase, member: Member, row_id: str | None
) -> list[Choice] | None:
    """The cart's choices less the one being changed; None when it is gone."""
    cart = rows(session, member)
    if row_id is not None and all(row.id != row_id for row in cart):
        return None
    return [row.choice for row in cart if row.id != row_id]
