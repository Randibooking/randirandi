# SPDX-License-Identifier: AGPL-3.0-only
"""The second container: what a call from the platform sets going .

Every password here is made up, or made for the run and thrown away.
"""

import http.server
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, ClassVar

import psycopg
import pytest
from django.conf import settings
from django.test import override_settings
from psycopg import sql

from randirandi import infisical
from randirandi.environment import Environment
from randirandi.logs import STRUCK
from randirandi.mail_configuration import mailers
from randirandi.ops import calls
from randirandi.ops.jobs import Job, JobRunner

OWNER = "randirandi_owner"
ONE_RUN = "made-up-for-one-run"


def runner(ran: list[str] | None = None) -> JobRunner:
    """A job runner with one job that writes down that it ran, or with none."""
    if ran is None:
        return JobRunner([])
    return JobRunner([Job("erase", timedelta(minutes=5), lambda: ran.append("erase"))])


# --- The platform's schedule ---------------------------------------------------


@pytest.mark.django_db
def test_a_call_of_the_schedule_runs_what_is_due_and_asks_the_app() -> None:
    ran: list[str] = []
    asked: list[str] = []

    def ask(address: str) -> str:
        asked.append(address)
        return "ok"

    told = calls.Calls(
        runner(ran), "https://harbourview.sandbox.example/health/", ask=ask
    ).schedule()

    assert told == {"ok": True, "outside": "ok"}
    assert ran == ["erase"]
    assert asked == ["https://harbourview.sandbox.example/health/"]


@pytest.mark.django_db
def test_each_call_writes_one_line_for_the_alert_to_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    answers = iter(["ok", "failed: 503"])
    answering = calls.Calls(
        runner(),
        "https://harbourview.sandbox.example/health/",
        ask=lambda _: next(answers),
    )

    with caplog.at_level(logging.INFO, logger="randirandi.ops.calls"):
        answering.schedule()
        told = answering.schedule()

    lines = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert lines == [
        ("INFO", "outside check: ok"),
        ("ERROR", "outside check: failed: 503"),
    ]
    # The app failed, not this call.
    assert told == {"ok": True, "outside": "failed: 503"}


@pytest.mark.django_db
def test_without_an_address_the_app_is_not_asked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def ask(address: str) -> str:
        raise AssertionError(address)

    with caplog.at_level(logging.INFO, logger="randirandi.ops.calls"):
        told = calls.Calls(runner(), ask=ask).schedule()

    assert told == {"ok": True, "outside": None}
    assert caplog.records == []


# --- The app's health page, asked from outside ----------------------------------


class App(http.server.BaseHTTPRequestHandler):
    """Stands in for the app at its public address."""

    answers: ClassVar[dict[str, tuple[int, bytes, str]]] = {
        "/health/": (200, b'{"status": "ok"}', ""),
        "/down/": (503, b'{"status": "unavailable"}', ""),
        "/moved/": (301, b"", "/health/"),
        "/other/": (200, b"<html>a page of something else</html>", ""),
    }
    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        App.seen.append(self.path)
        status, body, location = self.answers[self.path]
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def serving(
    handler: type[http.server.BaseHTTPRequestHandler],
) -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def app() -> Iterator[str]:
    App.seen = []
    yield from serving(App)


@pytest.mark.parametrize(
    ("path", "answer"),
    [
        ("/health/", "ok"),
        ("/down/", "failed: 503"),
        ("/other/", "failed: JSONDecodeError"),
    ],
)
def test_the_app_is_asked_the_way_a_visitor_would(
    app: str, path: str, answer: str
) -> None:
    assert calls.ask_outside(app + path) == answer


def test_an_answer_that_sends_elsewhere_is_not_followed(app: str) -> None:
    assert calls.ask_outside(app + "/moved/") == "failed: 301"
    assert App.seen == ["/moved/"]


def test_an_app_that_is_not_there_has_failed() -> None:
    # Nothing listens on port 1.
    assert calls.ask_outside("http://127.0.0.1:1/health/") == "failed: URLError"


# --- A call, over HTTP -----------------------------------------------------------


class Kept:
    """Keeps what the calls set going."""

    def __init__(self) -> None:
        self.runs: list[tuple[object, object]] = []
        self.schedules = 0
        self.fail = False

    def migrate(self, user: object, password: object) -> calls.Told:
        self.runs.append((user, password))
        return {"ok": True, "steps": []}


@pytest.fixture
def second(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Kept]]:
    kept = Kept()
    answering = calls.Calls(runner(), migrate=kept.migrate)

    def schedule() -> calls.Told:
        if kept.fail:
            raise RuntimeError(f"it broke, and it knew {ONE_RUN}")
        kept.schedules += 1
        return {"ok": True, "outside": None}

    monkeypatch.setattr(answering, "schedule", schedule)
    for address in serving(calls.handler(answering)):
        yield address, kept


def call(
    address: str, body: bytes | None = b"{}", method: str = "POST"
) -> tuple[int, Any]:
    request = urllib.request.Request(  # noqa: S310
        address, data=body, method=method, headers={"X-Auth-Token": "made-up"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read()
            return error.code, json.loads(raw) if raw.startswith(b"{") else raw


def test_the_schedules_call_runs_the_schedule(second: tuple[str, Kept]) -> None:
    address, kept = second

    assert call(address + "/") == (200, {"ok": True, "outside": None})
    assert (kept.schedules, kept.runs) == (1, [])


@pytest.mark.parametrize(
    "body", [None, b"", b"not json", b"[1, 2]", b'{"run": "something else"}']
)
def test_whatever_else_is_sent_means_run_the_schedule(
    second: tuple[str, Kept], body: bytes | None
) -> None:
    address, kept = second

    assert call(address + "/", body)[0] == 200
    assert (kept.schedules, kept.runs) == (1, [])


def test_the_deploys_call_runs_the_deploys_run(second: tuple[str, Kept]) -> None:
    address, kept = second
    asked = {"run": "migrate", "user": OWNER, "password": ONE_RUN}

    assert call(address + "/", json.dumps(asked).encode()) == (
        200,
        {"ok": True, "steps": []},
    )
    assert (kept.schedules, kept.runs) == (0, [(OWNER, ONE_RUN)])


def test_a_call_that_is_too_long_is_not_read(second: tuple[str, Kept]) -> None:
    address, kept = second
    asked = {"run": "migrate", "user": OWNER, "password": "x" * calls.LARGEST_CALL}

    assert call(address + "/", json.dumps(asked).encode())[0] == 200
    assert (kept.schedules, kept.runs) == (1, [])


def test_a_call_that_breaks_says_so_and_no_more(
    second: tuple[str, Kept], caplog: pytest.LogCaptureFixture
) -> None:
    address, kept = second
    kept.fail = True

    with caplog.at_level(logging.INFO, logger="randirandi.ops.calls"):
        assert call(address + "/") == (500, {"ok": False})
        kept.fail = False
        # The next call is answered.
        assert call(address + "/")[0] == 200

    assert "a call failed" in caplog.text


def test_only_a_call_is_answered(second: tuple[str, Kept]) -> None:
    address, kept = second

    status, _ = call(address + "/health/", None, method="GET")

    assert status == 501
    assert (kept.schedules, kept.runs) == (0, [])


def test_the_line_for_a_call_has_no_query_and_no_network_address(
    second: tuple[str, Kept], caplog: pytest.LogCaptureFixture
) -> None:
    address, _ = second

    with caplog.at_level(logging.INFO, logger="randirandi.ops.calls"):
        call(address + "/?typed=something")

    assert [record.getMessage() for record in caplog.records] == ["call POST / 200"]


# --- The deploy's run -------------------------------------------------------------


class Processes:
    """Stands in for the processes the run starts, and keeps how each was
    started."""

    def __init__(self, *codes: int, prints: str = "") -> None:
        self.codes = list(codes)
        self.prints = prints
        self.started: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self, command: list[str], **how: Any
    ) -> subprocess.CompletedProcess[str]:
        self.started.append((command[1:], dict(how["env"])))
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(command, code, self.prints, "")


AT_THE_PLATFORM = {
    "PATH": "/app/.venv/bin:/usr/bin",
    "RANDIRANDI_ENVIRONMENT": "sandbox",
    "RANDIRANDI_ALLOWED_HOSTS": ".sandbox.example",
    "RANDIRANDI_ORGANIZATION_DOMAIN": "sandbox.example",
    "RANDIRANDI_SENDER": "Fictional <post@sandbox.example>",
    "RANDIRANDI_DB_HOST": "10.0.0.2",
    "RANDIRANDI_DB_NAME": "randirandi",
    "RANDIRANDI_DB_USER": "randirandi_app",
    infisical.CLIENT_ID: "made-up-client-id",
    infisical.PROJECT: "made-up-project",
    "RANDIRANDI_OUTSIDE_CHECK_ADDRESS": "https://harbourview.sandbox.example/health/",
}


def production_settings(
    closed: bool, security: str = "starttls", newline: str = "\n"
) -> override_settings:
    return override_settings(
        ENVIRONMENT=Environment.PRODUCTION,
        SWITCHES=replace(settings.SWITCHES, access_gate=closed),
        ACCESS_GATE_PASSWORD=f"fictional-gate-line-one{newline}fictional-gate-line-two",
        MAILERS=mailers(
            {
                "RANDIRANDI_SMTP_HOST": "127.0.0.1",
                "RANDIRANDI_SMTP_PORT": "2525",
                "RANDIRANDI_SMTP_SECURITY": security,
                "RANDIRANDI_SMTP_USER": "fictional-smtp-user",
                "RANDIRANDI_SMTP_PASSWORD": (
                    f"fictional-smtp-line-one{newline}fictional-smtp-line-two"
                ),
            },
            console=False,
        ),
    )


def test_the_run_is_the_check_the_grants_the_migrations_and_the_rights() -> None:
    processes = Processes()

    told = calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=processes)

    assert told["ok"] is True
    assert [step["step"] for step in told["steps"]] == [
        "the deploy check",
        "the grants",
        "the migrations",
        "the application's rights",
        "the retention cleanup",
    ]
    assert [command for command, _ in processes.started] == [
        ["manage.py", "check", "--deploy", "--fail-level", "WARNING"],
        ["manage.py", "grants"],
        ["manage.py", "migrate", "--no-input"],
        ["manage.py", "apply_privileges"],
        ["manage.py", "purge_expired"],
    ]


def test_the_run_is_the_owner_of_the_tables_and_gets_nothing_of_the_apps() -> None:
    processes = Processes()

    calls.migrate(
        OWNER,
        ONE_RUN,
        environ=AT_THE_PLATFORM,
        run=processes,
        authenticator_key="cd" * 32,
    )

    for _, environ in processes.started:
        assert environ["RANDIRANDI_AUTHENTICATOR_KEY"] == "cd" * 32
        assert environ["RANDIRANDI_DB_USER"] == OWNER
        assert environ["RANDIRANDI_DB_PASSWORD"] == ONE_RUN
        assert environ["RANDIRANDI_DB_HOST"] == "10.0.0.2"
        # Without a machine identity the run fetches nothing from Infisical.
        assert not set(infisical.IDENTITY) & set(environ)
        assert set(environ) == {
            "PATH",
            "RANDIRANDI_ENVIRONMENT",
            "RANDIRANDI_ALLOWED_HOSTS",
            "RANDIRANDI_ORGANIZATION_DOMAIN",
            "RANDIRANDI_SENDER",
            "RANDIRANDI_DB_HOST",
            "RANDIRANDI_DB_NAME",
            "RANDIRANDI_DB_USER",
            "RANDIRANDI_DB_PASSWORD",
            "RANDIRANDI_SECRET_KEY",
            "RANDIRANDI_AUTHENTICATOR_KEY",
        }
    keys = {environ["RANDIRANDI_SECRET_KEY"] for _, environ in processes.started}
    assert len(keys) == 1
    assert len(keys.pop()) >= 50


def test_the_run_gets_the_processs_own_authenticator_key_unless_it_has_none(
    settings: Any,
) -> None:
    processes = Processes()
    settings.AUTHENTICATOR_KEY = "ef" * 32
    calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=processes)
    assert {e["RANDIRANDI_AUTHENTICATOR_KEY"] for _, e in processes.started} == {
        "ef" * 32
    }

    other = Processes()
    settings.AUTHENTICATOR_KEY = ""
    calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=other)
    assert all("RANDIRANDI_AUTHENTICATOR_KEY" not in e for _, e in other.started)


@pytest.mark.parametrize("closed", [True, False])
@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_production_startup_uses_only_required_effective_settings(
    closed: bool, security: str
) -> None:
    processes = Processes()
    source = AT_THE_PLATFORM | {
        "RANDIRANDI_ENVIRONMENT": "production",
        "RANDIRANDI_DB_APP_PASSWORD": "persistent-app-db-secret",
        "RANDIRANDI_SECRET_KEY": "persistent-signing-secret",
        "VIPPS_CLIENT_SECRET": "payment-secret",
        "RANDIRANDI_SMTP_PASSWORD": "stale-process-value",
        "RANDIRANDI_ACCESS_GATE_PASSWORD": "stale-gate-value",
    }
    with production_settings(closed, security):
        calls.migrate(
            OWNER, ONE_RUN, environ=source, run=processes, authenticator_key="ab" * 32
        )
        for _, child in processes.started:
            assert child["RANDIRANDI_ACCESS_GATE_STATE"] == (
                "closed" if closed else "open"
            )
            if closed:
                assert (
                    child["RANDIRANDI_ACCESS_GATE_PASSWORD"]
                    == settings.ACCESS_GATE_PASSWORD
                )
            else:
                assert "RANDIRANDI_ACCESS_GATE_PASSWORD" not in child
            assert child["RANDIRANDI_SMTP_HOST"] == "127.0.0.1"
            assert child["RANDIRANDI_SMTP_PORT"] == "2525"
            assert child["RANDIRANDI_SMTP_SECURITY"] == security
            assert child["RANDIRANDI_SMTP_USER"] == "fictional-smtp-user"
            assert (
                child["RANDIRANDI_SMTP_PASSWORD"]
                == settings.MAILERS["default"]["OPTIONS"]["password"]
            )
            assert child["RANDIRANDI_DB_PASSWORD"] == ONE_RUN
            assert child["RANDIRANDI_AUTHENTICATOR_KEY"] == "ab" * 32
            assert set(child) == set(calls.HANDED_ON) - {"RANDIRANDI_DB_PORT"} | {
                "RANDIRANDI_DB_USER",
                "RANDIRANDI_DB_PASSWORD",
                "RANDIRANDI_SECRET_KEY",
                "RANDIRANDI_AUTHENTICATOR_KEY",
                "RANDIRANDI_ACCESS_GATE_STATE",
                "RANDIRANDI_SMTP_HOST",
                "RANDIRANDI_SMTP_PORT",
                "RANDIRANDI_SMTP_SECURITY",
                "RANDIRANDI_SMTP_USER",
                "RANDIRANDI_SMTP_PASSWORD",
            } | ({"RANDIRANDI_ACCESS_GATE_PASSWORD"} if closed else set())
            assert not set(infisical.IDENTITY) & child.keys()
            assert not {
                "persistent-app-db-secret",
                "persistent-signing-secret",
                "payment-secret",
            } & set(child.values())


def test_subprocess_diagnostics_mask_whole_and_overlapping_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handed: dict[str, str] = {}

    def fails(command: list[str], **how: Any) -> subprocess.CompletedProcess[str]:
        handed.update(how["env"])
        secrets = [
            value
            for name, value in handed.items()
            if name.endswith(("PASSWORD", "KEY")) or name == "RANDIRANDI_SMTP_USER"
        ]
        return subprocess.CompletedProcess(
            command, 1, "diagnostic\n" + "\n".join(secrets), "\n" + "\n".join(secrets)
        )

    with (
        production_settings(True),
        caplog.at_level(logging.ERROR, logger="randirandi.ops.calls"),
    ):
        # Owner secret is contained in another value; longest-first masking
        # must remove the containing SMTP value before its substring.
        told = calls.migrate(
            OWNER,
            "fictional-smtp-line-one",
            environ=AT_THE_PLATFORM,
            run=fails,
            authenticator_key="cd" * 32,
        )
    assert told["ok"] is False
    combined = json.dumps(told) + caplog.text
    for name, value in handed.items():
        if name.endswith(("PASSWORD", "KEY")) or name == "RANDIRANDI_SMTP_USER":
            for line in value.splitlines():
                assert line not in combined
    assert "diagnostic" in combined
    assert STRUCK in combined


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_actual_subprocess_newline_conversion_does_not_expose_secrets(
    newline: str, caplog: pytest.LogCaptureFixture
) -> None:
    def child(command: list[str], **how: Any) -> subprocess.CompletedProcess[str]:
        # Run an actual child with the exact handed environment and text mode.
        # It emits dummy settings instead of running a management command.
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, sys; "
                "sys.stdout.write(os.environ['RANDIRANDI_ACCESS_GATE_PASSWORD']); "
                "sys.stderr.write(os.environ['RANDIRANDI_SMTP_PASSWORD']); sys.exit(1)",
            ],
            **how,
        )

    with (
        production_settings(True, newline=newline),
        caplog.at_level(logging.ERROR, logger="randirandi.ops.calls"),
    ):
        told = calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=child)
    assert told["ok"] is False
    assert STRUCK in json.dumps(told)
    combined = json.dumps(told) + caplog.text
    assert "fictional-gate-line" not in combined
    assert "fictional-smtp-line" not in combined


def test_the_first_step_that_fails_ends_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processes = Processes(0, 1, prints="permission denied for schema public\n")

    with caplog.at_level(logging.INFO, logger="randirandi.ops.calls"):
        told = calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=processes)

    assert told["ok"] is False
    assert [(step["step"], step["ok"]) for step in told["steps"]] == [
        ("the deploy check", True),
        ("the grants", False),
    ]
    assert told["steps"][1]["said"] == ["permission denied for schema public"]
    assert len(processes.started) == 2
    assert "deploy run: the grants failed" in caplog.text


def test_the_password_is_struck_from_what_a_step_printed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processes = Processes(1, prints=f"could not log in with {ONE_RUN}\n")

    with caplog.at_level(logging.INFO, logger="randirandi.ops.calls"):
        told = calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=processes)

    assert told["steps"][0]["said"] == ["could not log in with [struck]"]
    assert ONE_RUN not in json.dumps(told)
    assert ONE_RUN not in caplog.text


def test_a_step_that_does_not_end_is_stopped() -> None:
    def never(command: list[str], **how: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, how["timeout"])

    told = calls.migrate(OWNER, ONE_RUN, environ=AT_THE_PLATFORM, run=never)

    assert told["ok"] is False
    assert told["steps"][0]["said"] == ["not done after 600 seconds, and stopped"]


@pytest.mark.parametrize(
    ("user", "password"),
    [
        (None, ONE_RUN),
        (OWNER, None),
        (OWNER, ""),
        ("randirandi_owner; drop table core_member", ONE_RUN),
        ("Randirandi_Owner", ONE_RUN),
        (7, ONE_RUN),
    ],
)
def test_a_call_without_a_user_and_a_password_starts_nothing(
    user: object, password: object
) -> None:
    processes = Processes()

    told = calls.migrate(user, password, environ=AT_THE_PLATFORM, run=processes)

    assert told == {"ok": False, "steps": [], "why": "no database user and password"}
    assert processes.started == []


# The run for real: the three steps as processes of their own, against a
# database made for this test with the two users, and removed after it.
DATABASE = "randirandi_the_deploys_run"


def the_test_database() -> dict[str, str]:
    """Where the tests' own Postgres is, and its superuser."""
    there = settings.DATABASES["default"]
    return {name: str(there[name]) for name in ("HOST", "PORT", "USER", "PASSWORD")}


def at_the_test_database() -> dict[str, str]:
    """What the second container's environment says, pointed at this test."""
    there = the_test_database()
    return {
        "PATH": os.environ["PATH"],
        "RANDIRANDI_ENVIRONMENT": "sandbox",
        "RANDIRANDI_ALLOWED_HOSTS": "localhost",
        "RANDIRANDI_ORGANIZATION_DOMAIN": "localhost",
        "RANDIRANDI_SENDER": "none@localhost",
        "RANDIRANDI_DB_HOST": there["HOST"],
        "RANDIRANDI_DB_PORT": there["PORT"],
        "RANDIRANDI_DB_NAME": DATABASE,
    }


def as_user(
    user: str, password: str, database: str = DATABASE
) -> psycopg.Connection[tuple[Any, ...]]:
    there = the_test_database()
    return psycopg.connect(
        host=there["HOST"],
        port=there["PORT"],
        user=user,
        password=password,
        dbname=database,
        autocommit=True,
        connect_timeout=5,
    )


@pytest.fixture
def two_users() -> Iterator[dict[str, str]]:
    there = the_test_database()
    passwords = {
        OWNER: secrets.token_urlsafe(32),
        "randirandi_app": secrets.token_urlsafe(32),
    }

    def remove(superuser: psycopg.Connection[tuple[Any, ...]]) -> None:
        superuser.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DATABASE)
            )
        )
        for user in passwords:
            superuser.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(user))
            )

    with as_user(there["USER"], there["PASSWORD"], "postgres") as superuser:
        remove(superuser)
        for user, password in passwords.items():
            superuser.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(user), sql.Literal(password)
                )
            )
        superuser.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(DATABASE), sql.Identifier(OWNER)
            )
        )
        # What docker/db/roles.sql does on the laptop, as the superuser.
        with as_user(there["USER"], there["PASSWORD"]) as inside:
            inside.execute("ALTER SCHEMA public OWNER TO randirandi_owner")
            inside.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        try:
            yield passwords
        finally:
            remove(superuser)


@pytest.mark.parametrize("mode", ["sandbox", "production-closed", "production-open"])
def test_the_run_for_real_leaves_the_app_able_to_use_rows_and_nothing_more(
    two_users: dict[str, str],
    mode: str,
) -> None:
    environ = at_the_test_database()
    production = mode.startswith("production")
    if production:
        environ["RANDIRANDI_ENVIRONMENT"] = "production"
    # Runtime-only secrets are in Django settings; the inherited environment
    # deliberately has neither SMTP credentials nor a gate password.
    with (
        production_settings(mode == "production-closed")
        if production
        else override_settings()
    ):
        told = calls.migrate(OWNER, two_users[OWNER], environ=environ)
        again = calls.migrate(OWNER, two_users[OWNER], environ=environ)

    assert told["ok"] is True, told["steps"][-1]
    assert again["ok"] is True, again["steps"][-1]
    assert two_users[OWNER] not in json.dumps([told, again])
    with as_user("randirandi_app", two_users["randirandi_app"]) as app:
        made = app.execute("SELECT count(*) FROM django_migrations").fetchone()
        assert made is not None
        assert made[0] > 0
        # A row is written and taken away again.
        app.execute(
            "INSERT INTO django_migrations (app, name, applied) "
            "VALUES ('made-up', 'made-up', now())"
        )
        app.execute("DELETE FROM django_migrations WHERE app = 'made-up'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE TABLE made_up (id integer)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(
                "ALTER TABLE core_capacityallocation "
                "DROP CONSTRAINT capacity_no_double_booking"
            )


def test_the_run_for_real_fails_with_a_wrong_password_and_says_which_step(
    two_users: dict[str, str],
) -> None:
    environ = at_the_test_database()

    told = calls.migrate(OWNER, "made-up-and-wrong", environ=environ)

    assert told["ok"] is False
    # Generated-column checks query PostgreSQL's capabilities, so the deploy
    # check now authenticates before the grants or migrations can run.
    assert [(step["step"], step["ok"]) for step in told["steps"]] == [
        ("the deploy check", False),
    ]
    assert "password authentication failed" in "\n".join(told["steps"][0]["said"])
