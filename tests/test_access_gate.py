# SPDX-License-Identifier: AGPL-3.0-only
"""The access gate: a shared password before anything else (control C26)."""

import base64

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from pytest_django.fixtures import Settings

from randirandi.environment import load

PASSWORD = "made-up-and-long-enough-to-pass"  # noqa: S105
WITH_COLONS = "made-up:with:colons:and-long-enough"


def with_password(password: str, name: str = "anyone") -> dict[str, str]:
    given = base64.b64encode(f"{name}:{password}".encode()).decode()
    return {"Authorization": f"Basic {given}"}


@pytest.fixture
def gated(settings: Settings) -> Client:
    settings.ACCESS_GATE_PASSWORD = PASSWORD
    return Client()


def test_with_no_password_set_there_is_no_gate() -> None:
    response = Client().get("/nothing-here/", secure=True)

    assert response.status_code == 404


def test_the_gate_asks_for_the_password_before_anything_else(gated: Client) -> None:
    response = gated.get("/nothing-here/", secure=True)

    assert response.status_code == 401
    assert response["WWW-Authenticate"] == (
        'Basic realm="RandiRandi sandbox", charset="UTF-8"'
    )
    assert response["Cache-Control"] == "no-store"
    assert response.content == b"This is a closed test site.\n"


def test_with_the_password_the_request_goes_on(gated: Client) -> None:
    response = gated.get("/nothing-here/", secure=True, headers=with_password(PASSWORD))

    assert response.status_code == 404


def test_any_user_name_goes_with_the_password(gated: Client) -> None:
    response = gated.get(
        "/nothing-here/", secure=True, headers=with_password(PASSWORD, name="")
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        with_password("not-the-password"),
        with_password(PASSWORD[:-1]),
        with_password(""),
        {"Authorization": f"Bearer {PASSWORD}"},
        {"Authorization": "Basic not base64"},
        {"Authorization": "Basic"},
    ],
)
def test_anything_but_the_password_is_refused(
    gated: Client, headers: dict[str, str]
) -> None:
    response = gated.get("/nothing-here/", secure=True, headers=headers)

    assert response.status_code == 401


def test_a_password_with_a_colon_in_it_is_read_whole(
    settings: Settings,
) -> None:
    settings.ACCESS_GATE_PASSWORD = WITH_COLONS

    response = Client().get(
        "/nothing-here/", secure=True, headers=with_password(WITH_COLONS)
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_the_health_page_stays_open(gated: Client) -> None:
    response = gated.get("/health/", secure=True)

    assert response.status_code == 200


def test_the_files_of_the_pages_are_behind_the_gate_too(gated: Client) -> None:
    assert gated.get("/static/anything.css", secure=True).status_code == 401


def test_the_password_is_never_asked_for_over_plain_http(gated: Client) -> None:
    response = gated.get("/nothing-here/")

    assert response.status_code == 301
    assert response["Location"] == "https://testserver/nothing-here/"
    assert "WWW-Authenticate" not in response


def test_the_refusal_carries_the_security_headers(gated: Client) -> None:
    response = gated.get("/nothing-here/", secure=True)

    assert response["Strict-Transport-Security"].startswith("max-age=31536000")
    assert response["Content-Security-Policy"].startswith("default-src 'none'")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert response["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"


def test_production_can_start_with_the_gate() -> None:
    deployment = load(
        {
            "RANDIRANDI_ENVIRONMENT": "production",
            "RANDIRANDI_AUTHENTICATOR_KEY": "ab" * 32,
            "RANDIRANDI_ACCESS_GATE_STATE": "closed",
            "RANDIRANDI_ACCESS_GATE_PASSWORD": PASSWORD,
        }
    )
    assert deployment.switches.access_gate


def test_production_gate_uses_the_production_challenge(
    gated: Client, settings: Settings
) -> None:
    settings.ENVIRONMENT = "production"
    response = gated.get("/login/", secure=True)
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == 'Basic realm="RandiRandi", charset="UTF-8"'
    assert response.content == b"This site is not open yet.\n"


def test_a_sandbox_has_the_gate_when_it_has_a_password() -> None:
    without = load({"RANDIRANDI_ENVIRONMENT": "sandbox"})
    gated = load(
        {
            "RANDIRANDI_ENVIRONMENT": "sandbox",
            "RANDIRANDI_ACCESS_GATE_PASSWORD": PASSWORD,
        }
    )

    assert without.switches.access_gate is False
    assert gated.switches.access_gate is True


def test_a_short_password_is_refused_and_not_repeated() -> None:
    with pytest.raises(ImproperlyConfigured) as refused:
        load(
            {
                "RANDIRANDI_ENVIRONMENT": "sandbox",
                "RANDIRANDI_ACCESS_GATE_PASSWORD": "short-and-made-up",
            }
        )

    assert "at least 24 characters" in str(refused.value)
    assert "short-and-made-up" not in str(refused.value)


@pytest.mark.parametrize(
    "password,expected", [(None, 401), ("wrong", 401), (PASSWORD, 404)]
)
def test_production_gate_accepts_only_the_shared_password(
    settings: Settings, password: str | None, expected: int
) -> None:
    settings.ENVIRONMENT = "production"
    settings.ACCESS_GATE_PASSWORD = PASSWORD
    headers = {} if password is None else with_password(password)
    response = Client().get("/nothing-here/", secure=True, headers=headers)
    assert response.status_code == expected
