# SPDX-License-Identifier: AGPL-3.0-only
"""the session's absolute end and the anonymous return's limit."""

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import Client

from randirandi.core.authentication import (
    ABSOLUTE_SECONDS,
    IDLE_SECONDS,
    login_moment,
    remaining,
)
from randirandi.core.login_codes import RETURN_LIMIT
from randirandi.core.models import Authenticator, LoginAttempt
from tests import made_up
from tests.capacity_app import application_login
from tests.test_login_sessions import NOW, get, post, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_payment_callbacks import RETURN
from tests.test_second_factor import clocks as clocks
from tests.test_second_factor import email_factor, token

pytestmark = pytest.mark.django_db(transaction=True)
HOST = "harbourview.sandbox.example.org"


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def at(seconds: int) -> mock._patch[mock.MagicMock]:
    return mock.patch(
        "randirandi.core.authentication.utc_now",
        return_value=NOW + timedelta(seconds=seconds),
    )


def keep_active(client: Client, until: int) -> None:
    """Use the session every 24 minutes, so it is never idle, up to `until`."""
    for elapsed in range(1440, until + 1, 1440):
        with at(elapsed):
            assert get(client, "/me/").status_code == 200


def test_the_constants_are_thirty_minutes_and_twelve_hours() -> None:
    assert (IDLE_SECONDS, ABSOLUTE_SECONDS) == (1800, 43200)


def test_an_active_member_session_ends_twelve_hours_after_the_login(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    assert sign_in(client, made.member).status_code == 302
    assert client.session["login_at"] == NOW.timestamp()
    keep_active(client, 41760)
    # Active just before the end: still in.
    with at(ABSOLUTE_SECONDS - 1):
        assert get(client, "/me/").status_code == 200
        # The activity did not move the login time, and the cookie is cut short.
        assert client.session["login_at"] == NOW.timestamp()
    with at(ABSOLUTE_SECONDS):
        assert get(client, "/me/").status_code == 302
        assert "_auth_user_id" not in client.session


def test_the_session_expiry_is_cut_at_the_absolute_end(made: made_up.MadeUp) -> None:
    client = Client()
    sign_in(client, made.member)
    keep_active(client, 41760)
    with at(ABSOLUTE_SECONDS - 600):
        response = get(client, "/me/")
        assert response.status_code == 200
        assert response.cookies["__Host-sessionid"]["max-age"] == 600


def test_an_idle_session_still_ends_after_thirty_minutes(made: made_up.MadeUp) -> None:
    client = Client()
    sign_in(client, made.member)
    with at(IDLE_SECONDS - 1):
        assert get(client, "/me/").status_code == 200
    with at(2 * IDLE_SECONDS):
        assert get(client, "/me/").status_code == 302
        assert "_auth_user_id" not in client.session


def test_a_session_without_a_login_time_is_ended(made: made_up.MadeUp) -> None:
    client = Client()
    sign_in(client, made.member)
    session = client.session
    del session["login_at"]
    session.save()
    assert get(client, "/me/").status_code == 302


def test_an_administrator_session_ends_twelve_hours_after_the_email_code(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        email_factor(client, made.administrator)
        device = Authenticator.objects.of(made.organization).get(
            member=made.administrator
        )
        # The authenticator is accepted 5 minutes after the email code.
        pending = client.session["pending_at"]
        assert pending == NOW.timestamp()
        with (
            at(300),
            mock.patch(
                "randirandi.core.authenticator_views.utc_now",
                return_value=NOW + timedelta(seconds=300),
            ),
            mock.patch(
                "randirandi.core.second_factor.utc_now",
                return_value=NOW + timedelta(seconds=300),
            ),
        ):
            assert (
                post(
                    client, "/login/authenticator/", code=token(device, 300)
                ).status_code
                == 302
            )
            assert client.session["admin_confirmed"] is True
            assert client.session["login_at"] == NOW.timestamp()
        keep_active(client, 41760)
        with at(ABSOLUTE_SECONDS - 1):
            assert get(client, "/me/").status_code == 200
            assert "_auth_user_id" in client.session
        with at(ABSOLUTE_SECONDS):
            assert get(client, "/me/").status_code == 302
            assert "_auth_user_id" not in client.session


def test_the_login_moment_is_the_accepted_email_code_for_an_administrator() -> None:
    now = NOW.timestamp()
    session = SessionStore()
    assert login_moment(session, now) == now
    session["pending_at"] = now - 300
    assert login_moment(session, now) == now - 300
    session["pending_at"] = now - 601  # older than a pending factor lives
    assert login_moment(session, now) == now
    session["pending_at"] = now + 5  # from the future
    assert login_moment(session, now) == now
    session["pending_at"] = True
    assert login_moment(session, now) == now


def test_remaining_never_exceeds_idle_and_never_reaches_zero() -> None:
    now = NOW.timestamp()
    session = SessionStore()
    session["login_at"] = now - 100
    assert remaining(session, now) == IDLE_SECONDS
    session["login_at"] = now - ABSOLUTE_SECONDS + 10
    assert remaining(session, now) == 10
    session["login_at"] = now - 5 * ABSOLUTE_SECONDS
    assert remaining(session, now) == 1


def returned(browser: Client, order: uuid.UUID) -> int:
    response = browser.get(RETURN, {"order": str(order)}, HTTP_HOST=HOST, secure=True)
    assert response["Location"] == "/login/"
    return response.status_code


def test_the_anonymous_return_writes_nothing_over_its_limit(
    made: made_up.MadeUp,
) -> None:
    order = uuid.uuid4()
    with mock.patch("randirandi.core.login_codes.utc_now", return_value=NOW):
        for _ in range(RETURN_LIMIT):
            browser = Client()
            assert returned(browser, order) == 302
            assert browser.session["return_order"] == str(order)
        rows = Session.objects.count()
        for _ in range(3):
            browser = Client()
            assert returned(browser, order) == 302
            assert settings.SESSION_COOKIE_NAME not in browser.cookies
        assert Session.objects.count() == rows
        assert (
            LoginAttempt.objects.of(made.organization).filter(kind="return").count()
            == RETURN_LIMIT
        )


def test_the_return_limit_is_per_network_and_per_window(made: made_up.MadeUp) -> None:
    order = uuid.uuid4()
    with mock.patch("randirandi.core.login_codes.utc_now", return_value=NOW):
        for _ in range(RETURN_LIMIT):
            returned(Client(), order)
        other = Client(REMOTE_ADDR="192.0.2.77")
        returned(other, order)
        assert other.session["return_order"] == str(order)
    with mock.patch(
        "randirandi.core.login_codes.utc_now",
        return_value=NOW + timedelta(minutes=15),
    ):
        later = Client()
        returned(later, order)
        assert later.session["return_order"] == str(order)
