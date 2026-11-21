# SPDX-License-Identifier: AGPL-3.0-only
"""Vipps's Report API, read only: the ledgers and one day's entries of one.

The documented replies (developer.vippsmobilepay.com, Report API, "Fetching
report data"): `GET /settlement/v1/ledgers` answers `{"items": [{"ledgerId",
"currency", "salesUnits": [{"recipientHandle"}], ...}], "cursor"}` and
`GET /report/v2/ledgers/{id}/{funds|fees}/dates/{YYYY-MM-DD}` answers
`{"items": [...], "hasMore", "tryLater", "cursor"}`; an entry has `pspReference`,
`time`, `ledgerDate`, `entryType`, `reference`, `currency`, `amount`,
`balanceBefore`, `balanceAfter` and `recipientHandle`, in minor units. Amounts
are accepted as an integer or as a string of digits (a sign allowed), never as a
float. An entry type this module does not know is kept as it came. The GDPR
fields (`message`, `name`, `maskedPhoneNo`) are never asked for and, if they
come, never read.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from randirandi.limits import CURRENCIES
from randirandi.vipps import Client, VippsResponseError

TOPICS = ("funds", "fees")
MAX_ITEMS = 10_000
MAX_LEDGER_PAGES = 20
_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_NUMBER = re.compile(r"-?[0-9]{1,15}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Ledger:
    ledger_id: str
    currency: str
    handles: tuple[str, ...]

    def settles(self, merchant_number: str) -> bool:
        """True when one of the ledger's sales units is the merchant number."""
        return any(handle.endswith(":" + merchant_number) for handle in self.handles)


@dataclass(frozen=True)
class Entry:
    psp_reference: str
    entry_type: str
    reference: str
    ledger_date: date
    at: datetime
    currency: str
    amount: int
    balance_before: int
    balance_after: int


@dataclass(frozen=True)
class Page:
    items: tuple[Entry, ...]
    has_more: bool
    try_later: bool
    cursor: str

    @property
    def closed(self) -> bool:
        """Nothing more is expected for the day: the last page, not open."""
        return not (self.has_more or self.try_later)


def _text(value: Any, maximum: int, *, blank: bool = False) -> str:
    if value is None and blank:
        return ""
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not value and not blank)
        or any(ord(char) < 32 for char in value)
    ):
        raise VippsResponseError()
    return value


def _flag(data: dict[str, Any], name: str) -> bool:
    value = data.get(name, False)
    if type(value) is not bool:
        raise VippsResponseError()
    return value


def _minor(value: Any) -> int:
    """Minor units as an integer, or as a string of digits."""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and _NUMBER.fullmatch(value):
        number = int(value)
    else:
        raise VippsResponseError()
    if abs(number) > 10**15:
        raise VippsResponseError()
    return number


def _currency(value: Any) -> str:
    if not isinstance(value, str) or value not in CURRENCIES:
        raise VippsResponseError()
    return value


def entry(data: Any) -> Entry:
    if not isinstance(data, dict):
        raise VippsResponseError()
    day, moment = data.get("ledgerDate"), data.get("time")
    if (
        not isinstance(day, str)
        or not _DAY.fullmatch(day)
        or not isinstance(moment, str)
    ):
        raise VippsResponseError()
    try:
        at = datetime.fromisoformat(moment)
        ledger_date = date.fromisoformat(day)
    except ValueError:
        raise VippsResponseError() from None
    if at.utcoffset() is None:
        raise VippsResponseError()
    return Entry(
        psp_reference=_text(data.get("pspReference"), 100),
        entry_type=_text(data.get("entryType"), 64),
        reference=_text(data.get("reference"), 200, blank=True),
        ledger_date=ledger_date,
        at=at,
        currency=_currency(data.get("currency")),
        amount=_minor(data.get("amount")),
        balance_before=_minor(data.get("balanceBefore")),
        balance_after=_minor(data.get("balanceAfter")),
    )


def page(data: Any) -> Page:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise VippsResponseError()
    if len(data["items"]) > MAX_ITEMS:
        raise VippsResponseError()
    has_more, try_later = _flag(data, "hasMore"), _flag(data, "tryLater")
    cursor = _text(data.get("cursor"), 2048, blank=True)
    if has_more and not cursor:
        raise VippsResponseError()
    return Page(
        tuple(entry(item) for item in data["items"]), has_more, try_later, cursor
    )


def ledgers(data: Any) -> tuple[tuple[Ledger, ...], str]:
    """The ledgers of one reply and the cursor to the next (empty: the last)."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise VippsResponseError()
    found = []
    for item in data["items"]:
        if not isinstance(item, dict):
            raise VippsResponseError()
        identity = item.get("ledgerId")
        if type(identity) is int:
            identity = str(identity)
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise VippsResponseError()
        units = item.get("salesUnits", [])
        if not isinstance(units, list) or not all(
            isinstance(unit, dict) and isinstance(unit.get("recipientHandle"), str)
            for unit in units
        ):
            raise VippsResponseError()
        handles = [unit["recipientHandle"] for unit in units]
        settles = item.get("settlesForRecipientHandles", [])
        if not isinstance(settles, list) or not all(
            isinstance(handle, str) for handle in settles
        ):
            raise VippsResponseError()
        found.append(
            Ledger(identity, _currency(item.get("currency")), tuple(handles + settles))
        )
    return tuple(found), _text(data.get("cursor"), 2048, blank=True)


def _path(base: str, cursor: str) -> str:
    return base + (f"?cursor={quote(cursor, safe='')}" if cursor else "")


def read_ledgers(client: Client) -> tuple[Ledger, ...]:
    """Every ledger the key can see (a key belongs to one sales unit)."""
    found: list[Ledger] = []
    cursor = ""
    for _ in range(MAX_LEDGER_PAGES):
        got, cursor = ledgers(client.get(_path("/settlement/v1/ledgers", cursor)))
        found.extend(got)
        if not cursor:
            return tuple(found)
    raise VippsResponseError()


def read_page(
    client: Client, ledger_id: str, topic: str, day: date, cursor: str = ""
) -> Page:
    if topic not in TOPICS or not _ID.fullmatch(ledger_id):
        raise ValueError("A ledger and a topic are required.")
    base = f"/report/v2/ledgers/{ledger_id}/{topic}/dates/{day.isoformat()}"
    return page(client.get(_path(base, cursor)))


def read_day(
    client: Client,
    ledger_id: str,
    topic: str,
    day: date,
    *,
    max_pages: int,
    keep: Callable[[Page], None],
) -> bool:
    """Read the day's pages in turn, handing each to `keep`; True when the day
    is closed (the last page came and Vipps expects nothing more)."""
    cursor = ""
    for _ in range(max_pages):
        got = read_page(client, ledger_id, topic, day, cursor)
        keep(got)
        if got.has_more:
            cursor = got.cursor
            continue
        return got.closed
    return False
