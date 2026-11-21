# SPDX-License-Identifier: AGPL-3.0-only
"""The cancel endpoint of a booking's page : the gate, CSRF and
origin, the body, the refusals as codes, another organization's booking, and
the answer that is the booking's page after the cancel."""

import json
import uuid
from typing import Any
from unittest import mock

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import Client, RequestFactory

from randirandi.core import admin_views
from randirandi.core import payment_adjustments as adjustments
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import AuditEvent, Member, Order, Refund
from randirandi.core.payments import PaymentBusyError
from randirandi.vipps import VippsError
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_bookings import book, order_of
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_ordering import NOW
from tests.test_payment_adjustments import administrator as administrator
from tests.test_payment_adjustments import paid
from tests.test_payment_capture import Money, ready
from tests.test_payment_capture import money as money
from tests.test_payments import Provider
from tests.test_payments import provider as provider
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def site() -> made_up.MadeUp:
    return made_up.everything()


def path(order: Order) -> str:
    return f"/admin/bookings/{order.number}/cancel/"


def send(
    who: Member,
    order: Order | int,
    body: Any,
    *,
    confirmed: bool = True,
) -> HttpResponse:
    """The view called directly, as a confirmed administrator."""
    number = order if isinstance(order, int) else order.number
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = RequestFactory().post(
        f"/admin/bookings/{number}/cancel/",
        data,
        content_type="application/json",
        HTTP_HOST=HOST,
        secure=True,
    )
    request.session = SessionStore()
    request.session["admin_confirmed"] = confirmed
    request.user = who
    return admin_views.booking_cancel(request, number)


def answer(response: HttpResponse, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    found = json.loads(response.content)
    assert isinstance(found, dict)
    return found


def body(
    refund: bool = True, line: int | None = None, key: Any = None
) -> dict[str, Any]:
    return {"key": str(key or uuid.uuid4()), "line": line, "refund": refund}


# --- The gate, CSRF and origin ----------------------------------------------------


def test_only_a_confirmed_administrator_of_this_organization_may(
    site: made_up.MadeUp,
) -> None:
    order = order_of(site)
    book(site, order, day=5)
    with pytest.raises(PermissionDenied):
        send(site.administrator, order, body(), confirmed=False)
    with pytest.raises(PermissionDenied):
        send(site.member, order, body())
    elsewhere = made_up.everything("elsewhere")
    with pytest.raises(PermissionDenied):
        send(elsewhere.administrator, order, body())
    get_request = RequestFactory().get(path(order), HTTP_HOST=HOST, secure=True)
    get_request.session = SessionStore()
    get_request.session["admin_confirmed"] = True
    get_request.user = site.administrator
    assert admin_views.booking_cancel(get_request, order.number).status_code == 405
    assert (
        not AuditEvent.objects.of(site.organization)
        .filter(action=AuditAction.CANCEL_BOOKING_LINE)
        .exists()
    )
    assert order.bookings.get().cancelled_at is None


def test_a_signed_out_browser_goes_to_sign_in(site: made_up.MadeUp) -> None:
    order = order_of(site)
    response = Client().post(path(order), HTTP_HOST=HOST, secure=True)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(site: made_up.MadeUp) -> None:
    order = order_of(site)
    line = book(site, order, day=5)
    client = Client(enforce_csrf_checks=True)
    with application_login():
        sign_in_administrator(client, site.administrator)
        token = str(config(get(client, "/admin/bookings/").content.decode())["csrf"])

        def post(headers: dict[str, Any]) -> HttpResponse:
            response = client.post(
                path(order),
                json.dumps(body(line=line.pk)),
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        assert post({"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        wrong = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://elsewhere.example"}
        assert post(wrong).status_code == 403
        assert order.bookings.get().cancelled_at is None
        right = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://" + HOST}
        found = answer(post(right))
    assert found["state"] == "cancelled"
    assert [row["state"] for row in found["lines"]] == ["cancelled"]
    (event,) = AuditEvent.objects.of(site.organization).filter(
        action=AuditAction.CANCEL_BOOKING_LINE
    )
    assert (event.object_id, event.administrator_id) == (
        line.pk,
        site.administrator.pk,
    )


# --- The body and the booking ------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        b"not json",
        b"[]",
        b"{}",
        {"key": "x", "line": None, "refund": True},
        {"key": str(uuid.uuid4()), "line": None},
        {"key": str(uuid.uuid4()), "line": None, "refund": "yes"},
        {"key": str(uuid.uuid4()), "line": None, "refund": 1},
        {"key": 5, "line": None, "refund": True},
        {"key": str(uuid.uuid4()), "line": 0, "refund": True},
        {"key": str(uuid.uuid4()), "line": "1", "refund": True},
        {"key": str(uuid.uuid4()), "line": True, "refund": True},
        {"key": str(uuid.uuid4()), "line": 2**70, "refund": True},
        {"key": str(uuid.uuid4()), "line": None, "refund": True, "amount": 5},
    ],
)
def test_a_malformed_body_is_a_400_and_changes_nothing(
    site: made_up.MadeUp, bad: Any
) -> None:
    order = order_of(site)
    book(site, order, day=5)
    assert answer(send(site.administrator, order, bad), 400) == {"error": "bad-request"}
    assert order.bookings.get().cancelled_at is None


def test_a_booking_or_line_that_is_not_here_is_a_404(site: made_up.MadeUp) -> None:
    mine = order_of(site)
    book(site, mine, day=5)
    other = order_of(site)
    theirs = book(site, other, day=6)
    elsewhere = made_up.everything("elsewhere")
    foreign = book(elsewhere, order_of(elsewhere), day=5).order
    missing = {"error": "not-found"}
    # No such number, a number only another organization has, and a line of
    # another booking (or of another organization).
    assert answer(send(site.administrator, 999, body()), 404) == missing
    assert answer(send(site.administrator, 0, body()), 404) == missing
    assert answer(send(site.administrator, mine, body(line=theirs.pk)), 404) == missing
    assert answer(send(site.administrator, mine, body(line=10**9)), 404) == missing
    same_number = Order.objects.of(site.organization).filter(number=foreign.number)
    if not same_number.exists():
        assert answer(send(site.administrator, foreign, body()), 404) == missing
    assert (
        answer(
            send(site.administrator, mine, body(line=foreign.bookings.get().pk)), 404
        )
        == missing
    )
    assert foreign.bookings.get().cancelled_at is None
    assert theirs.cancelled_at is None and mine.bookings.get().cancelled_at is None
    assert (
        not AuditEvent.objects.of(site.organization)
        .filter(action=AuditAction.CANCEL_BOOKING_LINE)
        .exists()
    )


# --- The answer and the refusals ------------------------------------------------------


def test_the_answer_is_the_booking_page_after_the_cancel_and_a_repeat_is_the_same(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    order = payment.order
    first, _second = order.bookings.order_by("pk")
    key = uuid.uuid4()
    found = answer(send(administrator, order, body(line=first.pk, key=key)))
    assert found["number"] == order.number and found["state"] == "paid"
    assert [line["state"] for line in found["lines"]] == ["cancelled", "paid"]
    assert [line["coming"] for line in found["lines"]] == [False, True]
    assert [r["amount"] for r in found["refunds"]] == [200]
    assert found["refunds"][0]["completed"] is None
    texts = [row["text"] for row in found["history"]]
    assert any("cancelled by Member" in text for text in texts)
    assert any(text.startswith("Refund of 2 kr requested") for text in texts)
    # Reading the answer wrote no "inspected" event.
    assert not AuditEvent.objects.of(made.org).filter(action="inspected").exists()
    assert answer(send(administrator, order, body(line=first.pk, key=key))) == found
    assert Refund.objects.of(made.org).count() == 1
    # The refund goes by the worker, as one asked for it.
    reconciliation.reconcile_requested(now=NOW)
    assert [row[1] for row in money.refunds] == [200]


def test_each_refusal_is_a_409_with_a_code(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    order = payment.order
    first, second = order.bookings.order_by("pk")
    answer(send(administrator, order, body(line=first.pk, refund=False)))
    assert answer(send(administrator, order, body(line=first.pk)), 409) == {
        "error": "already-cancelled"
    }
    answer(send(administrator, order, body(line=second.pk, refund=False)))
    assert answer(send(administrator, order, body()), 409) == {
        "error": "already-cancelled"
    }
    held = ready(made, money, multiple=True).order
    waiting = held.bookings.order_by("pk").first()
    assert waiting is not None
    assert answer(send(administrator, held, body(line=waiting.pk)), 409) == {
        "error": "whole-only"
    }
    waiting.refresh_from_db()
    assert waiting.cancelled_at is None


def test_a_busy_order_is_a_409_busy_and_an_unreachable_provider_a_503(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    order = payment.order
    with mock.patch.object(adjustments, "cancel", side_effect=PaymentBusyError("x")):
        assert answer(send(administrator, order, body()), 409) == {"error": "busy"}

    def down() -> None:
        raise VippsError(503)

    money.before_read = down
    assert answer(send(administrator, order, body()), 503) == {
        "error": "payment-unavailable"
    }
    assert [line.cancelled_at for line in order.bookings.all()] == [None, None]
    assert not Refund.objects.of(made.org).exists()
    # Without the refund nothing is asked of the provider at all.
    found = answer(send(administrator, order, body(refund=False)))
    assert found["state"] == "cancelled"
