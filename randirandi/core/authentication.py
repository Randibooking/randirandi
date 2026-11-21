# SPDX-License-Identifier: AGPL-3.0-only
"""Passwordless session loading, current authority, thirty-minute inactivity and
a twelve-hour absolute lifetime."""

import math
import uuid
from collections.abc import Callable

from django.contrib.auth import login, logout
from django.contrib.auth.backends import BaseBackend
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.http import urlencode

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.login_codes import VerifiedCode
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import Authenticator, Member, Organization
from randirandi.core.organization_request import organization_for_request

IDLE_SECONDS = 30 * 60
# A session ends this long after the login, whatever the activity . Members and
# administrators alike.
ABSOLUTE_SECONDS = 12 * 60 * 60
BACKEND = "randirandi.core.authentication.EmailCodeBackend"
# The one thing a login keeps from the browser's session before it: the id of
# an order the member came back to from Vipps .
RETURN_KEY = "return_order"


def checked_order(value: object) -> uuid.UUID | None:
    """The order id when the value is exactly a UUID in its written form."""
    if not isinstance(value, str):
        return None
    try:
        found = uuid.UUID(value)
    except ValueError:
        return None
    return found if str(found) == value else None


def keep_order(session: SessionBase, public_id: uuid.UUID) -> None:
    session[RETURN_KEY] = str(public_id)


def flush_keeping_order(session: SessionBase) -> None:
    """Flush the session; the one checked order id, and nothing else, stays."""
    kept = checked_order(session.get(RETURN_KEY))
    session.flush()
    if kept is not None:
        keep_order(session, kept)


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def login_moment(session: SessionBase, now: float) -> float:
    """When the person logged in: for an administrator the accepted email code,
    kept as the pending time, so the authenticator step does not restart the
    twelve hours; otherwise now."""
    pending = _seconds(session.get("pending_at"))
    if pending is not None and 0 <= now - pending <= 600:
        return pending
    return now


def remaining(session: SessionBase, now: float) -> int:
    """Seconds the session may still live: idle period, cut at the absolute end."""
    started = _seconds(session.get("login_at"))
    left = ABSOLUTE_SECONDS - (now - started) if started is not None else 0
    return max(1, min(IDLE_SECONDS, math.ceil(left)))


def landing(request: HttpRequest) -> str:
    """Where a login goes: back to the return view for the kept order id,
    otherwise home. The address is built here from a checked UUID; no
    address from the browser is used."""
    kept = checked_order(request.session.pop(RETURN_KEY, None))
    if kept is None:
        return reverse("home")
    return f"{reverse('payment-return')}?{urlencode({'order': str(kept)})}"


class EmailCodeBackend(BaseBackend):
    """Only reload a trusted session id; authenticate() never accepts credentials."""

    def get_user(self, user_id: int) -> Member | None:
        # Django stores a globally unique pk in its server-side session.
        # SessionPolicy checks the resolved hostname before any view runs.
        member = (
            Member.across_organizations.select_related("role")
            .filter(pk=user_id)
            .first()
        )
        return member if member is not None and member.is_active else None


def establish_session(
    request: HttpRequest, proof: VerifiedCode, *, admin_confirmed: bool = False
) -> bool:
    """Reload authority before granting a rotated session; no supplied ids suffice."""
    organization = organization_for_request(request)
    if organization.pk != proof.member.organization_id:
        return False

    def grant(org: Organization) -> bool:
        member = (
            Member.objects.of(org)
            .select_related("role")
            .filter(pk=proof.member.pk)
            .first()
        )
        if (
            member is None
            or not member.is_active
            or not constant_time_compare(
                proof.authority, member.get_session_auth_hash()
            )
            or (
                member.is_staff
                and (
                    not admin_confirmed
                    or not Authenticator.objects.of(org)
                    .filter(member=member, confirmed=True)
                    .exists()
                )
            )
        ):
            return False
        if member.is_staff:
            record_event(actor=member, action=AuditAction.ADMIN_LOGIN, target=member)
        now = utc_now(None).timestamp()
        started = login_moment(request.session, now)
        # Flush pending factors and rotate through Django's normal login path.
        flush_keeping_order(request.session)
        login(request, member, backend=BACKEND)
        request.session["organization_id"] = org.pk
        request.session["admin_confirmed"] = bool(member.is_staff and admin_confirmed)
        request.session["active_at"] = now
        request.session["login_at"] = started
        request.session.set_expiry(remaining(request.session, now))
        return True

    return organization_transaction(organization.pk, grant)


class SessionPolicy:
    """Apply host, role and inactivity checks immediately after Django auth loads."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path != "/health/" and "_auth_user_id" in request.session:
            member = request.user
            now = utc_now(None).timestamp()
            active_at = request.session.get("active_at")
            valid_time = (
                isinstance(active_at, (int, float))
                and not isinstance(active_at, bool)
                and math.isfinite(active_at)
                and 0 <= now - active_at < IDLE_SECONDS
            )
            login_at = _seconds(request.session.get("login_at"))
            valid_time = (
                valid_time
                and login_at is not None
                and 0 <= now - login_at < ABSOLUTE_SECONDS
            )
            if (
                not isinstance(member, Member)
                or not member.is_active
                or not valid_time
                or request.session.get("organization_id") != member.organization_id
                or organization_for_request(request).pk != member.organization_id
                or (
                    member.is_staff
                    and request.session.get("admin_confirmed") is not True
                )
            ):
                logout(request)
            else:
                request.session["active_at"] = now
                request.session.set_expiry(remaining(request.session, now))
        return self.get_response(request)
