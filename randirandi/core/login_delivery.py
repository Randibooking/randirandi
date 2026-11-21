# SPDX-License-Identifier: AGPL-3.0-only
"""Persistent sign-in requests; identity lookup and email run off the HTTP path."""

import ipaddress
import logging
import secrets
import threading

from django.core import mail
from django.db import close_old_connections, connections
from django.utils.crypto import constant_time_compare

from randirandi.core.emails import sign_in_message
from randirandi.core.login_codes import (
    LIFETIME,
    NETWORK_REQUEST_LIMIT,
    REQUEST_WINDOW,
    IssuedCode,
    browser_digest,
    expire_requests,
    issue_locked_code,
)
from randirandi.core.login_identity import identifier_digest
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import LoginAttempt, LoginRequest, Member, Organization

logger = logging.getLogger(__name__)
_wake_lock = threading.Lock()


def enqueue_code(
    *, organization: Organization, identifier: str, browser: str, network: str
) -> str:
    """The caller has validated/normalized the identifier; never look it up here."""
    network = str(ipaddress.ip_address(network))
    moment = utc_now(None)
    binding = browser_digest(organization.pk, browser)
    challenge = secrets.token_hex(16)

    def apply(org: Organization) -> None:
        completed = (
            LoginAttempt.objects.of(org)
            .filter(
                kind="request",
                network=network,
                at__gt=moment - REQUEST_WINDOW,
            )
            .count()
        )
        pending = (
            LoginRequest.objects.of(org)
            .filter(
                claimed_at__isnull=True,
                network=network,
                at__gt=moment - REQUEST_WINDOW,
            )
            .count()
        )
        if completed + pending >= NETWORK_REQUEST_LIMIT:
            LoginAttempt.objects.create(
                organization=org,
                network=network,
                at=moment,
                kind="request",
                outcome="limited",
            )
            return
        LoginRequest.objects.create(
            organization=org,
            identifier_digest=identifier_digest(org.pk, identifier),
            browser_digest=binding,
            challenge=challenge,
            network=network,
            at=moment,
        )

    organization_transaction(organization.pk, apply)
    return challenge


def _deliver(org: Organization, request_id: int) -> None:
    def claim(current: Organization) -> tuple[IssuedCode, str] | None:
        row = (
            LoginRequest.objects.of(current)
            .filter(
                pk=request_id,
                claimed_at__isnull=True,
            )
            .first()
        )
        if row is None:
            return None
        moment = utc_now(None)
        if row.at <= moment - LIFETIME:
            expire_requests(current, moment)
            return None
        # No HTTP response waits for this identity lookup.
        member = None
        # The small association's current contacts are matched in the worker.
        # No second contact copy or identity lookup is kept on the HTTP path.
        for candidate in Member.objects.of(current).order_by("pk").iterator():
            email_matches = constant_time_compare(
                row.identifier_digest, identifier_digest(current.pk, candidate.email)
            )
            phone_matches = constant_time_compare(
                row.identifier_digest, identifier_digest(current.pk, candidate.phone)
            )
            if email_matches or phone_matches:
                member = candidate
        issued = issue_locked_code(
            organization=current,
            member_id=member.pk if member is not None else 0,
            binding=row.browser_digest,
            network=row.network,
            challenge=row.challenge,
            now=moment,
            requested_at=row.at,
        )
        row.identifier_digest = ""
        row.claimed_at = moment
        row.save(update_fields=["identifier_digest", "claimed_at"])
        return (
            (issued, member.email)
            if issued is not None and member is not None
            else None
        )

    try:
        delivery = organization_transaction(org.pk, claim)
        if delivery is None:
            return
        issued, recipient = delivery
        message = sign_in_message(
            organization=org, recipient=recipient, code=issued.code
        )
        with mail.mailers["default"] as connection:
            connection.send_messages([message])
    except Exception:  # noqa: BLE001 — never log provider secrets or recipients
        # Provider exceptions may contain recipients or authentication material.
        # A failed request can be retried by asking for a new code.
        logger.warning("login email delivery failed")
    finally:
        LoginRequest.objects.of(org).filter(
            pk=request_id, claimed_at__isnull=False
        ).delete()


def send_pending_codes() -> None:
    """The existing scheduled worker also drains requests after a web restart."""
    moment = utc_now(None)
    for org in Organization.objects.order_by("pk"):
        organization_transaction(
            org.pk, lambda current: expire_requests(current, moment)
        )
        requests = list(
            LoginRequest.objects.of(org)
            .filter(
                claimed_at__isnull=True,
            )
            .order_by("at", "pk")
            .values_list("pk", flat=True)[:50]
        )
        for request_id in requests:
            _deliver(org, request_id)


def wake_delivery() -> None:
    """One bounded background drain per web process; durable worker is fallback."""
    if not _wake_lock.acquire(blocking=False):
        return

    def run() -> None:
        close_old_connections()
        try:
            send_pending_codes()
        except Exception:  # noqa: BLE001 — never log provider secrets or recipients
            logger.warning("login email worker failed")
        finally:
            connections.close_all()
            _wake_lock.release()

    try:
        threading.Thread(target=run, name="login-email", daemon=True).start()
    except RuntimeError:
        _wake_lock.release()
        logger.warning("login email worker unavailable")
