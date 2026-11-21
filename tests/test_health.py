# SPDX-License-Identifier: AGPL-3.0-only
"""The health page, and the headers every answer carries (controls C9, C10)."""

from unittest import mock

import pytest
from django.db import OperationalError
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_the_health_page_answers_when_the_database_does(client: Client) -> None:
    response = client.get("/health/", secure=True)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_health_page_says_so_when_the_database_is_gone(client: Client) -> None:
    with mock.patch(
        "randirandi.ops.views.connection.cursor", side_effect=OperationalError
    ):
        response = client.get("/health/", secure=True)

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.django_db
def test_the_health_page_answers_a_monitor_that_asks_for_headers_only(
    client: Client,
) -> None:
    response = client.head("/health/", secure=True)

    assert response.status_code == 200


def test_the_health_page_only_reads(client: Client) -> None:
    response = client.post("/health/", secure=True)

    assert response.status_code == 405


@pytest.mark.django_db
def test_the_platform_asking_the_container_itself_is_answered(client: Client) -> None:
    # Plain HTTP, under the container's own address: how Scaleway asks
    # whether the app is alive (tried on 2026-10-10).
    response = client.get("/health/", headers={"Host": "127.0.0.1:8000"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_platform_hears_it_when_the_database_is_gone(client: Client) -> None:
    with mock.patch(
        "randirandi.ops.views.connection.cursor", side_effect=OperationalError
    ):
        response = client.get("/health/", headers={"Host": "127.0.0.1:8000"})

    assert response.status_code == 503


def test_that_name_opens_nothing_but_the_health_page(client: Client) -> None:
    response = client.get("/nothing-here/", headers={"Host": "127.0.0.1:8000"})

    assert response.status_code == 400


def test_that_name_opens_nothing_through_the_front_door(client: Client) -> None:
    # The platform's front door sets this header on everything it lets in.
    response = client.get(
        "/health/",
        headers={"Host": "127.0.0.1:8000", "X-Envoy-External-Address": "203.0.113.7"},
    )

    assert response.status_code == 400


def test_the_container_itself_can_only_read_the_health_page(client: Client) -> None:
    response = client.post("/health/", headers={"Host": "127.0.0.1:8000"})

    assert response.status_code == 405


def test_plain_http_is_sent_to_https(client: Client) -> None:
    response = client.get("/health/")

    assert response.status_code == 301
    assert response["Location"] == "https://testserver/health/"


@pytest.mark.django_db
def test_every_answer_carries_the_security_headers(client: Client) -> None:
    response = client.get("/health/", secure=True)

    assert response["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains; preload"
    )
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "same-origin"
    assert response["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"


@pytest.mark.django_db
def test_the_content_security_policy_names_no_outside_source(client: Client) -> None:
    response = client.get("/health/", secure=True)

    policy = dict(
        directive.strip().split(" ", 1)
        for directive in response["Content-Security-Policy"].split(";")
    )
    assert policy["default-src"] == "'none'"
    assert policy["frame-ancestors"] == "'none'"
    assert policy["base-uri"] == "'none'"
    for directive in (
        "script-src",
        "style-src",
        "img-src",
        "font-src",
        "connect-src",
        "manifest-src",
    ):
        assert policy[directive] == "'self'", directive
    assert "unsafe" not in response["Content-Security-Policy"]
    assert "http" not in response["Content-Security-Policy"]
