# SPDX-License-Identifier: AGPL-3.0-only
"""Organization-scoped enrollment, replay protection and administrator resets."""

import ipaddress
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError
from django.utils.crypto import constant_time_compare

from randirandi.core import authenticator_secret
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.login_codes import VerifiedCode, record_refusal
from randirandi.core.member_data import (
    notify_administrators,
    organization_transaction,
    pending_requests,
    utc_now,
)
from randirandi.core.models import (
    Authenticator,
    LoginAttempt,
    LoginCode,
    Member,
    Organization,
)
from randirandi.core.otp import accepted_counter


def checked_counter(device: Authenticator, code: str, now: float) -> int | None:
    """The accepted time step of a code, or None. A changed row, a wrong key and
    a key that is not set refuse the code like any other wrong one."""
    try:
        return accepted_counter(
            device.plain_secret(), code, now=now, last=device.last_counter
        )
    except (
        authenticator_secret.SecretUnreadableError,
        authenticator_secret.KeyMissingError,
    ):
        return None


def current_administrator(org: Organization, proof: VerifiedCode) -> Member | None:
    member = (
        Member.objects.of(org).select_related("role").filter(pk=proof.member.pk).first()
    )
    if (
        member is None
        or not member.is_staff
        or not constant_time_compare(proof.authority, member.get_session_auth_hash())
    ):
        return None
    return member


def prepare_authenticator(
    proof: VerifiedCode, *, now: datetime | None = None
) -> Authenticator | None:
    moment = utc_now(now)

    def prepare(org: Organization) -> Authenticator | None:
        member = current_administrator(org, proof)
        if member is None:
            return None
        device = Authenticator.objects.of(org).filter(member=member).first()
        if (
            device is not None
            and not device.confirmed
            and device.created_at <= moment - timedelta(minutes=10)
        ):
            device.delete()
            device = None
        if device is None:
            device = Authenticator.objects.create(
                organization=org,
                member=member,
                secret=authenticator_secret.seal(
                    secrets.token_hex(20),
                    key=settings.AUTHENTICATOR_KEY,
                    organization_id=org.pk,
                    member_id=member.pk,
                ),
                created_at=moment,
            )
        return device

    for attempt in range(5):
        try:
            return organization_transaction(proof.member.organization_id, prepare)
        except IntegrityError as error:
            # The serializable snapshot may predate the organization lock wait.
            # Retry only this known competing enrollment, in a fresh transaction.
            if (
                getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
                != "authenticator_member_once"
            ):
                raise
            time.sleep(0.01 * (attempt + 1))
    return None


def verify_authenticator(
    proof: VerifiedCode, code: str, *, network: str, now: datetime | None = None
) -> VerifiedCode | None:
    moment = utc_now(now)
    address = str(ipaddress.ip_address(network))

    def verify(org: Organization) -> VerifiedCode | None:
        member = current_administrator(org, proof)
        if member is None:
            LoginAttempt.objects.create(
                organization=org,
                member=Member.objects.of(org).filter(pk=proof.member.pk).first(),
                at=moment,
                network=address,
                kind="mfa",
                outcome="refused",
            )
            return None
        recent = LoginAttempt.objects.of(org).filter(
            kind="mfa", at__gt=moment - timedelta(minutes=15), outcome="refused"
        )
        limited = (
            recent.filter(member=member).count() >= 5
            or recent.filter(network=address).count() >= 20
        )
        device = Authenticator.objects.of(org).filter(member=member).first()
        counter = None
        if (
            not limited
            and device is not None
            and (device.confirmed or device.created_at > moment - timedelta(minutes=10))
        ):
            counter = checked_counter(device, code, moment.timestamp())
        LoginAttempt.objects.create(
            organization=org,
            member=member,
            at=moment,
            network=address,
            kind="mfa",
            outcome="limited"
            if limited
            else "accepted"
            if counter is not None
            else "refused",
        )
        if counter is None or device is None:
            return None
        enrolling = not device.confirmed
        device.confirmed, device.last_counter = True, counter
        device.save(update_fields=["confirmed", "last_counter"])
        if enrolling:
            record_event(
                actor=member, action=AuditAction.ENROLL_AUTHENTICATOR, target=member
            )
        Member.objects.of(org).filter(pk=member.pk).update(last_login_at=moment)
        return VerifiedCode(member, member.get_session_auth_hash())

    try:
        return organization_transaction(proof.member.organization_id, verify)
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) not in {"40001", "40P01"}:
            raise
        # The factor transaction rolled back. Record its capacity refusal in
        # a fresh transaction without evaluating or accepting the code again.
        record_refusal(
            organization=proof.member.organization,
            kind="mfa",
            network=address,
            member_id=proof.member.pk,
            limited=True,
        )
        return None


CODE_REFUSED = "The code from your authenticator app was not accepted."
CODE_LIMITED = "Too many codes were not accepted. Wait 15 minutes and try again."


class CodeRefusedError(PermissionDenied):
    """The authenticator code was not accepted; `limited` when too many were
    refused, so this one was not even judged. The admin pages answer 409 with
    "code-limited" or "code-refused"."""

    def __init__(self, limited: bool) -> None:
        super().__init__(CODE_LIMITED if limited else CODE_REFUSED)
        self.limited = limited


class _Refused:
    """A refused code leaves its attempt record; the caller raises afterwards."""

    def __init__(self, limited: bool) -> None:
        self.limited = limited


def with_fresh_code[T](
    actor: Member,
    code: str,
    action: Callable[[Organization], T],
    *,
    network: str,
    now: datetime | None = None,
) -> T:
    """Run `action` only when this administrator's current code is accepted.

    The code is judged in the action's own organization transaction, before
    it, with the login's device, replay rule, limits and attempt record. The
    accepted code's counter is used up with the action, so a code the login
    used, or one used here, is refused. A refusal commits only its attempt.
    """
    moment = utc_now(now)
    address = str(ipaddress.ip_address(network))

    def apply(org: Organization) -> T | _Refused:
        member = (
            Member.objects.of(org).select_related("role").filter(pk=actor.pk).first()
        )
        if member is None or not member.is_staff:
            raise PermissionDenied(
                "An active administrator of this organization is required."
            )
        recent = LoginAttempt.objects.of(org).filter(
            kind="mfa", at__gt=moment - timedelta(minutes=15), outcome="refused"
        )
        limited = (
            recent.filter(member=member).count() >= 5
            or recent.filter(network=address).count() >= 20
        )
        device = Authenticator.objects.of(org).filter(member=member).first()
        counter = None
        if not limited and device is not None and device.confirmed:
            counter = checked_counter(device, code, moment.timestamp())
        LoginAttempt.objects.create(
            organization=org,
            member=member,
            at=moment,
            network=address,
            kind="mfa",
            outcome="limited"
            if limited
            else "accepted"
            if counter is not None
            else "refused",
        )
        if counter is None or device is None:
            return _Refused(limited)
        device.last_counter = counter
        device.save(update_fields=["last_counter"])
        return action(org)

    try:
        result = organization_transaction(actor.organization_id, apply)
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) not in {"40001", "40P01"}:
            raise
        # Rolled back, so neither the action nor the counter happened. Record
        # the capacity refusal in a fresh transaction without judging the code.
        record_refusal(
            organization=actor.organization,
            kind="mfa",
            network=address,
            member_id=actor.pk,
            limited=True,
        )
        raise CodeRefusedError(True) from None
    if isinstance(result, _Refused):
        raise CodeRefusedError(result.limited)
    return result


def _reset(org: Organization, member_id: int, actor: Member | None) -> Member:
    """The reset itself, in the caller's organization transaction."""
    target = Member.objects.of(org).select_related("role").get(pk=member_id)
    if not target.is_staff:
        raise PermissionDenied("An active administrator is required.")
    if actor is not None:
        current = (
            Member.objects.of(org).select_related("role").filter(pk=actor.pk).first()
        )
        if (
            current is None
            or not current.is_staff
            or current.pk == target.pk
            or not Authenticator.objects.of(org)
            .filter(member=current, confirmed=True)
            .exists()
        ):
            raise PermissionDenied(
                "Another enrolled administrator of this organization is required."
            )
        action = AuditAction.RESET_AUTHENTICATOR
    else:
        # Same operator-event convention as first-administrator bootstrap;
        # the action explicitly distinguishes operator from member activity.
        current, action = target, AuditAction.OPERATOR_RESET_AUTHENTICATOR
    Authenticator.objects.of(org).filter(member=target).delete()
    LoginCode.objects.of(org).filter(member=target).delete()
    pending_requests(target).delete()
    target.authenticator_generation += 1
    target.save(update_fields=["authenticator_generation"])
    notify_administrators(
        org, record_event(actor=current, action=action, target=target)
    )
    return target


def reset_authenticator(
    *,
    organization: Organization,
    member_id: int,
    actor: Member | None = None,
    no_other_administrator_available: bool = False,
) -> None:
    if type(member_id) is not int or member_id < 1:
        raise ValueError("A positive member identifier is required.")
    if actor is None and no_other_administrator_available is not True:
        raise PermissionDenied(
            "The operator must confirm no other administrator is available."
        )

    def reset(org: Organization) -> None:
        _reset(org, member_id, actor)

    organization_transaction(organization.pk, reset)


def reset_member_authenticator(
    *,
    actor: Member,
    member_id: int,
    code: str,
    network: str,
    now: datetime | None = None,
) -> Member:
    """An administrator resets another administrator's authenticator from the
    member's page , on their own current code. The reset member
    signs in next with the email code and sets up the authenticator again;
    every administrator is emailed. A refusal is a ValidationError whose code
    is "self" or "not-administrator", or a CodeRefusedError."""
    if type(member_id) is not int or member_id < 1:
        raise ValueError("A positive member identifier is required.")

    def apply(org: Organization) -> Member:
        target = Member.objects.of(org).select_related("role").get(pk=member_id)
        if target.pk == actor.pk:
            raise ValidationError(
                "An administrator cannot reset their own authenticator.", "self"
            )
        if not target.is_staff:
            raise ValidationError(
                "The member is not an administrator.", "not-administrator"
            )
        return _reset(org, member_id, actor)

    return with_fresh_code(actor, code, apply, network=network, now=now)
