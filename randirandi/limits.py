# SPDX-License-Identifier: AGPL-3.0-only
"""Limits the setup file and the models both hold, kept free of Django."""

# The most spots one floor plan holds : a load
# makes a row for each, and the calendar looks at each. It bounds that cost;
# the application holds it, not a database rule.
MOST_SPOTS = 10_000
TOO_MANY_SPOTS = f"must be at most {MOST_SPOTS:,}, the most spots a floor plan holds."

# The currencies the payment client takes (`vipps.py`), and so the ones an
# organization and a price book may have (`models.organization.Currency`, which
# a test holds to this list).
CURRENCIES = ("NOK", "DKK", "EUR")

# The most an organization's tax rate may be, in hundredths of a percent: all
# of the price (`models.organization`, the constraint; the setup file).
MOST_TAX_RATE = 10_000

# The days of free cancellation an organization has until it sets its own
# , and the most it
# may set .
DEFAULT_FREE_CANCELLATION_DAYS = 5

# One plain address: a dot-atom local part, "@", and a domain of dotted labels
# with at least one dot (no quoted local part, no comment, no bare host).
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
PLAIN_ADDRESS = rf"{_ATOM}(?:\.{_ATOM})*@(?:{_LABEL}\.)+[A-Za-z]{{2,63}}"
MOST_FREE_CANCELLATION_DAYS = 365

# The icons a booking category may have , the short list the
# member page maps to an icon of its own; blank is no icon.
CATEGORY_ICONS = ("person", "work", "building", "home", "group")

# The name of a booking type, booking category or role : one rule
# for the setup file, the admin screens and the column. Text, not empty, no
# space at either end, at most as long as the column; a list holds a name once,
# whatever its case.
LONGEST_NAME = 200


def name_fault(value: object) -> str | None:
    if type(value) is not str:
        return "must be a text."
    if not value or value != value.strip():
        return "must not be empty, or start or end with a space."
    if len(value) > LONGEST_NAME:
        return f"must be at most {LONGEST_NAME} characters."
    return None


def same_name(one: str, other: str) -> bool:
    return one.lower() == other.lower()


# The word for a floor plan's spots and for a group of spots :
# text, not empty, no space at either end, at most as long as the column.
LONGEST_WORD = 80


def word_fault(value: object) -> str | None:
    if type(value) is not str:
        return "must be a text."
    if not value or value != value.strip():
        return "must not be empty, or start or end with a space."
    if len(value) > LONGEST_WORD:
        return f"must be at most {LONGEST_WORD} characters."
    return None


# The most spots one request adds (the admin screens' Add), under the most a
# floor plan holds.
MOST_SPOTS_AT_ONCE = 500
