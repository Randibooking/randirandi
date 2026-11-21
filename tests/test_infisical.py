# SPDX-License-Identifier: AGPL-3.0-only
"""The secrets, fetched from Infisical when the process starts (control C31).

Infisical is played by a fake that keeps what it was asked. Every value here
is made up and opens nothing.
"""

import http.server
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from django.core.exceptions import ImproperlyConfigured

from randirandi import infisical

ROOT = Path(__file__).resolve().parent.parent

CLIENT_ID = "made-up-client-id"
KEY = "made-up-key-of-the-machine-identity"
PROJECT = "made-up-project"
LOGIN = "made-up-login"
APP_PASSWORD = "made-up-password-of-the-apps-database-user"  # noqa: S105
DJANGO_KEY = "made-up-key-of-django"
LEFT_BEHIND = "a-copy-left-at-the-platform"
SECRETS = {
    "RANDIRANDI_SECRET_KEY": DJANGO_KEY,
    "RANDIRANDI_DB_APP_PASSWORD": APP_PASSWORD,
    "RANDIRANDI_AUTHENTICATOR_KEY": "cd" * 32,
}

LOGIN_PATH = "/api/v1/auth/universal-auth/login"
READ_PATH = "/api/v3/secrets/raw"
END_PATH = "/api/v1/auth/token/revoke"


class FakeInfisical:
    """Answers the three questions the app asks, and keeps each one."""

    def __init__(self, secrets: Mapping[str, str] = SECRETS) -> None:
        self.secrets: list[dict[str, Any]] = [
            {"secretKey": name, "secretValue": value} for name, value in secrets.items()
        ]
        self.asked: list[tuple[str, str, Mapping[str, str] | None, str | None]] = []
        # What to answer instead, by path: a status with a body, or an error
        # to raise. Each is used once, in order.
        self.instead: dict[str, list[tuple[int, Any] | Exception]] = {}
        self.ended: list[str] = []

    def __call__(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, str] | None = None,
        login: str | None = None,
    ) -> tuple[int, Any]:
        self.asked.append((method, path, body, login))
        where = path.split("?")[0]
        if self.instead.get(where):
            answer = self.instead[where].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if (method, where) == ("POST", LOGIN_PATH):
            if body != {"clientId": CLIENT_ID, "clientSecret": KEY}:
                return 401, {"error": "UnauthorizedError", "message": "Invalid"}
            return 200, {"accessToken": LOGIN, "expiresIn": 2_592_000}
        if (method, where) == ("GET", READ_PATH):
            if login != LOGIN or LOGIN in self.ended:
                return 403, {"error": "TokenError", "message": "invalid token"}
            return 200, {"secrets": self.secrets, "imports": []}
        if (method, where) == ("POST", END_PATH):
            self.ended.append((body or {}).get("accessToken", ""))
            return 200, {"message": "Successfully revoked access token"}
        return 404, {"error": "Not Found"}

    def paths(self) -> list[str]:
        return [path.split("?")[0] for _, path, _, _ in self.asked]


def identity(**more: str) -> dict[str, str]:
    return {
        "RANDIRANDI_ENVIRONMENT": "sandbox",
        infisical.CLIENT_ID: CLIENT_ID,
        infisical.KEY: KEY,
        infisical.PROJECT: PROJECT,
        **more,
    }


def no_sleep(seconds: float) -> None:
    pass


def fetched(environ: dict[str, str], fake: FakeInfisical) -> Mapping[str, str]:
    return infisical.load(environ, "sandbox", ask=fake, sleep=no_sleep)


def test_without_a_machine_identity_nothing_is_asked() -> None:
    fake = FakeInfisical()
    environ = {"RANDIRANDI_ENVIRONMENT": "sandbox", "RANDIRANDI_SECRET_KEY": "x"}

    read = fetched(environ, fake)

    assert read is environ
    assert fake.asked == []


@pytest.mark.parametrize("missing", infisical.IDENTITY)
def test_half_a_machine_identity_does_not_start(missing: str) -> None:
    fake = FakeInfisical()
    environ = identity()
    del environ[missing]

    with pytest.raises(ImproperlyConfigured, match=missing) as refusal:
        fetched(environ, fake)

    assert fake.asked == []
    assert KEY not in str(refusal.value)


def test_the_secrets_are_read_for_this_environment_and_no_other() -> None:
    fake = FakeInfisical()

    read = fetched(identity(), fake)

    assert read["RANDIRANDI_SECRET_KEY"] == DJANGO_KEY
    assert read["RANDIRANDI_DB_APP_PASSWORD"] == APP_PASSWORD
    # What the deployment handed over is still there.
    assert read["RANDIRANDI_ENVIRONMENT"] == "sandbox"
    method, path, body, login = fake.asked[1]
    assert (method, body, login) == ("GET", None, LOGIN)
    assert path == (
        f"{READ_PATH}?workspaceId={PROJECT}&environment=sandbox&secretPath=%2F"
    )


def test_the_key_is_taken_out_of_the_environment() -> None:
    environ = identity()

    fetched(environ, FakeInfisical())

    assert infisical.KEY not in environ


def test_no_secret_is_put_into_the_environment() -> None:
    environ = identity()

    fetched(environ, FakeInfisical())

    assert not set(SECRETS) & set(environ)
    assert not set(SECRETS.values()) & set(environ.values())


def test_the_login_is_ended_when_the_secrets_are_read() -> None:
    fake = FakeInfisical()

    fetched(identity(), fake)

    assert fake.paths() == [LOGIN_PATH, READ_PATH, END_PATH]
    assert fake.ended == [LOGIN]


def test_the_login_is_ended_when_the_read_is_refused() -> None:
    fake = FakeInfisical()
    fake.instead[READ_PATH] = [(403, {"error": "PermissionDenied"})]

    with pytest.raises(ImproperlyConfigured, match="403"):
        fetched(identity(), fake)

    assert fake.ended == [LOGIN]


def test_the_app_starts_when_the_login_could_not_be_ended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeInfisical()
    fake.instead[END_PATH] = [OSError("the connection was cut")]

    with caplog.at_level(logging.WARNING, logger="randirandi.infisical"):
        read = fetched(identity(), fake)

    assert read["RANDIRANDI_SECRET_KEY"] == DJANGO_KEY
    assert "was not ended" in caplog.text
    assert LOGIN not in caplog.text


def test_what_is_shown_of_the_secrets_is_how_many() -> None:
    read = fetched(identity(), FakeInfisical())

    for shown in (repr(read), str(read), f"{read}"):
        assert shown == "<the environment, with 3 secrets from Infisical>"


def test_a_secret_that_is_also_in_the_environment_does_not_start() -> None:
    environ = identity(RANDIRANDI_SECRET_KEY=LEFT_BEHIND)

    with pytest.raises(ImproperlyConfigured, match="RANDIRANDI_SECRET_KEY") as refusal:
        fetched(environ, FakeInfisical())

    assert "nowhere else" in str(refusal.value)
    assert LEFT_BEHIND not in str(refusal.value)
    assert DJANGO_KEY not in str(refusal.value)


def test_a_wrong_key_is_refused_once_and_not_repeated() -> None:
    fake = FakeInfisical()
    environ = identity()
    environ[infisical.KEY] = "made-up-wrong-key"

    with pytest.raises(ImproperlyConfigured) as refusal:
        fetched(environ, fake)

    said = str(refusal.value)
    assert said == (
        "The secrets were not fetched: Infisical answered 401 (UnauthorizedError) "
        "when the machine identity logged in."
    )
    # A refusal is not asked again, and nothing was there to end.
    assert fake.paths() == [LOGIN_PATH]
    assert infisical.KEY not in environ


def test_a_refused_request_names_what_it_lacked_and_no_value() -> None:
    fake = FakeInfisical()
    fake.instead[READ_PATH] = [
        (
            422,
            {
                "error": "ValidationFailure",
                "message": [
                    {"code": "invalid_type", "path": ["workspaceId"], "received": KEY},
                    {"code": "custom", "path": ["has a space"], "message": LOGIN},
                ],
            },
        )
    ]

    with pytest.raises(ImproperlyConfigured) as refusal:
        fetched(identity(), fake)

    said = str(refusal.value)
    assert "answered 422 (ValidationFailure, workspaceId)" in said
    assert KEY not in said
    assert LOGIN not in said


def test_infisical_away_is_asked_three_times_and_then_the_app_does_not_start() -> None:
    fake = FakeInfisical()
    fake.instead[LOGIN_PATH] = [
        OSError("no address"),
        TimeoutError("no answer in time"),
        (503, None),
    ]
    waits: list[float] = []

    with pytest.raises(ImproperlyConfigured, match="answered 503") as refusal:
        infisical.load(identity(), "sandbox", ask=fake, sleep=waits.append)

    assert fake.paths() == [LOGIN_PATH] * 3
    assert waits == [2.0, 4.0]
    assert KEY not in str(refusal.value)


def test_infisical_back_at_the_second_try_is_enough() -> None:
    fake = FakeInfisical()
    fake.instead[LOGIN_PATH] = [OSError("the connection was cut")]
    fake.instead[READ_PATH] = [(429, {"error": "RateLimit"})]

    read = fetched(identity(), fake)

    assert read["RANDIRANDI_DB_APP_PASSWORD"] == APP_PASSWORD
    assert fake.paths() == [LOGIN_PATH, LOGIN_PATH, READ_PATH, READ_PATH, END_PATH]


def test_a_placeholder_for_a_secret_the_identity_may_not_read_does_not_start() -> None:
    fake = FakeInfisical()
    fake.secrets[1] = {
        "secretKey": "RANDIRANDI_DB_APP_PASSWORD",
        "secretValue": "<hidden-by-infisical>",
        "secretValueHidden": True,
    }

    with pytest.raises(ImproperlyConfigured, match="may not read") as refusal:
        fetched(identity(), fake)

    assert "RANDIRANDI_DB_APP_PASSWORD" in str(refusal.value)
    assert fake.ended == [LOGIN]


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {"secrets": "none"},
        {"secrets": ["RANDIRANDI_SECRET_KEY"]},
        {"secrets": [{"secretKey": "RANDIRANDI_SECRET_KEY"}]},
        {"secrets": [{"secretKey": 1, "secretValue": "x"}]},
    ],
)
def test_an_answer_in_an_unknown_form_does_not_start(answer: Any) -> None:
    fake = FakeInfisical()
    fake.instead[READ_PATH] = [(200, answer)]

    with pytest.raises(ImproperlyConfigured, match="a form this app does not know"):
        fetched(identity(), fake)

    assert fake.ended == [LOGIN]


def test_a_login_without_a_token_does_not_start() -> None:
    fake = FakeInfisical()
    fake.instead[LOGIN_PATH] = [(200, {"tokenType": "Bearer"})]

    with pytest.raises(ImproperlyConfigured, match="a form this app does not know"):
        fetched(identity(), fake)

    assert fake.paths() == [LOGIN_PATH]


def test_an_environment_without_secrets_does_not_start() -> None:
    with pytest.raises(ImproperlyConfigured, match="holds none"):
        fetched(identity(), FakeInfisical(secrets={}))


def test_the_key_goes_to_infisical_in_the_eu() -> None:
    assert infisical.HOST == "https://eu.infisical.com"


class Stand(http.server.BaseHTTPRequestHandler):
    """A server on this machine that stands in for the address, to see what
    a question really carries. It sends every /first on to /elsewhere."""

    seen: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        Stand.seen.append(
            (self.command, self.path, dict(self.headers), self.rfile.read(length))
        )
        if self.path == "/first":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        told = b'{"heard": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(told)))
        self.end_headers()
        self.wfile.write(told)

    do_GET = do_POST = answer  # noqa: N815

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def stand(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[Stand]]:
    Stand.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Stand)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(infisical, "HOST", f"http://127.0.0.1:{server.server_port}")
    try:
        yield Stand
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_question_carries_the_login_and_says_who_asks(stand: type[Stand]) -> None:
    status, told = infisical._ask("POST", "/ask", body={"a": "b"}, login=LOGIN)

    assert (status, told) == (200, {"heard": True})
    [(method, path, headers, sent)] = stand.seen
    assert (method, path, sent) == ("POST", "/ask", b'{"a": "b"}')
    assert headers["Authorization"] == f"Bearer {LOGIN}"
    # Infisical's front door refuses the name Python gives itself.
    assert headers["User-Agent"] == "randirandi"


def test_a_question_is_not_sent_on_to_another_address(stand: type[Stand]) -> None:
    status, told = infisical._ask("GET", "/first", login=LOGIN)

    assert (status, told) == (302, None)
    assert [path for _, path, _, _ in stand.seen] == ["/first"]


def test_a_proxy_named_in_the_environment_is_not_used(
    stand: type[Stand], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        # Nothing listens there: a question that went that way would fail.
        monkeypatch.setenv(name, "http://127.0.0.1:1")

    status, _ = infisical._ask("GET", "/ask")

    assert status == 200
    assert len(stand.seen) == 1


# The settings, loaded in a process of their own with a made-up machine
# identity and Infisical played by the fake above. It prints what holds, as
# yes and no, and never a value.
SETTINGS_PROBE = """
import os, sys
sys.path.insert(0, os.environ["PROBE_ROOT"])
from tests.test_infisical import APP_PASSWORD, KEY, SECRETS, FakeInfisical
from randirandi import infisical
fake = FakeInfisical({**SECRETS, "RANDIRANDI_ACCESS_GATE_PASSWORD": "made-up-" * 4})
infisical._ask = fake
import django
django.setup()
from django.conf import settings
held = set(SECRETS.values()) | {KEY}
print("asked", ",".join(fake.paths()))
database = settings.DATABASES["default"]
print("database password is the app's", database["PASSWORD"] == APP_PASSWORD)
print("secret key fetched", settings.SECRET_KEY == SECRETS["RANDIRANDI_SECRET_KEY"])
gate = settings.ACCESS_GATE_PASSWORD
print("gate on", bool(settings.SWITCHES.access_gate and len(gate) == 32))
print("environment clean", not held & set(os.environ.values()))
print("key gone", infisical.KEY not in os.environ)
print("no setting holds them all", not hasattr(settings, "_environ"))
"""


def probe_settings(
    probe: str = SETTINGS_PROBE, **more: str
) -> subprocess.CompletedProcess[str]:
    environ = {
        "PATH": os.environ["PATH"],
        "PROBE_ROOT": str(ROOT),
        "DJANGO_SETTINGS_MODULE": "randirandi.settings",
        "RANDIRANDI_ALLOWED_HOSTS": "harbourview.sandbox.example",
        "RANDIRANDI_ORGANIZATION_DOMAIN": "sandbox.example",
        "RANDIRANDI_SENDER": "Fictional <post@sandbox.example>",
        "RANDIRANDI_DB_HOST": "none",
        "RANDIRANDI_DB_NAME": "randirandi",
        "RANDIRANDI_DB_USER": "randirandi_app",
        **identity(),
        **more,
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_settings_read_their_secrets_from_what_was_fetched() -> None:
    done = probe_settings()

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.splitlines() == [
        f"asked {LOGIN_PATH},{READ_PATH},{END_PATH}",
        "database password is the app's True",
        "secret key fetched True",
        "gate on True",
        "environment clean True",
        "key gone True",
        "no setting holds them all True",
    ]


def test_a_password_handed_over_with_the_user_is_the_one_used() -> None:
    # The one run of the migrations: the owner of the tables, with a password
    # made for that run, and no machine identity.
    environ = {
        "PATH": os.environ["PATH"],
        "DJANGO_SETTINGS_MODULE": "randirandi.settings",
        "RANDIRANDI_ENVIRONMENT": "sandbox",
        "RANDIRANDI_ALLOWED_HOSTS": "harbourview.sandbox.example",
        "RANDIRANDI_ORGANIZATION_DOMAIN": "sandbox.example",
        "RANDIRANDI_SENDER": "Fictional <post@sandbox.example>",
        "RANDIRANDI_SECRET_KEY": "made-up-for-one-run",
        "RANDIRANDI_DB_HOST": "none",
        "RANDIRANDI_DB_NAME": "randirandi",
        "RANDIRANDI_DB_USER": "randirandi_owner",
        "RANDIRANDI_DB_PASSWORD": "made-up-for-one-run",
    }
    code = (
        "import django; django.setup(); from django.conf import settings; "
        "d = settings.DATABASES['default']; "
        "print(d['USER'], d['PASSWORD'] == 'made-up-for-one-run')"
    )

    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.split() == ["randirandi_owner", "True"]


GATE_ITEM = '{**SECRETS, "RANDIRANDI_ACCESS_GATE_PASSWORD": "made-up-" * 4}'


def test_a_deployed_sandbox_starts_opened_on_purpose() -> None:
    probe = SETTINGS_PROBE.replace(
        GATE_ITEM, '{**SECRETS, "RANDIRANDI_SANDBOX_OPEN": "on"}'
    )
    assert probe != SETTINGS_PROBE

    done = probe_settings(probe)

    assert done.returncode == 0, done.stderr[-2000:]
    assert "gate on False" in done.stdout.splitlines()
    for value in (*SECRETS.values(), KEY):
        assert value not in done.stderr


def test_a_deployed_sandbox_without_gate_or_opening_does_not_start() -> None:
    probe = SETTINGS_PROBE.replace(GATE_ITEM, "SECRETS")
    assert probe != SETTINGS_PROBE

    done = probe_settings(probe)

    assert done.returncode != 0
    assert "A deployed sandbox starts with its access gate or opened" in done.stderr
    for value in (*SECRETS.values(), KEY):
        assert value not in done.stderr


# Django reads the settings a second time when the first reading failed. It
# gets the same answer, and Infisical is not asked again.
TWICE_PROBE = """
import os, sys
sys.path.insert(0, os.environ["PROBE_ROOT"])
from tests.test_infisical import LOGIN_PATH, SECRETS, FakeInfisical
from randirandi import infisical
fake = FakeInfisical({**SECRETS, "RANDIRANDI_ACCESS_GATE_PASSWORD": "made-up-" * 4})
if os.environ.get("PROBE_REFUSES"):
    fake.instead[LOGIN_PATH] = [(401, {"error": "UnauthorizedError"})]
infisical._ask = fake
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
for _ in range(2):
    try:
        settings.SECRET_KEY
    except ImproperlyConfigured as refusal:
        print(refusal)
print("asked", ",".join(fake.paths()))
"""


def test_a_refusal_is_the_same_at_a_second_reading_of_the_settings() -> None:
    done = probe_settings(TWICE_PROBE, PROBE_REFUSES="1")

    refusal = (
        "The secrets were not fetched: Infisical answered 401 (UnauthorizedError) "
        "when the machine identity logged in."
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.splitlines() == [refusal, refusal, f"asked {LOGIN_PATH}"]


def test_what_else_stops_the_start_is_said_at_a_second_reading_too() -> None:
    done = probe_settings(TWICE_PROBE, RANDIRANDI_ALLOWED_HOSTS="")

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.splitlines() == [
        "RANDIRANDI_ALLOWED_HOSTS is not set.",
        "RANDIRANDI_ALLOWED_HOSTS is not set.",
        f"asked {LOGIN_PATH},{READ_PATH},{END_PATH}",
    ]


@pytest.mark.parametrize("state,enabled", [("closed", True), ("open", False)])
def test_production_settings_apply_explicit_state_with_password_kept_in_infisical(
    state: str, enabled: bool
) -> None:
    probe = (
        SETTINGS_PROBE.replace(
            GATE_ITEM,
            '{**SECRETS, "RANDIRANDI_ACCESS_GATE_PASSWORD": "made-up-" * 4, '
            '"RANDIRANDI_SMTP_USER": "made-up-project", '
            '"RANDIRANDI_SMTP_PASSWORD": "made-up-smtp-password"}',
        )
        + '\nprint("effective password", bool(settings.ACCESS_GATE_PASSWORD))\n'
    )
    result = probe_settings(
        probe,
        RANDIRANDI_ENVIRONMENT="production",
        RANDIRANDI_ACCESS_GATE_STATE=state,
    )
    assert result.returncode == 0, result.stderr
    assert f"gate on {enabled}" in result.stdout
    assert f"effective password {enabled}" in result.stdout
    assert "environment clean True" in result.stdout
    assert "made-up-smtp-password" not in result.stdout + result.stderr
