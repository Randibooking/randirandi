# SPDX-License-Identifier: AGPL-3.0-only
"""The system mails in the organization's name : the From, the Reply-To of the booking
mails only, the footer, a header an
organization's name cannot break, and no mail without its organization."""

import re
from collections.abc import Callable
from typing import Any

import pytest
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from randirandi.core.emails import (
    admin_access_message,
    booking_message,
    email_changed_message,
    reset_message,
    sender_header,
    sign_in_message,
)
from randirandi.core.models import Organization
from tests.mail_checks import check_system_mail

TO = "fictional@example.test"
BOOKING = {
    "heading": "Your booking",
    "organization": "Fictional Club",
    "facts": [],
    "days": [],
    "after": [],
}


def org(name: str = "Fictional Club", contact: str = "board@fictional.example") -> Any:
    return Organization(name=name, contact_email=contact, labels={})


KINDS: dict[str, tuple[Callable[[Organization], EmailMultiAlternatives], bool]] = {
    "sign-in": (
        lambda o: sign_in_message(organization=o, recipient=TO, code="012345"),
        False,
    ),
    "reset": (lambda o: reset_message(organization=o, recipient=TO, event_id=7), False),
    "admin-access": (
        lambda o: admin_access_message(organization=o, recipient=TO, event_id=7),
        False,
    ),
    "email-changed": (
        lambda o: email_changed_message(organization=o, recipient=TO, event_id=7),
        False,
    ),
    "booking": (
        lambda o: booking_message(
            organization=o, subject="Booked", recipient=TO, context=BOOKING
        ),
        True,
    ),
}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_kind_has_the_from_the_reply_to_and_the_footer(kind: str) -> None:
    build, reply = KINDS[kind]
    organization = org()
    message = build(organization)
    found = check_system_mail(message, organization, reply=reply)
    assert str(found["From"]) == (
        f"Fictional Club via RandiRandi <{settings.SENDER.address}>"
    )
    assert message.from_email == str(found["From"])
    # The footer is in both texts, under the sign-off.
    assert "Questions? Write to board@fictional.example." in message.body


def test_the_footer_is_the_organizations_own_words() -> None:
    organization = org()
    organization.labels = {"email.contact": "Write to {email} if in doubt."}
    message = KINDS["sign-in"][0](organization)
    assert message.body.rstrip().endswith(
        "Write to board@fictional.example if in doubt."
    )
    assert "Questions?" not in message.body


HOSTILE = [
    'The "Quoted" Co-op',
    "Hall, Hill and Sons, Ltd.",
    "Jørgen & Åse's Båtlag \N{SNOWMAN}",
    "Co-op\r\nBcc: spy@example.test",
    "Co-op\nSubject: forged",
    "Co-op\rX-Injected: 1",
    "Co-op" + chr(0x2028) + "Bcc: spy@example.test",
    "Co-op\x00\x1b[31m",
    "Evil <spy@example.test>",
    "a@b.example, c@d.example",
    "\\",
    "=?utf-8?b?Zm9v?=",
    "x" * 200,
]


@pytest.mark.parametrize("name", HOSTILE)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_an_organization_name_cannot_break_or_add_a_header(
    name: str, kind: str
) -> None:
    build, reply = KINDS[kind]
    organization = org(name)
    message = build(organization)
    raw = message.message().as_bytes().decode("utf-8", "surrogateescape")
    header_block = raw.split("\n\n", 1)[0]
    # No header the code did not write.
    names = re.findall(r"^([A-Za-z-]+):", header_block, re.M)
    assert not {"Bcc", "Cc", "X-Injected"} & set(names)
    assert names.count("From") == 1 and names.count("Subject") == 1
    assert names.count("Reply-To") == (1 if reply else 0)
    found = check_system_mail(message, organization, reply=reply)
    (sender,) = found["From"].addresses
    # The sender stays the deployment's, whatever the name holds.
    assert sender.addr_spec == settings.SENDER.address
    assert found["To"].addresses[0].addr_spec == TO
    assert len(found["To"].addresses) == 1


def test_the_header_is_built_by_one_function() -> None:
    assert sender_header(org("A\nB")) == (
        f"A B via RandiRandi <{settings.SENDER.address}>"
    )


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_mail_cannot_be_built_without_its_organization(kind: str) -> None:
    build, _ = KINDS[kind]
    with pytest.raises(TypeError):
        build(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build("Fictional Club")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call",
    [
        lambda: sign_in_message(recipient=TO, code="012345"),  # type: ignore[call-arg]
        lambda: reset_message(recipient=TO, event_id=7),  # type: ignore[call-arg]
        lambda: admin_access_message(recipient=TO, event_id=7),  # type: ignore[call-arg]
        lambda: email_changed_message(recipient=TO, event_id=7),  # type: ignore[call-arg]
        lambda: booking_message(subject="s", recipient=TO, context=BOOKING),  # type: ignore[call-arg]
    ],
)
def test_the_builders_take_the_organization_as_a_required_argument(
    call: Callable[[], object],
) -> None:
    with pytest.raises(TypeError, match="organization"):
        call()


@pytest.mark.parametrize(
    "contact",
    [
        '"a b"@example.org',
        "a@localhost",
        "a..b@example.org",
        "",
        "a@b.example, c@d.example",
        "Name <a@b.example>",
        "a@b.example\r\nBcc: x@y.example",
    ],
)
def test_a_booking_mail_is_not_built_for_a_contact_that_is_not_one_address(
    contact: str,
) -> None:
    with pytest.raises(ValueError, match="contact address"):
        KINDS["booking"][0](org(contact=contact))
