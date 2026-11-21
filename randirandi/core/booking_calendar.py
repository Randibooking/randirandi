# SPDX-License-Identifier: AGPL-3.0-only
"""A confirmed order's bookings in the member's own calendar .
Nothing fancy.").

Two ways, both chosen by the member: a calendar file (.ics, RFC 5545) of the
order's confirmed bookings, which the confirmation page offers and the
confirmation email attaches, and an add-to-Google-Calendar address for each
booking, which carries that booking's times and words to Google only when
the member opens it. No feed or subscription address: that would be a list
of a member's bookings kept open outside the app .

The words are the organization's labels (`calendar.title`,
`calendar.details`, `email.contact`); the place is the organization's name.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from randirandi import labels
from randirandi.core import booking_words
from randirandi.core.booking_money import currency_of, money_words
from randirandi.core.models import BookingState, Order, Organization

GOOGLE = "https://calendar.google.com/calendar/render"
# RFC 5545 3.1: a line is at most 75 octets; a longer one is folded.
LONGEST_LINE = 75
# The control characters RFC 5545 3.3.11 does not allow in a text.
_CONTROL = {code: " " for code in (*range(0x20), 0x7F) if code != 0x09}


@dataclass(frozen=True)
class Event:
    booking: uuid.UUID
    start: datetime
    end: datetime
    title: str
    details: str
    place: str
    asset: str
    day: str


def events(org: Organization, found: Order) -> list[Event]:
    """The order's confirmed bookings as calendar entries, by start; a
    booking cancelled since is left out."""
    bookings = (
        found.bookings.filter(taken_time__state=BookingState.CONFIRMED)
        .select_related("taken_time__asset", "booking_configuration__floor_plan")
        .prefetch_related("lines__price_book")
        .order_by("taken_time__starts_at", "pk")
    )
    reference = labels.word(org, "calendar.details", reference=found.reference)
    made = []
    for booking in bookings:
        title = labels.word(
            org,
            "calendar.title",
            type=booking.booking_type_name,
            asset=booking.taken_time.asset.name,
        )
        details = [
            title,
            labels.word(org, "line.booking-as", category=booking.booking_category_name),
        ]
        places = booking.taken_time.quantity
        if places > 1:
            plan = booking.booking_configuration.floor_plan
            word = booking_words.spot_word(None if plan is None else plan.unit)
            details.append(
                labels.plural(
                    org,
                    "line.places",
                    places,
                    word=word or labels.word(org, "spot.word"),
                )
            )
        total = sum(line.total for line in booking.lines.all())
        details += [
            money_words(total, currency_of(org, booking))
            if total
            else labels.word(org, "price.free"),
            reference,
            org.name,
            labels.word(org, "email.contact", email=org.contact_email),
        ]
        zone = ZoneInfo(booking.taken_time.time_zone)
        made.append(
            Event(
                booking=booking.public_id,
                start=booking.taken_time.starts_at,
                end=booking.taken_time.ends_at,
                title=title,
                details="\n".join(details),
                place=org.name,
                asset=booking.taken_time.asset.name,
                day=booking.taken_time.starts_at.astimezone(zone).date().isoformat(),
            )
        )
    return made


def _moment(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _text(value: str) -> str:
    """A text value written as RFC 5545 3.3.11 asks: backslash, semicolon,
    comma and line break escaped, other control characters as spaces."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )
    return escaped.translate(_CONTROL)


def _fold(line: str) -> str:
    """One content line folded at 75 octets (RFC 5545 3.1), each further
    line after a break and a space, never inside a character."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for char in line:
        width = len(char.encode())
        if size + width > (LONGEST_LINE if not parts else LONGEST_LINE - 1):
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    parts.append("".join(current))
    return "\r\n ".join(parts)


def calendar_file(
    org: Organization, found: Order, now: datetime | None = None
) -> str | None:
    """The order's confirmed bookings as one calendar file, or None when it
    has none. Each booking keeps one id across downloads, so a calendar that
    already has it updates it instead of adding it twice."""
    made = events(org, found)
    if not made:
        return None
    stamp = _moment(now if now is not None else timezone.now())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RandiRandi//Booking//EN",
        "CALSCALE:GREGORIAN",
    ]
    for event in made:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event.booking}@{settings.ORGANIZATION_DOMAIN}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_moment(event.start)}",
            f"DTEND:{_moment(event.end)}",
            f"SUMMARY:{_text(event.title)}",
            f"DESCRIPTION:{_text(event.details)}",
            f"LOCATION:{_text(event.place)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


# Letters with no ASCII decomposition, written the Norwegian way.
_LETTERS = str.maketrans({"æ": "ae", "Æ": "Ae", "ø": "o", "Ø": "O", "ß": "ss"})


def _slug(text: str) -> str:
    """Lowercase ASCII words joined by hyphens: "Harbourview" is "harbourview",
    "Sjøhuset Åse" is "sjohuset-ase", "Bælplass" is "baelplass"."""
    plain = unicodedata.normalize("NFKD", text.translate(_LETTERS))
    ascii_only = plain.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")


def file_name(org: Organization, found: Order) -> str:
    """ "randi-pool-2026-10-21.ics": the first booking's asset and date (on
    the asset's clock); "randi-<reference>.ics" when none is confirmed."""
    made = events(org, found)
    if not made:
        return f"randi-{_slug(found.reference)}.ics"
    return (
        "-".join(part for part in ("randi", _slug(made[0].asset), made[0].day) if part)
        + ".ics"
    )


def google_link(event: Event) -> str:
    """The address that opens Google Calendar with the booking filled in."""
    return f"{GOOGLE}?" + urlencode(
        {
            "action": "TEMPLATE",
            "text": event.title,
            "dates": f"{_moment(event.start)}/{_moment(event.end)}",
            "details": event.details,
            "location": event.place,
        }
    )


def google_links(org: Organization, found: Order) -> dict[uuid.UUID, str]:
    """Each confirmed booking's Google Calendar address, by booking."""
    return {event.booking: google_link(event) for event in events(org, found)}
