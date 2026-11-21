# SPDX-License-Identifier: AGPL-3.0-only
"""Branded email: local mark, plain text and HTML. The sign-in, reset and
admin-access messages carry no member-specific body; the booking messages carry the
member's own bookings only (`booking_emails`)."""

import re
from email.message import MIMEPart
from email.utils import formataddr
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.template.loader import render_to_string

from randirandi import labels
from randirandi.core.models import Organization
from randirandi.limits import PLAIN_ADDRESS

LOGO = Path(__file__).parent / "static" / "core" / "brand" / "logo-128.png"

# The mail colours, each the value scheme C already has for the role
# (frontend/src/theme/themePrimitives.jsx, colorSchemes.dark). A mail uses no
# other colour; tests/test_email_colours.py compares each with the theme.
COLOURS = {
    "page": "#222221",  # palette.background.default
    "card": "#31312e",  # palette.background.paper = gray[700]
    "card_edge": "#494844",  # palette.divider = gray[600]
    "text": "#eeeeec",  # palette.text.primary
    "text_quiet": "#b5b3ad",  # palette.text.secondary = gray[300]
}


def sender_header(organization: Organization) -> str:
    """The From header: "<organization> via RandiRandi" at the deployment's
    sender. The name is one line without control characters, and `formataddr`
    quotes or encodes whatever else it holds (quotes, commas, non-ASCII), so
    an organization's name cannot end the header or add one."""
    name = "".join(
        " " if ord(c) < 32 or ord(c) == 127 else c for c in organization.name
    )
    # "=?" would open an encoded word a mail program decodes into other text.
    name = " ".join(name.replace("=?", "= ?").split())
    return formataddr((f"{name} via RandiRandi", settings.SENDER.address))


def _reply_to(organization: Organization) -> list[str]:
    """The organization's contact address alone, or the mail is not built."""
    address = organization.contact_email
    try:
        validate_email(address)
    except ValidationError:
        raise ValueError("The organization has no single contact address.") from None
    if not re.fullmatch(PLAIN_ADDRESS, address):
        raise ValueError("The organization has no single contact address.")
    return [address]


def _message(
    *,
    organization: Organization,
    template: str,
    subject: str,
    recipient: str,
    context: dict[str, Any],
    reply: bool = False,
) -> EmailMultiAlternatives:
    """Every system mail is built here: from the deployment's sender in the
    organization's name; `reply` (the booking mails) makes the organization's
    contact address the Reply-To; the footer of every mail names that
    address."""
    if not isinstance(organization, Organization):
        raise TypeError("A mail is built for its organization.")
    footer = labels.word(
        organization, "email.contact", email=organization.contact_email
    )
    # Every message ends with the sign-off, a label like the rest of the words.
    logo_cid = f"logo@{settings.ORGANIZATION_DOMAIN}"
    context = {
        **context,
        "colour": COLOURS,
        "logo_cid": logo_cid,
        "lang": labels.language_of(organization),
        "sign_off": labels.word(organization, "email.sign-off"),
        "footer": footer,
    }
    text = render_to_string(f"core/email/{template}.txt", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=f"{text.rstrip()}\n\n{footer}\n",
        from_email=sender_header(organization),
        to=[recipient],
        reply_to=_reply_to(organization) if reply else None,
    )
    message.attach_alternative(
        render_to_string(f"core/email/{template}.html", context), "text/html"
    )
    mark = MIMEPart()
    mark.set_content(
        LOGO.read_bytes(),
        maintype="image",
        subtype="png",
        disposition="inline",
        cid=f"<{logo_cid}>",
    )
    message.attach(mark)
    return message


def sign_in_message(
    *, organization: Organization, recipient: str, code: str
) -> EmailMultiAlternatives:
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("A six-digit code is required.")
    return _message(
        organization=organization,
        template="sign_in",
        subject=labels.word(organization, "email.sign-in-subject"),
        recipient=recipient,
        context={
            "code": code,
            "words": {
                "heading": labels.word(organization, "email.sign-in-heading"),
                "lead": labels.word(organization, "email.sign-in-lead"),
                "code": labels.word(organization, "email.sign-in-code", code=code),
                "ignore": labels.word(organization, "email.sign-in-ignore"),
            },
        },
    )


def reset_message(
    *, organization: Organization, recipient: str, event_id: int
) -> EmailMultiAlternatives:
    if type(event_id) is not int or event_id < 1:
        raise ValueError("A positive audit reference is required.")
    return _message(
        organization=organization,
        template="reset",
        subject=labels.word(organization, "email.reset-subject"),
        recipient=recipient,
        context={
            "event_id": event_id,
            "words": {
                "title": labels.word(organization, "email.reset-title"),
                "heading": labels.word(organization, "email.reset-heading"),
                "body": labels.word(organization, "email.reset-body"),
                "reference": labels.word(
                    organization, "email.reference", event_id=event_id
                ),
                "not_expected": labels.word(
                    organization, "email.not-expected-operator"
                ),
            },
        },
    )


def admin_access_message(
    *, organization: Organization, recipient: str, event_id: int
) -> EmailMultiAlternatives:
    """To every administrator when a member gets admin access .
    Like the reset message it names no one: the audit reference is the way to
    the event."""
    if type(event_id) is not int or event_id < 1:
        raise ValueError("A positive audit reference is required.")
    return _message(
        organization=organization,
        template="admin_access",
        subject=labels.word(organization, "email.admin-access-subject"),
        recipient=recipient,
        context={
            "event_id": event_id,
            "words": {
                "heading": labels.word(organization, "email.admin-access-heading"),
                "body": labels.word(organization, "email.admin-access-body"),
                "reference": labels.word(
                    organization, "email.reference", event_id=event_id
                ),
                "not_expected": labels.word(
                    organization, "email.not-expected-operator"
                ),
            },
        },
    )


def email_changed_message(
    *, organization: Organization, recipient: str, event_id: int
) -> EmailMultiAlternatives:
    """To the address a member had before an administrator changed it."""
    if type(event_id) is not int or event_id < 1:
        raise ValueError("A positive audit reference is required.")
    return _message(
        organization=organization,
        template="email_changed",
        subject=labels.word(organization, "email.email-changed-subject"),
        recipient=recipient,
        context={
            "event_id": event_id,
            "words": {
                "heading": labels.word(organization, "email.email-changed-heading"),
                "body": labels.word(organization, "email.email-changed-body"),
                "reference": labels.word(
                    organization, "email.reference", event_id=event_id
                ),
                "not_expected": labels.word(organization, "email.not-expected"),
            },
        },
    )


def booking_message(
    *,
    organization: Organization,
    subject: str,
    recipient: str,
    context: dict[str, Any],
) -> EmailMultiAlternatives:
    """A message about the member's bookings: `heading`, `organization`,
    `facts` (lines under the organization), `days` (from
    `booking_emails.by_day`) and `after`, the closing lines. The plain text
    is not escaped; the HTML is. The subject is made one line: a line break
    in an organization's words is a space. Replies go to the organization."""
    return _message(
        organization=organization,
        template="booking",
        subject=" ".join(subject.split()),
        recipient=recipient,
        context=context,
        reply=True,
    )
