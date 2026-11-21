# SPDX-License-Identifier: AGPL-3.0-only
"""Operator bootstrap, separate from data supplied by a membership list."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import (
    _id,
    _subject,
    gained_admin_access,
    organization_transaction,
    pending_requests,
)
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    LoginCode,
    Member,
    MemberStatus,
    Organization,
    Origin,
    Role,
)


def first_administrator(
    organization: Organization, member_id: int, role_name: str
) -> Member:
    """Promote an existing active member once. Later role edits use Admin.

    Bootstrap does not activate a disabled or blocked member. Marking the member as
    administrator-managed keeps a later list import from undoing bootstrap.
    The event's actor is the bootstrapped member; action identifies operator
    bootstrap, not an authenticated administrator action.
    """
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        member = Member.objects.of(organization).select_for_update().get(pk=member_id)
        role = (
            Role.objects.of(organization)
            .select_for_update()
            .get(name=role_name, admin_access=True)
        )
        if member.status != MemberStatus.ACTIVE:
            raise ValidationError("The first administrator must already be active.")
        if (
            Member.objects.of(organization)
            .filter(role__admin_access=True, status=MemberStatus.ACTIVE)
            .exclude(pk=member.pk)
            .exists()
        ):
            raise ValidationError(
                "An administrator already exists; use the administrator workflow."
            )
        if member.role_id == role.pk and member.origin == Origin.ADMINISTRATOR:
            return member
        member.role = role
        member.origin = Origin.ADMINISTRATOR
        member.save(update_fields=["role", "origin"])
        record_event(actor=member, action=AuditAction.BOOTSTRAP, target=member)
        # The operator named the first administrator: the member is the actor.
        gained_admin_access(organization, member, member)
        return member


def _change_status(
    org: Organization, administrator: Member, member: Member, status: MemberStatus
) -> None:
    member.status = status
    member.save(update_fields=["status"])
    AccessEvent.objects.create(
        organization=org,
        member=member,
        change=status,
        cause=AccessCause.ADMINISTRATOR,
        administrator=administrator,
    )


def block_member(*, actor: Member, member_id: int) -> Member:
    """Block a member: no login and no booking until an administrator unblocks.

    A member import never changes a blocked member's status. Unused login codes
    and waiting login requests end here; the session ends with the status.
    Bookings are left as they are. A refusal is a ValidationError whose code
    is "self" or "already-blocked" .
    """
    _id(member_id)

    def apply(org: Organization) -> Member:
        administrator, member = _subject(org, actor.pk, member_id)
        if member.pk == administrator.pk:
            raise ValidationError(
                "An administrator cannot block themselves.", code="self"
            )
        if member.status == MemberStatus.BLOCKED:
            raise ValidationError(
                "The member is already blocked.", code="already-blocked"
            )
        record_event(
            actor=administrator, action=AuditAction.BLOCK_MEMBER, target=member
        )
        _change_status(org, administrator, member, MemberStatus.BLOCKED)
        LoginCode.objects.of(org).filter(member=member, used_at__isnull=True).update(
            used_at=timezone.now()
        )
        pending_requests(member).filter(claimed_at__isnull=True).delete()
        return member

    return organization_transaction(actor.organization_id, apply)


def unblock_member(*, actor: Member, member_id: int) -> Member:
    """Back to what the member list says: active when the member is listed (and
    has an email), otherwise disabled. A refusal is a ValidationError whose
    code is "not-blocked".
    """
    _id(member_id)

    def apply(org: Organization) -> Member:
        administrator, member = _subject(org, actor.pk, member_id)
        if member.status != MemberStatus.BLOCKED:
            raise ValidationError("The member is not blocked.", code="not-blocked")
        record_event(
            actor=administrator, action=AuditAction.UNBLOCK_MEMBER, target=member
        )
        # An active member has an email; a listed member without one (added
        # by an administrator) comes back disabled.
        status = (
            MemberStatus.ACTIVE
            if member.listed and member.email
            else MemberStatus.DISABLED
        )
        _change_status(org, administrator, member, status)
        # A member whose role has admin access gains it by becoming active.
        if status == MemberStatus.ACTIVE and member.role.admin_access:
            gained_admin_access(org, administrator, member)
        return member

    return organization_transaction(actor.organization_id, apply)
