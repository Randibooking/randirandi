# SPDX-License-Identifier: AGPL-3.0-only
"""Search engines are kept out of every response (control C26)."""

import base64
from pathlib import Path

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

pytestmark = pytest.mark.django_db

NOINDEX = "noindex, nofollow"
PASSWORD = "made-up-and-long-enough-to-pass"  # noqa: S105


def test_robots_txt_disallows_everything_without_a_session() -> None:
    response = Client().get("/robots.txt", secure=True)

    assert response.status_code == 200
    assert response.content == b"User-agent: *\nDisallow: /\n"
    assert response["Content-Type"].startswith("text/plain")
    assert response["X-Robots-Tag"] == NOINDEX


def test_robots_txt_is_open_through_the_gate(settings: Settings) -> None:
    settings.ACCESS_GATE_PASSWORD = PASSWORD
    response = Client().get("/robots.txt", secure=True)

    assert response.status_code == 200
    assert response.content == b"User-agent: *\nDisallow: /\n"


@pytest.mark.parametrize(
    "path",
    [
        "/health/",
        "/login/",
        "/nothing-here/",
        "/static/nothing-here.css",
        "/robots.txt",
    ],
)
@pytest.mark.parametrize("gated", [False, True])
def test_every_response_carries_noindex(
    settings: Settings, path: str, gated: bool
) -> None:
    settings.ACCESS_GATE_PASSWORD = PASSWORD if gated else ""
    response = Client().get(path, secure=True)

    assert response["X-Robots-Tag"] == NOINDEX


def test_the_gate_refusal_and_the_password_answer_carry_noindex(
    settings: Settings,
) -> None:
    settings.ACCESS_GATE_PASSWORD = PASSWORD
    refused = Client().get("/nothing-here/", secure=True)
    given = base64.b64encode(f"x:{PASSWORD}".encode()).decode()
    through = Client().get(
        "/nothing-here/", secure=True, headers={"Authorization": f"Basic {given}"}
    )

    assert refused.status_code == 401 and refused["X-Robots-Tag"] == NOINDEX
    assert through.status_code == 404 and through["X-Robots-Tag"] == NOINDEX


def test_the_container_probe_and_a_refused_host_carry_noindex() -> None:
    probe = Client().get("/health/", headers={"Host": "127.0.0.1:8000"})
    refused = Client().get("/login/", secure=True, headers={"Host": "evil.example"})

    assert probe.status_code == 200 and probe["X-Robots-Tag"] == NOINDEX
    assert refused.status_code == 400 and refused["X-Robots-Tag"] == NOINDEX


@pytest.mark.parametrize("gated", [False, True])
def test_a_real_static_file_carries_noindex(
    settings: Settings, tmp_path: Path, gated: bool
) -> None:
    (tmp_path / "real.css").write_text("body{}\n")
    settings.STATIC_ROOT = tmp_path
    settings.ACCESS_GATE_PASSWORD = PASSWORD if gated else ""
    headers = {}
    if gated:
        given = base64.b64encode(f"x:{PASSWORD}".encode()).decode()
        headers = {"Authorization": f"Basic {given}"}
    response = Client().get("/static/real.css", secure=True, headers=headers)
    response.close()  # the file WhiteNoise opened

    assert response.status_code == 200
    assert response["X-Robots-Tag"] == NOINDEX
