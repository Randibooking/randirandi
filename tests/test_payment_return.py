# SPDX-License-Identifier: AGPL-3.0-only
"""the return from Vipps, the login carry and the outcome read.

Made-up members only. The return works logged in or out; an order that does
not exist and another member's order must look the same from outside.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client as Browser
from django.test import override_settings

from randirandi.core import booking_cart, booking_words
from randirandi.core.authentication import RETURN_KEY, flush_keeping_order
from randirandi.core.models import (
    BookingState,
    Member,
    MemberStatus,
    Order,
    Origin,
    Payment,
    TakenTime,
)
from randirandi.core.ordering import create_order
from tests import made_up
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_access_gate import PASSWORD, with_password
from tests.test_booking_checkout import fill_page, send
from tests.test_calendar import ZONE, Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, post, sign_in
from tests.test_ordering import NOW, request
from tests.test_payment_callbacks import (
    RETURN,
    payment_for,
)
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payments import Provider
from tests.test_payments import provider as provider
from tests.test_second_factor import clocks as clocks
from tests.test_second_factor import email_factor, token

pytestmark = pytest.mark.django_db(transaction=True)


def outcome(browser: Browser, order_id: object) -> Any:
    return get(browser, f"/booking/order/{order_id}/")


def returned(browser: Browser, order_id: object, **query: str) -> Any:
    return browser.get(
        RETURN, {"order": str(order_id), **query}, HTTP_HOST=HOST, secure=True
    )


def another_member(made: Example) -> Member:
    return Member.objects.create(
        organization=made.org,
        role=made.member.role,
        email="other@example.com",
        first_name="Other",
        last_name="Person",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )


def theirs(made: Example) -> Order:
    """An order of another member, on another time of the same asset."""
    return create_order(
        member=another_member(made),
        requests=[request(made, starts_at=at(hour=12), ends_at=at(hour=14))],
        now=NOW,
        accepted_version=made_up.accepted(made.org),
    )


def own(made: Example) -> Order:
    return create_order(
        member=made.member,
        requests=[request(made)],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )


def cookies(response: Any) -> dict[str, Any]:
    """What the cookies say but their values, which differ for each browser."""
    return {
        name: {key: value for key, value in morsel.items() if key != "expires"}
        for name, morsel in response.cookies.items()
    }


def headers(response: Any) -> dict[str, str]:
    """The headers but the moment the page expires, which is the clock's."""
    return {
        name: value for name, value in response.headers.items() if name != "Expires"
    }


def looks_the_same(one: Any, two: Any) -> None:
    assert one.status_code == two.status_code
    assert one.content == two.content
    assert headers(one) == headers(two)
    assert ("Expires" in one.headers) == ("Expires" in two.headers)
    assert cookies(one) == cookies(two)


def test_logged_out_return_logs_in_and_comes_back_to_the_same_order(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    order_id = payment.order.public_id
    browser = Browser(enforce_csrf_checks=True)
    with application_login():
        first = returned(browser, order_id, next="https://untrusted.example/")
        assert first.status_code == 302 and first["Location"] == "/login/"
        assert browser.session[RETURN_KEY] == str(order_id)
        payment.refresh_from_db()
        assert payment.callback_received_at is not None  # The read was asked for.
        done = sign_in(browser, made.member)
        assert done.status_code == 302
        assert done["Location"] == f"/payments/return/?order={order_id}"
        assert browser.session["_auth_user_id"] == str(made.member.pk)
        assert RETURN_KEY not in browser.session  # Used once.
        again = browser.get(done["Location"], HTTP_HOST=HOST, secure=True)
        assert again.status_code == 302
        assert again["Location"] == f"/book/order/{order_id}/"
        body = json.loads(outcome(browser, order_id).content)
    # The return asked for a read that has not been made: settling.
    assert body["id"] == str(order_id) and body["state"] == "settling"
    # The next login goes home: nothing is kept.
    assert post(browser, "/logout/").status_code == 302
    assert sign_in(browser, made.member)["Location"] == "/"


def test_logged_in_return_goes_to_the_outcome_address(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    response = returned(browser, payment.order.public_id)
    assert response.status_code == 302
    assert response["Location"] == f"/book/order/{payment.order.public_id}/"
    assert RETURN_KEY not in browser.session
    payment.refresh_from_db()
    assert payment.callback_received_at is not None


def test_logged_in_to_another_organization_is_logged_out_for_the_return(
    made: Example,
) -> None:
    order = own(made)
    other = made_up.organization("riverside")
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    host = other.subdomain + ".sandbox.example.org"
    response = browser.get(
        RETURN, {"order": str(order.public_id)}, HTTP_HOST=host, secure=True
    )
    assert response.status_code == 302 and response["Location"] == "/login/"
    assert "_auth_user_id" not in browser.session
    assert browser.session[RETURN_KEY] == str(order.public_id)


def test_administrator_returns_through_both_factors_to_the_same_order() -> None:
    world = made_up.everything()
    order_id = uuid.uuid4()
    browser = Browser(enforce_csrf_checks=True)
    with application_login():
        assert returned(browser, order_id)["Location"] == "/login/"
        email_factor(browser, world.administrator)
        # The email factor flushed the session; the one id came across.
        assert "_auth_user_id" not in browser.session
        assert browser.session[RETURN_KEY] == str(order_id)
        from randirandi.core.models import Authenticator

        device = Authenticator.objects.of(world.organization).get(
            member=world.administrator
        )
        done = post(browser, "/login/authenticator/", code=token(device))
        assert done.status_code == 302
        assert done["Location"] == f"/payments/return/?order={order_id}"
        assert browser.session["admin_confirmed"] is True
        assert RETURN_KEY not in browser.session
        last = browser.get(done["Location"], HTTP_HOST=HOST, secure=True)
    assert last.status_code == 302
    assert last["Location"] == f"/book/order/{order_id}/"


def test_only_one_checked_order_id_crosses_the_login_flush(made: Example) -> None:
    order_id = uuid.uuid4()
    browser = Browser(enforce_csrf_checks=True)
    session = browser.session
    session[RETURN_KEY] = str(order_id)
    session["next"] = "https://untrusted.example/"
    session["booking_cart"] = {"member": made.member.pk, "rows": []}
    session["anything"] = "else"
    session.save()
    assert session.session_key is not None
    browser.cookies["sessionid"] = session.session_key
    done = sign_in(browser, made.member)
    assert done["Location"] == f"/payments/return/?order={order_id}"
    assert browser.session["_auth_user_id"] == str(made.member.pk)
    for gone in ("next", "booking_cart", "anything"):
        assert gone not in browser.session
    assert RETURN_KEY not in browser.session  # Used by the redirect.


@pytest.mark.parametrize(
    "kept",
    [
        "https://untrusted.example/",
        "/payments/return/?order=x",
        "{" + str(uuid.uuid4()) + "}",
        str(uuid.uuid4()).upper(),
        str(uuid.uuid4()).replace("-", ""),
        "",
        7,
        None,
        [str(uuid.uuid4())],
    ],
)
def test_a_kept_value_that_is_not_a_written_uuid_is_dropped(
    made: Example, kept: object
) -> None:
    session = SessionStore()
    session[RETURN_KEY] = kept
    session["other"] = 1
    flush_keeping_order(session)
    assert dict(session.items()) == {}
    browser = Browser(enforce_csrf_checks=True)
    stored = browser.session
    stored[RETURN_KEY] = kept
    stored.save()
    assert stored.session_key is not None
    browser.cookies["sessionid"] = stored.session_key
    assert sign_in(browser, made.member)["Location"] == "/"


def test_the_flush_keeps_a_written_uuid_and_nothing_else() -> None:
    order_id = str(uuid.uuid4())
    session = SessionStore()
    session[RETURN_KEY] = order_id
    session["other"] = 1
    flush_keeping_order(session)
    assert dict(session.items()) == {RETURN_KEY: order_id}


def test_a_return_in_another_browser_reveals_nothing(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    order_id = payment.order.public_id
    member = Browser()
    assert sign_in(member, made.member).status_code == 302
    stranger = Browser()
    response = returned(stranger, order_id)
    assert response.status_code == 302 and response["Location"] == "/login/"
    assert payment.order.reference not in response.content.decode()
    assert made.member.email not in response.content.decode()
    assert "_auth_user_id" not in stranger.session
    # Nor does the outcome: it needs the member's own session.
    read = outcome(stranger, order_id)
    assert read.status_code == 302 and read["Location"].startswith("/login/")
    # The member's own browser is unchanged and reads its order.
    assert outcome(member, order_id).status_code == 200


def test_unknown_and_another_members_order_look_the_same(made: Example) -> None:
    other_order = theirs(made)
    unknown = uuid.uuid4()
    # Logged out, through the return.
    one, two = Browser(), Browser()
    looks_the_same(returned(one, other_order.public_id), returned(two, unknown))
    assert one.session[RETURN_KEY] == str(other_order.public_id)
    # Logged in, through the return.
    member = Browser()
    assert sign_in(member, made.member).status_code == 302
    a = returned(member, other_order.public_id)
    b = returned(member, unknown)
    assert a.status_code == b.status_code == 302
    assert a["Location"] == f"/book/order/{other_order.public_id}/"
    assert b["Location"] == f"/book/order/{unknown}/"
    assert headers(a) | {"Location": ""} == headers(b) | {"Location": ""}
    # And through the outcome read: one fixed answer.
    c = outcome(member, other_order.public_id)
    d = outcome(member, unknown)
    assert c.status_code == 404
    looks_the_same(c, d)
    assert json.loads(c.content) == {"error": "not-found"}
    # Nothing of the other member appears, and the order is not theirs to read.
    text = c.content.decode()
    for private in ("Other", "Person", "other@example.com", other_order.reference):
        assert private not in text
    # A badly written id is no order either.
    assert outcome(member, "not-an-order").status_code == 404


def test_the_outcome_gives_the_members_own_order(made: Example) -> None:
    order = own(made)
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    response = outcome(browser, order.public_id)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert json.loads(response.content) == {
        "id": str(order.public_id),
        "reference": order.reference,
        "state": "held",
        "text": "waiting for payment",
        "total": 200,
        "currency": "NOK",
        "hold_expires_at": (NOW + timedelta(minutes=10)).astimezone(ZONE).isoformat(),
        "contact_email": made.org.contact_email,
        "conditions": True,
        "bookings": [
            {
                "container": {"id": made.config.pk, "number": made.config.number},
                "spots": [],
                "spot_word": None,
                "asset": "Pool",
                "type": "Visit",
                "category": "Private",
                "places": 1,
                "start": at(hour=10).astimezone(ZONE).isoformat(),
                "end": at(hour=12).astimezone(ZONE).isoformat(),
                "total": 200,
                "currency": "NOK",
                "state": "held",
            }
        ],
    }
    assert get(Browser(), f"/booking/order/{order.public_id}/").status_code == 302
    assert (
        browser.post(
            f"/booking/order/{order.public_id}/", HTTP_HOST=HOST, secure=True
        ).status_code
        == 405
    )


def times_of(order: Order) -> Any:
    return TakenTime.objects.of(order.organization_id).filter(booking__order=order)


def read_state(made: Example, order: Order) -> str:
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    response = outcome(browser, order.public_id)
    assert response.status_code == 200
    return str(json.loads(response.content)["state"])


def test_held_without_a_read_pending_is_held(made: Example, provider: Provider) -> None:
    payment = payment_for(made)
    assert read_state(made, payment.order) == "held"
    # A read made after the last callback leaves nothing pending.
    Payment.objects.of(made.org).filter(pk=payment.pk).update(
        callback_received_at=NOW, provider_checked_at=NOW + timedelta(seconds=1)
    )
    assert read_state(made, payment.order) == "held"


@pytest.mark.parametrize(
    "changes",
    [
        {"callback_received_at": NOW},
        {
            "callback_received_at": NOW + timedelta(seconds=2),
            "provider_checked_at": NOW + timedelta(seconds=1),
        },
        {"state": "authorized"},
        {"state": "captured", "captured_amount": 200},
    ],
)
def test_held_with_a_read_pending_or_paid_but_not_confirmed_is_settling(
    made: Example, provider: Provider, changes: dict[str, Any]
) -> None:
    payment = payment_for(made)
    Payment.objects.of(made.org).filter(pk=payment.pk).update(**changes)
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    body = json.loads(outcome(browser, payment.order.public_id).content)
    assert body["state"] == "settling"
    assert body["text"] == "the payment is being checked"
    assert [row["state"] for row in body["bookings"]] == ["held"]
    assert body["contact_email"] == made.org.contact_email


def test_confirmed_cancelled_and_not_completed(made: Example) -> None:
    order = own(made)
    # Not completed: the hold ran out.
    Order.objects.of(made.org).filter(pk=order.pk).update(
        hold_expires_at=NOW - timedelta(minutes=1)
    )
    assert read_state(made, order) == "not-completed"
    # Rejected or expired bookings are not completed either.
    Order.objects.of(made.org).filter(pk=order.pk).update(
        hold_expires_at=NOW + timedelta(minutes=5)
    )
    times_of(order).update(state=BookingState.EXPIRED)
    assert read_state(made, order) == "not-completed"
    # Confirmed: paid, no hold left.
    times_of(order).update(state=BookingState.CONFIRMED)
    Order.objects.of(made.org).filter(pk=order.pk).update(hold_expires_at=None)
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    body = json.loads(outcome(browser, order.public_id).content)
    assert (body["state"], body["hold_expires_at"]) == ("confirmed", None)
    assert body["text"] == "confirmed"
    assert [row["state"] for row in body["bookings"]] == ["confirmed"]
    # Cancelled by an administrator after payment.
    times_of(order).update(state=BookingState.CANCELLED)
    assert read_state(made, order) == "cancelled"


def test_a_confirmed_order_read_empties_its_rows_from_the_cart(
    made: Example, wire: Wire
) -> None:
    browser = Browser(enforce_csrf_checks=True)
    assert sign_in(browser, made.member).status_code == 302
    fill_page(browser, made)
    placed = json.loads(
        send(browser, "/booking/cart/order/", made_up.order_body(200)).content
    )
    order = Order.objects.of(made.org).get(public_id=placed["order"])
    # A held order's read leaves the cart as it is.
    assert json.loads(outcome(browser, order.public_id).content)["state"] == "held"
    assert len(browser.session[booking_cart.SESSION_KEY]["rows"]) == 1
    times_of(order).update(state=BookingState.CONFIRMED)
    Order.objects.of(made.org).filter(pk=order.pk).update(hold_expires_at=None)
    # The cart itself was not looked at: the outcome read cleans it.
    response = outcome(browser, order.public_id)
    assert json.loads(response.content)["state"] == "confirmed"
    assert browser.session[booking_cart.SESSION_KEY]["rows"] == []
    cart = json.loads(get(browser, "/booking/cart/").content)
    assert (cart["count"], cart["order"]["state"]) == (0, "confirmed")


def test_reading_another_order_leaves_the_cart_alone(made: Example, wire: Wire) -> None:
    browser = Browser(enforce_csrf_checks=True)
    assert sign_in(browser, made.member).status_code == 302
    fill_page(browser, made)
    placed = json.loads(
        send(browser, "/booking/cart/order/", made_up.order_body(200)).content
    )
    mine = Order.objects.of(made.org).get(public_id=placed["order"])
    times_of(mine).update(state=BookingState.CONFIRMED)
    Order.objects.of(made.org).filter(pk=mine.pk).update(hold_expires_at=None)
    elsewhere = create_order(
        member=made.member,
        requests=[request(made, starts_at=at(hour=12), ends_at=at(hour=14))],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )
    assert outcome(browser, elsewhere.public_id).status_code == 200
    assert len(browser.session[booking_cart.SESSION_KEY]["rows"]) == 1


def test_each_state_of_an_order_has_its_default_text() -> None:
    states = ("held", "confirmed", "cancelled", "not-completed", "settling")
    assert [booking_words.text(state) for state in states] == [
        "waiting for payment",
        "confirmed",
        "cancelled",
        "not completed",
        "the payment is being checked",
    ]


def test_production_gate_on_fresh_payment_return_still_requires_member_login(
    made: Example, provider: Provider
) -> None:
    payment = payment_for(made)
    order_id = payment.order.public_id
    browser = Browser(enforce_csrf_checks=True)
    with override_settings(ENVIRONMENT="production", ACCESS_GATE_PASSWORD=PASSWORD):
        first = returned(browser, order_id)
        assert first.status_code == 302 and first["Location"] == "/login/"
        assert browser.session[RETURN_KEY] == str(order_id)
        assert get(browser, first["Location"]).status_code == 401
        browser.defaults["HTTP_AUTHORIZATION"] = with_password(PASSWORD)[
            "Authorization"
        ]
        assert get(browser, "/login/").status_code == 200
        assert "_auth_user_id" not in browser.session
        done = sign_in(browser, made.member)
        assert done["Location"] == f"/payments/return/?order={order_id}"
        assert browser.session["_auth_user_id"] == str(made.member.pk)
        assert returned(browser, order_id)["Location"] == f"/book/order/{order_id}/"
        assert json.loads(outcome(browser, order_id).content)["id"] == str(order_id)
        browser.defaults.pop("HTTP_AUTHORIZATION")
        assert get(browser, "/").status_code == 401
        with override_settings(ACCESS_GATE_PASSWORD=""):
            assert get(Browser(), "/").status_code == 302
            assert get(browser, "/book/").status_code == 200
