# SPDX-License-Identifier: AGPL-3.0-only
"""A confirmed order in the member's own calendar: the calendar file and the
Google Calendar address ."""

import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.test import Client as Browser

from randirandi import labels
from randirandi.core import booking_calendar, booking_outcome
from randirandi.core.models import BookingState, Organization, TakenTime
from randirandi.core.ordering import create_order
from tests import made_up
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get
from tests.test_member_bookings import order_of, other, settle, signed_in
from tests.test_ordering import NOW, request
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_second_factor import clocks as clocks

pytestmark = pytest.mark.django_db(transaction=True)


def path(order: object) -> str:
    return f"/booking/order/{order}/calendar/"


def unfolded(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_the_file_holds_each_confirmed_booking_in_utc(made: Example) -> None:
    stay = order_of(made, 2, 1)
    settle(stay)
    response = get(signed_in(made), path(stay.public_id))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"
    assert response["Content-Disposition"] == (
        'attachment; filename="randi-pool-2027-03-02.ics"'
    )
    assert "no-store" in response["Cache-Control"]
    text = response.content.decode()
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = unfolded(text)
    # No METHOD: a plain file to import, not a scheduling message (RFC 5546
    # would ask for an organizer).
    assert lines[:5] == [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RandiRandi//Booking//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
    ]
    assert lines[-2:] == ["END:VCALENDAR", ""]
    bookings = list(stay.bookings.order_by("taken_time__starts_at"))
    starts = [line for line in lines if line.startswith("DTSTART:")]
    # Oslo is UTC+1 on 2 and 3 March 2027: 10:00 there is 09:00Z.
    assert starts == ["DTSTART:20270302T090000Z", "DTSTART:20270303T090000Z"]
    assert [line for line in lines if line.startswith("DTEND:")] == [
        "DTEND:20270302T110000Z",
        "DTEND:20270303T110000Z",
    ]
    assert [line for line in lines if line.startswith("UID:")] == [
        f"UID:{booking.public_id}@{settings.ORGANIZATION_DOMAIN}"
        for booking in bookings
    ]
    assert lines.count("SUMMARY:Visit\\, Pool") == 2
    # What and where, booked as, price, reference, organization, contact.
    assert (
        lines.count(
            "DESCRIPTION:Visit\\, Pool\\nBooking as Private\\n2 kr"
            f"\\nReference {stay.reference}\\nThe harbourview housing co-op"
            f"\\nQuestions? Write to {made.org.contact_email}."
        )
        == 2
    )
    assert lines.count("LOCATION:The harbourview housing co-op") == 2
    # The same booking keeps its id in a second download.
    again = unfolded(get(signed_in(made), path(stay.public_id)).content.decode())
    assert [line for line in again if line.startswith("UID:")] == [
        line for line in lines if line.startswith("UID:")
    ]


def test_a_booking_cancelled_since_is_left_out(made: Example) -> None:
    stay = order_of(made, 1, 2)
    settle(stay)
    first = stay.bookings.order_by("taken_time__starts_at").first()
    assert first is not None
    TakenTime.objects.of(made.org).filter(pk=first.taken_time_id).update(
        state=BookingState.CANCELLED
    )
    text = get(signed_in(made), path(stay.public_id)).content.decode()
    assert text.count("BEGIN:VEVENT") == 1
    assert str(first.public_id) not in text
    read = booking_outcome.read(made.org, made.member, stay.public_id)
    assert read is not None and read["state"] == "confirmed"
    assert read["bookings"][0]["google"] is None
    assert read["bookings"][1]["google"].startswith(booking_calendar.GOOGLE)


def test_only_the_members_own_confirmed_order(made: Example) -> None:
    held = order_of(made, 1)
    theirs = order_of(made, 2, member=other(made))
    settle(theirs)
    cancelled = order_of(made, 3)
    settle(cancelled, BookingState.CANCELLED)
    browser = signed_in(made)
    answers = [
        get(browser, path(order))
        for order in (held.public_id, theirs.public_id, cancelled.public_id)
    ] + [get(browser, path(uuid.uuid4()))]
    assert [answer.status_code for answer in answers] == [404] * 4
    assert {answer.content for answer in answers} == {b'{"error": "not-found"}'}
    for private in ("Zebedee", "Quill", theirs.reference):
        assert private not in answers[1].content.decode()
    # Signed out goes to the login; nothing else is answered.
    settle(held)
    out = get(Browser(), path(held.public_id))
    assert out.status_code == 302 and out["Location"].startswith("/login/")
    assert (
        browser.post(path(held.public_id), HTTP_HOST=HOST, secure=True).status_code
        == 405
    )
    assert get(browser, path("not-an-order")).status_code == 404


def test_words_are_escaped_folded_and_the_organizations(made: Example) -> None:
    Organization.objects.filter(pk=made.org.pk).update(
        name="Sjøhuset; Øvre\\Nedre, styret",
        labels={
            "calendar.title": "{asset} / {type}",
            "calendar.details": "Nr. {reference}",
        },
    )
    made.org.refresh_from_db()
    made.asset.name = "Ærlig talt et svært langt navn på et selskapslokale, ved sjøen"
    made.asset.save()
    stay = order_of(made, 1)
    settle(stay)
    text = booking_calendar.calendar_file(made.org, stay, now=NOW)
    assert text is not None
    for line in text.split("\r\n"):
        assert len(line.encode()) <= 75
    lines = unfolded(text)
    long_name = "Ærlig talt et svært langt navn på et selskapslokale\\, ved sjøen"
    assert f"SUMMARY:{long_name} / Visit" in lines
    assert any(
        line.startswith("DESCRIPTION:")
        and f"\\nNr. {stay.reference}\\nSjøhuset\\; Øvre\\\\Nedre\\, styret\\n" in line
        for line in lines
    )
    assert "LOCATION:Sjøhuset\\; Øvre\\\\Nedre\\, styret" in lines
    assert f"DTSTAMP:{NOW.strftime('%Y%m%dT%H%M%SZ')}" in lines


def test_the_calendar_file_is_worded_in_the_organizations_language(
    made: Example, second_language: None
) -> None:
    Organization.objects.filter(pk=made.org.pk).update(language="xx")
    made.org.refresh_from_db()
    stay = order_of(made, 1)
    settle(stay)
    text = booking_calendar.calendar_file(made.org, stay, now=NOW)
    assert text is not None
    lines = unfolded(text)
    assert any(line.startswith("SUMMARY:") and "xx Visit" in line for line in lines)
    assert any(f"Ref xx {stay.reference}" in line for line in lines)


def test_a_control_character_or_line_break_cannot_add_a_property() -> None:
    assert booking_calendar._text("a\r\nEND:VEVENT\rb\nc\x00d\te") == (
        "a\\nEND:VEVENT\\nb\\nc d\te"
    )


def test_places_beyond_one_are_named(made: Example) -> None:
    made.capacity(8)
    stay = create_order(
        member=made.member,
        requests=[
            request(
                made,
                quantity=3,
                starts_at=at(DAY + timedelta(days=1), 10),
                ends_at=at(DAY + timedelta(days=1), 12),
            )
        ],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )
    settle(stay)
    lines = unfolded(booking_calendar.calendar_file(made.org, stay) or "")
    assert any(
        line.startswith(
            "DESCRIPTION:Visit\\, Pool\\nBooking as Private"
            "\\n3 \N{MULTIPLICATION SIGN} spot\\n"
        )
        for line in lines
    )


def test_the_confirmed_read_carries_each_bookings_google_address(made: Example) -> None:
    stay = order_of(made, 1)
    settle(stay)
    body = json.loads(get(signed_in(made), f"/booking/order/{stay.public_id}/").content)
    (link,) = [booking["google"] for booking in body["bookings"]]
    found = urlsplit(link)
    assert f"{found.scheme}://{found.netloc}{found.path}" == booking_calendar.GOOGLE
    assert parse_qs(found.query) == {
        "action": ["TEMPLATE"],
        "text": ["Visit, Pool"],
        "dates": ["20270302T090000Z/20270302T110000Z"],
        "details": [
            "Visit, Pool\nBooking as Private\n2 kr"
            f"\nReference {stay.reference}\nThe harbourview housing co-op"
            f"\nQuestions? Write to {made.org.contact_email}."
        ],
        "location": ["The harbourview housing co-op"],
    }
    # A held order's read has no address.
    held = order_of(made, 2)
    read = booking_outcome.read(made.org, made.member, held.public_id)
    assert read is not None and "google" not in read["bookings"][0]


def own(words: dict[str, str]) -> Organization:
    """An organization, to the resolver, with its own English words."""
    return Organization(language="en", labels=words)


def test_a_label_is_the_organizations_when_allowed() -> None:
    assert (
        labels.word(None, "calendar.details", reference="K7Q4MD") == "Reference K7Q4MD"
    )
    assert (
        labels.word(
            own({"calendar.details": "Nr. {reference}"}),
            "calendar.details",
            reference="K7Q4MD",
        )
        == "Nr. K7Q4MD"
    )
    # A refused text (a placeholder its default lacks) falls back.
    assert (
        labels.word(
            own({"calendar.details": "{who}"}), "calendar.details", reference="K7Q4MD"
        )
        == "Reference K7Q4MD"
    )
    assert (
        labels.word(None, "calendar.title", type="{asset}", asset="Pool")
        == "{asset}, Pool"
    )
    assert labels.word(None, "calendar.title", type="Visit") == "Visit, {asset}"


def test_the_file_name_writes_letters_without_an_ascii_form() -> None:
    from randirandi.core.booking_calendar import _slug

    assert _slug("Bælplass") == "baelplass"
    assert _slug("Sjøhuset Åse") == "sjohuset-ase"
    assert _slug("Harbourview") == "harbourview"
