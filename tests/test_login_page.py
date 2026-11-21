# SPDX-License-Identifier: AGPL-3.0-only
"""Public login boundary, with no code issuance or session grant yet."""

import html
import json
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from randirandi import labels
from randirandi.core.login import LoginForm
from randirandi.core.models import (
    LoginCode,
    LoginRequest,
    Member,
    MemberStatus,
    Organization,
    Origin,
    Role,
)
from randirandi.core.presentation import DOCUMENTATION_URL
from randirandi.environment import organization_domain, sender
from tests.made_up import organization

HOST = "harbourview.sandbox.example.org"


@pytest.fixture(autouse=True)
def page_settings() -> Iterator[None]:
    with (
        override_settings(
            ALLOWED_HOSTS=[".sandbox.example.org", ".example.org", ".localhost"],
            ORGANIZATION_DOMAIN="sandbox.example.org",
            ACCESS_GATE_PASSWORD="",
            STORAGES={
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                }
            },
        ),
        mock.patch("randirandi.core.login.wake_delivery"),
    ):
        yield


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Alex@Example.COM ", "alex@example.com"),
        ("alex+booking@example.com", "alex+booking@example.com"),
        (" +44 7700 900123 ", "+447700900123"),
        ("+4790000001", "+4790000001"),
    ],
)
def test_identifier_uses_the_member_storage_format(value: str, expected: str) -> None:
    form = LoginForm({"identifier": value})
    assert form.is_valid()
    assert form.cleaned_data["identifier"] == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "alex@",
        "@example.com",
        "90000001",
        "00447700900123",
        "+0123456",
        "+12345",
        "+1234567890123456",
        "+٤٤٧٧٠٠٩٠٠١٢٣",
        "x" * 255,
        "<script>alert(1)</script>",
    ],
)
def test_malformed_identifiers_are_refused(value: str) -> None:
    assert not LoginForm({"identifier": value}).is_valid()


@pytest.mark.parametrize("host", [HOST, HOST.upper(), HOST + ".", HOST + ":8443"])
@pytest.mark.django_db(transaction=True)
def test_login_selects_the_organization_from_the_validated_host(host: str) -> None:
    org = organization()
    organization("elsewhere")
    response = Client().get(
        "/login/?organization=elsewhere&identifier=ignore@example.com",
        HTTP_HOST=host,
        HTTP_X_FORWARDED_HOST="elsewhere.sandbox.example.org",
        secure=True,
    )
    assert response.status_code == 200
    assert response.context["organization"] == org
    body = response.content.decode()
    assert org.name in body
    assert "elsewhere housing" not in body
    assert "ignore@example.com" not in body
    assert 'name="identifier"' in body
    assert 'name="password"' not in body
    assert 'type="email"' not in body
    assert 'method="post" action="/login/"' in body
    assert "csrfmiddlewaretoken" in body
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "same-origin"
    assert response.cookies[settings.CSRF_COOKIE_NAME]["secure"]
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert not Session.objects.exists()


def assert_beta_bar(body: str, address: str) -> None:
    """The bar at the top : the label around the
    organization's contact address, above the card."""
    before, _, after = labels.DEFAULTS["shell.beta"].partition("{address}")
    link = f'<a href="mailto:{address}">{address}</a>'
    bar = f'<div class="fallback-beta">{before}{link}{after}</div>'
    assert bar in body
    assert before + address + after == (
        f"We just opened. Tell us what you think: {address}"
    )
    assert body.index(bar) < body.index("fallback-group")


@pytest.mark.django_db(transaction=True)
def test_the_sign_in_page_carries_the_beta_bar() -> None:
    organization()
    assert_beta_bar(
        Client().get("/login/", HTTP_HOST=HOST, secure=True).content.decode(),
        "board@harbourview.example",
    )


@pytest.mark.django_db(transaction=True)
def test_the_sign_in_page_is_in_the_organizations_language(
    second_language: None,
) -> None:
    org = organization()
    org.language = "xx"
    org.save()
    body = Client().get("/login/", HTTP_HOST=HOST, secure=True).content.decode()

    for text in (
        "Hi xx, I am Randi",
        f"I keep rooms of {org.name} xx",
        "See free xx",
        "Book xx",
        "Pay xx",
        "A service xx",
        "Documentation xx",
        "Prices xx",
        "Conditions link xx",
    ):
        assert html.escape(text, quote=True) in body, text
    address = "board@harbourview.example"
    link = f'<a href="mailto:{address}">{address}</a>'
    assert f'<div class="fallback-beta">Beta xx: {link}</div>' in body
    assert "We just opened" not in body and "Hi, I am Randi" not in body


@pytest.mark.django_db(transaction=True)
def test_the_beta_bar_names_the_organizations_contact_and_no_other() -> None:
    organization()
    client = Client(raise_request_exception=False)
    login = client.get("/login/", HTTP_HOST=HOST, secure=True)
    missing = client.get("/missing/", HTTP_HOST=HOST, secure=True)
    assert (login.status_code, missing.status_code) == (200, 404)
    assert_beta_bar(login.content.decode(), "board@harbourview.example")
    assert settings.SENDER.address not in login.content.decode()
    # No organization, no address to show: the page has no bar.
    assert "fallback-beta" not in missing.content.decode()


@pytest.mark.parametrize(
    "host",
    [
        "sandbox.example.org",
        "harbourview.example.org",
        "harbourview.other.sandbox.example.org",
        "unknown.sandbox.example.org",
        "www.sandbox.example.org",
        "localhost",
        "127.0.0.1",
    ],
)
@pytest.mark.django_db(transaction=True)
def test_no_fallback_organization_for_wrong_or_unknown_host(host: str) -> None:
    organization()
    response = Client().get("/login/", HTTP_HOST=host, secure=True)
    assert response.status_code in (400, 404)
    assert "harbourview" not in response.content.decode()


@pytest.mark.parametrize("host", ["harbourview.evil.example", HOST + ".evil.example"])
def test_untrusted_hosts_are_refused_before_any_database_read(host: str) -> None:
    response = Client().post(
        "/login/", {"identifier": "alex@example.com"}, HTTP_HOST=host, secure=True
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_localhost_domain_uses_the_same_organization_resolution() -> None:
    organization()
    with override_settings(ORGANIZATION_DOMAIN="localhost"):
        response = Client().get(
            "/login/", HTTP_HOST="harbourview.localhost:8443", secure=True
        )
    assert response.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_the_no_script_field_shows_an_example_and_keeps_its_label() -> None:
    organization()
    response = Client().get("/login/", HTTP_HOST=HOST, secure=True)
    body = response.content.decode()
    assert 'placeholder="name@domain"' in body
    assert '<label for="id_identifier">Email</label>' in body


@pytest.mark.django_db(transaction=True)
def test_the_no_script_page_gives_the_number_words_and_the_contact_address() -> None:
    organization()
    body = Client().get("/login/", HTTP_HOST=HOST, secure=True).content.decode()
    assert "Email, or phone" not in body
    assert "When you are registered, we send your code by email." in body
    assert (
        '<a href="mailto:board@harbourview.example">board@harbourview.example</a>'
        in body
    )


@pytest.mark.django_db(transaction=True)
def test_the_page_config_carries_the_contact_address_of_the_organization() -> None:
    organization()
    body = Client().get("/login/", HTTP_HOST=HOST, secure=True).content.decode()
    match = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>', body, re.S
    )
    assert match
    config = json.loads(match[1])
    assert config["contact_email"] == "board@harbourview.example"


@pytest.mark.parametrize("referer", [None, "https://unrelated.example/link"])
@pytest.mark.django_db(transaction=True)
def test_the_page_does_not_require_a_referral(referer: str | None) -> None:
    organization()
    headers = {} if referer is None else {"Referer": referer}
    response = Client().get("/login/", HTTP_HOST=HOST, headers=headers, secure=True)
    assert response.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_valid_posts_reveal_no_members_and_queue_without_identity_lookup() -> None:
    org = organization()
    role = Role.objects.create(organization=org, name="Standard")
    for email, phone, status in (
        ("alex@example.com", "+447700900123", MemberStatus.ACTIVE),
        ("revoked@example.com", "+447700900124", MemberStatus.DISABLED),
    ):
        Member.objects.create(
            organization=org,
            role=role,
            email=email,
            phone=phone,
            status=status,
            origin=Origin.MEMBER_IMPORT,
        )
    for value in (
        "alex@example.com",
        "unknown@example.com",
        "revoked@example.com",
        "+44 7700 900123",
    ):
        client = Client()
        with CaptureQueriesContext(connection) as queries:
            response = client.post(
                "/login/",
                {"identifier": value, "organization": "elsewhere"},
                HTTP_HOST=HOST,
                secure=True,
            )
        assert response.status_code == 302
        response = client.get(response.headers["Location"], HTTP_HOST=HOST, secure=True)
        assert response.status_code == 200
        assert "If you're on the member list, I've emailed you a code" in html.unescape(
            response.content.decode()
        )
        assert value not in response.content.decode()
        assert all("core_member" not in query["sql"] for query in queries)
        assert settings.SESSION_COOKIE_NAME in client.cookies
        assert set(client.session.keys()) == {
            "login_browser",
            "login_challenge",
            "login_organization",
            "_session_expiry",
        }
    assert LoginRequest.objects.of(org).count() == 4
    assert not LoginCode.objects.of(org).exists()
    assert Session.objects.count() == 4
    assert not mail.outbox


@pytest.mark.django_db(transaction=True)
def test_post_requires_a_matching_csrf_token_and_origin() -> None:
    organization()
    client = Client(enforce_csrf_checks=True)
    form = {"identifier": "alex@example.com"}
    assert client.post("/login/", form, HTTP_HOST=HOST, secure=True).status_code == 403
    client.get("/login/", HTTP_HOST=HOST, secure=True)
    form["csrfmiddlewaretoken"] = client.cookies[settings.CSRF_COOKIE_NAME].value
    assert (
        client.post(
            "/login/",
            form,
            HTTP_HOST=HOST,
            HTTP_ORIGIN="https://evil.example",
            secure=True,
        ).status_code
        == 403
    )
    response = client.post(
        "/login/", form, HTTP_HOST=HOST, HTTP_ORIGIN="https://" + HOST, secure=True
    )
    assert response.status_code == 302


@pytest.mark.django_db(transaction=True)
def test_validation_and_organization_text_are_escaped() -> None:
    org = organization()
    org.name = '<script>alert("organization")</script>'
    org.save()
    response = Client().post(
        "/login/",
        {"identifier": '<script>alert("input")</script>'},
        HTTP_HOST=HOST,
        secure=True,
    )
    assert response.status_code == 400
    body = response.content.decode()
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert 'aria-invalid="true"' in body
    assert 'id="identifier-error"' in body


@pytest.mark.parametrize("method", ["put", "patch", "delete", "options"])
def test_other_methods_are_refused_without_database_access(method: str) -> None:
    response = getattr(Client(), method)("/login/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 405


@pytest.mark.django_db(transaction=True)
def test_head_and_https_and_existing_sandbox_gate() -> None:
    organization()
    client = Client()
    assert client.head("/login/", HTTP_HOST=HOST, secure=True).status_code == 200
    assert client.get("/login/", HTTP_HOST=HOST).status_code == 301
    with override_settings(ACCESS_GATE_PASSWORD="made-up-" * 4):
        response = client.get("/login/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.com",
        ".example.com",
        "*.example.com",
        "example.com:443",
        "example.com/path",
        "example.com.",
        "bad_label.com",
        "-bad.com",
        "a" * 64 + ".com",
    ],
)
def test_invalid_domain_configuration_fails_at_start(domain: str) -> None:
    with pytest.raises(ImproperlyConfigured):
        organization_domain({"RANDIRANDI_ORGANIZATION_DOMAIN": domain})


def test_the_domain_is_required_and_normalized() -> None:
    for environ in ({}, {"RANDIRANDI_ORGANIZATION_DOMAIN": "  "}):
        with pytest.raises(ImproperlyConfigured, match="is required"):
            organization_domain(environ)
    assert organization_domain({"RANDIRANDI_ORGANIZATION_DOMAIN": " LocalHost "}) == (
        "localhost"
    )


@pytest.mark.parametrize(
    ("value", "address"),
    [
        ("post@example.org", "post@example.org"),
        ("Harbourview <post@example.org>", "post@example.org"),
        (" post@example.org ", "post@example.org"),
    ],
)
def test_the_sender_is_the_header_and_its_address(value: str, address: str) -> None:
    found = sender({"RANDIRANDI_SENDER": value})
    assert found.header == value.strip()
    assert found.address == address


@pytest.mark.parametrize(
    "value",
    [
        "",
        "post",
        "post@",
        "<post@example.org>",
        "Harbourview post@example.org",
        "Harbourview <post@example.org> again",
        "a@example.org, b@example.org",
    ],
)
def test_the_sender_is_required_and_one_address(value: str) -> None:
    with pytest.raises(ImproperlyConfigured):
        sender({"RANDIRANDI_SENDER": value})


def test_login_static_assets_are_local() -> None:
    # The deployed build uses the generated file and collects it via Django.
    from django.contrib.staticfiles import finders

    path = finders.find("core/ui/main.css")
    assert path
    with mock.patch(
        "randirandi.core.login.organization_for_request",
        return_value=Organization(
            name="Fictional organization", contact_email="a@b.example"
        ),
    ):
        response = Client().get("/login/", HTTP_HOST=HOST, secure=True)
    body = response.content.decode()
    assert 'src="/static/core/ui/app.js"' in body
    assert_beta_bar(body, "a@b.example")
    assert 'href="/static/core/ui/main.css"' in body
    assert 'href="/static/core/login.css"' not in body

    class Links(HTMLParser):
        hrefs: list[str]

        def __init__(self) -> None:
            super().__init__()
            self.hrefs = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag == "a":
                self.hrefs.extend(
                    value for key, value in attrs if key == "href" and value
                )

    links = Links()
    links.feed(body)
    assert DOCUMENTATION_URL in links.hrefs
    assert "/static/core/third-party-notices.txt" not in links.hrefs
    assert not any("third-party-notices" in href for href in links.hrefs)


@pytest.mark.django_db(transaction=True)
def test_mui_config_escapes_content_and_uses_a_fresh_style_nonce() -> None:
    org = organization()
    org.name = '</script><script>alert("organization")</script>'
    org.save(update_fields=["name"])
    nonces = []
    for _ in range(2):
        response = Client().get("/login/", HTTP_HOST=HOST, secure=True)
        body = response.content.decode()
        match = re.search(
            r'<script id="rr-config" type="application/json">(.*?)</script>', body, re.S
        )
        assert match
        config = json.loads(match[1])
        assert config["organization"] == org.name
        assert org.name not in body
        nonce = config["nonce"]
        assert f'name="csp-nonce" content="{nonce}"' in body
        assert f"'nonce-{nonce}'" in response["Content-Security-Policy"]
        assert "unsafe-inline" not in response["Content-Security-Policy"]
        assert "unsafe-eval" not in response["Content-Security-Policy"]
        assert 'name="identifier"' in body  # Native form exists before JavaScript.
        nonces.append(nonce)
    assert nonces[0] != nonces[1]


@pytest.mark.django_db
@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_shared_error_pages_preserve_status_without_disclosing_details(
    status: int,
) -> None:
    client = Client(enforce_csrf_checks=True, raise_request_exception=False)
    if status == 400:
        response = client.get("/login/", HTTP_HOST="untrusted.example", secure=True)
    elif status == 403:
        response = client.post(
            "/login/", {"identifier": "hidden@example.com"}, HTTP_HOST=HOST, secure=True
        )
    elif status == 404:
        response = client.get("/missing/", HTTP_HOST=HOST, secure=True)
    else:
        with mock.patch(
            "randirandi.core.login.organization_for_request",
            side_effect=RuntimeError("private-debug-detail"),
        ):
            response = client.get("/login/", HTTP_HOST=HOST, secure=True)
    body = response.content.decode()
    assert response.status_code == status
    assert 'src="/static/core/ui/app.js"' in body
    assert 'name="identifier"' not in body
    assert "hidden@example.com" not in body
    assert "private-debug-detail" not in body
    assert "harbourview" not in body
    assert "no-store" in response["Cache-Control"]
    assert "'nonce-" in response["Content-Security-Policy"]
    assert "unsafe-inline" not in response["Content-Security-Policy"]


@pytest.mark.django_db(transaction=True)
def test_the_sign_in_and_code_pages_carry_the_about_and_the_bar_without_script() -> (
    None
):
    org = organization()
    about = [
        labels.word(org, key, organization=org.name)
        for key in (
            "shell.name",
            "shell.intro",
            "shell.free",
            "shell.book",
            "shell.pay",
        )
    ]
    assert org.name in about[1]
    login = Client().get("/login/", HTTP_HOST=HOST, secure=True)
    client = Client()
    session = client.session
    session["login_organization"] = org.pk
    session["login_browser"] = "b" * 40
    session["login_challenge"] = "a" * 32
    session.save()
    code = client.get("/login/code/", HTTP_HOST=HOST, secure=True)
    assert code.status_code == 200
    for response in (login, code):
        body = response.content.decode()
        # Randi's words come before the card in the page (the card is first
        # on a phone by CSS), in order, and the bar closes the page after it.
        found = [html.escape(line, quote=True) for line in about]
        positions = [body.index(line) for line in found]
        assert positions == sorted(positions)
        assert positions[-1] < body.index('<section class="fallback-card">')
        assert "shell.about" not in body
        bar = body[body.index("fallback-bar") :]
        assert body.index("fallback-bar") > body.index("</section>\n        </div>")
        assert "Third-party notices" not in body
        company = html.escape(labels.DEFAULTS["shell.company"], quote=True)
        link = (
            f'href="{DOCUMENTATION_URL}" target="_blank" rel="noopener noreferrer">'
            + labels.DEFAULTS["shell.documentation"]
        )
        # The owner (2026-10-26, ): Prices and Booking conditions beside
        # Documentation, in the footer of every page.
        assert bar.index(company) < bar.index('href="/prices/"')
        assert bar.index('href="/prices/"') < bar.index('href="/conditions/"')
        assert bar.index('href="/conditions/"') < bar.index(link)
        assert bar.count("<a ") == 3
        assert "GitHub</a>" not in bar
    assert (
        DOCUMENTATION_URL == "https://github.com/Randibooking/public/blob/main/FAQ.md"
    )
    assert "third-party-notices" not in login.content.decode()
