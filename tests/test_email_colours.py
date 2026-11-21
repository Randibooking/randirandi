# SPDX-License-Identifier: AGPL-3.0-only
"""The mails use scheme C's colours for the same roles, and no others."""

import re
from pathlib import Path

import pytest

from randirandi.core.emails import (
    COLOURS,
    admin_access_message,
    booking_message,
    email_changed_message,
    reset_message,
    sign_in_message,
)
from randirandi.core.models import Organization

ROOT = Path(__file__).resolve().parents[1]
THEME = (ROOT / "frontend/src/theme/themePrimitives.jsx").read_text()
TEMPLATES = ROOT / "randirandi/core/templates/core/email"
ORG = Organization(name="Made Up Club", contact_email="board@madeup.example", labels={})
OLD_NAVY = {"#080f1a", "#0d1726", "#334155", "#bac6d8", "#f5f7fa"}


def gray(step: int) -> str:
    found = re.search(
        rf"export const gray = \{{.*?\n\s*{step}: '(#[0-9a-f]{{6}})'", THEME, re.S
    )
    assert found
    return found.group(1)


def dark_scheme() -> str:
    return THEME[THEME.index("  dark: {") :]


def dark(pattern: str) -> str:
    found = re.search(pattern, dark_scheme())
    assert found, pattern
    value = found.group(1)
    return gray(int(value[5:-1])) if value.startswith("gray[") else value


def theme_value(role: str) -> str:
    return {
        "page": dark(r"background: \{\s*default: '(#[0-9a-f]{6})'"),
        "card": dark(r"paper: (gray\[\d+\])"),
        "card_edge": dark(r"divider: (gray\[\d+\])"),
        "text": dark(r"primary: '(#[0-9a-f]{6})'"),
        "text_quiet": dark(r"text: \{[^}]*secondary: (gray\[\d+\])"),
    }[role]


def luminance(colour: str) -> float:
    parts = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts
    )
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, other: str) -> float:
    high, low = sorted((luminance(one), luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def html_of(message: object) -> str:
    return str(message.alternatives[0].content)  # type: ignore[attr-defined]


def every_mail() -> list[str]:
    to = "fictional@example.test"
    day = {
        "date": "Mon 2 Nov",
        "lines": [
            {
                "time": "10:00",
                "what": "Sauna",
                "as": "Ola",
                "places": "2",
                "price": "kr 100",
            }
        ],
    }
    booking = {
        "heading": "Your booking",
        "organization": "Made Up Club",
        "facts": ["Paid"],
        "days": [day],
        "after": ["See you."],
    }
    return [
        html_of(sign_in_message(organization=ORG, recipient=to, code="012345")),
        html_of(reset_message(organization=ORG, recipient=to, event_id=7)),
        html_of(email_changed_message(organization=ORG, recipient=to, event_id=7)),
        html_of(admin_access_message(organization=ORG, recipient=to, event_id=7)),
        html_of(
            booking_message(
                organization=ORG, subject="Booked", recipient=to, context=booking
            )
        ),
    ]


@pytest.mark.parametrize("role", sorted(COLOURS))
def test_each_mail_colour_is_the_themes_value_for_its_role(role: str) -> None:
    assert COLOURS[role] == theme_value(role)


def test_the_mails_use_only_the_mapped_colours_and_no_old_navy() -> None:
    allowed = set(COLOURS.values())
    for html in every_mail():
        used = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", html))
        assert used <= allowed
        assert not used & OLD_NAVY
        assert allowed - {COLOURS["card_edge"]} <= used | {COLOURS["card_edge"]}
    for template in TEMPLATES.glob("*.html"):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", template.read_text()), template


def test_every_mail_text_passes_aa_on_the_card_it_sits_on() -> None:
    for role in ("text", "text_quiet"):
        assert contrast(COLOURS[role], COLOURS["card"]) >= 4.5
        assert contrast(COLOURS[role], COLOURS["page"]) >= 4.5
