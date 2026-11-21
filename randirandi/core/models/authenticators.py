# SPDX-License-Identifier: AGPL-3.0-only
"""Administrator second factors and durable reset notification recipients."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from randirandi.core import authenticator_secret
from randirandi.core.models.base import Owned, organization_rules


class Authenticator(Owned):
    member = models.ForeignKey("core.Member", on_delete=models.CASCADE)
    # The TOTP shared key, encrypted at rest ("v1:" and AES-256-GCM, see
    # authenticator_secret.py). Never included in logs, inspection or export.
    secret = models.CharField(max_length=80)
    confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    last_counter = models.BigIntegerField(default=-1)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("member"),
            models.UniqueConstraint(
                fields=["member"], name="authenticator_member_once"
            ),
            models.CheckConstraint(
                condition=models.Q(last_counter__gte=-1),
                name="authenticator_counter_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(secret__regex=authenticator_secret.STORED_PATTERN),
                name="authenticator_secret_valid",
            ),
        ]

    def plain_secret(self) -> str:
        """The shared key in hex. Call it only to check a code or draw the QR."""
        return authenticator_secret.unseal(
            self.secret,
            key=settings.AUTHENTICATOR_KEY,
            organization_id=self.organization_id,
            member_id=self.member_id,
        )

    def __str__(self) -> str:
        return f"authenticator {self.pk}"


class SecurityNotice(Owned):
    recipient = models.ForeignKey("core.Member", on_delete=models.CASCADE)
    event = models.ForeignKey("core.AuditEvent", on_delete=models.DO_NOTHING)
    created_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # Where the notice goes when it is not the recipient's own address: the
    # address a member had before an administrator changed it .
    # Empty for every other notice. Emptied when the notice is delivered.
    address = models.EmailField(blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("recipient", "event"),
            models.UniqueConstraint(
                fields=["event", "recipient"], name="securitynotice_recipient_once"
            ),
        ]

    def __str__(self) -> str:
        return f"security notice {self.pk}"
