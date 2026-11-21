# SPDX-License-Identifier: AGPL-3.0-only
"""The confirmation and the reminder emails ."""

import email.policy
import html as markup
from datetime import datetime, timedelta
from email import message_from_bytes
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import booking_emails
from randirandi.core.booking_emails import (
    AHEAD,
    GIVE_UP,
    RETRY,
    date_words,
    money_words,
    send_confirmations,
    send_reminders,
)
from randirandi.core.models import (
    Booking,
    BookingState,
    Member,
    Order,
    Organization,
    PriceTier,
)
from randirandi.core.ordering import create_order
from tests import made_up
from tests.mail_checks import check_system_mail
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_emails import Resources
from tests.test_member_bookings import day, order_of, other, settle
from tests.test_ordering import NOW, request

pytestmark = pytest.mark.django_db(transaction=True)

SEND = "django.core.mail.backends.locmem.EmailBackend.send_messages"


def free(made: Example) -> None:
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(amount=0)


def parts(message: EmailMessage) -> dict[str, Any]:
    parsed = message_from_bytes(
        message.message().as_bytes(policy=email.policy.SMTP),
        policy=email.policy.default,
    )
    found: dict[str, Any] = {}
    for part in parsed.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        found[part.get_content_type()] = (part, payload)
    return found


def subject_of(order: Order) -> str:
    """The confirmation's subject: what and the first date."""
    start = start_of(order).astimezone(ZoneInfo("Europe/Oslo"))
    return f"You're booked: Visit, Pool, {date_words(None, start)}"


def text_of(message: EmailMessage, kind: str) -> str:
    return str(parts(message)[kind][1].decode())


def start_of(order: Order) -> datetime:
    booking = order.bookings.select_related("taken_time").get()
    return booking.taken_time.starts_at


# --- The confirmation ---------------------------------------------------------


def test_a_free_order_is_confirmed_by_one_email_with_its_calendar_file(
    made: Example,
) -> None:
    free(made)
    order = order_of(made, 1)
    assert order.total == 0 and order.confirmation_due_at == NOW
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    assert message.to == [made.member.email] and not message.cc and not message.bcc
    assert message.subject == subject_of(order)
    check_system_mail(message, made.org, reply=True)
    found = parts(message)
    assert list(found) == ["text/plain", "text/html", "image/png", "text/calendar"]
    calendar_part, calendar = found["text/calendar"]
    assert calendar_part.get_filename() == "randi-pool-2027-03-02.ics"
    assert b"DTSTART:20270302T090000Z" in calendar
    plain, html = text_of(message, "text/plain"), text_of(message, "text/html")
    for text in (plain, markup.unescape(html)):
        assert "You're booked!" in text
        assert f"Reference {order.reference}" in text and "Free of charge" in text
        assert "The harbourview housing co-op" in text
        assert "Tuesday 2 March" in text and "10:00\N{EN DASH}12:00" in text
        assert "Visit, Pool" in text and "Booking as Private" in text
        assert "Free" in text
        assert "I've attached the times for your calendar." in text
        assert f"Questions? Write to {made.org.contact_email}." in text
        assert made.member.email not in text
        assert "\N{EN DASH} Randi" in text
    # The sign-off, then the footer; the address is said once in each text.
    footer = f"Questions? Write to {made.org.contact_email}."
    assert plain.rstrip().endswith(footer)
    assert plain.index("\N{EN DASH} Randi") < plain.index(footer)
    assert plain.count(footer) == 1 and markup.unescape(html).count(footer) == 1
    resources = Resources()
    resources.feed(html)
    assert resources.forbidden == []
    # The logo, and the link to the text its order accepted .
    assert resources.sources == [
        f"cid:logo@{settings.ORGANIZATION_DOMAIN}",
        f"https://harbourview.{settings.ORGANIZATION_DOMAIN}"
        f"/book/order/{order.public_id}/conditions/",
    ]
    order.refresh_from_db()
    assert order.confirmation_sent_at == NOW + timedelta(seconds=5)
    # Once.
    send_confirmations(now=NOW + timedelta(minutes=10))
    assert len(mail.outbox) == 1


def test_several_dates_are_named_once_each_in_time_order_and_the_subject_says_more(
    made: Example,
) -> None:
    free(made)
    # Asked for out of order: a later hour of the second date first.
    create_order(
        member=made.member,
        requests=[
            request(
                made,
                starts_at=at(DAY + timedelta(days=offset), hour),
                ends_at=at(DAY + timedelta(days=offset), hour + 2),
            )
            for offset, hour in ((2, 12), (1, 10), (2, 10))
        ],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    assert message.subject == "You're booked: Visit, Pool, Tuesday 2 March and more"
    plain = text_of(message, "text/plain")
    html = markup.unescape(text_of(message, "text/html"))
    for text in (plain, html):
        assert text.count("Tuesday 2 March") == 1
        assert text.count("Wednesday 3 March") == 1
        assert text.index("Tuesday 2 March") < text.index("Wednesday 3 March")
        assert text.index("10:00\N{EN DASH}12:00", text.index("Wednesday")) < (
            text.index("12:00\N{EN DASH}14:00")
        )
        assert text.count("Reference") == 1
    assert plain.index("Reference") < plain.index("Free of charge")
    assert plain.index("Free of charge") < plain.index("Tuesday 2 March")
    name = parts(message)["text/calendar"][0].get_filename()
    assert name == "randi-pool-2027-03-02.ics"


def test_a_paid_order_is_confirmed_when_due_and_an_older_one_never(
    made: Example,
) -> None:
    order = order_of(made, 1)
    assert order.total and order.confirmation_due_at is None
    mail.outbox.clear()
    send_confirmations(now=NOW)
    # Held: not due. Confirmed before the email existed: never due.
    settle(order)
    send_confirmations(now=NOW)
    assert mail.outbox == []
    # Confirmed by its payment (payment_capture.confirm sets the moment).
    Order.objects.of(made.org).filter(pk=order.pk).update(confirmation_due_at=NOW)
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    assert f"Reference {order.reference}\nPaid {money_words(order.total, 'NOK')}" in (
        message.body
    )
    assert "2 kr" in message.body


def test_a_failed_send_is_tried_again_after_others_and_logged_without_address(
    made: Example, caplog: pytest.LogCaptureFixture
) -> None:
    free(made)
    first, second, third = (order_of(made, n) for n in (1, 2, 3))
    failing = subject_of(first)
    calls: list[str] = []

    def send(self: Any, messages: list[EmailMessage]) -> int:
        calls.append(str(messages[0].subject))
        if messages[0].subject == failing:
            raise RuntimeError(f"refused {made.member.email}")
        mail.outbox.extend(messages)
        return len(messages)

    mail.outbox.clear()
    with mock.patch(SEND, new=send):
        send_confirmations(now=NOW + timedelta(seconds=5))
        assert sorted(message.subject for message in mail.outbox) == sorted(
            subject_of(order) for order in (second, third)
        )
        # Not again before RETRY.
        send_confirmations(now=NOW + timedelta(minutes=1))
        assert calls.count(failing) == 1
        send_confirmations(now=NOW + timedelta(seconds=5) + RETRY)
        assert calls.count(failing) == 2
    assert len(calls) == 4
    assert (
        f"booking confirmation failed: organization-{made.org.pk} order-{first.pk}"
        in (caplog.text)
    )
    assert made.member.email not in caplog.text
    first.refresh_from_db()
    assert first.confirmation_sent_at is None
    assert first.confirmation_tried_at == NOW + timedelta(seconds=5) + RETRY


def test_rotation_reaches_later_orders_past_failing_ones(made: Example) -> None:
    free(made)
    orders = [order_of(made, n) for n in range(1, 4)]
    failing = {subject_of(order) for order in orders[:2]}

    def send(self: Any, messages: list[EmailMessage]) -> int:
        if messages[0].subject in failing:
            raise RuntimeError("refused")
        mail.outbox.extend(messages)
        return 1

    with mock.patch.object(booking_emails, "BATCH", 2), mock.patch(SEND, new=send):
        mail.outbox.clear()
        send_confirmations(now=NOW + timedelta(seconds=5))
        assert mail.outbox == []
        # The two that failed were tried last; the third goes first now.
        send_confirmations(now=NOW + timedelta(seconds=5) + RETRY)
    assert [message.subject for message in mail.outbox] == [subject_of(orders[2])]


def test_no_confirmation_after_a_day_or_for_a_cancelled_order_or_no_address(
    made: Example, caplog: pytest.LogCaptureFixture
) -> None:
    free(made)
    late, cancelled, erased = (order_of(made, n) for n in (1, 2, 3))
    settle(cancelled, BookingState.CANCELLED)
    stranger = other(made)
    Order.objects.of(made.org).filter(pk=erased.pk).update(member=stranger)
    Booking.objects.of(made.org).filter(order=erased).update(member=stranger)
    # Erased: no address left (an active member always has one).
    Member.objects.of(made.org).filter(pk=stranger.pk).update(
        email="", phone="", status="disabled"
    )
    Order.objects.of(made.org).filter(pk=late.pk).update(
        confirmation_due_at=NOW - GIVE_UP
    )
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=1))
    assert mail.outbox == []
    for order in (late, cancelled, erased):
        order.refresh_from_db()
        assert order.confirmation_due_at is None and order.confirmation_sent_at is None
    # The one given up is logged by its order.
    assert (
        f"booking confirmation given up: organization-{made.org.pk} order-{late.pk}"
        in caplog.text
    )


def test_no_confirmation_for_a_member_not_active(made: Example) -> None:
    free(made)
    order = order_of(made, 1)
    Member.objects.of(made.org).filter(pk=made.member.pk).update(status="blocked")
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    assert mail.outbox == []
    order.refresh_from_db()
    assert order.confirmation_due_at is None


def test_a_message_that_cannot_be_built_holds_up_no_other(
    made: Example, caplog: pytest.LogCaptureFixture
) -> None:
    free(made)
    broken, fine = order_of(made, 1), order_of(made, 2)
    build = booking_emails.confirmation_message

    def failing(org: Any, found: Order, *rest: Any) -> Any:
        if found.pk == broken.pk:
            raise ValueError(f"no words for {made.member.email}")
        return build(org, found, *rest)

    mail.outbox.clear()
    with mock.patch.object(booking_emails, "confirmation_message", new=failing):
        send_confirmations(now=NOW + timedelta(seconds=5))
    assert [message.subject for message in mail.outbox] == [subject_of(fine)]
    assert (
        f"booking confirmation not built: organization-{made.org.pk} order-{broken.pk}"
        in caplog.text
    )
    assert made.member.email not in caplog.text
    broken.refresh_from_db()
    assert broken.confirmation_tried_at == NOW + timedelta(seconds=5)
    # Tried again after RETRY, like a failed send.
    send_confirmations(now=NOW + timedelta(seconds=5) + RETRY)
    assert len(mail.outbox) == 2


def test_a_subject_is_one_line_whatever_the_organizations_words(
    made: Example,
) -> None:
    free(made)
    made.org.labels = {"email.confirmed-subject": "Booked:\n{what}, {date}"}
    made.org.save()
    order = order_of(made, 1)
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    assert message.subject == subject_of(order).replace("You're booked:", "Booked:")


def test_the_plain_text_is_not_escaped_and_the_html_is(made: Example) -> None:
    free(made)
    made.asset.name = "Pool & Spa <b>"
    made.asset.save()
    order_of(made, 1)
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    assert "Visit, Pool & Spa <b>" in text_of(message, "text/plain")
    html = text_of(message, "text/html")
    assert "Visit, Pool &amp; Spa &lt;b&gt;" in html and "<b>" not in html


def test_money_words_are_the_pages() -> None:
    assert money_words(125000, "NOK") == "1 250 kr"
    assert money_words(12550, "NOK") == "125,50 kr"
    assert money_words(0, "NOK") == "0 kr"
    assert money_words(123456789, "NOK") == "1 234 567,89 kr"
    assert money_words(12550, "EUR") == "125,50 \N{EURO SIGN}"
    assert money_words(125000, "DKK") == "1 250 DKK"
    with pytest.raises(ValueError):
        money_words(100, "GBP")


# --- The reminder -------------------------------------------------------------


def test_the_reminder_goes_48_hours_before_and_names_the_next_five_days(
    made: Example,
) -> None:
    # Thursday 4, Saturday 6 and Tuesday 9 March, 10:00; the order of each
    # made at NOW, 28 February.
    first, second, later = (order_of(made, n) for n in (3, 5, 8))
    beside = order_of(made, 4, member=other(made))
    for order in (first, second, later, beside):
        settle(order)
    mail.outbox.clear()
    start = start_of(first)
    send_reminders(now=start - AHEAD - timedelta(seconds=1))
    assert mail.outbox == []
    send_reminders(now=start - AHEAD)
    mine = [message for message in mail.outbox if message.to == [made.member.email]]
    (message,) = mine
    assert message.subject == "Your booking on Thursday 4 March"
    check_system_mail(message, made.org, reply=True)
    for text in (message.body, markup.unescape(text_of(message, "text/html"))):
        assert "See you soon" in text
        assert "Thursday 4 March" in text and "Saturday 6 March" in text
        assert "Tuesday 9 March" not in text
        assert "Friday 5 March" not in text
        assert "Zebedee" not in text and "zebedee" not in text
    assert "text/calendar" not in parts(message)
    # No prices in a reminder.
    assert " kr" not in message.body and "Free" not in message.body
    reminded = set(
        Booking.objects.of(made.org)
        .exclude(reminded_at=None)
        .values_list("order_id", flat=True)
    )
    assert reminded == {first.pk, second.pk}
    # The second, already named, gets none of its own; the other member's is
    # theirs; the later one gets its own.
    mail.outbox.clear()
    send_reminders(now=start_of(second) - AHEAD)
    send_reminders(now=start_of(beside) - AHEAD)
    assert [message.to for message in mail.outbox] == [["zebedee.quill@example.com"]]
    mail.outbox.clear()
    send_reminders(now=start_of(later) - AHEAD + timedelta(hours=1))
    (own,) = mail.outbox
    assert own.subject == "Your booking on Tuesday 9 March"
    send_reminders(now=start_of(later) - timedelta(hours=1))
    assert len(mail.outbox) == 1


def test_a_reminder_is_worded_in_its_organizations_language(
    made: Example, second_language: None
) -> None:
    # The background worker builds the reminder from the organization it holds.
    first = order_of(made, 3)
    settle(first)
    Organization.objects.filter(pk=made.org.pk).update(
        language="xx", labels={"email.sign-off": "- Own xx"}
    )
    mail.outbox.clear()
    send_reminders(now=start_of(first) - AHEAD)
    (message,) = [m for m in mail.outbox if m.to == [made.member.email]]
    assert message.subject == "Your xx booking Torsdag xx 4 mars xx"
    for text in (message.body, markup.unescape(text_of(message, "text/plain"))):
        assert "See you xx" in text and "See you soon" not in text
        assert "Torsdag xx 4 mars xx" in text
        assert "- Own xx" in text
        # A key the xx catalogue lacks is English.
        assert "Questions? Write to" in text


def test_no_reminder_for_a_booking_made_less_than_48_hours_ahead(
    made: Example,
) -> None:
    start = at(DAY + timedelta(days=3), 10)
    late = create_order(
        member=made.member,
        requests=[day(made, 3)],
        now=start - AHEAD + timedelta(minutes=1),
        accepted_version=made_up.accepted(made.member),
    )
    settle(late)
    mail.outbox.clear()
    for hours in (47, 24, 1):
        send_reminders(now=start - timedelta(hours=hours))
    assert mail.outbox == []


def test_no_reminder_for_a_booking_confirmed_less_than_48_hours_ahead(
    made: Example,
) -> None:
    # Ordered days ahead, but paid only inside the 48 hours.
    order = order_of(made, 3)
    settle(order)
    start = start_of(order)
    paid = start - AHEAD + timedelta(minutes=5)
    Order.objects.of(made.org).filter(pk=order.pk).update(
        confirmation_due_at=paid, confirmation_sent_at=paid
    )
    mail.outbox.clear()
    for hours in (47, 24, 1):
        send_reminders(now=start - timedelta(hours=hours))
    assert mail.outbox == []


def test_the_reminder_waits_for_the_confirmation(made: Example) -> None:
    order = order_of(made, 3)
    settle(order)
    start = start_of(order)
    Order.objects.of(made.org).filter(pk=order.pk).update(
        confirmation_due_at=start - AHEAD - timedelta(hours=1)
    )
    mail.outbox.clear()
    send_reminders(now=start - AHEAD)
    assert mail.outbox == []
    Order.objects.of(made.org).filter(pk=order.pk).update(
        confirmation_sent_at=start - AHEAD
    )
    send_reminders(now=start - AHEAD + timedelta(minutes=1))
    (message,) = mail.outbox
    assert message.subject == "Your booking on Thursday 4 March"


def test_the_five_days_are_the_organizations_across_a_clock_change(
    made: Example,
) -> None:
    # Friday 26 and Wednesday 31 March 2027, both 10:00; the clocks go
    # forward on Sunday 28 March. Five days on the clock end at 10:00 on the
    # 31st, an hour before five days of 24 hours.
    first, fifth = order_of(made, 25), order_of(made, 30)
    for order in (first, fifth):
        settle(order)
    mail.outbox.clear()
    send_reminders(now=start_of(first) - AHEAD)
    (message,) = mail.outbox
    assert "Friday 26 March" in message.body
    assert "Wednesday 31 March" not in message.body
    mail.outbox.clear()
    send_reminders(now=start_of(fifth) - AHEAD)
    (own,) = mail.outbox
    assert own.subject == "Your booking on Wednesday 31 March"


def test_no_reminder_for_a_cancelled_booking_or_a_member_not_active(
    made: Example,
) -> None:
    cancelled = order_of(made, 3)
    settle(cancelled, BookingState.CANCELLED)
    stranger = other(made)
    theirs = order_of(made, 4, member=stranger)
    settle(theirs)
    Member.objects.of(made.org).filter(pk=stranger.pk).update(status="disabled")
    mail.outbox.clear()
    send_reminders(now=start_of(cancelled) - AHEAD)
    send_reminders(now=start_of(theirs) - AHEAD)
    assert mail.outbox == []
    assert Booking.objects.of(made.org).get(order=theirs).reminded_at is not None
    assert Booking.objects.of(made.org).get(order=cancelled).reminded_at is None


def test_a_failed_reminder_is_tried_again_until_the_booking_starts(
    made: Example,
) -> None:
    order = order_of(made, 3)
    settle(order)
    start = start_of(order)
    calls: list[str] = []

    def send(self: Any, messages: list[EmailMessage]) -> int:
        calls.append(str(messages[0].subject))
        raise RuntimeError("refused")

    with mock.patch(SEND, new=send):
        send_reminders(now=start - AHEAD)
        send_reminders(now=start - AHEAD + timedelta(minutes=1))
        assert len(calls) == 1
        send_reminders(now=start - AHEAD + RETRY)
        assert len(calls) == 2
        send_reminders(now=start)
        assert len(calls) == 2
    booking = Booking.objects.of(made.org).get(order=order)
    assert booking.reminded_at is None
    assert booking.reminder_tried_at == start - AHEAD + RETRY


def test_a_reminder_that_cannot_be_built_is_tried_again(
    made: Example, caplog: pytest.LogCaptureFixture
) -> None:
    order = order_of(made, 3)
    settle(order)
    start = start_of(order)
    booking = Booking.objects.of(made.org).get(order=order)
    mail.outbox.clear()
    with mock.patch.object(
        booking_emails, "reminder_message", side_effect=ValueError("no words")
    ):
        send_reminders(now=start - AHEAD)
    assert mail.outbox == []
    assert (
        f"booking reminder not built: organization-{made.org.pk} booking-{booking.pk}"
        in caplog.text
    )
    send_reminders(now=start - AHEAD + RETRY)
    assert len(mail.outbox) == 1


# --- The migration ------------------------------------------------------------

BEFORE = [("core", "0025_member_status")]


def migrate_to(target: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)


def test_the_migration_reverses_only_while_no_email_is_recorded(
    made: Example,
) -> None:
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        order = order_of(made, 1)
        migrate_to(BEFORE)
        migrate_to(latest)
        free(made)
        freed = order_of(made, 2)
        Booking.objects.of(made.org).filter(order=order).update(reminded_at=NOW)
        for refused in ("an order", "a booking"):
            with pytest.raises(RuntimeError, match=f"{refused} records an email"):
                migrate_to(BEFORE)
            Order.objects.of(made.org).filter(pk=freed.pk).update(
                confirmation_due_at=None
            )
        Booking.objects.of(made.org).filter(order=order).update(reminded_at=None)
        migrate_to(BEFORE)
    finally:
        migrate_to(MigrationExecutor(connection).loader.graph.leaf_nodes())
