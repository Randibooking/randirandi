# SPDX-License-Identifier: AGPL-3.0-only
"""The Payouts : the gate, the list from the record, the filters, a
payout's page with its payments and refunds and who booked them, the look-up
event of that page only, another organization's payouts, the 400s and the
fee, net and payout on a booking's page."""

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory

from randirandi.core import admin_views, booking_admin, payout_admin
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, Payment, Payout, Refund
from tests import made_up, payouts_made
from tests.capacity_app import application_login
from tests.test_admin_bookings import (
    answer,
    request_for,
)
from tests.test_admin_bookings import (
    made as made,
)
from tests.test_admin_bookings import (
    now as now,
)
from tests.test_admin_payments import pay
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
LIST = "/admin/payments/payouts/list/"


def payouts(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    return answer(admin_views.payouts_list(request_for(made, LIST, query)))


def one(made: made_up.MadeUp, payout: int, **query: Any) -> dict[str, Any]:
    path = f"/admin/payments/payouts/{payout}/data/"
    return answer(admin_views.payout_data(request_for(made, path, query), payout))


def status(made: made_up.MadeUp, payout: int, **query: Any) -> int:
    path = f"/admin/payments/payouts/{payout}/data/"
    return int(
        admin_views.payout_data(request_for(made, path, query), payout).status_code
    )


def events(made: made_up.MadeUp) -> list[str]:
    return list(
        AuditEvent.objects.of(made.organization)
        .order_by("pk")
        .values_list("action", flat=True)
    )


def day(number: int, **over: Any) -> Payout:
    """A payout on the `number`th day of February 2027."""
    made = over.pop("made")
    return payouts_made.payout(
        made,
        psp_reference=f"9001-20000{number:02d}",
        ledger_date=date(2027, 2, number),
        scheduled_at=datetime(2027, 2, number, 23, 0, tzinfo=UTC),
        **over,
    )


def test_the_gate_is_the_shells_and_the_two_reads(made: made_up.MadeUp) -> None:
    payout = payouts_made.payout(made)
    paths = [
        "/admin/payments/payouts/",
        LIST,
        f"/admin/payments/payouts/{payout.pk}/",
        f"/admin/payments/payouts/{payout.pk}/data/",
    ]
    for path in paths:
        assert get(Client(), path).status_code == 302
    member, admin = Client(), Client()
    with application_login():
        assert post(member, "/login/code/", code=request_code(member, made.member))
        sign_in_administrator(admin, made.administrator)
        for path in paths:
            assert get(member, path).status_code == 403
            assert get(admin, path).status_code == 200
            assert "no-store" in get(admin, path).headers["Cache-Control"]
            assert post(admin, path).status_code == 405


def test_without_the_second_step_or_from_another_organization(
    made: made_up.MadeUp,
) -> None:
    payout = payouts_made.payout(made)
    request = RequestFactory().get(LIST, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made.administrator
    with pytest.raises(PermissionDenied):
        admin_views.payouts_list(request)
    with pytest.raises(PermissionDenied):
        admin_views.payout_data(request, payout.pk)
    elsewhere = made_up.everything("elsewhere")
    request.session["admin_confirmed"] = True
    request.user = elsewhere.administrator
    # An administrator of another organization is refused on this address.
    with pytest.raises(PermissionDenied):
        admin_views.payouts_list(request)
    with pytest.raises(PermissionDenied):
        admin_views.payout_data(request, payout.pk)


def test_a_payout_as_the_list_shows_it_from_the_record(made: made_up.MadeUp) -> None:
    payout = payouts_made.payout(made, other_total=0)
    assert payouts(made) == {
        "count": 1,
        "per_page": 25,
        "page": 1,
        "pages": 1,
        "payouts": [
            {
                "id": payout.pk,
                "day": "2027-02-20",
                "payments": 3,
                "refunds": 1,
                "captured": 30000,
                "refunded": 10000,
                "fees": 1200,
                "other": 0,
                "amount": 18800,
                "currency": "NOK",
                "reference": "9001-2000023",
            }
        ],
    }


def test_with_no_payout_the_list_is_empty_and_made_up_nothing(
    made: made_up.MadeUp,
) -> None:
    assert payouts(made)["payouts"] == [] and payouts(made)["count"] == 0
    # A payment that no report has reached has no payout in it.
    booking = booking_admin.booking(made.organization, made.order)
    assert booking["payment"]["fee"] is None  # type: ignore[index]
    assert booking["payment"]["net"] is None  # type: ignore[index]
    assert booking["payment"]["payout"] is None  # type: ignore[index]


def days(found: dict[str, Any]) -> list[str]:
    return [row["day"] for row in found["payouts"]]


def test_newest_or_oldest_first_and_the_days_paid_out(made: made_up.MadeUp) -> None:
    rows = {number: day(number, made=made) for number in (3, 9, 15, 21)}
    assert days(payouts(made)) == [
        "2027-02-21",
        "2027-02-15",
        "2027-02-09",
        "2027-02-03",
    ]
    assert days(payouts(made, order="oldest"))[0] == "2027-02-03"
    assert days(payouts(made, **{"from": "2027-02-09"})) == [
        "2027-02-21",
        "2027-02-15",
        "2027-02-09",
    ]
    assert days(payouts(made, to="2027-02-09")) == ["2027-02-09", "2027-02-03"]
    assert days(payouts(made, **{"from": "2027-02-09", "to": "2027-02-15"})) == [
        "2027-02-15",
        "2027-02-09",
    ]
    # One day: both ends the same; the wrong way round reads the same.
    assert days(payouts(made, **{"from": "2027-02-15", "to": "2027-02-15"})) == [
        "2027-02-15"
    ]
    assert days(payouts(made, **{"from": "2027-02-15", "to": "2027-02-09"})) == days(
        payouts(made, **{"from": "2027-02-09", "to": "2027-02-15"})
    )
    assert rows[3].pk and payouts(made)["count"] == 4


def test_the_list_pages_at_most_25_whatever_is_asked(made: made_up.MadeUp) -> None:
    for number in range(1, 28):
        day(number, made=made)
    first = payouts(made)
    assert (first["count"], first["pages"], len(first["payouts"])) == (27, 2, 25)
    assert len(payouts(made, page="2")["payouts"]) == 2
    assert payouts(made, page="3")["payouts"] == []
    big = payouts(made, per_page="500", limit="500", all="1")
    assert len(big["payouts"]) == 25 and big["per_page"] == 25


@pytest.mark.parametrize(
    "query",
    [
        {"from": "yesterday"},
        {"to": "2027-13-40"},
        {"from": "1999-12-31"},
        {"order": "sideways"},
        {"page": "0"},
        {"page": "-1"},
        {"page": "abc"},
    ],
)
def test_a_wrong_filter_is_a_400_and_writes_nothing(
    made: made_up.MadeUp, query: dict[str, str]
) -> None:
    before = events(made)
    response = admin_views.payouts_list(request_for(made, LIST, query))
    assert response.status_code == 400
    assert events(made) == before


def test_the_list_names_no_member_and_writes_no_event(made: made_up.MadeUp) -> None:
    payouts_made.payout(made)
    before = events(made)
    found = payouts(made)
    assert events(made) == before
    assert "Alex" not in json.dumps(found) and "member" not in json.dumps(found)


def test_another_organizations_payout_is_not_listed_or_opened(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    theirs = payouts_made.payout(elsewhere, psp_reference="7-1")
    mine = payouts_made.payout(made)
    assert [row["id"] for row in payouts(made)["payouts"]] == [mine.pk]
    theirs_only = payout_admin.payouts(
        elsewhere.organization, payout_admin.PayoutsQuery()
    )
    assert [row["id"] for row in theirs_only["payouts"]] == [theirs.pk]  # type: ignore[attr-defined]
    before = events(made)
    assert status(made, theirs.pk) == 404
    assert events(made) == before
    assert status(made, 0) == 404 and status(made, 2**70) == 404


def tied(made: made_up.MadeUp, payout: Payout, **over: Any) -> Payment:
    order = pay(made, reference=over.pop("reference", "tied-1"), **over)
    Payment.objects.of(made.organization).filter(order=order).update(
        payout=payout,
        fee_amount=125,
        captured_at=datetime(2027, 2, 19, 22, 30, 5, tzinfo=UTC),
    )
    return Payment.objects.of(made.organization).get(order=order)


def test_a_payouts_page_has_its_totals_and_what_is_in_it(made: made_up.MadeUp) -> None:
    payout = payouts_made.payout(made)
    payment = tied(made, payout, amount=5000)
    Refund.objects.of(made.organization).update(payout=payout)
    refund = Refund.objects.of(made.organization).get()
    found = one(made, payout.pk)
    assert {
        key: found[key]
        for key in (
            "id",
            "day",
            "amount",
            "captured",
            "refunded",
            "fees",
            "other",
            "currency",
            "reference",
            "statement",
            "scheduled",
        )
    } == {
        "id": payout.pk,
        "day": "2027-02-20",
        "amount": 18800,
        "captured": 30000,
        "refunded": 10000,
        "fees": 1200,
        "other": 0,
        "currency": "NOK",
        "reference": "9001-2000023",
        "statement": "Vipps utbet. 2000023 Vippsnr 123456",
        # Oslo is UTC+1 in February.
        "scheduled": "2027-02-21, 01:00:00",
    }
    assert found["payment_rows"] == [
        {
            "number": payment.order.number,
            "order_reference": payment.order.reference,
            "member": "Alex Rivers",
            "captured_at": "2027-02-19, 23:30:05",
            "captured": 5000,
            "fee": 125,
            "reference": "tied-1",
        }
    ]
    assert found["refund_rows"] == [
        {
            "number": made.order.number,
            "order_reference": made.order.reference,
            "member": "Alex Rivers",
            "requested": found["refund_rows"][0]["requested"],
            "amount": refund.amount,
            "by": made.administrator.full_name,
            "reference": "",
        }
    ]
    assert found["payments_on_record"]["count"] == 1
    assert found["refunds_on_record"]["count"] == 1


def test_an_automatic_refund_has_no_one_behind_it_and_an_erased_member_a_number(
    made: made_up.MadeUp,
) -> None:
    payout = payouts_made.payout(made)
    Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
        payout=payout
    )
    Refund.objects.of(made.organization).update(
        payout=payout,
        made_by=None,
        automatic=True,
        refund_key=uuid.uuid4(),
    )
    from randirandi.core.models import Member

    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        first_name="", last_name="", full_name=""
    )
    found = one(made, payout.pk)
    assert found["refund_rows"][0]["by"] is None
    assert found["refund_rows"][0]["member"] == f"Member {made.member.pk}"
    assert found["payment_rows"][0]["member"] == f"Member {made.member.pk}"


def test_a_page_of_payments_and_a_page_of_refunds_are_25_each(
    made: made_up.MadeUp,
) -> None:
    payout = payouts_made.payout(made)
    for number in range(30):
        tied(made, payout, reference=f"many-{number}", day=number % 5)
    found = one(made, payout.pk)
    assert len(found["payment_rows"]) == 25
    assert found["payments_on_record"] == {
        "count": 30,
        "per_page": 25,
        "page": 1,
        "pages": 2,
    }
    second = one(made, payout.pk, payments_page="2")
    assert len(second["payment_rows"]) == 5
    huge = one(made, payout.pk, per_page="500", limit="500", all="1")
    assert len(huge["payment_rows"]) == 25
    assert status(made, payout.pk, payments_page="0") == 400
    assert status(made, payout.pk, refunds_page="x") == 400


def test_a_payouts_page_is_a_look_up_of_who_booked_one_event_each(
    made: made_up.MadeUp,
) -> None:
    payout = payouts_made.payout(made)
    before = events(made)
    one(made, payout.pk)
    one(made, payout.pk, payments_page="1")
    assert events(made)[len(before) :] == [AuditAction.LISTED_PAYOUTS.value] * 2
    event = AuditEvent.objects.of(made.organization).latest("pk")
    assert (event.object_kind, event.object_id) == (
        "organization",
        made.organization.pk,
    )
    assert event.administrator_id == made.administrator.pk


def test_the_page_counts_the_payments_it_names_not_the_ones_erased(
    made: made_up.MadeUp,
) -> None:
    payout = payouts_made.payout(made, captured_count=40, refunded_count=6)
    found = one(made, payout.pk)
    # Counts and totals are the payout's own; no payment is on record.
    assert (found["payments"], found["refunds"]) == (40, 6)
    assert found["payment_rows"] == [] and found["refund_rows"] == []
    assert found["captured"] == 30000


def test_a_bookings_payment_card_has_fee_net_and_payout_or_nothing_yet(
    made: made_up.MadeUp,
) -> None:
    card = booking_admin.booking(made.organization, made.order)["payment"]
    assert (card["fee"], card["net"], card["payout"]) == (None, None, None)  # type: ignore[index]
    payout = payouts_made.payout(made)
    Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
        fee_amount=375, payout=payout
    )
    card = booking_admin.booking(made.organization, made.order)["payment"]
    assert card["fee"] == 375  # type: ignore[index]
    # The net is what was captured less the fee, not what was left after refunds.
    assert card["net"] == 25000 - 375  # type: ignore[index]
    assert card["payout"] == {"id": payout.pk, "day": "2027-02-20"}  # type: ignore[index]


def test_a_fee_of_nothing_is_a_fee_and_not_unknown(made: made_up.MadeUp) -> None:
    Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
        fee_amount=0
    )
    card = booking_admin.booking(made.organization, made.order)["payment"]
    assert (card["fee"], card["net"]) == (0, 25000)  # type: ignore[index]


def test_the_queries_of_a_page_do_not_grow_with_its_rows(made: made_up.MadeUp) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    small = payouts_made.payout(made)
    tied(made, small, reference="q-0")
    with CaptureQueriesContext(connection) as few:
        one(made, small.pk)
    big = day(5, made=made)
    for number in range(20):
        tied(made, big, reference=f"q-{number + 1}")
    with CaptureQueriesContext(connection) as many:
        one(made, big.pk)
    assert len(many) == len(few)
    assert payout_admin.PER_PAGE == 25
