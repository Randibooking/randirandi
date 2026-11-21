# SPDX-License-Identifier: AGPL-3.0-only
"""The email about a day a repeating event skipped : to the
event's maker, or to every active administrator when the maker no longer is
one. The notices are made with the skipped day (`event_admin`); the job sends
them, never inside a database transaction, and tries a failed one again after
`RETRY` (the oldest try last), as the booking emails do. A notice is no
longer tried once its skipped day is older than `GIVE_UP`."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError
from django.db.models import F, Q

from randirandi import labels
from randirandi.core.booking_emails import (
    BATCH,
    GIVE_UP,
    RETRY,
    _reachable,
    _send,
    date_words,
)
from randirandi.core.emails import booking_message
from randirandi.core.event_admin import notice_recipients
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    Event,
    EventNotice,
    EventSkippedDay,
    Member,
    Organization,
    SkipReason,
)

logger = logging.getLogger(__name__)


def reason_words(org: Organization, skipped: EventSkippedDay, title: str | None) -> str:
    """Why the day was skipped, in the organization's words. The key of each
    is written out so that the label set can be checked against the code."""
    match skipped.reason:
        case SkipReason.CLOSED:
            return labels.word(org, "email.event-skipped-closed")
        case SkipReason.BOOKED:
            return labels.word(
                org, "email.event-skipped-booked", number=skipped.in_the_way or ""
            )
        case SkipReason.BLOCKED:
            return labels.word(org, "email.event-skipped-blocked")
        case _:
            if title is None:
                return labels.word(org, "email.event-skipped-event")
            return labels.word(org, "email.event-skipped-event-named", title=title)


def skipped_message(
    org: Organization,
    event: Event,
    skipped: EventSkippedDay,
    other_title: str | None,
    recipient: str,
) -> EmailMultiAlternatives:
    """The email: the event, the day and why."""
    named = {"event": event.title, "date": date_words(org, _local(skipped, event))}
    return booking_message(
        organization=org,
        subject=labels.word(org, "email.event-skipped-subject", **named),
        recipient=recipient,
        context={
            "heading": labels.word(org, "email.event-skipped-heading"),
            "organization": org.name,
            "facts": [
                labels.word(org, "email.event-skipped-fact", **named),
                reason_words(org, skipped, other_title),
            ],
            "days": [],
            "after": [],
        },
    )


def _local(skipped: EventSkippedDay, event: Event) -> datetime:
    """The day as a date-time, for the words of its weekday and month."""
    return datetime.combine(skipped.day, datetime.min.time())


@dataclass(frozen=True)
class Claimed:
    notice_id: int
    message: EmailMultiAlternatives = field(repr=False)


def _claim(org: Organization, now: datetime) -> Claimed | bool:
    """Take the next notice to send: True when there was one that needs no
    message (the recipient is no longer reachable) or could not be built,
    False when none is left."""
    notice = (
        EventNotice.objects.of(org)
        .filter(delivered_at=None, skipped_day__created_at__gt=now - GIVE_UP)
        .filter(Q(claimed_at=None) | Q(claimed_at__lte=now - RETRY))
        .select_related("skipped_day", "skipped_day__event")
        .order_by(F("claimed_at").asc(nulls_first=True), "pk")
        .select_for_update(skip_locked=True, of=("self",))
        .first()
    )
    if notice is None:
        return False
    recipient = (
        Member.objects.of(org).select_related("role").get(pk=notice.recipient_id)
    )
    if not _reachable(recipient) or not recipient.is_staff:
        # The recipient was decided at the skip; decide again now, so that a
        # maker who stopped being an administrator does not leave nobody told.
        skipped_day = notice.skipped_day
        for current in notice_recipients(org, skipped_day.event):
            if _reachable(current):
                EventNotice.objects.of(org).get_or_create(
                    organization=org, skipped_day=skipped_day, recipient=current
                )
        notice.delivered_at = now
        notice.save(update_fields=["delivered_at"])
        return True
    notice.claimed_at = now
    notice.save(update_fields=["claimed_at"])
    skipped = notice.skipped_day
    try:
        other = None
        if skipped.reason == SkipReason.EVENT and skipped.in_the_way:
            other = (
                Event.objects.of(org)
                .filter(pk=skipped.in_the_way)
                .values_list("title", flat=True)
                .first()
            )
        message = skipped_message(org, skipped.event, skipped, other, recipient.email)
    except DatabaseError:
        raise
    except Exception:  # noqa: BLE001 — its text may name the event
        logger.warning(
            "event notice not built: organization-%s notice-%s", org.pk, notice.pk
        )
        return True
    return Claimed(notice.pk, message)


def send_event_notices(now: datetime | None = None) -> None:
    """Send each skipped-day email that is due (the job "event-notices")."""
    for org in Organization.objects.order_by("pk"):
        for _ in range(BATCH):
            moment = utc_now(now)
            claimed = organization_transaction(org.pk, partial(_claim, now=moment))
            if isinstance(claimed, bool):
                if not claimed:
                    break
                continue
            if _send(
                claimed.message, "event notice", org, f"notice-{claimed.notice_id}"
            ):
                EventNotice.objects.of(org).filter(pk=claimed.notice_id).update(
                    delivered_at=utc_now(now)
                )
