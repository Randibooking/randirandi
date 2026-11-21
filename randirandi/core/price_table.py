# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: RUF001
"""The price list as a table, worked out on the server .

The price list page draws itself in the browser (`frontend/src/book/prices.js`
`priceBook`, `PricesPage.jsx`) from the public calendar answers of the standard
role. Without JavaScript the same page is drawn here, from the same answers,
by the same steps: `price_book` is `priceBook` in Python and returns the same
data (`frontend/src/book/price-book.golden.json` holds the answers and the
data both sides must reach: `frontend/src/book/prices.test.js` checks the JavaScript,
`tests/test_public_price_table.py` this module). Nothing of a member is in it:
the answers hold none (`booking_views.calendar_answer`, `public`).
"""

import json
import re
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.http import HttpRequest, QueryDict
from django.utils import timezone

from randirandi import labels
from randirandi.core import booking_views
from randirandi.core.models import Member, Organization

Data = dict[str, Any]

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_SYMBOLS = {"NOK": "kr", "DKK": "DKK", "EUR": "€"}
# Two weeks hold every weekday of every container (PricesPage.jsx DAYS).
DAYS = booking_views.PUBLIC_DAYS


def keep(text: str) -> str:
    """Amounts, lengths and hours never break inside (PricesPage.jsx `keep`)."""
    text = re.sub(r"(\d) (?=\d|h\b|kr\b|min\b)", "\\1\u00a0", text)
    return re.sub(r"(\d)–(\d)", "\\1\u2060–\u2060\\2", text)


class Words:
    """The organization's words for the labels the list uses."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    def __call__(self, key: str, **values: object) -> str:
        return labels.word(self.organization, key, **values)

    def plural(self, key: str, count: int, **values: object) -> str:
        """A counted text, `<key>.one` or `<key>.other`."""
        return labels.plural(self.organization, key, count, **values)


def _clock(minute: int) -> str:
    return f"{minute // 60 % 24:02d}:{minute % 60:02d}"


def _wall(moment: str) -> int:
    return int(moment[11:13]) * 60 + int(moment[14:16])


def _hhmm(moment: str) -> str:
    return moment[11:16]


def _weekday(day: str) -> int:
    return date.fromisoformat(day).weekday()


def _length(minutes: int, word: Words) -> str:
    hours, rest = divmod(minutes, 60)
    parts = [
        word.plural("length.hours", hours) if hours else "",
        word("length.minutes", count=rest) if rest else "",
    ]
    return " ".join(part for part in parts if part) or word("length.minutes", count=0)


def _money(amount: int, currency: str) -> str:
    whole, cents = divmod(amount, 100)
    text = f"{whole:,}".replace(",", " ") + (f",{cents:02d}" if cents else "")
    return f"{text} {_SYMBOLS.get(currency, currency)}"


def _weekday_name(index: int, word: Words) -> str:
    return word(f"weekdays.{WEEKDAYS[index]}")


def days_words(indices: list[int], word: Words) -> str:
    days = set(indices)
    if len(days) == 7:
        return word("prices.every-day")
    runs = []
    for day in range(7):
        if day not in days or (day + 6) % 7 in days:
            continue
        end = day
        while (end + 1) % 7 in days:
            end = (end + 1) % 7
        runs.append((day, end))
    out = []
    for first, last in runs:
        count = (last - first + 7) % 7 + 1
        if count == 1:
            out.append(_weekday_name(first, word))
        else:
            key = "prices.days-pair" if count == 2 else "prices.days-range"
            out.append(
                word(
                    key,
                    first=_weekday_name(first, word),
                    last=_weekday_name(last, word),
                )
            )
    return ", ".join(out)


def _rule_lines(
    row: Data, container: Data, night: bool, currency: str, word: Words
) -> list[str]:
    tiers = row["tiers"]

    def money(amount: int) -> str:
        return _money(amount, currency)

    if all(tier["amount"] == 0 for tier in tiers):
        return [word("prices.free")]
    if container["period_type"] == "fixed":
        return [
            word(
                "prices.night" if night else "prices.booking",
                amount=money(tiers[0]["amount"]),
            )
        ]
    if row["tiers_go_by"] == "length":
        lines = []
        for index, tier in enumerate(tiers):
            if index == 0 and tier["paid_per"] >= tier["up_to"]:
                lines.append(
                    word(
                        "prices.first",
                        amount=money(tier["amount"]),
                        length=_length(tier["up_to"], word),
                    )
                )
                continue
            lines.append(
                word(
                    "prices.per-started" if index == 0 else "prices.then",
                    amount=money(tier["amount"]),
                    length=_length(tier["paid_per"], word),
                )
            )
        if container["maximum_length"]:
            lines.append(
                word("prices.most", length=_length(container["maximum_length"], word))
            )
        return lines
    lines = []
    previous = None
    for tier in tiers:
        start = previous
        previous = tier["up_to"]
        lines.append(
            word(
                "prices.until" if start is None else "prices.clock",
                **{"from": _clock(start or 0)},
                to=_clock(tier["up_to"]),
                amount=money(tier["amount"]),
                length=_length(tier["paid_per"], word),
            )
        )
    return lines


def _own_hours(container: Data, category: int, word: Words) -> list[str]:
    by_words: dict[str, set[int]] = {}
    for period in container["periods"]:
        own = next(
            (
                item
                for item in period["category_hours"]
                if int(item["category"]) == int(category)
            ),
            None,
        )
        if own is None:
            continue
        words = ", ".join(f"{_hhmm(a)}–{_hhmm(b)}" for a, b in own["spans"])
        by_words.setdefault(words, set()).add(_weekday(period["day"]))
    found = [
        (
            min(days),
            word("prices.day-hours", days=days_words(sorted(days), word), hours=hours),
        )
        for hours, days in by_words.items()
    ]
    return [line for _, line in sorted(found, key=lambda one: one[0])]


def _tier_parts(
    tiers: list[Data], goes_by: str, start: int, end: int
) -> list[tuple[Data, int]] | None:
    parts = []
    previous = 0
    length = end - start
    for tier in tiers:
        if goes_by == "length":
            minutes = min(length, tier["up_to"]) - previous
        else:
            minutes = min(end, tier["up_to"]) - max(start, previous)
        if minutes > 0:
            parts.append((tier, minutes))
        previous = tier["up_to"]
    reach = length if goes_by == "length" else end
    return None if reach > previous else parts


def _spans_of(container: Data, row: Data, start: int) -> list[tuple[int, int]]:
    if (
        container["period_type"] == "fixed"
        or row["groups"]
        or all(tier["amount"] == 0 for tier in row["tiers"])
    ):
        return []
    boundary = row["tiers"][0]["up_to"]
    if row["tiers_go_by"] == "length":
        return [
            (start, start + boundary),
            (start, start + boundary + container["interval"]),
        ]
    return [
        (start, min(start + 4 * 60, boundary)),
        (boundary - 2 * 60, boundary + 2 * 60),
    ]


def _span_total(
    container: Data, row: Data | None, span: tuple[int, int], currency: str, word: Words
) -> str | None:
    if row is None:
        return None
    first, last = span
    if (
        last - first < container["minimum_length"]
        or last - first > container["maximum_length"]
    ):
        return None
    if all(tier["amount"] == 0 for tier in row["tiers"]):
        return word("price.free")
    parts = _tier_parts(row["tiers"], row["tiers_go_by"], first, last)
    if parts is None:
        return None
    total = sum(
        -(-minutes // tier["paid_per"]) * tier["amount"] for tier, minutes in parts
    )
    return _money(total, currency) if total else word("price.free")


def _tiers_of(container: Data) -> Callable[[Data], Data | None]:
    """The container's tiers for a category column, or None."""
    prices = container.get("prices")

    def tiers(category: Data) -> Data | None:
        if not prices:
            return None
        return next(
            (
                one
                for one in prices["categories"]
                if int(one["category"]) == int(category["id"])
            ),
            None,
        )

    return tiers


def _row(tiers_of: Callable[[Data], Data | None], category: Data) -> Data:
    """The tiers of a category that is offered with payment: there are some."""
    row = tiers_of(category)
    assert row is not None  # noqa: S101
    return row


def _offered(
    container: Data, tiers_of: Callable[[Data], Data | None]
) -> Callable[[Data], bool]:
    """Whether the container takes the category column, and prices it."""
    takes = {int(one) for one in container.get("categories") or []}

    def offered(category: Data) -> bool:
        return int(category["id"]) in takes and (
            not container["with_payment"] or tiers_of(category) is not None
        )

    return offered


def price_book(
    answers: list[Data], categories: list[Data], order: list[str], word: Words
) -> list[Data]:
    """`priceBook` of prices.js: one section per booking type in the
    organization's order, a row per container, a cell per category, then
    worked examples priced in every column."""
    by_id: dict[int, Data] = {}
    for answer in answers:
        for container in answer["containers"]:
            was = by_id.get(container["id"])
            if was is None:
                by_id[container["id"]] = {
                    **container,
                    "periods": list(container["periods"]),
                }
            else:
                for period in container["periods"]:
                    if not any(one["day"] == period["day"] for one in was["periods"]):
                        was["periods"].append(period)
    types: dict[str, Data] = {}
    for container in sorted(by_id.values(), key=lambda one: one["number"]):
        if not container["periods"]:
            continue
        longest = container["periods"][0]
        for one in container["periods"]:
            if len(one["rows"]) > len(longest["rows"]):
                longest = one
        night = longest["end"][:10] > longest["day"]
        days = days_words(
            sorted({_weekday(one["day"]) for one in container["periods"]}), word
        )
        prices = container.get("prices")
        currency = prices["currency"] if prices else ""
        tiers_of = _tiers_of(container)
        offered = _offered(container, tiers_of)

        cells: list[Data] = []
        for category in categories:
            if not offered(category):
                cells.append({"category": category, "offered": False})
                continue
            lines = (
                _rule_lines(_row(tiers_of, category), container, night, currency, word)
                if container["with_payment"]
                else [word("prices.free")]
            )
            cells.append(
                {
                    "category": category,
                    "offered": True,
                    "lines": lines,
                    "hours": _own_hours(container, category["id"], word),
                }
            )
        start = _wall(longest["rows"][0])
        spans: dict[str, tuple[int, int]] = {}
        if container["with_payment"]:
            for category in (one for one in categories if offered(one)):
                for span in _spans_of(container, _row(tiers_of, category), start):
                    spans[f"{span[0]}-{span[1]}"] = span
        examples = []
        for first, last in sorted(spans.values())[:3]:
            totals = [
                _span_total(
                    container, tiers_of(category), (first, last), currency, word
                )
                if offered(category)
                else None
                for category in categories
            ]
            if any(totals):
                examples.append(
                    {
                        "hours": f"{_clock(first)}–{_clock(last)}",
                        "length": _length(last - first, word),
                        "totals": totals,
                    }
                )
        kind = types.setdefault(
            container["type"]["name"],
            {"name": container["type"]["name"], "night": night, "blocks": []},
        )
        kind["blocks"].append(
            {
                "id": container["id"],
                "days": word("prices.nights", days=days) if night else days,
                "hours": f"{_hhmm(longest['rows'][0])}–{_hhmm(longest['end'])}",
                "cells": cells,
                "examples": examples,
            }
        )

    def rank(kind: Data) -> int:
        return order.index(kind["name"]) if kind["name"] in order else len(order)

    return sorted(types.values(), key=rank)


def type_order(choices: Data) -> list[str]:
    """`typeOrder` of cart.js."""
    names: list[str] = []
    for asset in choices.get("assets") or []:
        for kind in asset.get("types") or []:
            if kind["name"] not in names:
                names.append(kind["name"])
    return names


def first_page(choices: Data) -> tuple[dict[str, int], str] | None:
    """The page Book opens on (`pagesOf`, `pageOf` of filter.js with no
    address): the first asset's; the space whole when it is shown whole.
    The calendar's query and the page's name."""
    assets = choices.get("assets") or []
    if not assets or not choices.get("categories"):
        return None
    spaces = {space["id"]: space for space in choices.get("spaces") or []}
    asset = assets[0]
    space = spaces.get(asset["space"])
    if space is None:
        return {"asset": asset["id"]}, asset["name"]
    if space["booking_page"] == "whole_space":
        return {"space": space["id"]}, space["name"]
    return {"asset": asset["id"]}, space["name"]


def table(
    org: Organization, member: Member | None, choices: Data, categories: list[Data]
) -> Data | None:
    """The rows `PricesPage` draws: the standard role's columns and prices, on the
    page of `choices`, which are the choices of the role the app reads them for.
    `member` is the signed-in member, or without one the standard role's
    stand-in (`public_pages._stand_in`). None when the page has nothing to
    draw: the app then shows its error, this page nothing."""
    page = first_page(choices)
    if member is None or page is None or not categories:
        return None
    query, name = page
    start = timezone.now().astimezone(ZoneInfo(org.time_zone)).date()
    answers = []
    for category in categories:
        ask = HttpRequest()
        ask.GET = QueryDict(
            urlencode(
                {
                    **query,
                    "category": category["id"],
                    "start": start.isoformat(),
                    "days": DAYS,
                    "prices": 1,
                }
            )
        )
        response = booking_views.calendar_answer(
            ask, member, org, price_list=True, public=True
        )
        if response.status_code != 200:
            return None
        answers.append(json.loads(response.content))
    word = Words(org)
    return {
        "name": name,
        "categories": categories,
        "types": price_book(answers, categories, type_order(choices), word),
    }


def kept(found: Data) -> Data:
    """The table as the page words it: `keep` on what PricesPage.jsx keeps
    (hours, lines and totals); the rows are the same."""
    return {
        **found,
        "types": [
            {
                **kind,
                "blocks": [
                    {
                        **block,
                        "hours": keep(block["hours"]),
                        "cells": [
                            {
                                **cell,
                                **(
                                    {
                                        "lines": [keep(line) for line in cell["lines"]],
                                        "hours": [keep(line) for line in cell["hours"]],
                                    }
                                    if cell["offered"]
                                    else {}
                                ),
                            }
                            for cell in block["cells"]
                        ],
                        "examples": [
                            {
                                **example,
                                "totals": [
                                    keep(total) if total else total
                                    for total in example["totals"]
                                ],
                            }
                            for example in block["examples"]
                        ],
                    }
                    for block in kind["blocks"]
                ],
            }
            for kind in found["types"]
        ],
    }
