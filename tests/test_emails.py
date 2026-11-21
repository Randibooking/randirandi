# SPDX-License-Identifier: AGPL-3.0-only
"""Delivered MIME content contains only required information and local branding."""

import email.policy
import html as markup
from collections.abc import Callable
from email import message_from_bytes
from html.parser import HTMLParser

import pytest
from django.conf import settings

from randirandi.core.emails import (
    LOGO,
    admin_access_message,
    reset_message,
    sign_in_message,
)
from randirandi.core.models import Organization

ORG = Organization(
    name="Fictional Club", contact_email="board@fictional.example", labels={}
)
FOOTER = "Questions? Write to board@fictional.example."


class Resources(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []
        self.forbidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "link", "iframe", "form"}:
            self.forbidden.append(tag)
        for name, value in attrs:
            if name in {"src", "href", "srcset", "background"} and value:
                self.sources.append(value)


@pytest.mark.parametrize("kind", ["login", "reset", "admin-access"])
def test_actual_mime_contains_plain_html_and_exact_local_mark(kind: str) -> None:
    message = (
        sign_in_message(
            organization=ORG, recipient="fictional@example.test", code="012345"
        )
        if kind == "login"
        else admin_access_message(
            organization=ORG, recipient="fictional@example.test", event_id=987
        )
        if kind == "admin-access"
        else reset_message(
            organization=ORG, recipient="fictional@example.test", event_id=987
        )
    )
    parsed = message_from_bytes(
        message.message().as_bytes(policy=email.policy.SMTP),
        policy=email.policy.default,
    )
    assert str(parsed["To"]) == "fictional@example.test"
    assert parsed["Cc"] is None and parsed["Bcc"] is None
    parts = [part for part in parsed.walk() if not part.is_multipart()]
    assert [part.get_content_type() for part in parts] == [
        "text/plain",
        "text/html",
        "image/png",
    ]
    plain_bytes = parts[0].get_payload(decode=True)
    html_bytes = parts[1].get_payload(decode=True)
    assert isinstance(plain_bytes, bytes) and isinstance(html_bytes, bytes)
    plain, html = plain_bytes.decode(), html_bytes.decode()
    for text in (plain, markup.unescape(html)):
        assert "fictional@example.test" not in text
        assert "RandiRandi" in text
        assert ("012345" if kind == "login" else "987") in text
        # Every message ends with Randi's sign-off, on its own line.
        assert "\N{EN DASH} Randi" in text
    if kind == "login":
        for text in (plain, markup.unescape(html)):
            assert "10 minutes" in text and "browser you asked from" in text
            assert "Didn't ask for it? Ignore this email" in text
            assert "don't share the code." in text
    # The sign-off, then the footer naming the organization's address.
    assert plain.rstrip().endswith(FOOTER)
    assert plain.index("\N{EN DASH} Randi") < plain.index(FOOTER)
    parser = Resources()
    parser.feed(html)
    assert parser.forbidden == []
    assert parser.sources == [f"cid:logo@{settings.ORGANIZATION_DOMAIN}"]
    assert parts[2].get_payload(decode=True) == LOGO.read_bytes()
    assert parts[2]["Content-ID"] == f"<logo@{settings.ORGANIZATION_DOMAIN}>"
    assert parts[2].get_content_disposition() == "inline"
    assert len(message.message().as_bytes()) < 50_000


@pytest.mark.parametrize("code", ["12345", "1234567", "١٢٣٤٥٦", "<script>", "12345\n"])
def test_templates_refuse_anything_except_the_six_digit_code(code: str) -> None:
    with pytest.raises(ValueError, match="six-digit"):
        sign_in_message(organization=ORG, recipient="fictional@example.test", code=code)


@pytest.mark.parametrize("event_id", [0, -1, True])
@pytest.mark.parametrize("build", [reset_message, admin_access_message])
def test_reset_template_requires_a_positive_audit_reference(
    build: Callable[..., object], event_id: int
) -> None:
    with pytest.raises(ValueError, match="positive audit"):
        build(organization=ORG, recipient="fictional@example.test", event_id=event_id)
