# SPDX-License-Identifier: AGPL-3.0-only
"""What the member sees in the Vipps app: a description and an itemized receipt.

Both are built from saved rows when the payment intent is made, and saved with
it, so a retry sends the same bytes whatever changed since. Neither carries a
member's name, email or phone.
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from randirandi.core.models import Booking, Order, Organization

logger = logging.getLogger(__name__)
# Vipps' own limit for the description is unknown; the names get the same.
LIMIT = 100


def cut(text: str, limit: int = LIMIT) -> str:
    """At most `limit` characters, cut on a word."""
    if len(text) <= limit:
        return text
    if text[limit] == " ":
        return text[:limit]
    words = text[:limit].rsplit(" ", 1)
    return (words[0] if len(words) == 2 else text[:limit]).rstrip(",:;")


def when(starts_at: datetime, ends_at: datetime, zone: ZoneInfo) -> str:
    """`23.10.2026 17:00-20:00` (an en dash), in the zone given: the booking's
    asset's."""
    start, end = starts_at.astimezone(zone), ends_at.astimezone(zone)
    first = start.strftime("%d.%m.%Y %H:%M")
    if start.date() == end.date():
        return f"{first}\u2013{end:%H:%M}"
    return f"{first}\u2013{end:%d.%m.%Y %H:%M}"


def tax_split(total: int, rate: int) -> tuple[int, int]:
    """Amount without tax and the tax, which add up to `total`.

    `rate` is in hundredths of a percent. The tax is rounded half up.
    """
    divisor = 10000 + rate
    tax = (2 * total * rate + divisor) // (2 * divisor)
    return total - tax, tax


def build(organization: Organization, order: Order) -> tuple[str, str]:
    """The description, and the receipt as JSON text (empty when refused)."""
    bookings: list[Booking] = list(
        order.bookings.select_related("taken_time")
        .prefetch_related("lines__price_book")
        .order_by("taken_time__starts_at", "pk")
    )
    if not bookings:
        raise ValueError("An order without bookings has no description.")
    # Each booking in the zone of its own asset, which its claim keeps.
    times = [
        when(
            row.taken_time.starts_at,
            row.taken_time.ends_at,
            ZoneInfo(row.taken_time.time_zone),
        )
        for row in bookings
    ]
    description = f"{organization.name}, reference {order.reference}: {times[0]}"
    if len(times) > 1:
        description += f" and {len(times) - 1} more"
    lines: list[dict[str, str | int]] = []
    paid = 0
    for position, (row, time) in enumerate(zip(bookings, times, strict=True), 1):
        total = sum(line.total for line in row.lines.all())
        paid += total
        without_tax, tax = tax_split(total, organization.tax_rate)
        lines.append(
            {
                "name": cut(f"{row.booking_type_name}, {time}"),
                "id": str(position),
                "totalAmount": total,
                "totalAmountExcludingTax": without_tax,
                "totalTaxAmount": tax,
                "taxRate": organization.tax_rate,
            }
        )
    # The currency of the price books the order was priced in: one.
    currencies = {
        line.price_book.currency for row in bookings for line in row.lines.all()
    }
    if paid != order.total or len(currencies) != 1:
        logger.error("payment receipt refused: order=%d", order.pk)
        return cut(description), ""
    receipt = {
        "orderLines": lines,
        "bottomLine": {
            "currency": currencies.pop(),
            "receiptNumber": order.reference,
        },
    }
    return cut(description), json.dumps(receipt, separators=(",", ":"))
