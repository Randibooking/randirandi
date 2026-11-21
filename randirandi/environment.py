# SPDX-License-Identifier: AGPL-3.0-only
"""The environment and the test switches.

The deployment says which environment this is: sandbox or production. The
test switches follow from it. They are read here and nowhere else, and a
combination that must never run stops the app before it starts.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parseaddr
from enum import StrEnum
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import URLValidator, validate_email

from randirandi import infisical

GATE = "RANDIRANDI_ACCESS_GATE_PASSWORD"
GATE_STATE = "RANDIRANDI_ACCESS_GATE_STATE"
ORGANIZATION_DOMAIN = "RANDIRANDI_ORGANIZATION_DOMAIN"
SENDER = "RANDIRANDI_SENDER"
DOCUMENTATION = "RANDIRANDI_DOCUMENTATION_URL"
DEFAULT_DOCUMENTATION_URL = "https://help.randirandi.com/"
SANDBOX_OPEN = "RANDIRANDI_SANDBOX_OPEN"
GATE_PASSWORD_LENGTH = 24
SANDBOX_MAIL_TO = "RANDIRANDI_SANDBOX_MAIL_TO"
AUTHENTICATOR_KEY = "RANDIRANDI_AUTHENTICATOR_KEY"

_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}


class Environment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


def organization_domain(environ: Mapping[str, str]) -> str:
    """The domain beneath which an organization's single label is resolved.
    The deployment sets it; the code has no domain of its own."""
    domain = environ.get(ORGANIZATION_DOMAIN, "").strip().lower()
    if not domain:
        raise ImproperlyConfigured(
            f"{ORGANIZATION_DOMAIN} is required: the domain beneath which each "
            "organization has its own name, such as example.org."
        )
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(domain) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", domain):
        raise ImproperlyConfigured(f"{ORGANIZATION_DOMAIN} must be a DNS name.")
    return domain


@dataclass(frozen=True)
class Sender:
    """Who the deployment's email comes from."""

    # As the From header carries it: "Name <address>" or the address alone.
    header: str
    address: str


def sender(environ: Mapping[str, str]) -> Sender:
    """The deployment's sender, required: there is no address in the code."""
    header = environ.get(SENDER, "").strip()
    if not header:
        raise ImproperlyConfigured(
            f"{SENDER} is required: the address the deployment's email comes "
            "from, as Name <address> or the address alone."
        )
    name, address = parseaddr(header)
    try:
        validate_email(address)
    except ValidationError:
        address = ""
    if not address or header not in (address, f"{name} <{address}>"):
        raise ImproperlyConfigured(f"{SENDER} must be Name <address> or an address.")
    return Sender(header=header, address=address)


def documentation_url(environ: Mapping[str, str]) -> str:
    """An operator-selected Help destination, without changing existing installs."""
    value = environ.get(DOCUMENTATION, "") or DEFAULT_DOCUMENTATION_URL
    try:
        URLValidator(schemes=["https", "http"])(value)
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Credentials are not part of a Help address")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Control character in Help address")
    except ValidationError, ValueError:
        raise ImproperlyConfigured(
            f"{DOCUMENTATION} must be an absolute HTTP(S) URL without credentials."
        ) from None
    return value


def client_address_source(environ: Mapping[str, str]) -> str:
    source = environ.get("RANDIRANDI_CLIENT_ADDRESS_SOURCE", "direct")
    if source not in {"direct", "scaleway"}:
        raise ImproperlyConfigured(
            "RANDIRANDI_CLIENT_ADDRESS_SOURCE must be direct or scaleway."
        )
    return source


def sandbox_mail_to(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The addresses a sandbox may really send to, in lower case as members
    store them. Empty: every message goes to the console."""
    found: dict[str, None] = {}
    for part in environ.get(SANDBOX_MAIL_TO, "").split(","):
        address = part.strip().lower()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError:
            # The value is not repeated: it is an address.
            raise ImproperlyConfigured(
                f"{SANDBOX_MAIL_TO} must hold email addresses, separated by commas."
            ) from None
        found[address] = None
    return tuple(found)


def authenticator_key(environ: Mapping[str, str], *, required: bool) -> str:
    """The key that encrypts the administrators' authenticator secrets: 32
    random bytes as 64 lower-case hex characters. A deployed sandbox and
    production refuse to start without it; a laptop may leave it unset. A
    value that is set but malformed is refused anywhere, and never repeated."""
    value = environ.get(AUTHENTICATOR_KEY, "").strip()
    if not value:
        if required:
            raise ImproperlyConfigured(
                f"{AUTHENTICATOR_KEY} is not set. A deployed sandbox and "
                f"production need it (openssl rand -hex 32, kept in Infisical)."
            )
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ImproperlyConfigured(
            f"{AUTHENTICATOR_KEY} must be 32 random bytes written as 64 "
            f"lower-case hex characters."
        )
    return value


@dataclass(frozen=True)
class Switches:
    """The access gate and the sandbox-only test options."""

    # Messages are printed to the log instead of being sent.
    email_to_console: bool
    # Payments go to the payment provider's test environment.
    payment_test_environment: bool
    # The command that loads made-up members may run.
    made_up_members: bool
    # The whole address asks for a shared password first (the access gate).
    access_gate: bool
    # With email_to_console: a message whose every recipient is here is really
    # sent; any other message is still printed. Never more than the operator's own
    # addresses .
    sandbox_mail_to: tuple[str, ...] = ()
    # The sandbox is open to the login alone, on purpose .
    sandbox_open: bool = False


@dataclass(frozen=True)
class Deployment:
    environment: Environment
    debug: bool
    switches: Switches


def load(environ: Mapping[str, str]) -> Deployment:
    """Read the environment and the switches, and refuse what must not run."""
    environment = which(environ)
    sandbox = environment is Environment.SANDBOX
    # A machine identity is what a deployed container has and a laptop has not.
    deployed = bool(environ.get(infisical.CLIENT_ID))
    debug = switch(environ, "RANDIRANDI_DEBUG", default=False)
    # Production has an explicit state, independent of the password's presence.
    # Sandbox retains its existing password/open-switch contract.
    opened = switch(environ, SANDBOX_OPEN, default=False)
    gate = environ.get(GATE, "")
    authenticator_key(environ, required=deployed or not sandbox)
    gate_state = environ.get(GATE_STATE, "").strip().lower()
    if sandbox and gate_state:
        raise ImproperlyConfigured(f"{GATE_STATE} is for production only.")
    if not sandbox and gate_state not in {"open", "closed"}:
        raise ImproperlyConfigured(
            f"{GATE_STATE} must explicitly be open or closed in production."
        )
    gated = bool(gate) if sandbox else gate_state == "closed"
    if gated and (not gate.strip() or len(gate) < GATE_PASSWORD_LENGTH):
        raise ImproperlyConfigured(
            f"{GATE} is required for a closed gate: at least "
            f"{GATE_PASSWORD_LENGTH} characters, not only whitespace."
        )
    switches = Switches(
        email_to_console=switch(
            environ, "RANDIRANDI_EMAIL_TO_CONSOLE", default=sandbox
        ),
        payment_test_environment=switch(
            environ, "RANDIRANDI_PAYMENT_TEST_ENVIRONMENT", default=sandbox
        ),
        made_up_members=switch(environ, "RANDIRANDI_MADE_UP_MEMBERS", default=sandbox),
        access_gate=gated,
        sandbox_open=opened,
        sandbox_mail_to=sandbox_mail_to(environ),
    )

    if sandbox:
        # A sandbox never touches real money or real members.
        if not switches.payment_test_environment:
            raise ImproperlyConfigured(
                "A sandbox pays in the payment provider's test environment only. "
                "RANDIRANDI_PAYMENT_TEST_ENVIRONMENT cannot be off."
            )
        if not switches.made_up_members:
            raise ImproperlyConfigured(
                "A sandbox holds made-up members only. "
                "RANDIRANDI_MADE_UP_MEMBERS cannot be off."
            )
        # The list narrows the console; without the console it would narrow
        # nothing and every message would be sent.
        if switches.sandbox_mail_to and not switches.email_to_console:
            raise ImproperlyConfigured(
                f"{SANDBOX_MAIL_TO} needs RANDIRANDI_EMAIL_TO_CONSOLE on."
            )
        # Both is ambiguous: refused, so neither wins by accident.
        if gate and opened:
            raise ImproperlyConfigured(
                f"{GATE} and {SANDBOX_OPEN} are both set: a sandbox is closed "
                f"by the gate or open on purpose, not both. Remove one."
            )
        # On the internet a sandbox is closed to everyone but its testers,
        # unless it was opened on purpose.
        if deployed and not gate and not opened:
            raise ImproperlyConfigured(
                f"A deployed sandbox starts with its access gate or opened on "
                f"purpose: {GATE} is not in Infisical and {SANDBOX_OPEN} is not on."
            )
    else:
        on = [
            name
            for name, value in (
                ("RANDIRANDI_DEBUG", debug),
                ("RANDIRANDI_EMAIL_TO_CONSOLE", switches.email_to_console),
                (
                    "RANDIRANDI_PAYMENT_TEST_ENVIRONMENT",
                    switches.payment_test_environment,
                ),
                ("RANDIRANDI_MADE_UP_MEMBERS", switches.made_up_members),
                (SANDBOX_OPEN, switches.sandbox_open),
                (SANDBOX_MAIL_TO, bool(switches.sandbox_mail_to)),
            )
            if value
        ]
        if on:
            raise ImproperlyConfigured(
                "Production does not start with a test switch or debug on: "
                + ", ".join(on)
                + "."
            )

    return Deployment(environment=environment, debug=debug, switches=switches)


def which(environ: Mapping[str, str]) -> Environment:
    """Which environment this is. The deployment says it."""
    value = environ.get("RANDIRANDI_ENVIRONMENT", "")
    try:
        return Environment(value)
    except ValueError:
        raise ImproperlyConfigured(
            "RANDIRANDI_ENVIRONMENT must be 'sandbox' or 'production'. "
            "The deployment sets it; there is no default."
        ) from None


def switch(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise ImproperlyConfigured(f"{name} must be on or off, written as 1 or 0.")
