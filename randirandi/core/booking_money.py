# SPDX-License-Identifier: AGPL-3.0-only
"""Money as the member pages and the messages write it (`moneyWords`)."""

from randirandi.core.models import Booking, Organization

# How the page writes each currency (`moneyWords`: Intl, nb-NO, with plain
# spaces); each of the three has two decimals. A test holds these to the page's.
CURRENCY_WORD = {"NOK": "kr", "DKK": "DKK", "EUR": "\N{EURO SIGN}"}


def money_words(amount: int, currency: str) -> str:
    """An amount in the currency's smallest unit as the page writes it
    (`moneyWords`, nb-NO): "1 250 kr", "125,50 kr", "1 250 DKK", "125,50 €"."""
    if currency not in CURRENCY_WORD:
        raise ValueError(f"No words for the currency {currency}.")
    main, cents = divmod(amount, 100)
    whole = f"{main:,}".replace(",", " ")
    return f"{whole}{f',{cents:02d}' if cents else ''} {CURRENCY_WORD[currency]}"


def currency_of(org: Organization, booking: Booking) -> str:
    """The currency of the booking's price book; the organization's for a
    booking without a line. The booking's lines must be loaded with their
    price books."""
    lines = list(booking.lines.all())
    return lines[0].price_book.currency if lines else org.currency
