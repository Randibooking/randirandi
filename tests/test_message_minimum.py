# SPDX-License-Identifier: AGPL-3.0-only
"""The booking mails carry the member's own booking and nothing else personal
(control C37): the confirmation, the reminder, the cancellation and their
calendar file."""

import html as markup
import re
import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage

from randirandi.core import booking_emails
from randirandi.core.booking_emails import (
    AHEAD,
    send_cancellations,
    send_confirmations,
    send_reminders,
)
from randirandi.core.models import Member, Order
from tests.test_booking_cancel import cancel
from tests.test_booking_emails import free, parts, start_of, text_of
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_member_bookings import order_of, other, settle
from tests.test_ordering import NOW
from tests.test_payment_adjustments import administrator as administrator

pytestmark = pytest.mark.django_db(transaction=True)

MINE = "+4712345678"
# The confirmation's link names its order's public id. One of digits only is
# the worst case for the phone-shaped check, so every run has it: a random id
# held eight digits in a row about one run in twenty (the deploy of d4e67f4).
DIGITS_ONLY = uuid.UUID("12345678-1234-4123-8123-123456789012")
CONDITIONS_LINK = r"https://[^\s/]+/book/order/[0-9a-f-]{36}/conditions/"
STAGES = ("confirmation", "reminder", "cancellation")


def everything(message: EmailMessage) -> str:
    """All that a message carries after its header: both bodies and the
    calendar file, as text."""
    found = parts(message)
    return "\n".join(
        [
            text_of(message, "text/plain"),
            markup.unescape(text_of(message, "text/html")),
            *(
                payload.decode()
                for kind, (_, payload) in found.items()
                if kind == "text/calendar"
            ),
        ]
    )


def header_is_the_recipients_alone(
    message: EmailMessage, member: Member, contact: str
) -> None:
    # The recipient alone; replies go to the organization's contact address
    # alone .
    assert message.to == [member.email]
    assert not message.cc and not message.bcc
    assert message.reply_to == [contact]
    assert message.extra_headers == {}


@pytest.fixture
def sent(made: Example, administrator: Member) -> dict[str, EmailMessage]:
    """One of each mail for the member, with another member booked at the
    the next day and a phone number and name on the member."""
    Member.objects.of(made.org).filter(pk=made.member.pk).update(
        phone=MINE,
        full_name="Quentin Marlowe",
        first_name="Quentin",
        last_name="Marlowe",
    )
    made.member.refresh_from_db()
    free(made)
    beside = other(made)
    mine, theirs = order_of(made, 3), order_of(made, 4, member=beside)
    kept = order_of(made, 8)
    for order in (mine, theirs, kept):
        settle(order)
    Order.objects.of(made.org).filter(pk=mine.pk).update(public_id=DIGITS_ONLY)
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    send_reminders(now=start_of(mine) - AHEAD)
    cancel(administrator, kept)
    send_cancellations(now=NOW + timedelta(seconds=5))
    mine_to = [m for m in mail.outbox if m.to == [made.member.email]]
    by_subject = {
        "confirmation": next(
            m for m in mine_to if m.subject.startswith("You're booked")
        ),
        "reminder": next(
            m
            for m in mine_to
            if "Thursday 4 March" in m.subject and "booked" not in m.subject
        ),
        "cancellation": next(m for m in mine_to if "cancelled" in m.subject),
    }
    assert set(by_subject) == set(STAGES)
    assert {m.to[0] for m in mail.outbox} == {made.member.email, beside.email}
    return by_subject


@pytest.mark.parametrize("stage", STAGES)
def test_a_booking_mail_goes_to_the_member_alone(
    made: Example, sent: dict[str, EmailMessage], stage: str
) -> None:
    header_is_the_recipients_alone(sent[stage], made.member, made.org.contact_email)


@pytest.mark.parametrize("stage", STAGES)
def test_a_booking_mail_names_no_other_person_and_no_contact_detail(
    made: Example, sent: dict[str, EmailMessage], stage: str, administrator: Member
) -> None:
    message = sent[stage]
    carried = everything(message) + "\n" + message.subject
    lowered = carried.lower()
    for forbidden in (
        "zebedee",
        "quill",
        "+4799887766",
        MINE,
        "quentin",
        "marlowe",
        made.member.email,
        administrator.email,
        "admin@",
    ):
        assert forbidden.lower() not in lowered, forbidden
    # No address of anyone: the only address is the organization's contact.
    # An iCalendar line folds at 75 characters; read the lines unfolded.
    unfolded = re.sub(r"\r?\n[ \t]", "", carried)
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", unfolded))
    allowed = {made.org.contact_email, f"logo@{settings.ORGANIZATION_DOMAIN}"}
    calendar_ids = {
        a
        for a in addresses
        if re.fullmatch(r"[0-9a-f-]{36}@" + re.escape(settings.ORGANIZATION_DOMAIN), a)
    }
    assert addresses - calendar_ids <= allowed
    # The one link, to the order's own conditions, names an id, not a phone.
    without_ids = re.sub(r"UID:\S+|DT\w+:\S+|" + CONDITIONS_LINK, "", carried)
    assert not re.search(r"\+?\d{8,}", without_ids)


@pytest.mark.parametrize("stage", STAGES)
def test_a_booking_mail_has_no_internal_id_or_link(
    made: Example, sent: dict[str, EmailMessage], stage: str
) -> None:
    carried = everything(sent[stage])
    # The one link a mail may hold: its order's own accepted booking conditions,
    # which open only for its member .
    carried = re.sub(CONDITIONS_LINK, "", carried)
    for order in Order.objects.of(made.org):
        assert str(order.public_id) not in carried
    assert not re.search(r"\b(order|booking|member)[-_ #:/]*\d", carried, re.I)
    assert "http" not in carried.replace("http://www.w3.org", "")


def test_the_confirmation_carries_what_where_when_as_price_and_reference(
    made: Example, sent: dict[str, EmailMessage]
) -> None:
    message = sent["confirmation"]
    plain = text_of(message, "text/plain")
    order = Order.objects.of(made.org).filter(member=made.member).order_by("pk").first()
    assert order is not None
    assert [line for line in plain.splitlines() if line.strip()] == [
        "RandiRandi",
        "You're booked!",
        "The harbourview housing co-op",
        f"Reference {order.reference}",
        "Free of charge",
        "Thursday 4 March",
        "10:00\N{EN DASH}12:00: No charge",
        "Visit, Pool · Booking as Private",
        "I've attached the times for your calendar.",
        f"Booking conditions: https://harbourview.{settings.ORGANIZATION_DOMAIN}"
        f"/book/order/{order.public_id}/conditions/",
        "\N{EN DASH} Randi",
        f"Questions? Write to {made.org.contact_email}.",
    ]


def test_the_reminder_carries_what_where_when_and_as_without_price_or_reference(
    made: Example, sent: dict[str, EmailMessage]
) -> None:
    plain = text_of(sent["reminder"], "text/plain")
    assert [line for line in plain.splitlines() if line.strip()] == [
        "RandiRandi",
        "See you soon",
        "The harbourview housing co-op",
        "Thursday 4 March",
        "10:00\N{EN DASH}12:00",
        "Visit, Pool \N{MIDDLE DOT} Booking as Private",
        "\N{EN DASH} Randi",
        f"Questions? Write to {made.org.contact_email}.",
    ]
    assert "text/calendar" not in parts(sent["reminder"])


def test_the_cancellation_carries_what_where_when_as_and_reference_without_price(
    made: Example, sent: dict[str, EmailMessage]
) -> None:
    message = sent["cancellation"]
    plain = text_of(message, "text/plain")
    cancelled = (
        Order.objects.of(made.org).filter(member=made.member).order_by("pk").last()
    )
    assert cancelled is not None
    assert [line for line in plain.splitlines() if line.strip()] == [
        "RandiRandi",
        "Cancelled",
        "The harbourview housing co-op",
        f"Reference {cancelled.reference}",
        "Tuesday 9 March",
        "10:00\N{EN DASH}12:00",
        "Visit, Pool \N{MIDDLE DOT} Booking as Private",
        "No refund.",
        "\N{EN DASH} Randi",
        f"Questions? Write to {made.org.contact_email}.",
    ]
    assert "text/calendar" not in parts(message)


def test_the_calendar_file_carries_only_the_members_booking_and_the_organization(
    made: Example, sent: dict[str, EmailMessage]
) -> None:
    _, payload = parts(sent["confirmation"])["text/calendar"]
    unfolded = payload.decode().replace("\r\n ", "").replace("\r", "")
    names = {line.split(":", 1)[0].split(";")[0] for line in unfolded.splitlines()}
    assert names == {
        "BEGIN", "VERSION", "PRODID", "CALSCALE", "UID", "DTSTAMP",
        "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION", "END",
    }  # fmt: skip
    # No attendee, organizer or contact property: nobody is named.
    assert "ATTENDEE" not in unfolded and "ORGANIZER" not in unfolded
    assert unfolded.count("BEGIN:VEVENT") == 1
    assert "LOCATION:The harbourview housing co-op" in unfolded
    assert "SUMMARY:Visit\\, Pool" in unfolded
    uid = re.search(r"UID:(.*)\n", unfolded)
    assert uid and re.fullmatch(
        r"[0-9a-f-]{36}@" + re.escape(settings.ORGANIZATION_DOMAIN), uid.group(1)
    )
    assert booking_emails.__doc__  # the module's rule is the one under test
