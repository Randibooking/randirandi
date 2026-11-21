# SPDX-License-Identifier: AGPL-3.0-only
"""What every system mail is : from the deployment's sender
in the organization's name, the footer naming the organization's contact
address once in each text, and Reply-To that address on the booking mails only."""

import email.policy
import html as markup
from email import message_from_bytes
from email.message import EmailMessage as Parsed
from typing import Any

from django.conf import settings
from django.core.mail import EmailMessage

from randirandi import labels


def parsed(message: EmailMessage) -> Parsed:
    found = message_from_bytes(
        message.message().as_bytes(policy=email.policy.SMTP),
        policy=email.policy.default,
    )
    assert isinstance(found, Parsed)
    return found


def name_of(organization: Any) -> str:
    """The organization's name as the header carries it: one line."""
    return " ".join(
        "".join(
            c if c.isprintable() else " "
            for c in organization.name.replace("=?", "= ?")
        ).split()
    )


def check_system_mail(
    message: EmailMessage, organization: Any, *, reply: bool
) -> Parsed:
    """From, Reply-To and the footer of a built message; returns it parsed."""
    found = parsed(message)
    assert len(found.get_all("From") or []) == 1
    (sender,) = found["From"].addresses
    assert sender.display_name == f"{name_of(organization)} via RandiRandi"
    assert sender.addr_spec == settings.SENDER.address
    assert found["Bcc"] is None and found["Cc"] is None
    if reply:
        assert len(found.get_all("Reply-To") or []) == 1
        (back,) = found["Reply-To"].addresses
        assert back.addr_spec == organization.contact_email
    else:
        assert found["Reply-To"] is None
    footer = labels.word(
        organization, "email.contact", email=organization.contact_email
    )
    plain = message.body
    html = str(getattr(message, "alternatives")[0].content)  # noqa: B009
    assert plain.rstrip().endswith(footer)
    assert plain.count(footer) == 1
    assert markup.unescape(html).count(footer) == 1
    return found
