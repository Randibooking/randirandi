# SPDX-License-Identifier: AGPL-3.0-only
"""The booking conditions of an organization .

Every organization has them from its first day: the product's standard text,
version `VERSION`, as labels (`randirandi/locale/en.json`, `conditions.*`), with
the organization's name, contact email and days of free cancellation filled
in. The words are the organization's own where it set labels. `current` is the
text in force; its `digest` is what the cart shows and an order carries as
`accepted_version`. At acceptance `freeze` keeps the filled-in text as one row
for each distinct text, and the order points to it.

The cancelling rule is the administrator's to apply: `free_until` is the
moment free cancelling ended for a booked time, which the cancel dialog shows.
The product never cancels or refunds on its own.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from randirandi import labels
from randirandi.core.models import BookingConditions, Organization

# The version of the standard text. Raise it when the words of the standard
# text change; a deploy ships it .
VERSION = 1


@dataclass(frozen=True)
class Text:
    """One filled-in text of the conditions."""

    version: int
    organization_name: str
    contact_email: str
    days: int
    title: str
    intro: str
    # Heading and text, in the order members read them.
    sections: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        """SHA-256 of the whole text with its values: its identifier."""
        whole = json.dumps(
            [
                self.version,
                self.organization_name,
                self.contact_email,
                self.days,
                self.title,
                self.intro,
                [list(section) for section in self.sections],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(whole.encode("utf-8")).hexdigest()

    def page(self) -> dict[str, object]:
        """What the page shows: the identifier, the title, the intro and the
        sections. No personal data."""
        return {
            "version": self.digest,
            "title": self.title,
            "intro": self.intro,
            "sections": [
                {"heading": heading, "text": text} for heading, text in self.sections
            ],
        }


def current(organization: Organization) -> Text:
    """The text in force for the organization, filled in from its row as it
    is now."""
    values = {
        "organization": organization.name,
        "contact": organization.contact_email,
        "days": organization.free_cancellation_days,
    }
    return Text(
        version=VERSION,
        organization_name=organization.name,
        contact_email=organization.contact_email,
        days=organization.free_cancellation_days,
        title=labels.word(organization, "conditions.title"),
        intro=labels.word(organization, "conditions.intro", **values),
        sections=(
            (
                labels.word(organization, "conditions.payment-heading"),
                labels.word(organization, "conditions.payment", **values),
            ),
            (
                labels.word(organization, "conditions.cancelling-heading"),
                labels.word(organization, "conditions.cancelling", **values),
            ),
            (
                labels.word(organization, "conditions.we-cancel-heading"),
                labels.word(organization, "conditions.we-cancel", **values),
            ),
            (
                labels.word(organization, "conditions.using-heading"),
                labels.word(organization, "conditions.using", **values),
            ),
            (
                labels.word(organization, "conditions.damage-heading"),
                labels.word(organization, "conditions.damage", **values),
            ),
            (
                labels.word(organization, "conditions.changes-heading"),
                labels.word(organization, "conditions.changes", **values),
            ),
            (
                labels.word(organization, "conditions.questions-heading"),
                labels.word(organization, "conditions.questions", **values),
            ),
        ),
    )


def kept(row: BookingConditions) -> Text:
    """A frozen text as it was accepted."""
    return Text(
        version=row.standard_version,
        organization_name=row.organization_name,
        contact_email=row.contact_email,
        days=row.free_cancellation_days,
        title=row.title,
        intro=row.intro,
        sections=tuple((str(heading), str(text)) for heading, text in row.sections),
    )


def freeze(
    organization: Organization, text: Text, now: datetime | None = None
) -> BookingConditions:
    """The row that keeps this text for the organization, made when it is
    the first to accept it. Call it inside the order's transaction, where the
    organization's row is locked, so two first acceptances do not both make
    it."""
    found = (
        BookingConditions.objects.of(organization).filter(digest=text.digest).first()
    )
    if found is not None:
        return found
    return BookingConditions.objects.create(
        organization=organization,
        standard_version=text.version,
        digest=text.digest,
        organization_name=text.organization_name,
        contact_email=text.contact_email,
        free_cancellation_days=text.days,
        title=text.title,
        intro=text.intro,
        sections=[list(section) for section in text.sections],
        created_at=now if now is not None else timezone.now(),
    )


def free_until(starts_at: datetime, days: int) -> datetime:
    """The moment free cancelling of a booked time ended: `days` times 24
    hours before the time starts. What counts is when the member sent the
    email, not when the administrator acts."""
    return starts_at - timedelta(days=days)
