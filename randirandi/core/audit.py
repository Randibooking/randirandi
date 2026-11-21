# SPDX-License-Identifier: AGPL-3.0-only
"""The one administrator-event writer ."""

import re

from django.core.exceptions import PermissionDenied
from django.db import models, transaction

from randirandi.core.audit_types import OBJECT_KINDS, AuditAction
from randirandi.core.models import AuditEvent, Member, Organization, Owned

# Operator actions on a member, administrator or not: the operator's command
# names the target as the actor (as the first-administrator bootstrap and the
# operator reset do), so the actor is the target and nobody else.
OPERATOR_MEMBER_ACTIONS = frozenset({AuditAction.OPERATOR_ERASE_MEMBER})


def object_kind(target: models.Model) -> str:
    kind = re.sub(r"(?<!^)(?=[A-Z])", "_", type(target).__name__).lower()
    if target._meta.app_label != "core" or kind not in OBJECT_KINDS:
        raise ValueError("Unknown audit target.")
    return kind


def record_event(
    *, actor: Member, action: AuditAction, target: models.Model
) -> AuditEvent:
    """Append fixed vocabulary and IDs, atomically with the caller's action.

    Caller-supplied text and timestamps are deliberately not accepted. The
    caller is responsible for wrapping a mutation and this writer in one
    transaction. The helper locks the organization and reloads authority.
    """
    if not isinstance(action, AuditAction):
        raise ValueError("A fixed audit action is required.")
    kind = object_kind(target)
    if type(target.pk) is not int or target.pk < 1:
        raise ValueError("A saved audit target is required.")
    with transaction.atomic():
        org = Organization.objects.select_for_update(no_key=True).get(
            pk=actor.organization_id
        )
        current = (
            Member.objects.of(org).select_related("role").filter(pk=actor.pk).first()
        )
        if action in OPERATOR_MEMBER_ACTIONS:
            if (
                current is None
                or not isinstance(target, Member)
                or target.pk != actor.pk
            ):
                raise PermissionDenied("The operator's target must be its own actor.")
        elif current is None or not current.is_staff:
            raise PermissionDenied("An active administrator is required.")
        if isinstance(target, Organization):
            exists = target.pk == org.pk
        elif isinstance(target, Owned):
            exists = type(target).objects.of(org).filter(pk=target.pk).exists()
        else:
            exists = False
        if not exists:
            raise PermissionDenied("Audit target is outside this organization.")
        return AuditEvent.objects.create(
            organization=org,
            administrator=current,
            action=action,
            object_kind=kind,
            object_id=target.pk,
        )
