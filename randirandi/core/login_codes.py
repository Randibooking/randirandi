# SPDX-License-Identifier: AGPL-3.0-only
"""Browser-bound, single-use email codes; no session or email is granted here."""

import ipaddress
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from django.db import connection
from django.utils.crypto import constant_time_compare, salted_hmac

from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    LoginAttempt,
    LoginCode,
    LoginRequest,
    Member,
    MemberStatus,
    Organization,
)

LIFETIME = timedelta(minutes=10)
REQUEST_WINDOW = timedelta(minutes=15)
# A stranger who knows a member's email must not stop that member signing in
# from their own network. The five-request limit and the voiding of earlier
# codes are counted per member and network; the ceiling counts the codes
# issued to the member in the window (limited requests issue nothing and do
# not count), and a network the member signed in from in the last 30 days is
# exempt from it. IPv6 is one network per /64. Residual risk: docs/data-model.md.
MEMBER_REQUEST_LIMIT = 5
MEMBER_CEILING = 8
KNOWN_NETWORK_DAYS = 30


def network_key(network: str) -> str:
    """The address as limits count it: IPv4 whole, IPv6 as its /64."""
    found = ipaddress.ip_address(network)
    if isinstance(found, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{found}/64", strict=False).network_address)
    return str(found)


def _known_network(
    org: Organization, member: Member, address: str, moment: datetime
) -> bool:
    """The member signed in from this network (kind verify, accepted) in the
    last 30 days; the attempt records already hold both."""
    since = moment - timedelta(days=KNOWN_NETWORK_DAYS)
    return any(
        network_key(seen) == address
        for seen in LoginAttempt.objects.of(org)
        .filter(member=member, kind="verify", outcome="accepted", at__gt=since)
        .values_list("network", flat=True)
        .distinct()
    )


NETWORK_REQUEST_LIMIT = 20
MAX_ATTEMPTS = 5
# The anonymous payment return keeps an order id in a new session row; one
# network address may start this many in a window . Over it, the
# return writes nothing and keeps no order.
RETURN_LIMIT = 20


def record_refusal(
    *,
    organization: Organization,
    kind: Literal["request", "verify", "mfa"],
    network: str,
    member_id: int | None = None,
    limited: bool = False,
) -> None:
    """Record an application submission without storing its supplied contents."""
    moment, address = utc_now(None), str(ipaddress.ip_address(network))

    def apply(org: Organization) -> None:
        member = (
            Member.objects.of(org).filter(pk=member_id).first()
            if member_id is not None
            else None
        )
        LoginAttempt.objects.create(
            organization=org,
            member=member,
            at=moment,
            network=address,
            kind=kind,
            outcome=(
                "limited"
                if limited and kind == "mfa"
                else "unknown"
                if kind == "request"
                else "refused"
            ),
        )

    organization_transaction(organization.pk, apply)


def allow_return(organization: Organization, network: str) -> bool:
    """Count an anonymous payment return from this network; False when the
    network is over its limit, in which case nothing is written, so a flood
    adds at most RETURN_LIMIT rows per window."""
    moment, address = utc_now(None), network_key(network)

    def apply(org: Organization) -> bool:
        recent = LoginAttempt.objects.of(org).filter(
            kind="return", network=address, at__gt=moment - REQUEST_WINDOW
        )
        if recent.count() >= RETURN_LIMIT:
            return False
        LoginAttempt.objects.create(
            organization=org,
            at=moment,
            network=address,
            kind="return",
            outcome="unknown",
        )
        return True

    return organization_transaction(organization.pk, apply)


def expire_requests(organization: Organization, moment: datetime) -> int:
    """Preserve unprocessed requests within retention; caller holds the org lock."""
    if not connection.in_atomic_block:
        raise RuntimeError("Request expiry requires an organization transaction.")
    rows = LoginRequest.objects.of(organization).filter(at__lte=moment - LIFETIME)
    LoginAttempt.objects.of(organization).bulk_create(
        [
            LoginAttempt(
                organization=organization,
                at=row.at,
                network=row.network,
                kind="request",
                outcome="unknown",
            )
            for row in rows.filter(
                claimed_at__isnull=True, at__gt=moment - timedelta(days=30)
            )
        ]
    )
    count = rows.count()
    rows.delete()
    return count


@dataclass(frozen=True)
class IssuedCode:
    challenge: str
    code: str = field(repr=False)
    member_id: int


@dataclass(frozen=True)
class VerifiedCode:
    member: Member
    authority: str = field(repr=False)


def browser_digest(organization_id: int, browser: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", browser):
        raise ValueError("A random browser binding is required.")
    return salted_hmac(
        f"randirandi.login-browser.{organization_id}", browser, algorithm="sha256"
    ).hexdigest()


def code_digest(organization_id: int, member_id: int, challenge: str, code: str) -> str:
    return salted_hmac(
        f"randirandi.login-code.{organization_id}.{member_id}.{challenge}",
        code,
        algorithm="sha256",
    ).hexdigest()


def issue_code(
    *,
    organization: Organization,
    member_id: int,
    browser: str,
    network: str,
    challenge: str | None = None,
    now: datetime | None = None,
) -> IssuedCode | None:
    """Issue after reloading the status; serialize with imports and other requests.

    Only the caller performing background delivery receives the clear code.
    The database keeps an HMAC using Django's externally supplied secret key.
    """
    binding = browser_digest(organization.pk, browser)
    # Reject malformed/out-of-range input before opening a transaction.
    moment, address, token, _expires, _window = _issue_inputs(network, challenge, now)
    return organization_transaction(
        organization.pk,
        lambda org: issue_locked_code(
            organization=org,
            member_id=member_id,
            binding=binding,
            network=address,
            challenge=token,
            now=moment,
        ),
    )


def _issue_inputs(
    network: str,
    challenge: str | None,
    now: datetime | None,
) -> tuple[datetime, str, str, datetime, datetime]:
    moment = utc_now(now)
    address = network_key(network)
    token = secrets.token_hex(16) if challenge is None else challenge
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("A random challenge is required.")
    try:
        return moment, address, token, moment + LIFETIME, moment - REQUEST_WINDOW
    except OverflowError:
        raise ValueError("Datetime exceeds the supported range.") from None


def issue_locked_code(
    *,
    organization: Organization,
    member_id: int,
    binding: str,
    network: str,
    challenge: str | None = None,
    now: datetime | None = None,
    requested_at: datetime | None = None,
) -> IssuedCode | None:
    """Internal worker primitive; caller holds the organization transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError("Code issuance requires an organization transaction.")
    if not re.fullmatch(r"[0-9a-f]{64}", binding):
        raise ValueError("A keyed browser digest is required.")
    moment, address, token, expires, window_start = _issue_inputs(
        network, challenge, now
    )

    def apply(org: Organization) -> IssuedCode | None:
        member = Member.objects.of(org).filter(pk=member_id).first()
        valid_member = (
            member is not None
            and member.status == MemberStatus.ACTIVE
            and bool(member.email)
        )
        recent = LoginAttempt.objects.of(org).filter(
            kind="request", at__gt=window_start
        )
        limited = recent.filter(network=address).count() >= NETWORK_REQUEST_LIMIT
        if member is not None:
            own = recent.filter(member=member)
            limited |= own.filter(network=address).count() >= MEMBER_REQUEST_LIMIT
            limited |= own.filter(
                outcome="issued"
            ).count() >= MEMBER_CEILING and not _known_network(
                org, member, address, moment
            )
        LoginAttempt.objects.create(
            organization=org,
            member=member,
            at=utc_now(requested_at) if requested_at is not None else moment,
            network=address,
            kind="request",
            outcome="limited" if limited else "issued" if valid_member else "unknown",
        )
        if limited or not valid_member or member is None:
            return None
        # A new request invalidates the member's earlier codes asked for from the
        # same network, in any browser; a request from another network leaves
        # them alone, so a stranger cannot void the member's code.
        LoginCode.objects.of(org).filter(
            member=member, network=address, used_at__isnull=True
        ).update(used_at=moment)
        code = f"{secrets.randbelow(1_000_000):06d}"
        LoginCode.objects.create(
            organization=org,
            member=member,
            challenge=token,
            browser_digest=binding,
            network=address,
            scrambled_code=code_digest(org.pk, member.pk, token, code),
            expires_at=expires,
        )
        return IssuedCode(token, code, member.pk)

    return apply(organization)


def verify_code(
    *,
    organization: Organization,
    challenge: str,
    browser: str,
    code: str,
    network: str,
    now: datetime | None = None,
) -> VerifiedCode | None:
    """Consume atomically; the caller still must complete admin MFA/session setup."""
    moment = utc_now(now)
    binding = browser_digest(organization.pk, browser)
    address = str(ipaddress.ip_address(network))
    valid_input = re.fullmatch(r"[0-9a-f]{32}", challenge) is not None
    challenge = challenge if valid_input else ""
    code = code if len(code) <= 128 else ""

    def apply(org: Organization) -> VerifiedCode | None:
        row = (
            LoginCode.objects.of(org)
            .select_related("member")
            .filter(challenge=challenge)
            .exclude(challenge="")
            .first()
        )
        member = row.member if row is not None else None
        expected = code_digest(org.pk, member.pk if member else 0, challenge, code)
        matches = constant_time_compare(
            expected, row.scrambled_code if row else "0" * 64
        )
        same_browser = constant_time_compare(
            binding, row.browser_digest if row else "0" * 64
        )
        usable = (
            valid_input
            and row is not None
            and row.used_at is None
            and row.expires_at > moment
            and row.attempts < MAX_ATTEMPTS
            and member is not None
            and member.status == MemberStatus.ACTIVE
            and bool(member.email)
            and same_browser
        )
        accepted = usable and matches and re.fullmatch(r"[0-9]{6}", code) is not None
        if usable and row is not None:
            row.attempts += 1
            if accepted or row.attempts >= MAX_ATTEMPTS:
                row.used_at = moment
            row.save(update_fields=["attempts", "used_at"])
        LoginAttempt.objects.create(
            organization=org,
            member=member,
            at=moment,
            network=address,
            kind="verify",
            outcome="accepted" if accepted else "refused",
        )
        # A member signs in with the code alone; an administrator's sign-in
        # ends with the authenticator code (`second_factor.verify_authenticator`).
        if accepted and member is not None and not member.is_staff:
            Member.objects.of(org).filter(pk=member.pk).update(last_login_at=moment)
        return (
            VerifiedCode(member, member.get_session_auth_hash())
            if accepted and member is not None
            else None
        )

    return organization_transaction(organization.pk, apply)
