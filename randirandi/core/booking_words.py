# SPDX-License-Identifier: AGPL-3.0-only
"""The refusal and order-state keys of the booking page .

The server sends a key and its default English text; the page shows the
organization's own words for the key when it has them, and the default
otherwise. The default is written once, in the label set
; this module names the keys.
"""

from typing import TYPE_CHECKING

from randirandi import labels

if TYPE_CHECKING:
    from randirandi.core.models import Organization

NOT_OFFERED = "not-offered"
TAKEN = "taken"
# The booking would fit if the member's own cart were not there.
BESIDE_CART = "does-not-fit-beside-cart"
# The start is already behind the moment of the answer.
STARTED = "already-started"
PRICE_CHANGED = "price-changed"
CART_FULL = "cart-full"
# An order is one payment in one currency : the booking is priced
# in another currency than the cart's paid bookings.
OTHER_CURRENCY = "other-currency"
ORDER_HELD = "order-held"
NOT_READY = "not-ready"
CART_EMPTY = "cart-empty"
TOO_LARGE = "too-large"
BUSY = "busy"
# The member's bookings made today would pass the organization's limit for one
# day; the refusal carries the limit, in `limit`, and how many are left, in
# `left`. Its text is counted by the limit, one text when none is left and one
# when some are (T3, 2026-11-15: the old text read as the chosen date full),
# and a third when the cart's own lines are what leave none: nothing is used
# yet, and the cart can still be ordered today (seen rendered, 2026-11-16).
DAY_LIMIT = "day-limit"
# Its texts, by whether any are left today, or the cart leaves none, and the
# limit's counted form.
DAY_LIMIT_TEXTS = {
    ("used", "one"): "day-limit.used.one",
    ("used", "other"): "day-limit.used.other",
    ("left", "one"): "day-limit.left.one",
    ("left", "other"): "day-limit.left.other",
    ("cart", "one"): "day-limit.cart.one",
    ("cart", "other"): "day-limit.cart.other",
}
# The start is after the last date the organization lets a member book.
TOO_FAR_AHEAD = "too-far-ahead"
# The order call did not carry the booking conditions in force : none, or
# the text of an earlier moment.
CONDITIONS_NOT_ACCEPTED = "conditions-not-accepted"
CONDITIONS_CHANGED = "conditions-changed"
# An order's state, as the cart and the outcome read it ;
# settling is an order whose payment is being read at the provider or captured.
HELD, CONFIRMED, CANCELLED = "held", "confirmed", "cancelled"
NOT_COMPLETED = "not-completed"
SETTLING = "settling"

DEFAULT_TEXT = {
    key: labels.DEFAULTS[key]
    for key in (
        NOT_OFFERED,
        TAKEN,
        BESIDE_CART,
        STARTED,
        PRICE_CHANGED,
        CART_FULL,
        OTHER_CURRENCY,
        ORDER_HELD,
        NOT_READY,
        CART_EMPTY,
        TOO_LARGE,
        BUSY,
        TOO_FAR_AHEAD,
        CONDITIONS_NOT_ACCEPTED,
        CONDITIONS_CHANGED,
        HELD,
        CONFIRMED,
        CANCELLED,
        NOT_COMPLETED,
        SETTLING,
    )
}


# The word a floor plan has when none was written for it: an asset made
# without a floor plan gets one of a single spot. It names nothing a member
# should read, so it is sent as no word.
DEFAULT_SPOT_WORD = "booking"


def spot_word(unit: str | None) -> str | None:
    """A floor plan's word for its spots as the member pages get it: None for
    the default word, and for no floor plan; the page then uses the label
    `spot.word`."""
    return None if unit in (None, DEFAULT_SPOT_WORD) else unit


def text(key: str, /, *, org: Organization | None = None, **values: object) -> str:
    """The text of a key in the organization's language (English for none),
    its placeholders filled by `values`; empty for an unknown key (a booking
    that is fine has the state "ok", which has none). The day's limit is
    counted: it needs `limit` and `left`, and `in_cart` true when the cart's
    own lines are what leave none."""
    if key == DAY_LIMIT:
        limit, left = values["limit"], values["left"]
        if not isinstance(limit, int) or not isinstance(left, int):
            raise TypeError("The day's limit text needs a whole limit and left.")
        kind = "left" if left > 0 else "cart" if values.get("in_cart") else "used"
        key = DAY_LIMIT_TEXTS[kind, labels.plural_form(limit)]
        return labels.word(org, key, count=limit, left=left)
    return labels.word(org, key, **values) if key in DEFAULT_TEXT else ""
