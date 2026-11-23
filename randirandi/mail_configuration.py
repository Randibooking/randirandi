# SPDX-License-Identifier: AGPL-3.0-only
"""TLS-only SMTP settings; credentials stay in the process's Infisical mapping."""

import contextlib
import re
from collections.abc import Collection, Mapping, Sequence
from email.utils import make_msgid, parseaddr
from typing import Any

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.core.mail.backends import smtp
from django.core.mail.backends.base import BaseEmailBackend
from django.utils.encoding import punycode

CONSOLE = {"BACKEND": "django.core.mail.backends.console.EmailBackend"}


class SmtpBackend(smtp.EmailBackend):
    """Django's SMTP backend, with each message's Message-ID on the domain of
    its own From address.

    Django names the Message-ID after the machine (`socket.getfqdn()`); in a
    container that is a bare host name with no domain. On 21 November 2026
    every login code to Hotmail and Outlook.com went to junk with SPF, DKIM
    and DMARC passing, and this header was the one difference left in the
    message itself. A Message-ID the message already has is kept, and so is
    Django's when the From address has no usable domain. The id is set on
    the message itself, so sending the same message again keeps it.
    """

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        for message in email_messages:
            headers = message.extra_headers
            if any(name.lower() == "message-id" for name in headers):
                continue
            # The From header Django writes: an explicit one wins, as there.
            sender = str(headers.get("From", message.from_email))
            _local, at, domain = parseaddr(sender)[1].rpartition("@")
            if not at or not domain:
                continue
            # A domain IDNA refuses is not usable: Django's own Message-ID stays.
            with contextlib.suppress(UnicodeError):
                headers["Message-ID"] = make_msgid(domain=punycode(domain))
        return super().send_messages(email_messages)


class SandboxRouter(BaseEmailBackend):
    """A sandbox that prints every message but those for real people it may
    reach, which are sent: the operator's own addresses, and the email of an
    active member an administrator added . A message with any other recipient is printed
    whole: it is never split.

    Members are read from the database for each message, so a member's later
    disabling or blocking stops real mail at once; nothing is cached. A
    member from an import (the made-up ones) is never mailed, and neither is
    an address that any other member also has without an add of their own.
    The permission ends when the member's email is changed after the add.
    The router exists only when RANDIRANDI_SANDBOX_MAIL_TO is not empty (it
    carries the SMTP settings); the deployed sandbox has the operator's list.
    """

    def __init__(self, *, allowed: Collection[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.allowed = {address.lower() for address in allowed}

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        real = [message for message in email_messages if self._real(message)]
        printed = [message for message in email_messages if message not in real]
        sent = 0
        for alias, group in (("console", printed), ("smtp", real)):
            if group:
                with mail.mailers[alias] as connection:
                    sent += connection.send_messages(group) or 0
        return sent

    def _real(self, message: EmailMessage) -> bool:
        recipients = {address.lower() for address in message.recipients()}
        if not recipients:
            return False
        others = recipients - self.allowed
        return not others or others <= self._added_by_administrator(others)

    @staticmethod
    def _added_by_administrator(addresses: Collection[str]) -> set[str]:
        """The addresses that belong to an active member an administrator added
        (an "added member" audit event targets the member), where no other
        member, in any organization, has the address without such an event.
        Origin alone proves nothing: the first-administrator command sets it
        on any active member."""
        # Imported here: the settings module imports this one before the
        # models can load.
        from randirandi.core.audit_types import AuditAction
        from randirandi.core.models import AuditEvent, Member, MemberStatus

        members = list(
            Member.across_organizations.filter(email__in=addresses).values_list(
                "pk", "organization_id", "email", "status"
            )
        )
        # The permission is to the address the administrator typed. It ends when
        # an "changed member email" event follows the add: audit events hold ids
        # only, so the order of the two events (their ids) is what is compared.
        first_add: dict[tuple[int, int], int] = {}
        last_change: dict[tuple[int, int], int] = {}
        for action, organization, object_id, event_id in (
            AuditEvent.across_organizations.filter(
                action__in=[AuditAction.ADD_MEMBER, AuditAction.CHANGE_MEMBER_EMAIL],
                object_kind="member",
                object_id__in=[pk for pk, *_ in members],
            )
            .order_by("pk")
            .values_list("action", "organization_id", "object_id", "pk")
        ):
            if action == AuditAction.ADD_MEMBER:
                first_add.setdefault((organization, object_id), event_id)
            else:
                last_change[(organization, object_id)] = event_id
        added = {
            key
            for key, event_id in first_add.items()
            if last_change.get(key, 0) < event_id
        }
        found: set[str] = set()
        refused: set[str] = set()
        for pk, organization, email, status in members:
            if (organization, pk) not in added:
                refused.add(email)
            elif status == MemberStatus.ACTIVE:
                found.add(email)
        return found - refused


def mailers(
    environ: Mapping[str, str],
    *,
    console: bool,
    sandbox_to: Collection[str] = (),
) -> dict[str, Any]:
    if console and not sandbox_to:
        return {"default": CONSOLE}
    smtp = _smtp(environ)
    if console:
        return {
            "default": {
                "BACKEND": f"{__name__}.SandboxRouter",
                "OPTIONS": {"allowed": sorted(sandbox_to)},
            },
            "console": CONSOLE,
            "smtp": smtp,
        }
    return {"default": smtp}


def _smtp(environ: Mapping[str, str]) -> dict[str, Any]:
    host = environ.get("RANDIRANDI_SMTP_HOST", "smtp.tem.scaleway.com")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host):
        raise ImproperlyConfigured("RANDIRANDI_SMTP_HOST must be a hostname.")
    security = environ.get("RANDIRANDI_SMTP_SECURITY", "starttls")
    if security not in {"starttls", "tls"}:
        raise ImproperlyConfigured("RANDIRANDI_SMTP_SECURITY must be starttls or tls.")
    value = environ.get("RANDIRANDI_SMTP_PORT", "465" if security == "tls" else "587")
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise ImproperlyConfigured("RANDIRANDI_SMTP_PORT must be a valid port.")
    credentials = {}
    for option, name in (
        ("username", "RANDIRANDI_SMTP_USER"),
        ("password", "RANDIRANDI_SMTP_PASSWORD"),
    ):
        credential = environ.get(name, "")
        if not credential:
            raise ImproperlyConfigured(f"{name} is required when email is really sent.")
        credentials[option] = credential
    return {
        "BACKEND": f"{__name__}.SmtpBackend",
        "OPTIONS": {
            "host": host,
            "port": int(value),
            "use_tls": security == "starttls",
            "use_ssl": security == "tls",
            "timeout": 10,
            **credentials,
        },
    }
