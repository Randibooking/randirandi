# SPDX-License-Identifier: AGPL-3.0-only
"""The Payments : the gate, one row per order that has a payment,
the filters and their combination, both orders, the pages, another
organization's payments, the amounts and currency from the record, the 400s,
one look-up event for each page, and the number of queries."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from randirandi.core import admin_views, booking_admin
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import erase_identity
from randirandi.core.models import AuditEvent, Order, Payment, PaymentTrouble
from randirandi.core.payment_trouble import LASTING
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_bookings import (
    FIRST,
    another_member,
    answer,
    one,
    order_of,
    request_for,
)
from tests.test_admin_bookings import (
    made as made,
)
from tests.test_admin_bookings import (
    now as now,
)
from tests.test_admin_shell import sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
LIST = "/admin/payments/list/"


def payments(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    return answer(admin_views.payments(request_for(made, LIST, query)))


def refused(made: made_up.MadeUp, **query: Any) -> int:
    return int(admin_views.payments(request_for(made, LIST, query)).status_code)


def numbers(found: dict[str, Any]) -> list[int]:
    return [row["number"] for row in found["payments"]]


def pay(
    made: made_up.MadeUp,
    *,
    day: int = 0,
    state: str = "captured",
    amount: int = 1000,
    captured: int | None = None,
    refunded: int = 0,
    currency: str = "NOK",
    reference: str | None = None,
    member: Any = None,
) -> Order:
    """An order made `day` days after FIRST, with a payment of its own."""
    order = order_of(
        made,
        member=member,
        created=FIRST + timedelta(days=day),
        total=amount,
    )
    Payment.objects.create(
        organization=made.organization,
        order=order,
        payment_provider="vipps",
        provider_reference=reference or f"vp-{uuid.uuid4().hex[:12]}",
        amount=amount,
        state=state,
        captured_amount=amount if captured is None else captured,
        refunded_amount=refunded,
        currency=currency,
    )
    return order


@pytest.mark.parametrize("path", [LIST])
def test_the_gate_is_the_shells(made: made_up.MadeUp, path: str) -> None:
    signed_out = get(Client(), path)
    assert signed_out.status_code == 302
    assert signed_out.headers["Location"].startswith("/login/")
    member, admin = Client(), Client()
    with application_login():
        assert post(member, "/login/code/", code=request_code(member, made.member))
        assert get(member, path).status_code == 403
        sign_in_administrator(admin, made.administrator)
        assert get(admin, path).status_code == 200
        assert "no-store" in get(admin, path).headers["Cache-Control"]
        assert post(admin, path).status_code == 405
        assert admin.head(path, HTTP_HOST=HOST, secure=True).status_code == 405
        # The page itself is the shell's place.
        assert get(admin, "/admin/payments/").status_code == 200


def test_without_the_second_step_or_from_another_organization(
    made: made_up.MadeUp,
) -> None:
    request = RequestFactory().get(LIST, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made.administrator
    with pytest.raises(PermissionDenied):
        admin_views.payments(request)
    elsewhere = made_up.everything("elsewhere")
    request.session["admin_confirmed"] = True
    request.user = elsewhere.administrator
    with pytest.raises(PermissionDenied):
        admin_views.payments(request)


def test_a_payment_as_the_list_shows_it_from_the_record(made: made_up.MadeUp) -> None:
    order = order_of(
        made, created=datetime(2027, 1, 10, 9, 5, 7, tzinfo=UTC), total=9999
    )
    Payment.objects.create(
        organization=made.organization,
        order=order,
        payment_provider="vipps",
        provider_reference="vp-record-1",
        amount=2500,
        state="refunded",
        captured_amount=2000,
        refunded_amount=500,
        currency="DKK",
    )
    row = next(r for r in payments(made)["payments"] if r["number"] == order.number)
    # The payment's own amounts and currency, not the order's total or the
    # organization's currency; Oslo is UTC+1 in January.
    assert row == {
        "number": order.number,
        "order_reference": order.reference,
        "created": "2027-01-10, 10:05:07",
        "member": "Alex Rivers",
        "state": "refunded",
        "state_label": "Refunded",
        "amount": 2500,
        "captured": 2000,
        "refunded": 500,
        "currency": "DKK",
        "reference": "vp-record-1",
        "trouble": None,
        "missing_refund": False,
        "attention": False,
    }
    assert [s["value"] for s in payments(made)["states"]] == [
        "created", "authorized", "captured", "cancelled", "expired", "refunded",
        "attention",
    ]  # fmt: skip


def test_search_finds_a_payment_by_its_orders_reference(
    made: made_up.MadeUp,
) -> None:
    paid = pay(made)
    found = payments(made, q=paid.reference.lower())
    assert numbers(found) == [paid.number]
    assert found["payments"][0]["order_reference"] == paid.reference
    assert numbers(payments(made, q="zzzzzz-no")) == []


def test_a_free_order_is_not_a_row(made: made_up.MadeUp) -> None:
    free = order_of(made)
    paid = pay(made)
    found = numbers(payments(made))
    assert free.number not in found
    assert made.spare_order.number not in found
    assert paid.number in found
    assert made.order.number in found
    assert payments(made)["count"] == len(found)


def test_each_state_is_a_filter_and_its_word(made: made_up.MadeUp) -> None:
    words = booking_admin.PAYMENT_WORDS
    made_by = {state: pay(made, state=state, day=i) for i, state in enumerate(words)}
    for state, label in words.items():
        found = payments(made, state=state)
        assert made_by[state].number in numbers(found)
        assert {r["state"] for r in found["payments"]} == {state}
        assert {r["state_label"] for r in found["payments"]} == {label}
    assert {made_by[s].number for s in words} <= set(numbers(payments(made)))


def test_created_one_day_a_period_and_the_organizations_zone(
    made: made_up.MadeUp,
) -> None:
    early = pay(made, day=0)
    middle = pay(made, day=3)
    late = pay(made, day=6)
    # 23:30 UTC on the 12th is 00:30 on the 13th in Oslo.
    edge = order_of(made, created=datetime(2027, 1, 12, 23, 30, tzinfo=UTC))
    Payment.objects.create(
        organization=made.organization,
        order=edge,
        payment_provider="vipps",
        provider_reference="vp-edge",
        amount=100,
        state="captured",
        captured_amount=100,
        currency="NOK",
    )
    day = payments(made, **{"from": "2027-01-13", "to": "2027-01-13"})
    assert numbers(day) == [middle.number, edge.number]
    assert set(
        numbers(payments(made, **{"from": "2027-01-10", "to": "2027-01-10"}))
    ) == {early.number}
    period = set(numbers(payments(made, **{"from": "2027-01-13", "to": "2027-01-16"})))
    assert {middle.number, late.number, edge.number} == period
    assert numbers(payments(made, **{"from": "2027-01-16"}))[0] == late.number
    assert early.number not in numbers(payments(made, **{"from": "2027-01-11"}))
    assert late.number not in numbers(payments(made, to="2027-01-15"))


def test_the_search_is_the_member_or_the_reference(made: made_up.MadeUp) -> None:
    robin = another_member(made)
    theirs = pay(made, member=robin, reference="vp-robin-1", day=1)
    ours = pay(made, reference="vp-ours-77", day=2)
    gone = another_member(made, "Pat Quill")
    erased = pay(made, member=gone, day=3)
    erase_identity(gone)
    assert numbers(payments(made, q="robin")) == [theirs.number]
    assert numbers(payments(made, q="  LAKE ")) == [theirs.number]
    assert numbers(payments(made, q="VP-OURS")) == [ours.number]
    assert numbers(payments(made, q="ours-77")) == [ours.number]
    assert numbers(payments(made, q=f"member {gone.pk}")) == [erased.number]
    assert payments(made, q="100%")["count"] == 0
    assert payments(made, q="nothing like it")["payments"] == []


def test_the_filters_together(made: made_up.MadeUp) -> None:
    robin = another_member(made)
    wanted = pay(made, member=robin, state="refunded", day=3, reference="vp-want")
    pay(made, member=robin, state="captured", day=3, reference="vp-want-2")
    pay(made, member=robin, state="refunded", day=9, reference="vp-want-3")
    pay(made, state="refunded", day=3, reference="vp-want-4")
    found = payments(
        made,
        **{"from": "2027-01-13", "to": "2027-01-13"},
        state="refunded",
        q="robin",
    )
    assert numbers(found) == [wanted.number]
    assert found["count"] == 1


def test_newest_first_and_oldest_first_with_the_number_breaking_a_tie(
    made: made_up.MadeUp,
) -> None:
    a, b, c = pay(made, day=5), pay(made, day=1), pay(made, day=9)
    same_a, same_b = pay(made, day=7), pay(made, day=7)
    mine = {a.number, b.number, c.number, same_a.number, same_b.number}

    def ours(found: dict[str, Any]) -> list[int]:
        return [n for n in numbers(found) if n in mine]

    newest = [c.number, same_b.number, same_a.number, a.number, b.number]
    assert ours(payments(made)) == newest
    assert ours(payments(made, order="newest")) == newest
    assert ours(payments(made, order="oldest")) == newest[::-1]


def test_pages_of_25_and_the_last_page(made: made_up.MadeUp) -> None:
    for index in range(30):
        pay(made, day=index)
    first = payments(made)
    assert (first["count"], first["per_page"], first["pages"]) == (31, 25, 2)
    assert len(first["payments"]) == 25
    second = payments(made, page="2")
    assert (second["page"], len(second["payments"])) == (2, 6)
    assert not set(numbers(first)) & set(numbers(second))
    assert payments(made, page="3")["payments"] == []


def test_another_organizations_payments_are_never_shown(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    assert Payment.objects.of(elsewhere.organization).count() == 1
    found = payments(made)
    assert {r["reference"] for r in found["payments"]} == {
        made.payment.provider_reference
    }
    assert found["count"] == 1
    assert payments(made, q=elsewhere.payment.provider_reference)["count"] == 0


def test_each_page_of_the_list_is_one_look_up(made: made_up.MadeUp) -> None:
    pay(made)
    payments(made)
    payments(made, q="vp", state="captured", order="oldest")
    assert (
        AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.LISTED_PAYMENTS)
        .count()
        == 2
    )


def test_malformed_parameters_answer_400(made: made_up.MadeUp) -> None:
    for query in (
        {"to": "2027-02-30"}, {"to": "2101-01-01"}, {"from": "1999-12-31"},
        {"from": "nope"}, {"page": "1e3"}, {"page": "0"}, {"page": "1000000000"},
        {"state": ""}, {"state": "paid"}, {"q": "x" * 101}, {"order": ""},
        {"order": "oldest-first"}, {"order": "x"},
    ):  # fmt: skip
        assert refused(made, **query) == 400, query
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        assert get(client, f"{LIST}?page=x").status_code == 400
        assert get(client, f"{LIST}?state=x").status_code == 400


def test_the_number_of_queries_does_not_grow_with_the_rows(
    made: made_up.MadeUp,
) -> None:
    def cost() -> int:
        with CaptureQueriesContext(connection) as seen:
            assert admin_views.payments(request_for(made, LIST, {})).status_code == 200
        return len(seen)

    for index in range(2):
        pay(made, day=index)
    few = cost()
    for index in range(2, 40):
        pay(made, day=index, currency="EUR" if index % 2 else "NOK")
    assert cost() == few
    # The gate (the administrator and the organization), the count, the page.
    # Five, the look-up the list writes, the page's spells and the refund check
    # (nine, however many rows).
    assert few == 14


SINCE = datetime(2027, 2, 27, 8, 30, 15, tzinfo=UTC)


@pytest.fixture
def lasting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clock 15 minutes after SINCE: a spell begun then has just lasted."""
    monkeypatch.setattr(booking_admin, "_now", lambda: SINCE + LASTING)


def in_trouble(
    made: made_up.MadeUp,
    order: Order,
    *,
    ended: bool = False,
    step: str = "capture",
    kind: str = "unanswered",
    tries: int = 14,
    since: datetime = SINCE,
) -> None:
    payment = Payment.objects.of(made.organization).get(order=order)
    PaymentTrouble.objects.create(
        organization=made.organization,
        payment=payment,
        since=since,
        ended_at=since + timedelta(minutes=3) if ended else None,
        step=step,
        kind=kind,
        first_step=step,
        first_kind=kind,
        tries=tries,
    )


def test_a_payment_in_trouble_needs_attention_with_step_kind_and_start(
    made: made_up.MadeUp, lasting: None
) -> None:
    well = pay(made, day=0)
    sick = pay(made, day=1)
    in_trouble(made, sick)
    rows = {r["number"]: r for r in payments(made)["payments"]}
    assert rows[well.number]["trouble"] is None
    assert rows[sick.number]["trouble"] == {
        "label": "Needs attention",
        "since": "2027-02-27, 09:30:15",
        "step": "capture",
        "step_label": "Taking the money",
        "kind": "unanswered",
        "kind_label": "Vipps does not answer",
        "tries": 14,
    }
    # The payment's own state is still its own.
    assert rows[sick.number]["state"] == "captured"
    # On its booking's page, on the Payment card; History has the one line.
    card = one(made, sick.number)["payment"]["trouble"]
    assert card == rows[sick.number]["trouble"]
    history = [e["text"] for e in one(made, sick.number)["history"]]
    assert history.count("Payment check began failing") == 1
    assert not any("works again" in text for text in history)
    assert one(made, well.number)["payment"]["trouble"] is None


def test_needs_attention_is_a_filter_and_a_state_word(
    made: made_up.MadeUp, lasting: None
) -> None:
    well = pay(made, day=0)
    sick = pay(made, day=1, state="authorized")
    gone = pay(made, day=2)
    in_trouble(made, sick)
    in_trouble(made, gone, ended=True)
    found = payments(made, state="attention")
    assert numbers(found) == [sick.number]
    assert {"value": "attention", "label": "Needs attention"} in found["states"]
    # Each of the six states still filters on the payment's own state.
    assert well.number in numbers(payments(made, state="captured"))
    assert sick.number in numbers(payments(made, state="authorized"))
    assert refused(made, state="attention") == 200


def test_a_spell_that_ended_shows_no_attention_and_two_history_lines(
    made: made_up.MadeUp, lasting: None
) -> None:
    order = pay(made)
    in_trouble(made, order, ended=True, step="read", kind="database", tries=3)
    assert payments(made)["payments"][0]["trouble"] is None
    page = one(made, order.number)
    assert page["payment"]["trouble"] is None
    lines = [e for e in page["history"] if e["text"].startswith("Payment check")]
    assert lines == [
        {"at": "2027-02-27, 09:30:15", "text": "Payment check began failing"},
        {
            "at": "2027-02-27, 09:33:15",
            "text": "Payment check works again "
            "(reading the payment, our database failed, 3 tries)",
        },
    ]


def test_another_organizations_trouble_is_never_shown(
    made: made_up.MadeUp, lasting: None
) -> None:
    elsewhere = made_up.everything("elsewhere")
    in_trouble(elsewhere, elsewhere.order)
    assert payments(made, state="attention")["count"] == 0
    assert all(r["trouble"] is None for r in payments(made)["payments"])
    mine = pay(made)
    in_trouble(made, mine)
    assert numbers(payments(made, state="attention")) == [mine.number]
    # Its own organization sees its own, and only that.
    own = booking_admin.payments(
        elsewhere.organization, booking_admin.PaymentsQuery(state="attention")
    )
    assert own["count"] == 1


def test_the_list_waits_15_minutes_but_the_booking_card_does_not(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = pay(made)
    in_trouble(made, order)
    early = SINCE + LASTING - timedelta(seconds=1)
    monkeypatch.setattr(booking_admin, "_now", lambda: early)
    assert payments(made)["payments"][0]["trouble"] is None
    assert payments(made, state="attention")["count"] == 0
    assert one(made, order.number)["payment"]["trouble"]["tries"] == 14
    monkeypatch.setattr(booking_admin, "_now", lambda: SINCE + LASTING)
    assert payments(made)["payments"][0]["trouble"]["tries"] == 14
    assert payments(made, state="attention")["count"] == 1


def test_every_spell_keeps_its_lines_in_the_history(
    made: made_up.MadeUp, lasting: None
) -> None:
    order = pay(made)
    in_trouble(made, order, ended=True, step="read", kind="database", tries=3)
    later = SINCE + timedelta(hours=2)
    in_trouble(made, order, step="save", kind="other", tries=1, since=later)
    page = one(made, order.number)
    lines = [e["text"] for e in page["history"] if e["text"].startswith("Payment")]
    assert lines == [
        "Payment check began failing",
        "Payment check works again (reading the payment, our database failed, 3 tries)",
        "Payment check began failing",
    ]
    assert page["payment"]["trouble"]["step_label"] == "Saving the answer"
