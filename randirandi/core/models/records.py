# SPDX-License-Identifier: AGPL-3.0-only
"""Record keeping: what administrators did."""

from django.db import models
from django.utils import timezone

from randirandi.core.audit_types import OBJECT_KINDS, AuditAction
from randirandi.core.models.base import Owned, organization_rules
from randirandi.core.models.members import Member


class AuditEvent(Owned):
    """The record of one administrator action. It holds ids only, never a
    name or a contact detail.
    """

    administrator = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name="+"
    )
    # For example "cancelled booking".
    action = models.CharField(max_length=100, choices=AuditAction)
    # What was acted on: the kind of object and its id.
    object_kind = models.CharField(max_length=100)
    object_id = models.PositiveBigIntegerField()
    at = models.DateTimeField(default=timezone.now)

    class Meta(Owned.Meta):
        # The audit log reads one organization's events by time .
        indexes = [models.Index(fields=["organization", "at"], name="audit_org_at")]
        constraints = [
            *organization_rules("administrator"),
            models.CheckConstraint(
                condition=models.Q(action__in=AuditAction.values),
                name="audit_fixed_action",
            ),
            models.CheckConstraint(
                condition=models.Q(object_kind__in=OBJECT_KINDS),
                name="audit_fixed_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(object_id__gte=1), name="audit_positive_target"
            ),
        ]

    def __str__(self) -> str:
        return f"audit event {self.pk}"
