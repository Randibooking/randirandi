# SPDX-License-Identifier: AGPL-3.0-only
"""Real passwordless HTTP sessions, idle expiry and current authority."""

import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings

from randirandi import labels as labels_module
from randirandi import setup_file
from randirandi.core import login_session_views, setup
from randirandi.core.authentication import establish_session
from randirandi.core.login_codes import issue_code, verify_code
from randirandi.core.login_delivery import send_pending_codes
from randirandi.core.models import AccessEvent, LoginCode, Member, Role
from tests import made_up
from tests.capacity_app import application_login
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2028, 3, 1, 12, tzinfo=UTC)
HOST = "harbourview.sandbox.example.org"


@pytest.fixture(autouse=True)
def page_settings() -> Iterator[None]:
    with (
        override_settings(
            ALLOWED_HOSTS=[".sandbox.example.org"],
            ORGANIZATION_DOMAIN="sandbox.example.org",
            ACCESS_GATE_PASSWORD="",
        ),
        mock.patch("randirandi.core.login.wake_delivery"),
        mock.patch("randirandi.core.authentication.utc_now", return_value=NOW),
    ):
        yield


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def get(client: Client, path: str = "/", host: str = HOST) -> HttpResponse:
    response = client.get(path, HTTP_HOST=host, secure=True)
    assert isinstance(response, HttpResponse)
    return response


def post(client: Client, path: str, host: str = HOST, **data: str) -> HttpResponse:
    data["csrfmiddlewaretoken"] = client.cookies[settings.CSRF_COOKIE_NAME].value
    response = client.post(
        path, data, HTTP_HOST=host, HTTP_ORIGIN="https://" + host, secure=True
    )
    assert isinstance(response, HttpResponse)
    return response


def request_code(client: Client, member: Member, host: str = HOST) -> str:
    assert get(client, "/login/", host).status_code == 200
    response = post(client, "/login/", host, identifier=member.email)
    assert response.status_code == 302
    assert response.headers["Location"] == "/login/code/"
    assert get(client, "/login/code/", host).status_code == 200
    send_pending_codes()
    message = next(
        message for message in reversed(mail.outbox) if message.to == [member.email]
    )
    match = re.search(r"\b[0-9]{6}\b", str(message.body))
    assert match is not None
    return match.group()


def sign_in(client: Client, member: Member, host: str = HOST) -> HttpResponse:
    code = request_code(client, member, host)
    return post(client, "/login/code/", host, code=code)


@pytest.mark.parametrize("administrator", [False, True])
@pytest.mark.parametrize(
    "delivery_delay,code_age,accepted",
    [(240, 420, True), (599, 599, True), (240, 600, False)],
)
def test_delayed_email_retains_browser_binding_without_extending_code_expiry(
    made: made_up.MadeUp,
    administrator: bool,
    delivery_delay: int,
    code_age: int,
    accepted: bool,
) -> None:
    client = Client(enforce_csrf_checks=True)
    member = made.administrator if administrator else made.member
    delivered_at = NOW + timedelta(seconds=delivery_delay)
    submitted_at = delivered_at + timedelta(seconds=code_age)
    with application_login():
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            assert get(client, "/login/").status_code == 200
            assert post(client, "/login/", identifier=member.email).status_code == 302
            challenge = client.session["login_challenge"]
            session_key = client.cookies[settings.SESSION_COOKIE_NAME].value
            browser_expiry = Session.objects.get(pk=session_key).expire_date
        with mock.patch("django.utils.timezone.now", return_value=delivered_at):
            send_pending_codes()
            issued = LoginCode.objects.of(made.organization).get(challenge=challenge)
            match = re.search(r"\b[0-9]{6}\b", str(mail.outbox[-1].body))
            assert match is not None
        assert issued.expires_at == delivered_at + timedelta(minutes=10)
        assert browser_expiry >= issued.expires_at
        with (
            mock.patch("django.utils.timezone.now", return_value=submitted_at),
            mock.patch(
                "randirandi.core.authentication.utc_now", return_value=submitted_at
            ),
        ):
            response = post(client, "/login/code/", code=match.group())
            if not accepted:
                assert response.status_code == 400
                assert "_auth_user_id" not in client.session
                assert "pending_member" not in client.session
            elif administrator:
                assert response.status_code == 302
                assert response.headers["Location"] == "/login/authenticator/"
                assert client.session["pending_member"] == member.pk
                assert "_auth_user_id" not in client.session
                assert response.cookies[settings.SESSION_COOKIE_NAME]["max-age"] == 600
            else:
                assert response.status_code == 302
                assert response.headers["Location"] == "/"
                assert client.session["_auth_user_id"] == str(member.pk)
                assert response.cookies[settings.SESSION_COOKIE_NAME]["max-age"] == 1800


def test_retained_browser_binding_does_not_extend_queue_lifetime(
    made: made_up.MadeUp,
) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            assert get(client, "/login/").status_code == 200
            assert (
                post(client, "/login/", identifier=made.member.email).status_code == 302
            )
            challenge = client.session["login_challenge"]
        with mock.patch(
            "django.utils.timezone.now", return_value=NOW + timedelta(minutes=10)
        ):
            send_pending_codes()
            assert not mail.outbox
            assert (
                not LoginCode.objects.of(made.organization)
                .filter(challenge=challenge)
                .exists()
            )
            assert post(client, "/login/code/", code="000000").status_code == 400
            assert "_auth_user_id" not in client.session
        with mock.patch(
            "django.utils.timezone.now", return_value=NOW + timedelta(minutes=20)
        ):
            response = get(client, "/login/code/")
            assert response.status_code == 302
            assert response.headers["Location"] == "/login/"
            assert "login_browser" not in client.session


def test_member_login_rotates_ids_csrf_and_has_secure_host_only_cookie(
    made: made_up.MadeUp,
) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        code = request_code(client, made.member)
        before = client.session.session_key
        csrf_before = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = post(client, "/login/code/", code=code)
        assert response.status_code == 302 and response.headers["Location"] == "/"
        assert client.session.session_key != before
        assert not Session.objects.filter(session_key=before).exists()
        assert client.cookies[settings.CSRF_COOKIE_NAME].value != csrf_before
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        assert cookie["secure"] and cookie["httponly"]
        assert cookie["samesite"] == "Lax" and cookie["domain"] == ""
        assert cookie["path"] == "/" and cookie["max-age"] == 1800
        assert client.session["_auth_user_id"] == str(made.member.pk)
        assert "login_browser" not in client.session
        assert get(client).headers["Location"] == "/book/"
        assert "My page" in get(client, "/me/").content.decode()
        assert get(client, "/login/").headers["Location"] == "/"


def test_idle_limit_is_thirty_minutes_and_activity_extends_it(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    sign_in(client, made.member)
    for elapsed in (1799, 3598):
        with mock.patch(
            "randirandi.core.authentication.utc_now",
            return_value=NOW + timedelta(seconds=elapsed),
        ):
            assert get(client, "/me/").status_code == 200
            assert client.session["active_at"] == NOW.timestamp() + elapsed
    with mock.patch(
        "randirandi.core.authentication.utc_now",
        return_value=NOW + timedelta(seconds=5398),
    ):
        assert get(client).status_code == 302
        assert "_auth_user_id" not in client.session


def test_health_poll_does_not_extend_activity(made: made_up.MadeUp) -> None:
    client = Client()
    sign_in(client, made.member)
    with mock.patch(
        "randirandi.core.authentication.utc_now",
        return_value=NOW + timedelta(seconds=1799),
    ):
        assert get(client, "/health/").status_code == 200
        assert client.session["active_at"] == NOW.timestamp()
    with mock.patch(
        "randirandi.core.authentication.utc_now",
        return_value=NOW + timedelta(seconds=1800),
    ):
        assert get(client).status_code == 302


@pytest.mark.parametrize("change", ["revoke", "revoke-regrant", "email", "admin-role"])
def test_changed_authority_ends_existing_sessions(
    made: made_up.MadeUp, change: str
) -> None:
    client = Client()
    sign_in(client, made.member)
    if change in {"revoke", "revoke-regrant"}:
        Member.objects.of(made.organization).filter(pk=made.member.pk).update(
            status="disabled"
        )
        AccessEvent.objects.create(
            organization=made.organization,
            member=made.member,
            change="disabled",
            cause="member import",
        )
        if change == "revoke-regrant":
            Member.objects.of(made.organization).filter(pk=made.member.pk).update(
                status="active"
            )
            AccessEvent.objects.create(
                organization=made.organization,
                member=made.member,
                change="active",
                cause="member import",
            )
    elif change == "email":
        Member.objects.of(made.organization).filter(pk=made.member.pk).update(
            email="changed@example.test"
        )
    else:
        Role.objects.of(made.organization).filter(pk=made.member.role_id).update(
            admin_access=True
        )
    assert get(client).status_code == 302
    assert "_auth_user_id" not in client.session


def test_copied_cookie_does_not_cross_organization(made: made_up.MadeUp) -> None:
    other = made_up.everything("elsewhere")
    client = Client()
    sign_in(client, made.member)
    response = client.get(
        "/",
        HTTP_HOST=f"{other.organization.subdomain}.sandbox.example.org",
        secure=True,
    )
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


def test_logout_is_csrf_protected_and_invalidates_old_cookie(
    made: made_up.MadeUp,
) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    old = client.cookies[settings.SESSION_COOKIE_NAME].value
    assert get(client, "/logout/").status_code == 405
    assert client.post("/logout/", HTTP_HOST=HOST, secure=True).status_code == 403
    assert post(client, "/logout/").status_code == 302
    assert get(client).status_code == 302
    client.cookies[settings.SESSION_COOKIE_NAME] = old
    assert get(client).status_code == 302
    assert not Session.objects.filter(session_key=old).exists()


def test_email_alone_never_grants_an_administrator_session(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    response = sign_in(client, made.administrator)
    assert response.status_code == 302
    assert response.headers["Location"] == "/login/authenticator/"
    assert "_auth_user_id" not in client.session
    assert client.session["pending_member"] == made.administrator.pk
    assert get(client).status_code == 302
    assert get(client, "/login/authenticator/").status_code == 200


def test_browser_binding_still_holds_through_http(made: made_up.MadeUp) -> None:
    first, second = Client(), Client()
    code = request_code(first, made.member)
    get(second, "/login/")
    session = second.session
    session["login_organization"] = made.organization.pk
    session["login_challenge"] = first.session["login_challenge"]
    session["login_browser"] = "a_different_browser_binding_123456789"
    session.save()
    assert post(second, "/login/code/", code=code).status_code == 400
    assert post(first, "/login/code/", code=code).status_code == 302
    assert get(first, "/me/").status_code == 200
    assert get(second).status_code == 302


def test_revocation_between_code_and_grant_cannot_be_hidden_by_regrant(
    made: made_up.MadeUp,
) -> None:
    issued = issue_code(
        organization=made.organization,
        member_id=made.member.pk,
        browser="fictional_browser_binding_only_123456789",
        network="192.0.2.1",
    )
    assert issued is not None
    proof = verify_code(
        organization=made.organization,
        challenge=issued.challenge,
        browser="fictional_browser_binding_only_123456789",
        code=issued.code,
        network="192.0.2.1",
    )
    assert proof is not None
    for change in ("disabled", "active"):
        AccessEvent.objects.create(
            organization=made.organization,
            member=made.member,
            change=change,
            cause="member import",
        )
    request = RequestFactory().get("/", HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = AnonymousUser()
    assert not establish_session(request, proof)
    assert "_auth_user_id" not in request.session


ORDER_PAGE = "/book/order/5d7f0c3e-0000-4000-8000-000000000001/"


@pytest.mark.parametrize("path,heading", [("/book/", "Book"), ("/me/", "My page")])
def test_member_pages_show_their_place_and_are_never_cached(
    made: made_up.MadeUp, path: str, heading: str
) -> None:
    client = Client()
    sign_in(client, made.member)
    response = get(client, path)
    body = response.content.decode()
    assert response.status_code == 200
    assert f"<h1>{heading}</h1>" in body
    assert 'href="/book/"' in body and 'href="/me/"' in body
    assert 'href="/events/"' in body
    assert "no-store" in response.headers["Cache-Control"]
    assert get(client).headers["Location"] == "/book/"
    order = "/?order=5d7f0c3e-0000-4000-8000-000000000001"
    assert get(client, order).headers["Location"] == f"/book/{order[1:]}"


MEMBER_SCREENS = [
    "/book/",
    "/book/cart/",
    "/book/prices/",
    ORDER_PAGE,
    "/events/",
    "/events/1/",
    "/me/",
]


def assert_sign_out_and_admin_only_on_my_page(
    body: str, path: str, administrator: bool
) -> None:
    assert 'href="/book/"' in body and 'href="/me/"' in body
    assert ("Sign out" in body) == (path == "/me/")
    assert ('href="/admin/"' in body) == (path == "/me/" and administrator)
    if path == "/me/":
        # They come last, after the heading.
        assert body.index("<h1>") < body.index("Sign out")
        if administrator:
            assert body.index('href="/admin/"') < body.index("Sign out")
            assert body.index("<h1>") < body.index('href="/admin/"')


@pytest.mark.parametrize("path", MEMBER_SCREENS)
def test_only_my_page_carries_sign_out(made: made_up.MadeUp, path: str) -> None:
    client = Client()
    sign_in(client, made.member)
    body = get(client, path).content.decode()
    assert_sign_out_and_admin_only_on_my_page(body, path, False)


ORDER = "5d7f0c3e-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    "path,heading,screen",
    [
        ("/book/cart/", "Cart", "cart"),
        (f"/book/order/{ORDER}/", "Booking", "order"),
        ("/book/prices/", "Prices", "prices"),
    ],
)
def test_cart_and_order_pages_are_member_pages_and_never_cached(
    made: made_up.MadeUp, path: str, heading: str, screen: str
) -> None:
    client = Client()
    sign_in(client, made.member)
    response = get(client, path)
    body = response.content.decode()
    assert response.status_code == 200
    assert f"<h1>{heading}</h1>" in body
    assert "no-store" in response.headers["Cache-Control"]
    found = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>', body, re.S
    )
    assert found
    config = json.loads(found.group(1))
    assert config["screen"] == screen
    assert "choices" in config
    assert config.get("order") == (ORDER if screen == "order" else None)


def test_my_page_carries_the_members_name_and_no_currency(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    sign_in(client, made.member)
    body = get(client, "/me/").content.decode()
    found = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>', body, re.S
    )
    assert found
    config = json.loads(found.group(1))
    assert config["member_name"] == "Alex Rivers"
    # The role in the setup's own words, under the name.
    assert config["member_role"] == made.member.role.name
    # Each booking on the page says its own order's currency (7.3).
    assert "currency" not in config
    # Book has them too: My page is reached from it without a reload.
    book = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>',
        get(client, "/book/").content.decode(),
        re.S,
    )
    assert book
    assert json.loads(book.group(1))["member_name"] == "Alex Rivers"


def test_my_page_carries_who_the_member_is_and_nothing_of_the_others(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core.models import MemberMembership

    made.membership.address = "Fjordgata 1"
    made.membership.address_unit = "H0201"
    made.membership.postal_code = "0476"
    made.membership.city = "Oslo"
    made.membership.save()
    # Another member of the same membership: none of their data may appear.
    MemberMembership.objects.create(
        organization=made.organization,
        member=made.administrator,
        membership=made.membership,
    )
    client = Client()
    sign_in(client, made.member)
    config = config_of(get(client, "/me/"))
    assert config["member_email"] == made.member.email
    assert config["member_phone"] == "+4790000001"
    assert config["member_memberships"] == [
        {"identifier": "H0201", "address": "Fjordgata 1 H0201, 0476 Oslo"}
    ]
    assert config["contact_email"] == made.organization.contact_email
    assert made.administrator.email not in json.dumps(config)
    assert "Sam Field" not in json.dumps(config)


def config_of(response: HttpResponse) -> dict[str, Any]:
    found = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>',
        response.content.decode(),
        re.S,
    )
    assert found
    config: dict[str, Any] = json.loads(found.group(1))
    return config


@pytest.mark.parametrize(
    "path", ["/book/", "/me/", "/book/cart/", "/book/prices/", f"/book/order/{ORDER}/"]
)
def test_member_pages_carry_the_organizations_own_labels_only(
    made: made_up.MadeUp, path: str
) -> None:
    client = Client()
    sign_in(client, made.member)
    # None set: the page holds the defaults itself and is sent none.
    assert config_of(get(client, path))["labels"] == {}

    organization = made.member.organization
    organization.labels = {"place.book": "Reserve", "cart.title": "Your reservations"}
    organization.save()

    assert config_of(get(client, path))["labels"] == {
        "place.book": "Reserve",
        "cart.title": "Your reservations",
    }

    # A stored text the rule refuses never reaches the page.
    organization.labels = {"place.book": "Reserve", "no.such": "x", "place.me": ""}
    organization.save()

    assert config_of(get(client, path))["labels"] == {"place.book": "Reserve"}


@pytest.mark.parametrize("path", ["/book/", "/me/"])
def test_member_pages_carry_the_words_of_the_organizations_language(
    made: made_up.MadeUp, path: str, second_language: None
) -> None:
    client = Client()
    sign_in(client, made.member)
    organization = made.member.organization
    organization.labels = {"place.book": "Mine"}
    organization.save()

    # English: the page holds the English catalogue itself; only its own words.
    assert config_of(get(client, path))["labels"] == {"place.book": "Mine"}

    # Another language: the catalogue's words, its own words over them.
    organization.language = "xx"
    organization.save()
    sent = config_of(get(client, path))["labels"]
    assert sent == {**labels_module.CATALOGUES["xx"], "place.book": "Mine"}
    assert sent["weekdays.thu"] == "Torsdag xx"

    # A stored text the catalogue's placeholders refuse never reaches the page.
    organization.labels = {"stack.needs": "Needs {length} free from {time}"}
    organization.save()
    assert config_of(get(client, path))["labels"]["stack.needs"] == "Needs {length} xx"


def test_the_labels_of_a_loaded_setup_reach_the_member_pages() -> None:
    written = setup_file.read(SETUPS / "scenarios" / "four-beds.toml")
    organization = setup.load(written, today=NOW.date())
    assert organization.standard_role_id is not None
    member = Member.objects.create(
        organization=organization,
        role_id=organization.standard_role_id,
        email="guest@four-beds.example",
        status="active",
        origin="member import",
    )
    host = "four-beds.sandbox.example.org"
    client = Client()

    assert sign_in(client, member, host).status_code == 302

    for path in ("/book/", "/me/"):
        assert config_of(get(client, path, host))["labels"] == {
            "place.book": "Reserve",
            "button.book": "Reserve",
            "cart.title": "Your reservations",
            "outcome.booked": "You are booked in",
        }


@pytest.mark.parametrize(
    "path", ["/book/cart/", "/book/prices/", f"/book/order/{ORDER}/"]
)
def test_cart_and_order_pages_send_a_signed_out_browser_to_sign_in(
    path: str,
) -> None:
    response = get(Client(), path)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


def test_order_page_needs_an_order_id(made: made_up.MadeUp) -> None:
    client = Client()
    sign_in(client, made.member)
    assert get(client, "/book/order/x/").status_code == 404


def test_cart_and_order_pages_refuse_a_signed_in_non_member(
    made: made_up.MadeUp,
) -> None:
    request = RequestFactory().get("/book/cart/", HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made_up.everything("elsewhere").member
    with pytest.raises(PermissionDenied):
        login_session_views.cart(request)
    with pytest.raises(PermissionDenied):
        login_session_views.prices(request)
    with pytest.raises(PermissionDenied):
        login_session_views.order(request, uuid.UUID(ORDER))


@pytest.mark.parametrize("path", ["/book/", "/me/"])
def test_member_pages_send_a_signed_out_browser_to_sign_in(
    made: made_up.MadeUp, path: str
) -> None:
    response = get(Client(), path)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


@pytest.mark.parametrize("name", ["book", "me"])
def test_member_pages_refuse_a_signed_in_non_member(
    made: made_up.MadeUp, name: str
) -> None:
    request = RequestFactory().get(f"/{name}/", HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made_up.everything("elsewhere").member
    with pytest.raises(PermissionDenied):
        getattr(login_session_views, name)(request)
    assert login_session_views.home(request).status_code == 200


@pytest.mark.parametrize(
    "path", ["/book/", "/me/", "/book/cart/", "/book/prices/", f"/book/order/{ORDER}/"]
)
def test_member_pages_answer_only_get_and_head(made: made_up.MadeUp, path: str) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    assert post(client, path).status_code == 405
    assert client.head(path, HTTP_HOST=HOST, secure=True).status_code == 200


def test_prices_page_carries_the_standard_roles_categories_and_no_other(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core.models import BookingCategory, Role, RoleCategory

    internal = BookingCategory.objects.create(
        organization=made.organization, name="internal"
    )
    staff = Role.objects.create(organization=made.organization, name="staff")
    RoleCategory.objects.create(
        organization=made.organization, role=staff, booking_category=internal
    )
    client = Client()
    sign_in(client, made.member)
    standard = made.organization.standard_role
    assert standard is not None
    expected = [
        {"id": row.pk, "name": row.name, "icon": row.icon}
        for row in standard.booking_categories.order_by("pk")
    ]
    assert expected and internal.pk not in [row["id"] for row in expected]
    for path in ("/book/prices/", "/book/"):
        assert config_of(get(client, path))["price_categories"] == expected


def test_prices_page_has_no_columns_without_a_standard_role(
    made: made_up.MadeUp,
) -> None:
    made.organization.standard_role = None
    made.organization.save()
    client = Client()
    sign_in(client, made.member)
    assert config_of(get(client, "/book/prices/"))["price_categories"] == []


def test_prices_page_has_every_standard_column_for_a_member_who_lacks_one(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core.models import BookingCategory, Role, RoleCategory

    work = BookingCategory.objects.create(organization=made.organization, name="work")
    standard = made.organization.standard_role
    assert standard is not None
    RoleCategory.objects.create(
        organization=made.organization, role=standard, booking_category=work
    )
    narrow = Role.objects.create(organization=made.organization, name="narrow")
    for row in standard.booking_categories.exclude(pk=work.pk):
        RoleCategory.objects.create(
            organization=made.organization, role=narrow, booking_category=row
        )
    made.member.role = narrow
    made.member.save()
    client = Client()
    sign_in(client, made.member)
    shown = [
        row["name"]
        for row in config_of(get(client, "/book/prices/"))["price_categories"]
    ]
    assert "work" in shown
    assert shown == [row.name for row in standard.booking_categories.order_by("pk")]


def test_a_signed_in_user_from_elsewhere_gets_a_page_with_sign_out(
    made: made_up.MadeUp,
) -> None:
    # Home redirects only a member of this organization; anyone else must still
    # have a way out. It is not a member page: no places. Today the session
    # policy middleware ends such a session before the view, so no request
    # reaches this case; the test calls the views directly and guards the page
    # should that ever change.
    elsewhere = made_up.everything("elsewhere")
    request = RequestFactory().get("/", HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = elsewhere.member
    response = login_session_views.home(request)
    body = response.content.decode()
    assert response.status_code == 200
    assert "<h1>Signed in</h1>" in body and "Sign out" in body
    assert 'action="/logout/"' in body and 'name="csrfmiddlewaretoken"' in body
    assert 'href="/book/"' not in body
    out = RequestFactory().post("/logout/", HTTP_HOST=HOST, secure=True)
    out.session = SessionStore()
    out.session["_auth_user_id"] = str(elsewhere.member.pk)
    out.user = elsewhere.member
    assert login_session_views.logout(out).status_code == 302
    assert "_auth_user_id" not in out.session
