# SPDX-License-Identifier: AGPL-3.0-only
"""Reset alerts: durable recipient IDs, bounded retry, no email under a DB lock."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q

from randirandi.core.audit_types import AuditAction
from randirandi.core.emails import (
    admin_access_message,
    email_changed_message,
    reset_message,
)
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import Member, Organization, SecurityNotice

logger = logging.getLogger(__name__)


# Each notice action and the message it sends; no other action is sent.
MESSAGES: dict[str, Callable[..., EmailMultiAlternatives]] = {
    AuditAction.GAVE_ADMIN_ACCESS: admin_access_message,
    AuditAction.RESET_AUTHENTICATOR: reset_message,
    AuditAction.OPERATOR_RESET_AUTHENTICATOR: reset_message,
    AuditAction.CHANGE_MEMBER_EMAIL: email_changed_message,
}


@dataclass(frozen=True)
class Delivery:
    notice_id: int
    event_id: int
    address: str = field(repr=False)
    # A notice that names its own address: the old email of a member whose
    # email an administrator changed .
    old_address: bool = False
    # The audit action of the notice's event; `MESSAGES` names the message it
    # sends. An action with no entry is logged and the notice not delivered.
    action: str = ""


def send_security_notices() -> None:
    for organization in Organization.objects.order_by("pk"):
        for _ in range(50):
            now = utc_now(None)

            def claim(org: Organization, now: datetime = now) -> Delivery | None:
                notice = (
                    SecurityNotice.objects.of(org)
                    .select_related("event")
                    .filter(delivered_at=None)
                    .filter(
                        Q(claimed_at=None)
                        | Q(claimed_at__lte=now - timedelta(minutes=5))
                    )
                    .order_by("pk")
                    .first()
                )
                if notice is None:
                    return None
                recipient = (
                    Member.objects.of(org)
                    .select_related("role")
                    .get(pk=notice.recipient_id)
                )
                if notice.address:
                    notice.claimed_at = now
                    notice.save(update_fields=["claimed_at"])
                    return Delivery(
                        notice.pk,
                        notice.event_id,
                        notice.address,
                        True,
                        notice.event.action,
                    )
                if not recipient.is_staff:
                    notice.delivered_at = now
                    notice.save(update_fields=["delivered_at"])
                    return Delivery(notice.pk, notice.event_id, "")
                notice.claimed_at = now
                notice.save(update_fields=["claimed_at"])
                return Delivery(
                    notice.pk,
                    notice.event_id,
                    recipient.email,
                    False,
                    notice.event.action,
                )

            delivery = organization_transaction(organization.pk, claim)
            if delivery is None:
                break
            if not delivery.address:
                continue
            build = MESSAGES.get(delivery.action)
            if build is None:
                # Never a reset email by default: the notice stays undelivered.
                logger.error(
                    "security notice has no message for its action: "
                    "organization %s notice %s",
                    organization.pk,
                    delivery.notice_id,
                )
                continue
            message = build(
                organization=organization,
                recipient=delivery.address,
                event_id=delivery.event_id,
            )
            try:
                with mail.mailers["default"] as connection:
                    sent = connection.send_messages([message])
            except Exception:  # noqa: BLE001 — never log recipient-bearing SMTP errors
                logger.warning(
                    "security notice delivery failed: organization %s notice %s",
                    organization.pk,
                    delivery.notice_id,
                )
                continue
            if sent:
                # The old address is kept only until it has been told.
                SecurityNotice.objects.of(organization).filter(
                    pk=delivery.notice_id
                ).update(delivered_at=utc_now(None), address="")
