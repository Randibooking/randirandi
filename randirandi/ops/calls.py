# SPDX-License-Identifier: AGPL-3.0-only
"""The second container: it answers the platform's calls, and nothing else.

At the platform the migrations and the job runner cannot be jobs, because a
job does not reach the database . So a second container runs from the
same image, on the private network. It is private: the platform lets a call
through only with a token of its own. Two kinds of call come.

The platform's schedule, each minute. The job runner runs what is due, and
the app's health page is asked from outside, the way a visitor would ask it.
The platform has no such check of its own.

The deploy, once for each deploy. The deploy check, the grants, the
migrations and the application's rights run as the owner of the tables.
That user's password is made for the one run and comes with the call; it is
kept nowhere. The run is a process of its own. It shares only the startup
inputs it needs: the authenticator key, and production's gate and SMTP settings.
It gets neither the runtime identity nor payment credentials or the app's
database and signing keys.

No page of the product is served from here.
"""

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import close_old_connections

from randirandi.environment import Environment
from randirandi.logs import STRUCK, strike
from randirandi.ops.jobs import JobRunner

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
PORT = 8000
# A call is a few words. More than this is not read.
LARGEST_CALL = 16 * 1024
# How long one step of the deploy's run may take, in seconds.
PATIENCE = 10 * 60
# How many lines of what a step printed go back to the deploy.
LINES_KEPT = 30

# The deploy's run, in order. The first step that fails ends it.
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("the deploy check", ("check", "--deploy", "--fail-level", "WARNING")),
    ("the grants", ("grants",)),
    ("the migrations", ("migrate", "--no-input")),
    ("the application's rights", ("apply_privileges",)),
    ("the retention cleanup", ("purge_expired",)),
)

# What the deploy's run gets from this process's environment: which
# environment this is and where the database is. Not the machine identity.
HANDED_ON = (
    "PATH",
    "RANDIRANDI_ENVIRONMENT",
    "RANDIRANDI_ALLOWED_HOSTS",
    "RANDIRANDI_ORGANIZATION_DOMAIN",
    "RANDIRANDI_SENDER",
    "RANDIRANDI_DB_HOST",
    "RANDIRANDI_DB_PORT",
    "RANDIRANDI_DB_NAME",
)

_USER = re.compile(r"[a-z_][a-z0-9_]{0,62}")

type Told = dict[str, Any]
type Run = Callable[..., subprocess.CompletedProcess[str]]


def _production_startup() -> dict[str, str]:
    """Infisical values live in effective settings, not os.environ."""
    if settings.ENVIRONMENT is not Environment.PRODUCTION:
        return {}
    smtp = settings.MAILERS["default"]["OPTIONS"]
    gated = settings.SWITCHES.access_gate
    result = {
        "RANDIRANDI_ACCESS_GATE_STATE": "closed" if gated else "open",
        "RANDIRANDI_SMTP_HOST": smtp["host"],
        "RANDIRANDI_SMTP_PORT": str(smtp["port"]),
        "RANDIRANDI_SMTP_SECURITY": "tls" if smtp["use_ssl"] else "starttls",
        "RANDIRANDI_SMTP_USER": smtp["username"],
        "RANDIRANDI_SMTP_PASSWORD": smtp["password"],
    }
    if gated:
        result["RANDIRANDI_ACCESS_GATE_PASSWORD"] = settings.ACCESS_GATE_PASSWORD
    return result


def migrate(
    user: object,
    password: object,
    *,
    environ: Mapping[str, str] | None = None,
    run: Run = subprocess.run,
    authenticator_key: str | None = None,
) -> Told:
    """The deploy's run, as the owner of the tables."""
    if (
        not isinstance(user, str)
        or not _USER.fullmatch(user)
        or not isinstance(password, str)
        or not password
    ):
        return {"ok": False, "steps": [], "why": "no database user and password"}
    environ = os.environ if environ is None else environ
    handed = {name: environ[name] for name in HANDED_ON if environ.get(name)}
    handed |= {
        "RANDIRANDI_DB_USER": user,
        "RANDIRANDI_DB_PASSWORD": password,
        # Django does not start without a key. This one is for the run and
        # signs nothing that is kept.
        "RANDIRANDI_SECRET_KEY": secrets.token_urlsafe(50),
    }
    handed.update(_production_startup())
    # The run has no machine identity, so it fetches nothing from Infisical;
    # migration 0044 and production's start need the authenticator key, which
    # this process read from Infisical at its own start .
    key = settings.AUTHENTICATOR_KEY if authenticator_key is None else authenticator_key
    if key:
        handed["RANDIRANDI_AUTHENTICATOR_KEY"] = key
    hidden = sorted(
        {
            variant
            for name, value in handed.items()
            if value
            and (name.endswith(("PASSWORD", "KEY")) or name == "RANDIRANDI_SMTP_USER")
            # text=True performs universal-newline conversion on capture.
            for variant in (value, value.replace("\r\n", "\n").replace("\r", "\n"))
        },
        key=len,
        reverse=True,
    )

    steps: list[Told] = []
    for name, arguments in STEPS:
        started = time.monotonic()
        try:
            done = run(
                [sys.executable, "manage.py", *arguments],
                cwd=ROOT,
                env=handed,
                capture_output=True,
                text=True,
                timeout=PATIENCE,
                check=False,
            )
            code, printed = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            code, printed = -1, f"not done after {PATIENCE} seconds, and stopped"
        # Redact before splitting: SMTP and gate values can contain newlines.
        for value in hidden:
            printed = printed.replace(value, STRUCK)
        said = [strike(line) for line in printed.strip().splitlines()[-LINES_KEPT:]]
        seconds = round(time.monotonic() - started, 1)
        steps.append({"step": name, "ok": code == 0, "seconds": seconds, "said": said})
        if code != 0:
            logger.error("deploy run: %s failed\n%s", name, "\n".join(said))
            break
        logger.info("deploy run: %s done in %s s", name, seconds)
    return {"ok": len(steps) == len(STEPS) and steps[-1]["ok"], "steps": steps}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The health page answers where it is asked, or it has not answered."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def ask_outside(address: str, *, timeout: float = 10) -> str:
    """Asks the app's health page the way a visitor would. "ok", or "failed"
    with the status or the kind of error: never what the answer held."""
    request = urllib.request.Request(  # noqa: S310
        address, headers={"User-Agent": "randirandi-outside-check"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status == 200 and json.loads(response.read(200)) == {
                "status": "ok"
            }:
                return "ok"
            return "failed: not the health page's answer"
    except urllib.error.HTTPError as error:
        with error:
            return f"failed: {error.code}"
    except (OSError, ValueError) as error:
        return f"failed: {type(error).__name__}"


@dataclass
class Calls:
    """What a call sets going."""

    runner: JobRunner
    # The app's health page at its public address. Empty: it is not asked.
    outside_address: str = ""
    ask: Callable[[str], str] = ask_outside
    migrate: Callable[[object, object], Told] = migrate

    def schedule(self) -> Told:
        self.runner.run_due()
        close_old_connections()
        outside = None
        if self.outside_address:
            outside = self.ask(self.outside_address)
            # One line for each call. An alert reads these lines: it goes off
            # when they say failed, and when they stop coming.
            if outside == "ok":
                logger.info("outside check: ok")
            else:
                logger.error("outside check: %s", outside)
        return {"ok": True, "outside": outside}


def handler(calls: Calls) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "randirandi"
        sys_version = ""
        # A caller that connects and says nothing is let go after this long.
        timeout = 15

        def do_POST(self) -> None:
            try:
                told, status = self._run(), 200
            except Exception:
                # Whatever a call raises, the next one is answered.
                logger.exception("a call failed")
                told, status = {"ok": False}, 500
            body = json.dumps(told).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _run(self) -> Told:
            asked = self._asked()
            if asked.get("run") == "migrate":
                return calls.migrate(asked.get("user"), asked.get("password"))
            # Whatever else the platform's schedule sends, it means: run.
            return calls.schedule()

        def _asked(self) -> Told:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if not 0 < length <= LARGEST_CALL:
                return {}
            try:
                asked = json.loads(self.rfile.read(length))
            except ValueError:
                return {}
            return asked if isinstance(asked, dict) else {}

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # The method, the path without its query, and the status.
            logger.info("call %s %s %s", self.command, self.path.split("?")[0], code)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    return Handler


def serve(calls: Calls, *, port: int = PORT) -> None:
    """Answers one call at a time, until the platform says stop."""
    server = HTTPServer(("0.0.0.0", port), handler(calls))  # noqa: S104

    def stop(*_: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
