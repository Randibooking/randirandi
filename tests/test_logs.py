# SPDX-License-Identifier: AGPL-3.0-only
"""Logs carry ids only (control C18)."""

import io
import logging
import sys
from collections.abc import Iterator

import pytest
from django.test import Client, override_settings

from randirandi.logs import STRUCK, IdsOnlyFormatter, RequiredWriteHandler, strike
from tests.made_up import organization

EMAIL = "kari.nordmann@example.com"
PHONE = "+4791234567"


@pytest.mark.parametrize(
    "personal",
    [
        "kari.nordmann@example.com",
        "kari.nordmann%40example.com",
        "+4791234567",
        "004791234567",
        "%2B4791234567",
        "+47 912 34 567",
        "91234567",
        "912 34 567",
        "91 23 45 67",
        "+46701234567",
    ],
)
def test_an_email_address_or_a_phone_number_is_struck(personal: str) -> None:
    line = strike(f"login for {personal} failed")

    assert line == f"login for {STRUCK} failed"


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-09T12:34:56Z INFO randirandi.ops.jobs job done: erase in 1234 ms",
        "order 0199c8f2-7d4e-7a31-9c55-446655440000 confirmed",
        "booking 12345678-90ab-7def-8123-456789abcdef held until 12:40:00",
        "GET /health/ 200 3ms",
        "member 4711 logged in from organization 12",
        "payment of 200000 for order 42 captured",
    ],
)
def test_ids_times_and_amounts_are_left_alone(line: str) -> None:
    assert strike(line) == line


def test_the_formatter_strikes_the_message_and_the_traceback() -> None:
    try:
        raise ValueError(f"cannot send to {EMAIL}")
    except ValueError:
        record = logging.getLogger("test").makeRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "code sent to %s",
            (PHONE,),
            exc_info=sys.exc_info(),
        )

    line = IdsOnlyFormatter("%(asctime)sZ %(message)s").format(record)

    assert EMAIL not in line
    assert PHONE not in line
    assert line.count(STRUCK) >= 2
    assert "ValueError" in line


def _app_handlers() -> set[logging.StreamHandler[io.StringIO]]:
    """The handlers the app's log setup made, without the test runner's own."""
    return {
        handler
        for name in ("", "django", "django.server")
        for handler in logging.getLogger(name).handlers
        if isinstance(handler, logging.StreamHandler)
        and not type(handler).__module__.startswith("_pytest")
    }


def test_every_handler_writes_through_the_ids_only_formatter() -> None:
    handlers = _app_handlers()

    assert handlers
    for handler in handlers:
        assert isinstance(handler, RequiredWriteHandler), handler
        assert isinstance(handler.formatter, IdsOnlyFormatter), handler


@pytest.fixture
def written() -> Iterator[io.StringIO]:
    """What the configured log handlers write, instead of the terminal."""
    buffer = io.StringIO()
    handlers = _app_handlers()
    assert handlers, "the log setup has no handler to listen to"
    streams = {handler: handler.setStream(buffer) for handler in handlers}
    yield buffer
    for handler, stream in streams.items():
        if stream is not None:
            handler.setStream(stream)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("identifier", [EMAIL, PHONE])
def test_a_login_attempt_writes_no_email_or_phone_to_the_log(
    written: io.StringIO,
    identifier: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("randirandi.core.login.wake_delivery", lambda: None)
    # Exercise the real login page, including ignored query-string identities.
    organization()
    client = Client()
    with override_settings(
        ALLOWED_HOSTS=["harbourview.sandbox.example.org"],
        ORGANIZATION_DOMAIN="sandbox.example.org",
        STORAGES={
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            }
        },
    ):
        response = client.post(
            f"/login/?email={EMAIL}&phone={PHONE}",
            {"identifier": identifier},
            HTTP_HOST="harbourview.sandbox.example.org",
            secure=True,
        )

    assert response.status_code == 302
    log = written.getvalue()
    assert "login request: organization" in log, "the attempt was not logged at all"
    assert EMAIL not in log
    assert PHONE not in log
    assert "example.com" not in log
    assert "91234567" not in log


def test_an_unknown_host_is_rejected_without_a_traceback_or_host_value(
    written: io.StringIO,
) -> None:
    response = Client().get("/", HTTP_HOST="untrusted.example", secure=True)

    assert response.status_code == 400
    log = written.getvalue()
    assert "request rejected: unrecognized host" in log
    assert "untrusted.example" not in log
    assert "Traceback" not in log
    assert len(log.splitlines()) <= 2


@pytest.mark.urls("tests.urls_failing")
def test_a_login_that_crashes_writes_no_email_or_phone_to_the_log(
    written: io.StringIO,
) -> None:
    client = Client(raise_request_exception=False)

    response = client.post("/login/", {"email": EMAIL, "phone": PHONE}, secure=True)

    assert response.status_code == 500
    log = written.getvalue()
    assert "RuntimeError" in log, "the crash was not logged at all"
    assert STRUCK in log
    assert EMAIL not in log
    assert PHONE not in log
    assert "example.com" not in log
    assert "91234567" not in log
