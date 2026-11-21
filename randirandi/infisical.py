# SPDX-License-Identifier: AGPL-3.0-only
"""The secrets, fetched from Infisical when the process starts.

A deployed container holds one secret: the key of its machine identity. With
it the process logs in to Infisical, reads its environment's secrets once and
keeps them in memory. They are not written to disk and not put into the
process environment, so a process started from this one does not get them.
The key is taken out of the environment for the same reason, and the login
it gave is ended as soon as the secrets are read.

With no machine identity in the environment, on the laptop and in the tests,
nothing is fetched and the environment is all the settings read.

Nothing here prints a value: not the key, not the login and not a secret.
"""

import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from typing import Any, Protocol

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

# The key is sent to this address and to no other: Infisical's own, in the EU.
HOST = "https://eu.infisical.com"

# The machine identity. The first and the last are not secrets.
CLIENT_ID = "RANDIRANDI_INFISICAL_CLIENT_ID"
KEY = "RANDIRANDI_INFISICAL_CLIENT_SECRET"
PROJECT = "RANDIRANDI_INFISICAL_PROJECT"
IDENTITY = (CLIENT_ID, KEY, PROJECT)

TIMEOUT_SECONDS = 10
TRIES = 3
WAIT_SECONDS = 2.0

UNKNOWN_FORM = (
    "The secrets were not fetched: Infisical answered in a form this app does not know."
)


class Ask(Protocol):
    """One question to Infisical: the status, and what it answered."""

    def __call__(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, str] | None = None,
        login: str | None = None,
    ) -> tuple[int, Any]: ...


class Held(Mapping[str, str]):
    """The environment with the secrets over it. Shows names, never values."""

    def __init__(self, environ: Mapping[str, str], secrets: Mapping[str, str]) -> None:
        self._values = {**environ, **secrets}
        self._secrets = len(secrets)

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"<the environment, with {self._secrets} secrets from Infisical>"


# What this process's one question to Infisical gave: the secrets, or the
# refusal.
_once: list[Mapping[str, str] | ImproperlyConfigured] = []


def at_start(environment: str) -> Mapping[str, str]:
    """What this process reads its settings from. Infisical is asked once for
    each process, whatever it answers: Django reads the settings a second
    time when the first reading failed, and gets the same answer."""
    if not _once:
        try:
            _once.append(load(os.environ, environment))
        except ImproperlyConfigured as refusal:
            _once.append(refusal)
    read = _once[0]
    if isinstance(read, ImproperlyConfigured):
        raise read
    return read


def load(
    environ: MutableMapping[str, str],
    environment: str,
    *,
    ask: Ask | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, str]:
    """What the settings read: the environment, and with a machine identity
    in it the secrets of this environment from Infisical as well."""
    there = [name for name in IDENTITY if environ.get(name)]
    if not there:
        return environ
    missing = [name for name in IDENTITY if name not in there]
    if missing:
        raise ImproperlyConfigured(
            "Half a machine identity is in the environment: "
            + ", ".join(missing)
            + " is not set. With none of the three, nothing is fetched."
        )

    # Out of the environment first, so that nothing started from here gets it.
    key = environ.pop(KEY)
    secrets = fetch(
        environ[CLIENT_ID],
        key,
        environ[PROJECT],
        environment,
        ask=ask or _ask,
        sleep=sleep,
    )
    twice = sorted(name for name in secrets if environ.get(name))
    if twice:
        raise ImproperlyConfigured(
            "In the environment and in Infisical: "
            + ", ".join(twice)
            + ". A secret rests in Infisical and nowhere else."
        )
    return Held(environ, secrets)


def fetch(
    client_id: str,
    key: str,
    project: str,
    environment: str,
    *,
    ask: Ask,
    sleep: Callable[[float], None],
) -> dict[str, str]:
    """Logs in, reads the environment's secrets and ends the login."""
    told = _call(
        ask,
        sleep,
        "when the machine identity logged in",
        "POST",
        "/api/v1/auth/universal-auth/login",
        body={"clientId": client_id, "clientSecret": key},
    )
    login = told.get("accessToken") if isinstance(told, dict) else None
    if not isinstance(login, str) or not login:
        raise ImproperlyConfigured(UNKNOWN_FORM)
    asked = urllib.parse.urlencode(
        {"workspaceId": project, "environment": environment, "secretPath": "/"}
    )
    try:
        told = _call(
            ask,
            sleep,
            "when the secrets were read",
            "GET",
            f"/api/v3/secrets/raw?{asked}",
            login=login,
        )
        return _secrets(told)
    finally:
        _end(ask, login)


def _secrets(told: Any) -> dict[str, str]:
    listed = told.get("secrets") if isinstance(told, dict) else None
    if not isinstance(listed, list):
        raise ImproperlyConfigured(UNKNOWN_FORM)
    secrets: dict[str, str] = {}
    for entry in listed:
        if not isinstance(entry, dict):
            raise ImproperlyConfigured(UNKNOWN_FORM)
        name, value = entry.get("secretKey"), entry.get("secretValue")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ImproperlyConfigured(UNKNOWN_FORM)
        # Infisical puts a placeholder where an identity may see that a
        # secret is there and may not read it. A placeholder is not a secret.
        if entry.get("secretValueHidden"):
            raise ImproperlyConfigured(
                f"The secrets were not fetched: the machine identity may not "
                f"read {_plain(name)}."
            )
        secrets[name] = value
    if not secrets:
        raise ImproperlyConfigured(
            "The secrets were not fetched: Infisical holds none for this environment."
        )
    return secrets


def _call(
    ask: Ask,
    sleep: Callable[[float], None],
    when: str,
    method: str,
    path: str,
    *,
    body: Mapping[str, str] | None = None,
    login: str | None = None,
) -> Any:
    """Asks, and asks again when Infisical is away or busy. A refusal is not
    asked twice."""
    answer = "did not answer"
    for attempt in range(TRIES):
        if attempt:
            sleep(WAIT_SECONDS * attempt)
        try:
            status, told = ask(method, path, body=body, login=login)
        except OSError, http.client.HTTPException:
            answer = "did not answer"
            continue
        if status == 200:
            return told
        answer = f"answered {status}{_why(told)}"
        if status < 500 and status != 429:
            break
    raise ImproperlyConfigured(
        f"The secrets were not fetched: Infisical {answer} {when}."
    )


def _end(ask: Ask, login: str) -> None:
    """Ends the login the fetch made. If that fails the app still starts: the
    login ends by itself, at the time set on the machine identity."""
    try:
        status, _ = ask(
            "POST", "/api/v1/auth/token/revoke", body={"accessToken": login}
        )
    except OSError, http.client.HTTPException:
        status = 0
    if status != 200:
        logger.warning(
            "the login to Infisical was not ended (%s); it ends by itself",
            status or "no answer",
        )


def _why(told: Any) -> str:
    """Infisical's own name for a refusal, and the names of what a refused
    request lacked. Names only: what was sent is never repeated."""
    if not isinstance(told, dict):
        return ""
    names = [_plain(told.get("error"))]
    issues = told.get("message")
    if isinstance(issues, list):
        for issue in issues:
            where = issue.get("path") if isinstance(issue, dict) else None
            if isinstance(where, list):
                names.append(_plain(".".join(str(part) for part in where)))
    names = [name for name in names if name]
    return f" ({', '.join(names)})" if names else ""


def _plain(name: Any) -> str:
    """A name as letters, digits, dots and underscores, or nothing."""
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,60}", name):
        return name
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the key or the login to another address."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _ask(
    method: str,
    path: str,
    *,
    body: Mapping[str, str] | None = None,
    login: str | None = None,
) -> tuple[int, Any]:
    # Infisical's front door refuses a client that does not say what it is.
    headers = {"Accept": "application/json", "User-Agent": "randirandi"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if login is not None:
        headers["Authorization"] = f"Bearer {login}"
    request = urllib.request.Request(  # noqa: S310
        HOST + path,
        data=None if body is None else json.dumps(dict(body)).encode(),
        method=method,
        headers=headers,
    )
    # Straight to Infisical: no proxy named in the environment is used.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, _json(error.read())


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None
