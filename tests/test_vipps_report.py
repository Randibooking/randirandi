# SPDX-License-Identifier: AGPL-3.0-only
"""The report parser  on every shape Vipps's documentation shows:
the ledgers reply, a day's page with and without more to come, an entry with
an unknown type, amounts as integers and as strings, and what it refuses.
Fictional replies; nothing is sent to Vipps."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from randirandi import vipps_report
from randirandi.environment import Environment
from randirandi.vipps import Client, Credentials, VippsError, VippsResponseError


def entry(**over: Any) -> dict[str, Any]:
    """The documentation's example capture, as a reply carries it."""
    return {
        "pspReference": "3343121302",
        "time": "2025-10-01T16:33:00.824993+0200",
        "ledgerDate": "2025-10-01",
        "entryType": "capture",
        "reference": "purchase-12",
        "currency": "NOK",
        "amount": 10000,
        "balanceBefore": 0,
        "balanceAfter": 10000,
        "recipientHandle": "NO:57860",
    } | over


def reply(items: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    return {"items": items, "hasMore": False, "tryLater": False} | over


def test_the_documented_capture_reads_in_minor_units() -> None:
    got = vipps_report.page(reply([entry()]))
    assert got.items == (
        vipps_report.Entry(
            psp_reference="3343121302",
            entry_type="capture",
            reference="purchase-12",
            ledger_date=date(2025, 10, 1),
            at=datetime(2025, 10, 1, 14, 33, 0, 824993, tzinfo=UTC),
            currency="NOK",
            amount=10000,
            balance_before=0,
            balance_after=10000,
        ),
    )
    assert got.items[0].at.utcoffset() == timedelta(hours=2)
    assert got.closed and got.cursor == ""


def test_a_refund_a_retained_fee_and_a_payout_are_negative_and_keep_their_text() -> (
    None
):
    items = [
        entry(
            pspReference="1154320987",
            entryType="refund",
            amount=-10000,
            balanceAfter=30000,
        ),
        entry(
            pspReference="01H7W7Q6Y5R-3G58CTAZX0MHKV2",
            entryType="fees-retained",
            reference="",
            amount=-1200,
        ),
        entry(
            pspReference="12345-2000023",
            entryType="payout-scheduled",
            reference="Vipps utbet. 2000023 Vippsnr 57860",
            amount=-28800,
        ),
    ]
    got = vipps_report.page(reply(items)).items
    assert [e.amount for e in got] == [-10000, -1200, -28800]
    assert got[1].reference == ""
    assert got[2].reference == "Vipps utbet. 2000023 Vippsnr 57860"
    # An entry with no reference at all (the table leaves the cell empty).
    missing = entry(entryType="fees-retained")
    del missing["reference"]
    assert vipps_report.page(reply([missing])).items[0].reference == ""


def test_amounts_and_balances_may_be_numeric_strings_but_never_floats() -> None:
    got = vipps_report.page(
        reply([entry(amount="-49900", balanceBefore="0", balanceAfter="-49900")])
    ).items[0]
    assert (got.amount, got.balance_before, got.balance_after) == (-49900, 0, -49900)
    for bad in (499.0, "499.00", "4,99", "", " 5", "1e3", True, None, [5], "٣"):
        with pytest.raises(VippsResponseError):
            vipps_report.page(reply([entry(amount=bad)]))
    with pytest.raises(VippsResponseError):
        vipps_report.page(reply([entry(balanceBefore=0.0)]))
    with pytest.raises(VippsResponseError):
        vipps_report.page(reply([entry(amount=10**16)]))


def test_an_entry_type_nobody_knows_is_kept_as_it_came() -> None:
    got = vipps_report.page(
        reply(
            [
                entry(entryType="retained-disputed-capture"),
                entry(entryType="a-new-one-2031"),
            ]
        )
    ).items
    assert [e.entry_type for e in got] == [
        "retained-disputed-capture",
        "a-new-one-2031",
    ]


def test_more_to_come_needs_its_cursor_and_try_later_keeps_the_day_open() -> None:
    first = vipps_report.page(reply([entry()], hasMore=True, cursor="next-1"))
    assert first.has_more and first.cursor == "next-1" and not first.closed
    with pytest.raises(VippsResponseError):
        vipps_report.page(reply([entry()], hasMore=True))
    open_day = vipps_report.page(reply([entry()], tryLater=True))
    assert open_day.try_later and not open_day.closed
    empty = vipps_report.page(reply([]))
    assert empty.items == () and empty.closed
    for bad in ("true", 1, None):
        with pytest.raises(VippsResponseError):
            vipps_report.page(reply([], hasMore=bad))


def test_what_the_documentation_does_not_allow_is_refused() -> None:
    bad: object
    for bad in (
        None,
        [],
        "x",
        {},
        {"items": None},
        {"items": [None]},
        {"items": ["x"]},
        reply([entry(pspReference="")]),
        reply([entry(pspReference=5)]),
        reply([entry(entryType="")]),
        reply([entry(time="2025-10-01T16:33:00")]),  # no offset
        reply([entry(time="yesterday")]),
        reply([entry(time=None)]),
        reply([entry(ledgerDate="2025-13-01")]),
        reply([entry(ledgerDate="20251001")]),
        reply([entry(ledgerDate=None)]),
        reply([entry(currency="SEK")]),
        reply([entry(currency=None)]),
        reply([entry(reference=7)]),
        reply([entry(pspReference="x" * 101)]),
    ):
        with pytest.raises(VippsResponseError):
            vipps_report.page(bad)
    for field in (
        "pspReference",
        "time",
        "ledgerDate",
        "entryType",
        "currency",
        "amount",
        "balanceBefore",
        "balanceAfter",
    ):
        short = entry()
        del short[field]
        with pytest.raises(VippsResponseError):
            vipps_report.page(reply([short]))
    with pytest.raises(VippsResponseError):
        vipps_report.page(reply([entry()] * (vipps_report.MAX_ITEMS + 1)))


def test_the_personal_fields_are_never_read() -> None:
    got = vipps_report.page(
        reply([entry(message="a note", name="A Person", maskedPhoneNo="*** ** 123")])
    ).items[0]
    assert not any(value in repr(got) for value in ("a note", "A Person", "123"))
    assert not hasattr(got, "name") and not hasattr(got, "message")


def test_a_time_with_a_colon_in_the_offset_reads_too() -> None:
    got = vipps_report.page(reply([entry(time="2025-10-02T00:00:00.000000+02:00")]))
    assert got.items[0].at == datetime(2025, 10, 1, 22, 0, tzinfo=UTC)
    assert got.items[0].at.astimezone(timezone(timedelta(hours=2))).hour == 0


def ledger(**over: Any) -> dict[str, Any]:
    return {
        "ledgerId": "302321",
        "currency": "NOK",
        "payoutBankAccount": {"scheme": "BBAN", "id": "12345678901"},
        "owner": {"scheme": "business-no-orgno", "id": "987654321"},
        "settlesForRecipientHandles": ["NO:57860"],
        "salesUnits": [
            {
                "name": "Shop",
                "recipientHandle": "NO:57860",
                "businessIdentifier": {
                    "scheme": "business-no-orgno",
                    "id": "987654321",
                },
            }
        ],
    } | over


def test_the_ledgers_reply_and_which_ledger_settles_a_merchant_number() -> None:
    found, cursor = vipps_report.ledgers({"items": [ledger()], "cursor": ""})
    assert cursor == ""
    assert found == (vipps_report.Ledger("302321", "NOK", ("NO:57860", "NO:57860")),)
    assert found[0].settles("57860")
    assert not found[0].settles("7860") and not found[0].settles("12345")
    # A numeric id and a reply with no sales units still read.
    got, more = vipps_report.ledgers(
        {
            "items": [
                ledger(
                    ledgerId=302322,
                    salesUnits=[],
                    settlesForRecipientHandles=["DK:1234"],
                )
            ],
            "cursor": "c2",
        }
    )
    assert got[0].ledger_id == "302322" and got[0].settles("1234") and more == "c2"
    bad: object
    for bad in (
        None,
        {"items": [ledger(ledgerId="")]},
        {"items": [ledger(ledgerId="a/b")]},
        {"items": [ledger(currency="SEK")]},
        {"items": [ledger(salesUnits="x")]},
        {"items": [ledger(salesUnits=[{"name": "x"}])]},
        {"items": [ledger(settlesForRecipientHandles="NO:1")]},
        {"items": ["x"]},
        {"cursor": ""},
    ):
        with pytest.raises(VippsResponseError):
            vipps_report.ledgers(bad)


# --- The client: addresses, paging and the read of a day ---


class Wire:
    """A fictional report API: replies by path, calls recorded."""

    def __init__(self, replies: Mapping[str, tuple[int, Any]]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def __call__(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        assert host == "api.vipps.no" and body is None
        if path == "/accesstoken/get":
            return 200, b'{"access_token":"fictional-token","expires_in":"3600"}'
        self.calls.append((method, path, dict(headers)))
        status, data = self.replies[path]
        return status, json.dumps(data).encode()


def client(wire: Wire) -> Client:
    credentials = Credentials("id", "secret", "key", "57860")
    return Client(Environment.PRODUCTION, credentials, transport=wire)


def test_ledgers_are_read_page_by_page_with_the_key_of_the_sales_unit() -> None:
    wire = Wire(
        {
            "/settlement/v1/ledgers": (200, {"items": [ledger()], "cursor": "p 2"}),
            "/settlement/v1/ledgers?cursor=p%202": (
                200,
                {"items": [ledger(ledgerId="9")], "cursor": ""},
            ),
        }
    )
    got = vipps_report.read_ledgers(client(wire))
    assert [x.ledger_id for x in got] == ["302321", "9"]
    method, path, headers = wire.calls[0]
    assert (method, path) == ("GET", "/settlement/v1/ledgers")
    assert headers["Authorization"] == "Bearer fictional-token"
    assert headers["Merchant-Serial-Number"] == "57860"
    assert "Idempotency-Key" not in headers
    loop = Wire(
        {
            "/settlement/v1/ledgers": (200, {"items": [], "cursor": "same"}),
            "/settlement/v1/ledgers?cursor=same": (
                200,
                {"items": [], "cursor": "same"},
            ),
        }
    )
    with pytest.raises(VippsResponseError):
        vipps_report.read_ledgers(client(loop))


def test_a_day_is_read_through_its_pages_and_closes_on_the_last() -> None:
    base = "/report/v2/ledgers/302321/funds/dates/2025-10-01"
    wire = Wire(
        {
            base: (200, reply([entry()], hasMore=True, tryLater=True, cursor="c1")),
            base + "?cursor=c1": (200, reply([entry(pspReference="2")])),
        }
    )
    kept: list[int] = []
    closed = vipps_report.read_day(
        client(wire),
        "302321",
        "funds",
        date(2025, 10, 1),
        max_pages=5,
        keep=lambda page: kept.append(len(page.items)),
    )
    assert closed and kept == [1, 1]
    assert [path for _, path, _ in wire.calls] == [base, base + "?cursor=c1"]
    assert all("includeGDPRSensitiveData" not in path for _, path, _ in wire.calls)


def test_a_day_that_is_still_open_or_too_long_is_not_closed() -> None:
    base = "/report/v2/ledgers/302321/fees/dates/2025-10-01"
    open_day = Wire({base: (200, reply([entry()], tryLater=True))})
    assert not vipps_report.read_day(
        client(open_day),
        "302321",
        "fees",
        date(2025, 10, 1),
        max_pages=3,
        keep=lambda p: None,
    )
    endless = Wire(
        {
            base: (200, reply([], hasMore=True, cursor="a")),
            base + "?cursor=a": (200, reply([], hasMore=True, cursor="a")),
        }
    )
    assert not vipps_report.read_day(
        client(endless),
        "302321",
        "fees",
        date(2025, 10, 1),
        max_pages=3,
        keep=lambda p: None,
    )
    assert len(endless.calls) == 3


def test_a_refusal_or_a_bad_ledger_or_topic_raises_without_a_body() -> None:
    wire = Wire(
        {"/report/v2/ledgers/1/funds/dates/2025-10-01": (404, {"detail": "secret"})}
    )
    with pytest.raises(VippsError) as refused:
        vipps_report.read_page(client(wire), "1", "funds", date(2025, 10, 1))
    assert refused.value.status == 404 and "secret" not in str(refused.value)
    for ledger_id, topic in (("../x", "funds"), ("1", "payouts"), ("", "funds")):
        with pytest.raises(ValueError, match="ledger"):
            vipps_report.read_page(client(wire), ledger_id, topic, date(2025, 10, 1))
