# SPDX-License-Identifier: AGPL-3.0-only
"""The emails about a member's bookings : the confirmation, once an
order is complete, paid or free , with the calendar file
attached ; and the reminder, 48 hours before a booking starts, naming
the member's other coming bookings in the five days after its start; a
booking named in a reminder gets none of its own ; and the
cancellation, once for each cancel an administrator made, naming the times
cancelled and the money returned or that none is .

The job runner sends them, never inside a database transaction: a try is
written first, the message is sent, then it is marked sent. A message whose
send failed is tried again after `RETRY`, the oldest try last ; a confirmation or a
cancellation is no longer tried
after `GIVE_UP` (logged by its order), a reminder once its booking has
started. A message that cannot be built is logged and tried again the same
way, so it holds up no other. A crash between a send and its mark sends it
once more after `RETRY`. The words are the organization's labels; a message names the
member's own bookings and nothing of another member. Only an active member
with an address is sent either.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError
from django.db.models import F, Q, QuerySet
from django.db.models.functions import Coalesce

from randirandi import labels
from randirandi.core import booking_calendar, booking_cart, booking_words
from randirandi.core.booking_money import CURRENCY_WORD as CURRENCY_WORD
from randirandi.core.booking_money import currency_of
from randirandi.core.booking_money import money_words as money_words
from randirandi.core.emails import booking_message
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    Booking,
    BookingState,
    Member,
    MemberStatus,
    Order,
    Organization,
    Refund,
)

logger = logging.getLogger(__name__)

RETRY = timedelta(minutes=5)
GIVE_UP = timedelta(days=1)
# Messages one organization sends in one run of the job.
BATCH = 50
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def date_words(org: Organization | None, moment: datetime) -> str:
    """ "Tuesday 20 October", as the page writes a date (words.js)."""
    return labels.date_words(org, moment)


def hours_words(org: Organization, start: datetime, end: datetime) -> str:
    """ "16:00 to 17:00" with an en dash, with "next day" when it ends on a
    later date (`hoursWords`)."""
    first, last = start.strftime("%H:%M"), end.strftime("%H:%M")
    if end.date() > start.date():
        return labels.word(org, "hours.next-day", **{"from": first, "to": last})
    return f"{first}\N{EN DASH}{last}"


def booking_lines(
    org: Organization, bookings: list[Booking], priced: bool = True
) -> list[dict[str, str]]:
    """Each booking as a message shows it: its date, its hours, what it is,
    what it is booked as, its places when more than one, and its price when
    `priced` (the reminder has none). The bookings' times, assets,
    containers, floor plans and lines must be loaded."""
    made = []
    for booking in bookings:
        # In the zone of the booking's own asset, which its claim keeps.
        zone = ZoneInfo(booking.taken_time.time_zone)
        start = booking.taken_time.starts_at.astimezone(zone)
        end = booking.taken_time.ends_at.astimezone(zone)
        total = sum(line.total for line in booking.lines.all())
        places = booking.taken_time.quantity
        plan = booking.booking_configuration.floor_plan
        word = booking_words.spot_word(None if plan is None else plan.unit)
        made.append(
            {
                "date": date_words(org, start),
                "time": hours_words(org, start, end),
                "what": labels.word(
                    org,
                    "calendar.title",
                    type=booking.booking_type_name,
                    asset=booking.taken_time.asset.name,
                ),
                "as": labels.word(
                    org, "line.booking-as", category=booking.booking_category_name
                ),
                "places": labels.plural(
                    org,
                    "line.places",
                    places,
                    word=word or labels.word(org, "spot.word"),
                )
                if places > 1
                else "",
                "price": ""
                if not priced
                else money_words(total, currency_of(org, booking))
                if total
                else labels.word(org, "price.free"),
            }
        )
    return made


def by_day(lines: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The lines grouped under their date, each date once, in the order the
    lines come (by time): [{"date": ..., "lines": [...]}, ...]."""
    days: list[dict[str, Any]] = []
    for line in lines:
        if not days or days[-1]["date"] != line["date"]:
            days.append({"date": line["date"], "lines": []})
        days[-1]["lines"].append(line)
    return days


def _bookings(found: Order) -> list[Booking]:
    return list(
        found.bookings.filter(taken_time__state=BookingState.CONFIRMED)
        .select_related("taken_time__asset", "booking_configuration__floor_plan")
        .prefetch_related("lines__price_book")
        .order_by("taken_time__starts_at", "pk")
    )


def _conditions(org: Organization, found: Order) -> dict[str, Any]:
    """The line of the confirmation about the booking conditions: a link to the
    text the order accepted, or "not recorded" for an order that accepted none."""
    if found.conditions_id is None:
        return {"note": labels.word(org, "email.conditions-none")}
    return {
        "link": {
            "text": labels.word(org, "email.conditions"),
            "url": (
                f"https://{org.subdomain}.{settings.ORGANIZATION_DOMAIN}"
                f"/book/order/{found.public_id}/conditions/"
            ),
        }
    }


def confirmation_message(
    org: Organization, found: Order, recipient: str, now: datetime | None = None
) -> EmailMultiAlternatives | None:
    """The confirmation of a confirmed order, its calendar file attached;
    None for an order with no confirmed booking."""
    bookings = _bookings(found)
    calendar = booking_calendar.calendar_file(org, found, now)
    if not bookings or calendar is None:
        return None
    # An order is one payment in one currency: that of its first priced booking.
    currency = next(
        (currency_of(org, booking) for booking in bookings if booking.lines.all()),
        org.currency,
    )
    paid = (
        labels.word(org, "email.paid", amount=money_words(found.total, currency))
        if found.total
        else labels.word(org, "email.free")
    )
    shown = booking_lines(org, bookings)
    first = shown[0]
    dates = {line["date"] for line in shown}
    named = {"what": first["what"], "date": first["date"]}
    subject = (
        labels.word(org, "email.confirmed-subject-more", **named)
        if len(dates) > 1
        else labels.word(org, "email.confirmed-subject", **named)
    )
    message = booking_message(
        organization=org,
        subject=subject,
        recipient=recipient,
        context={
            "heading": labels.word(org, "outcome.booked"),
            "organization": org.name,
            "facts": [
                labels.word(org, "outcome.order", reference=found.reference),
                paid,
            ],
            "days": by_day(shown),
            "after": [
                labels.word(org, "email.calendar-attached"),
            ],
            # The conditions this order accepted, linked; an order from before
            # Legacy orders may have no recorded conditions.
            **_conditions(org, found),
        },
    )
    message.attach(
        booking_calendar.file_name(org, found),
        calendar,
        "text/calendar",
    )
    return message


@dataclass(frozen=True)
class Claimed:
    order_id: int
    message: EmailMultiAlternatives = field(repr=False)


def _claim_confirmation(org: Organization, now: datetime) -> Claimed | bool:
    """Take the next confirmation to send: True when there was one that needs
    no message (cancelled since, a member without an address or not active,
    or due longer than `GIVE_UP`: no longer due) or could not be built
    (tried), False when none is left."""
    late = (
        Order.objects.of(org)
        .filter(confirmation_sent_at=None, confirmation_due_at__lte=now - GIVE_UP)
        .select_for_update(skip_locked=True)
        .first()
    )
    if late is not None:
        Order.objects.of(org).filter(pk=late.pk).update(confirmation_due_at=None)
        logger.warning(
            "booking confirmation given up: organization-%s order-%s", org.pk, late.pk
        )
        return True
    found = (
        Order.objects.of(org)
        .filter(confirmation_sent_at=None, confirmation_due_at__gt=now - GIVE_UP)
        .filter(
            Q(confirmation_tried_at=None) | Q(confirmation_tried_at__lte=now - RETRY)
        )
        .order_by(
            F("confirmation_tried_at").asc(nulls_first=True),
            "confirmation_due_at",
            "pk",
        )
        .select_for_update(skip_locked=True)
        .first()
    )
    if found is None:
        return False
    member = Member.objects.of(org).get(pk=found.member_id)
    message: EmailMultiAlternatives | None = None
    if (
        _reachable(member)
        and booking_cart.order_state(found, now) == booking_cart.CONFIRMED
    ):
        try:
            message = confirmation_message(org, found, member.email, now)
        except DatabaseError:
            raise
        except Exception:  # noqa: BLE001 — its text may name the member
            _not_built("booking confirmation", org, f"order-{found.pk}")
            Order.objects.of(org).filter(pk=found.pk).update(confirmation_tried_at=now)
            return True
    if message is None:
        Order.objects.of(org).filter(pk=found.pk).update(confirmation_due_at=None)
        return True
    Order.objects.of(org).filter(pk=found.pk).update(confirmation_tried_at=now)
    return Claimed(found.pk, message)


def _reachable(member: Member) -> bool:
    return bool(member.email) and member.status == MemberStatus.ACTIVE


def _not_built(what: str, org: Organization, ref: str) -> None:
    logger.warning("%s not built: organization-%s %s", what, org.pk, ref)


def send_confirmations(now: datetime | None = None) -> None:
    """Send each confirmation that is due (the job "booking-confirmations")."""
    for org in Organization.objects.order_by("pk"):
        for _ in range(BATCH):
            moment = utc_now(now)
            claimed = organization_transaction(
                org.pk, partial(_claim_confirmation, now=moment)
            )
            if isinstance(claimed, bool):
                if not claimed:
                    break
                continue
            sent = _send(
                claimed.message,
                "booking confirmation",
                org,
                f"order-{claimed.order_id}",
            )
            if sent:
                Order.objects.of(org).filter(pk=claimed.order_id).update(
                    confirmation_sent_at=utc_now(now)
                )


def _send(
    message: EmailMultiAlternatives, what: str, org: Organization, ref: str
) -> bool:
    """Send one message; a failure is logged by its kind and ids only (the
    ids prefixed by their type, so the log's privacy filter keeps them)."""
    try:
        with mail.mailers["default"] as connection:
            return bool(connection.send_messages([message]))
    except Exception:  # noqa: BLE001 — never log recipient-bearing SMTP errors
        logger.warning("%s failed: organization-%s %s", what, org.pk, ref)
        return False


# The reminder : 48 hours before a booking starts, naming
# every other coming booking of the member in the five days after its start.
AHEAD = timedelta(hours=48)
AFTER = timedelta(days=5)


def reminder_message(
    org: Organization, bookings: list[Booking], recipient: str
) -> EmailMultiAlternatives:
    """The reminder of a member's coming bookings, the first one first."""
    # On the clock of the first booking's asset.
    first = bookings[0].taken_time.starts_at.astimezone(
        ZoneInfo(bookings[0].taken_time.time_zone)
    )
    return booking_message(
        organization=org,
        subject=labels.word(org, "email.reminder-subject", date=date_words(org, first)),
        recipient=recipient,
        context={
            "heading": labels.word(org, "email.reminder-heading"),
            "organization": org.name,
            "facts": [],
            "days": by_day(booking_lines(org, bookings, priced=False)),
            "after": [],
        },
    )


@dataclass(frozen=True)
class Reminder:
    booking_ids: tuple[int, ...]
    message: EmailMultiAlternatives = field(repr=False)


def _coming(org: Organization) -> QuerySet[Booking]:
    return Booking.objects.of(org).filter(
        taken_time__state=BookingState.CONFIRMED, reminded_at=None
    )


def _claim_reminder(org: Organization, now: datetime) -> Reminder | bool:
    """Take the next reminder to send: a confirmed booking that starts within
    `AHEAD`, was confirmed at least `AHEAD` before it starts, whose
    confirmation is not still waiting to be sent, and is named in no reminder
    yet, with the member's other confirmed bookings that start less than
    `AFTER` after it, by the clock of the first booking's asset. True when it needs no
    message (a member without an address, or not active: the bookings count
    as named) or could not be built (tried), False when none is due."""
    first = (
        _coming(org)
        .alias(confirmed_at=Coalesce("order__confirmation_due_at", "order__created_at"))
        .filter(
            taken_time__starts_at__gt=now,
            taken_time__starts_at__lte=now + AHEAD,
            confirmed_at__lte=F("taken_time__starts_at") - AHEAD,
        )
        .filter(
            Q(order__confirmation_sent_at__isnull=False)
            | Q(order__confirmation_due_at=None)
        )
        .filter(Q(reminder_tried_at=None) | Q(reminder_tried_at__lte=now - RETRY))
        .order_by(
            F("reminder_tried_at").asc(nulls_first=True),
            "taken_time__starts_at",
            "pk",
        )
        .select_related("taken_time")
        .select_for_update(skip_locked=True, of=("self",))
        .first()
    )
    if first is None:
        return False
    start = first.taken_time.starts_at
    # Five days on the clock of the first booking's asset, the same hour
    # across a change.
    until = start.astimezone(ZoneInfo(first.taken_time.time_zone)) + AFTER
    named = list(
        _coming(org)
        .filter(
            member_id=first.member_id,
            taken_time__starts_at__gte=start,
            taken_time__starts_at__lt=until,
        )
        .select_related("taken_time__asset", "booking_configuration__floor_plan")
        .prefetch_related("lines__price_book")
        .order_by("taken_time__starts_at", "pk")
    )
    ids = tuple(booking.pk for booking in named)
    member = Member.objects.of(org).get(pk=first.member_id)
    if not _reachable(member):
        Booking.objects.of(org).filter(pk__in=ids).update(reminded_at=now)
        return True
    Booking.objects.of(org).filter(pk__in=ids).update(reminder_tried_at=now)
    try:
        return Reminder(ids, reminder_message(org, named, member.email))
    except DatabaseError:
        raise
    except Exception:  # noqa: BLE001 — its text may name the member
        _not_built("booking reminder", org, f"booking-{first.pk}")
        return True


def send_reminders(now: datetime | None = None) -> None:
    """Send each reminder that is due (the job "booking-reminders")."""
    for org in Organization.objects.order_by("pk"):
        for _ in range(BATCH):
            moment = utc_now(now)
            claimed = organization_transaction(
                org.pk, partial(_claim_reminder, now=moment)
            )
            if isinstance(claimed, bool):
                if not claimed:
                    break
                continue
            first = f"booking-{claimed.booking_ids[0]}"
            if _send(claimed.message, "booking reminder", org, first):
                Booking.objects.of(org).filter(pk__in=claimed.booking_ids).update(
                    reminded_at=utc_now(now)
                )


# The cancellation : one email for each cancel an
# administrator made, the bookings of one order that share its key.


def _first_start(bookings: list[Booking]) -> datetime:
    """The first booking's start on its asset's clock."""
    first = bookings[0].taken_time
    return first.starts_at.astimezone(ZoneInfo(first.time_zone))


def cancellation_message(
    org: Organization,
    found: Order,
    cancelled: list[Booking],
    refunded: int,
    recipient: str,
) -> EmailMultiAlternatives:
    """The cancellation of the `cancelled` bookings, with the amount queued
    for refund (0 for none), each time on its asset's clock."""
    currency = next(
        (currency_of(org, booking) for booking in cancelled if booking.lines.all()),
        org.currency,
    )
    return booking_message(
        organization=org,
        subject=labels.word(
            org,
            "email.cancelled-subject",
            date=date_words(org, _first_start(cancelled)),
        ),
        recipient=recipient,
        context={
            "heading": labels.word(org, "email.cancelled-heading"),
            "organization": org.name,
            "facts": [
                labels.word(org, "email.cancelled-note", reference=found.reference)
            ],
            "days": by_day(booking_lines(org, cancelled, priced=False)),
            "after": [
                labels.word(
                    org,
                    "email.cancelled-refund",
                    amount=money_words(refunded, currency),
                )
                if refunded
                else labels.word(org, "email.cancelled-no-refund"),
            ],
        },
    )


@dataclass(frozen=True)
class Cancellation:
    order_id: int
    key: uuid.UUID
    message: EmailMultiAlternatives = field(repr=False)


def _cancelled_with(
    org: Organization, order_id: int, key: uuid.UUID
) -> QuerySet[Booking]:
    return Booking.objects.of(org).filter(order_id=order_id, cancellation_key=key)


def _claim_cancellation(org: Organization, now: datetime) -> Cancellation | bool:
    """Take the next cancellation email to send: True when there was one that
    needs no message (a member without an address or not active, or due
    longer than `GIVE_UP`) or could not be built (tried), False when none is
    left. A cancel's bookings are tried and sent together."""
    due = Booking.objects.of(org).filter(
        cancel_mailed_at=None, cancel_mail_due_at__isnull=False
    )
    late = (
        due.filter(cancel_mail_due_at__lte=now - GIVE_UP)
        .select_for_update(skip_locked=True, of=("self",))
        .first()
    )
    if late is not None and late.cancellation_key is not None:
        _cancelled_with(org, late.order_id, late.cancellation_key).update(
            cancel_mail_due_at=None, cancel_mail_tried_at=None
        )
        logger.warning(
            "booking cancellation given up: organization-%s order-%s",
            org.pk,
            late.order_id,
        )
        return True
    first = (
        due.filter(
            Q(cancel_mail_tried_at=None) | Q(cancel_mail_tried_at__lte=now - RETRY)
        )
        .order_by(
            F("cancel_mail_tried_at").asc(nulls_first=True), "cancel_mail_due_at", "pk"
        )
        .select_for_update(skip_locked=True, of=("self",))
        .first()
    )
    if first is None or first.cancellation_key is None:
        return False
    key = first.cancellation_key
    same = _cancelled_with(org, first.order_id, key)
    found = Order.objects.of(org).get(pk=first.order_id)
    member = Member.objects.of(org).get(pk=found.member_id)
    if not _reachable(member):
        same.update(cancel_mail_due_at=None, cancel_mail_tried_at=None)
        return True
    same.update(cancel_mail_tried_at=now)
    try:
        cancelled = list(
            same.select_related(
                "taken_time__asset", "booking_configuration__floor_plan"
            )
            .prefetch_related("lines__price_book")
            .order_by("taken_time__starts_at", "pk")
        )
        refund = (
            Refund.objects.of(org)
            .filter(payment__order=found, refund_key=key)
            .values_list("amount", flat=True)
            .first()
        )
        message = cancellation_message(org, found, cancelled, refund or 0, member.email)
    except DatabaseError:
        raise
    except Exception:  # noqa: BLE001 — its text may name the member
        _not_built("booking cancellation", org, f"order-{found.pk}")
        return True
    return Cancellation(found.pk, key, message)


def send_cancellations(now: datetime | None = None) -> None:
    """Send each cancellation email that is due (the job
    "booking-cancellations")."""
    for org in Organization.objects.order_by("pk"):
        for _ in range(BATCH):
            moment = utc_now(now)
            claimed = organization_transaction(
                org.pk, partial(_claim_cancellation, now=moment)
            )
            if isinstance(claimed, bool):
                if not claimed:
                    break
                continue
            if _send(
                claimed.message,
                "booking cancellation",
                org,
                f"order-{claimed.order_id}",
            ):
                _cancelled_with(org, claimed.order_id, claimed.key).update(
                    cancel_mailed_at=utc_now(now)
                )
