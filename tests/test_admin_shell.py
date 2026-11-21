# SPDX-License-Identifier: AGPL-3.0-only
"""The administrators' shell and its gate ."""

import html
import json
import re

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, override_settings

from randirandi.core import admin_views
from randirandi.core.models import Authenticator, Member, Role
from tests import made_up
from tests.capacity_app import application_login
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    MEMBER_SCREENS,
    assert_sign_out_and_admin_only_on_my_page,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
PLACES = [
    "bookings", "schedule", "blocks", "members", "payments", "assets",
    "containers", "types", "roles", "prices", "organization", "audit",
]  # fmt: skip


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def config(body: str) -> dict[str, object]:
    found = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>', body, re.S
    )
    assert found is not None
    parsed = json.loads(found.group(1))
    assert isinstance(parsed, dict)
    return parsed


def sign_in_administrator(client: Client, member: Member) -> None:
    response = post(client, "/login/code/", code=request_code(client, member))
    assert response.headers["Location"] == "/login/authenticator/"
    assert get(client, "/login/authenticator/").status_code == 200
    device = Authenticator.objects.of(member.organization_id).get(member=member)
    assert post(client, "/login/authenticator/", code=token(device)).status_code == 302


def test_administrator_gets_every_place_with_its_title(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for place in PLACES:
            response = get(client, f"/admin/{place}/")
            body = response.content.decode()
            ui = config(body)
            assert response.status_code == 200
            assert "no-store" in response.headers["Cache-Control"]
            assert ui["screen"] == "admin" and ui["place"] == place
            assert ui["organization"] == made.organization.name
            assert ui["time_zone"] == "Europe/Oslo"
            assert ui["name"] == "Sam Field" and ui["csrf"]
            assert f'href="/admin/{place}/"' in body and 'action="/logout/"' in body
            assert "<h1>" in body and "Sign out" in body
        assert (
            'aria-current="page">Roles' in get(client, "/admin/roles/").content.decode()
        )
        assert get(client, "/admin/").headers["Location"] == "/admin/bookings/"
        assert get(client, "/admin/nowhere/").status_code == 404
        assert get(client, "/admin/bookings/more/").status_code == 404


def test_name_falls_back_to_first_name(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        full_name=""
    )
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        assert config(get(client, "/admin/audit/").content.decode())["name"] == "Sam"


def test_signed_out_browser_goes_to_sign_in(made: made_up.MadeUp) -> None:
    for path in ("/admin/", "/admin/bookings/", "/admin/nowhere/"):
        response = get(Client(), path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")


def test_member_without_admin_access_gets_the_403_page(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        assert post(client, "/login/code/", code=request_code(client, made.member))
        for path in ("/admin/bookings/", "/admin/nowhere/"):
            response = get(client, path)
            assert response.status_code == 403
            assert "Please try again" in response.content.decode()
        assert 'href="/admin/' not in get(client, "/me/").content.decode()


def test_role_taken_away_closes_the_open_session(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        assert get(client, "/admin/bookings/").status_code == 200
        Role.objects.of(made.organization).filter(pk=made.administrator.role_id).update(
            admin_access=False
        )
        gone = get(client, "/admin/bookings/")
        assert gone.status_code == 302 and gone.headers["Location"].startswith(
            "/login/"
        )


def test_session_without_the_second_step_is_refused(made: made_up.MadeUp) -> None:
    request = RequestFactory().get("/admin/bookings/", HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made.administrator
    with pytest.raises(PermissionDenied):
        admin_views.place(request, "bookings")
    request.session["admin_confirmed"] = False
    with pytest.raises(PermissionDenied):
        admin_views.place(request, "bookings")
    request.session["admin_confirmed"] = True
    assert admin_views.place(request, "bookings").status_code == 200


def test_another_organizations_administrator_is_refused(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    request = RequestFactory().get("/admin/bookings/", HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = elsewhere.administrator
    with pytest.raises(PermissionDenied):
        admin_views.place(request, "bookings")


def test_methods_and_cache(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path in ("/admin/", "/admin/bookings/"):
            assert post(client, path).status_code == 405
        head = client.head("/admin/bookings/", HTTP_HOST=HOST, secure=True)
        assert head.status_code == 200 and "no-store" in head.headers["Cache-Control"]
        redirect = get(client, "/admin/")
        assert "no-store" in redirect.headers["Cache-Control"]


def test_the_admin_flag_and_the_no_script_link_only_for_an_administrator(
    made: made_up.MadeUp,
) -> None:
    member, admin = Client(), Client()
    with application_login():
        assert post(member, "/login/code/", code=request_code(member, made.member))
        sign_in_administrator(admin, made.administrator)
        shown = get(admin, "/me/")
        assert config(shown.content.decode())["admin"] is True
        assert 'href="/admin/"' in shown.content.decode()
        hidden = get(member, "/me/")
        assert config(hidden.content.decode())["admin"] is False
        assert 'href="/admin/"' not in hidden.content.decode()
        assert config(get(admin, "/book/").content.decode())["admin"] is True
        assert config(get(member, "/book/").content.decode())["admin"] is False


def test_the_browser_lists_the_same_places_as_the_server() -> None:
    script = (settings.BASE_DIR / "frontend/src/admin/places.js").read_text()
    shown = re.findall(r"\['([a-z]+)', '([^']+)'\]", script)
    assert shown == [row for _, rows in admin_views.PLACES for row in rows]


@pytest.mark.parametrize("path", MEMBER_SCREENS)
def test_an_administrator_finds_admin_and_sign_out_only_on_my_page(
    made: made_up.MadeUp, path: str
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        body = get(client, path).content.decode()
    assert_sign_out_and_admin_only_on_my_page(body, path, True)


def test_configured_help_reaches_login_member_admin_and_error_pages(
    made: made_up.MadeUp,
) -> None:
    destination = "https://help.example.org/index-nb.html?from=app&role=reader"
    client = Client()
    with override_settings(DOCUMENTATION_URL=destination), application_login():
        pages = [get(client, "/login/")]
        sign_in_administrator(client, made.administrator)
        pages.extend(get(client, path) for path in ("/me/", "/admin/bookings/"))
        missing = get(client, "/not-a-route/")
        assert missing.status_code == 404
        assert all(page.status_code == 200 for page in pages)
        for page in [*pages, missing]:
            body = page.content.decode()
            assert config(body)["documentation"] == destination
            assert f'href="{html.escape(destination, quote=True)}"' in body
