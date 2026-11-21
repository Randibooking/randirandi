# SPDX-License-Identifier: AGPL-3.0-only
"""What a person signing in reads is the catalogue's text in the
organization's language : the sign-in pages, the four mails,
the pages without JavaScript, the dates a member reads, the counted texts, and
the escaping of a text. English (an organization in `en`, and an error page
whose address names no organization) reads as before."""

import html
from collections.abc import Iterator
from datetime import UTC, date, datetime
from unittest import mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.core.exceptions import DisallowedHost, ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError
from django.http import Http404, HttpRequest
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.utils.safestring import mark_safe

from randirandi import labels
from randirandi.core import (
    booking_emails,
    emails,
    login,
    login_session_views,
    presentation,
    price_table,
    validator,
)
from randirandi.core.models import Organization

LANGUAGE = "yy"


@pytest.fixture(autouse=True)
def pages() -> Iterator[None]:
    with override_settings(ALLOWED_HOSTS=["*"]):
        yield


@pytest.fixture
def wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete second language, `yy`: every text of English between « and »,
    so any English outside the marks is a text the catalogue does not hold."""
    monkeypatch.setitem(
        labels.CATALOGUES,
        LANGUAGE,
        {key: f"«{text}»" for key, text in labels.DEFAULTS.items()},
    )


def speaking(
    own: dict[str, str] | None = None, language: str = LANGUAGE
) -> Organization:
    organization = Organization(
        name="Fjord & Co",
        contact_email="post@fjord.example",
        language=language,
        labels=own or {},
    )
    organization.pk = 7
    return organization


def request(
    method: str = "get",
    data: dict[str, str] | None = None,
    session: dict[str, object] | None = None,
) -> HttpRequest:
    made: HttpRequest = getattr(RequestFactory(), method)(
        "/login/", data or {}, HTTP_HOST="fjord.localhost"
    )
    made.user = AnonymousUser()
    made.session = SessionStore()
    made.session.update(session or {})
    return made


def sign_in(organization: Organization, **post: str) -> str:
    with (
        mock.patch.object(login, "organization_for_request", return_value=organization),
        mock.patch.object(login, "record_refusal"),
    ):
        asked = request("post", post) if post else request()
        return login.login(asked).content.decode()


def code_page(organization: Organization, wrong: bool = False) -> str:
    session = {
        "login_organization": organization.pk,
        "login_browser": "b" * 40,
        "login_challenge": "c" * 32,
    }
    with (
        mock.patch.object(
            login_session_views, "organization_for_request", return_value=organization
        ),
        mock.patch.object(login_session_views, "record_refusal"),
        mock.patch.object(login_session_views, "verify_code", return_value=None),
    ):
        asked = (
            request("post", {"code": "123456"}, session)
            if wrong
            else request(session=session)
        )
        return login_session_views.email_code(asked).content.decode()


def four_mails(organization: Organization) -> dict[str, EmailMultiAlternatives]:
    to = "m@example.org"
    return {
        "sign_in": emails.sign_in_message(
            organization=organization, recipient=to, code="123456"
        ),
        "reset": emails.reset_message(
            organization=organization, recipient=to, event_id=42
        ),
        "admin_access": emails.admin_access_message(
            organization=organization, recipient=to, event_id=42
        ),
        "email_changed": emails.email_changed_message(
            organization=organization, recipient=to, event_id=42
        ),
    }


def html_of(message: EmailMultiAlternatives) -> str:
    return str(message.alternatives[0][0])


# --- The sign-in pages ------------------------------------------------------------


def test_the_sign_in_page_is_in_the_organizations_language(wrapped: None) -> None:
    body = sign_in(speaking())

    assert '<html lang="yy">' in body
    assert "<title>«Sign in · Fjord &amp; Co · RandiRandi»</title>" in body
    assert "<h1>«Sign in»</h1>" in body
    assert "«Email»</label>" in body
    assert ">«Continue»</button>" in body
    assert "«What&#x27;s my login?»" in body
    # The address is a link inside the text, which is escaped around it.
    assert (
        "«Enter your mobile number with its country code instead. When you are "
        "registered, we send your code by email. If not, contact us: "
        '<a href="mailto:post@fjord.example">post@fjord.example</a>.»'
    ) in body
    for english in ("Continue</button>", "<h1>Sign in</h1>", "Email</label>"):
        assert english not in body


def test_the_sign_in_refusals_are_in_the_organizations_language(wrapped: None) -> None:
    assert "«Enter your email or phone number.»" in sign_in(speaking(), identifier="")
    assert "«Enter a valid email address.»" in sign_in(speaking(), identifier="a@@b")
    assert (
        "«Enter an email address or a phone number with its country code.»"
        in sign_in(speaking(), identifier="12")
    )


def test_the_code_page_and_its_refusal_are_in_the_organizations_language(
    wrapped: None,
) -> None:
    body = code_page(speaking())

    assert "<h1>«Check your email»</h1>" in body
    assert "«Email code»</label>" in body
    assert ">«Sign in»</button>" in body
    assert ">«Request a new code»</a>" in body
    assert "<title>«Enter code · RandiRandi»</title>" in body
    wrong = code_page(speaking(), wrong=True)
    assert (
        "«That code could not be accepted. Try again or request a new code.»" in wrong
    )
    # The page carries the words for the browser's own sign-in card.
    assert '"login.title": "\\u00abSign in\\u00bb"' in wrong


def test_an_english_organization_reads_the_sign_in_as_before() -> None:
    body = sign_in(speaking(language="en"))

    assert '<html lang="en">' in body
    assert "<h1>Sign in</h1>" in body
    assert ">Continue</button>" in body
    assert "What&#x27;s my login?" in body
    assert "Enter your mobile number with its country code instead." in body
    assert "Enter your email or phone number." in sign_in(
        speaking(language="en"), identifier=""
    )


@pytest.mark.parametrize(
    ("status", "title", "message"),
    [
        (400, "We could not read that request", "Check the address and try again."),
        (
            403,
            "Please try again",
            "This request could not be accepted. Open sign in and try again.",
        ),
        (404, "Page not found", "Check the address supplied by your organization."),
        (500, "Something went wrong", "Please try again later."),
    ],
)
def test_the_error_pages_of_an_english_organization_read_as_before(
    status: int, title: str, message: str
) -> None:
    body = error_body(status, speaking(language="en"))

    assert f"<h1>{title}</h1>" in body
    assert f"<p>{message}</p>" in body
    assert ">Back to sign in</a>" in body
    assert '<html lang="en">' in body


# --- The four mails --------------------------------------------------------------

ENGLISH_MAILS = {
    "sign_in": (
        "Your RandiRandi sign-in code",
        "Here's your code: 123456",
        "Type it in the browser you asked from. It works for 10 minutes.",
    ),
    "reset": (
        "RandiRandi administrator authenticator reset",
        "An administrator's authenticator was reset for your organization.\n"
        "Audit reference: 42.",
        "Not expected? Tell your administrator or operator right away.",
    ),
    "admin_access": (
        "RandiRandi administrator access given",
        "A member was given administrator access to your organization.\n"
        "Audit reference: 42.",
        "Not expected? Tell your administrator or operator right away.",
    ),
    "email_changed": (
        "Your RandiRandi email address was changed",
        "An administrator changed the email address on your RandiRandi account. "
        "This address is no longer the one we have for you.\nAudit reference: 42.",
        "Not expected? Tell your administrator right away.",
    ),
}


def test_the_four_mails_read_as_before_in_english() -> None:
    for name, message in four_mails(speaking(language="en")).items():
        subject, first, last = ENGLISH_MAILS[name]
        page = html.unescape(html_of(message))
        assert message.subject == subject
        assert first in message.body
        assert last in message.body
        # The HTML says the same (a quote written as an entity); the code line
        # of the sign-in mail is its heading and the code on the page.
        assert last in page
        assert first.split("\n")[0] in page or name == "sign_in"


def test_the_four_mails_are_in_the_organizations_language(wrapped: None) -> None:
    for name, message in four_mails(speaking()).items():
        subject, first, last = ENGLISH_MAILS[name]
        page = html.unescape(html_of(message))
        assert message.subject == f"«{subject}»"
        # The plain text and the HTML each hold the text once, between the marks.
        for line in first.split("\n"):
            assert f"«{line}»" in message.body
        assert f"«{last}»" in message.body
        assert f"«{last}»" in page
        assert "«\N{EN DASH} Randi»" in message.body and "«\N{EN DASH} Randi»" in page
        if name != "sign_in":
            assert f"«{first.split(chr(10))[0]}»" in page


# --- Escaping ---------------------------------------------------------------------


def test_a_text_with_html_in_it_is_shown_as_written_in_a_page(wrapped: None) -> None:
    own = {"login.title": "Sign <in> & out", "login.continue": "Go <b>&</b>"}
    body = sign_in(speaking(own))

    assert "<h1>Sign &lt;in&gt; &amp; out</h1>" in body
    assert ">Go &lt;b&gt;&amp;&lt;/b&gt;</button>" in body
    assert "<in>" not in body
    assert "<b>&</b>" not in body


def test_a_text_with_html_in_it_is_escaped_in_html_mail_and_not_in_plain_text(
    wrapped: None,
) -> None:
    own = {
        "email.sign-in-ignore": "Ignore <this> & that",
        "email.sign-in-code": "Code <&>: {code}",
        "email.sign-in-heading": "Hi <b> & co",
    }
    message = emails.sign_in_message(
        organization=speaking(own), recipient="m@example.org", code="123456"
    )
    page = html_of(message)

    assert "Ignore &lt;this&gt; &amp; that" in page
    assert "Hi &lt;b&gt; &amp; co" in page
    assert "<this>" not in page
    assert "<b> &" not in page
    assert "Ignore <this> & that" in message.body
    assert "Code <&>: 123456" in message.body
    assert "&lt;" not in message.body and "&amp;" not in message.body


def test_a_placeholder_is_filled_after_the_escaping() -> None:
    organization = speaking(language="en", own={"login.help-page": "A & B: {address}."})
    link = mark_safe('<a href="mailto:a@b.c">a@b.c</a>')

    assert str(labels.word_html(organization, "login.help-page", address=link)) == (
        'A &amp; B: <a href="mailto:a@b.c">a@b.c</a>.'
    )
    # A value that is not safe is escaped as it goes in, and not filled again.
    assert (
        str(labels.word_html(organization, "login.help-page", address="<x>{address}"))
        == "A &amp; B: &lt;x&gt;{address}."
    )
    # A placeholder without a value stays as written, as `word` leaves it.
    assert (
        str(labels.word_html(organization, "login.help-page"))
        == "A &amp; B: {address}."
    )
    assert labels.word(organization, "login.help-page", address="<x>") == "A & B: <x>."


# --- Dates ------------------------------------------------------------------------


def test_a_date_a_member_reads_is_the_catalogues(wrapped: None) -> None:
    monday = datetime(2027, 3, 1, 16, 0, tzinfo=UTC)

    # The pattern is a catalogue text too, so the marking language marks it.
    assert booking_emails.date_words(speaking(), monday) == "««Monday» 1 «March»»"
    assert (
        booking_emails.date_words(speaking(language="en"), monday) == "Monday 1 March"
    )
    assert (
        labels.date_words(speaking(), monday, year=True) == "««Monday» 1 «March»» 2027"
    )
    # Norwegian writes a dot after the day; the pattern is the catalogue's.
    assert labels.date_words(speaking(language="nb"), monday) == "mandag 1. mars"
    assert (
        labels.date_words(speaking(language="nb"), monday, year=True)
        == "mandag 1. mars 2027"
    )


def test_a_date_only_an_administrator_reads_stays_english(wrapped: None) -> None:
    # The refusals of the container's validator and the container screen are
    # English sentences: the date in them is the product's own English from
    # the same catalogue, whatever language the organization speaks.
    assert validator._day(date(2027, 3, 1)) == "Monday 1 March 2027"
    assert validator._day(date(2026, 12, 31)) == "Thursday 31 December 2026"
    assert labels.date_words(
        None, datetime(2027, 3, 1, 9, 30, tzinfo=UTC), year=True
    ) == ("Monday 1 March 2027")


# --- Counted texts ----------------------------------------------------------------

# count, form, then "line.places" and "length.hours" for it: the table the
# browser's test (frontend/src/book/plural.test.js) holds the same rows of.
COUNTED = [
    (0, "other", "0 \N{MULTIPLICATION SIGN} bed", "0 h"),
    (1, "one", "1 \N{MULTIPLICATION SIGN} bed", "1 h"),
    (2, "other", "2 \N{MULTIPLICATION SIGN} bed", "2 h"),
]


@pytest.mark.parametrize(("count", "form", "places", "hours"), COUNTED)
def test_a_counted_text_has_one_form_for_1_and_other_for_the_rest(
    count: int, form: str, places: str, hours: str
) -> None:
    english = speaking(language="en")

    assert labels.plural_form(count) == form
    assert labels.plural(english, "line.places", count, word="bed") == places
    assert labels.plural(english, "filter.quantity-chip", count, word="bed") == places
    assert labels.plural(english, "length.hours", count) == hours
    assert labels.plural(None, "length.hours", count) == hours


def test_a_counted_text_takes_its_forms_from_the_organizations_language(
    wrapped: None,
) -> None:
    assert labels.plural(speaking(), "length.hours", 1) == "«1 h»"
    assert labels.plural(speaking(), "length.hours", 2) == "«2 h»"
    own = {"length.hours.one": "{count} time", "length.hours.other": "{count} timer"}
    held = speaking(own)
    assert labels.plural(held, "length.hours", 1) == "1 time"
    assert labels.plural(held, "length.hours", 0) == "0 timer"
    assert labels.plural(held, "length.hours", 2) == "2 timer"
    assert price_table.Words(held).plural("length.hours", 1) == "1 time"


def test_the_old_counted_keys_are_gone() -> None:
    for key in ("line.places", "filter.quantity-chip", "length.hours"):
        assert key not in labels.DEFAULTS
        assert f"{key}.one" in labels.DEFAULTS and f"{key}.other" in labels.DEFAULTS


# --- The error pages  ---------------------------------------------------


def error_body(status: int, organization: Organization | None) -> str:
    with mock.patch.object(
        presentation, "organization_for_request", return_value=organization
    ):
        return presentation.error_page(request(), status).content.decode()


def test_an_error_page_is_in_the_language_of_the_organization_the_host_names(
    wrapped: None,
) -> None:
    for status in (400, 403, 404, 500):
        body = error_body(status, speaking())
        title = labels.DEFAULTS[f"login.error-{status}-title"]
        assert f"<h1>«{title}»</h1>" in body
        assert ">«Back to sign in»</a>" in body
        assert '<html lang="yy">' in body
        # The page names no contact, as before.
        assert "fallback-beta" not in body


@pytest.mark.parametrize("error", [Http404(), DisallowedHost(), DatabaseError("down")])
def test_an_error_page_is_english_when_the_host_names_no_organization(
    wrapped: None, error: Exception
) -> None:
    with mock.patch.object(presentation, "organization_for_request", side_effect=error):
        body = presentation.error_page(request(), 404).content.decode()

    assert "<h1>Page not found</h1>" in body
    assert ">Back to sign in</a>" in body
    assert '<html lang="en">' in body


# --- The pages without JavaScript -------------------------------------------------


def draw(
    template: str,
    organization: Organization,
    page: dict[str, object] | None = None,
) -> str:
    asked = request()
    ui = {
        **presentation.browser_context(asked, organization),
        "organization": organization.name,
        "screen": "x",
        "admin": True,
    }
    return render_to_string(
        template,
        {"ui": ui, "word_organization": organization, "member": True, **(page or {})},
        request=asked,
    )


@pytest.mark.parametrize(
    ("template", "heading"),
    [
        ("core/book.html", "Book"),
        ("core/cart.html", "Cart"),
        ("core/order.html", "Booking"),
        ("core/prices.html", "Prices"),
        ("core/events.html", "Events"),
        ("core/me.html", "My page"),
    ],
)
def test_a_member_page_without_javascript_is_in_the_organizations_language(
    wrapped: None, template: str, heading: str
) -> None:
    body = draw(template, speaking())

    assert f"<h1>«{heading}»</h1>" in body
    assert f"<title>««{heading}» · «RandiRandi»»</title>" in body
    for place in ("Book", "Events", "My page"):
        assert f"«{place}»</a>" in body
        assert f">{place}</a>" not in body
    assert '<html lang="yy">' in body
    if template == "core/me.html":
        assert ">«Admin»</a>" in body and ">«Sign out»</button>" in body
        assert ">Sign out</button>" not in body


def test_the_signed_in_page_is_in_the_organizations_language(wrapped: None) -> None:
    body = draw("core/home.html", speaking())

    assert "<h1>«Signed in»</h1>" in body
    assert ">«Sign out»</button>" in body


PRICES = {
    "name": "Prices",
    "categories": [{"name": "Members"}],
    "types": [
        {
            "name": "Sauna",
            "blocks": [
                {
                    "days": "Every day",
                    "hours": "09:00-10:00",
                    "cells": [{"offered": False}],
                    "examples": [{"hours": "09:00", "length": "1 h", "totals": [None]}],
                }
            ],
        }
    ],
}


def test_the_public_price_list_without_javascript_is_in_the_organizations_language(
    wrapped: None,
) -> None:
    body = draw(
        "core/public_prices.html", speaking(), {"table": PRICES, "member": False}
    )

    assert "<h1>«Prices»</h1>" in body
    assert "<title>««Prices» · «RandiRandi»»</title>" in body
    assert '<span class="fallback-hidden">«Price»</span>' in body
    assert body.count("«Not offered»") == 2
    assert ">«For example»</td>" in body
    assert '<html lang="yy">' in body


def test_the_public_price_list_links_sign_in_in_the_organizations_language(
    wrapped: None,
) -> None:
    asked = request()
    body = render_to_string(
        "core/public_prices.html",
        {
            "ui": {**presentation.browser_context(asked, speaking()), "screen": "x"},
            "word_organization": speaking(),
            "member": False,
            "table": None,
        },
        request=asked,
    )

    assert ">«Sign in»</a>" in body


def test_a_mail_declares_the_language_of_the_organization(wrapped: None) -> None:
    assert '<html lang="yy">' in html_of(four_mails(speaking())["sign_in"])
    assert '<html lang="en">' in html_of(four_mails(speaking(language="en"))["sign_in"])


def test_the_word_tag_fails_when_the_view_names_no_organization(wrapped: None) -> None:
    asked = request()
    ui = {**presentation.browser_context(asked, speaking()), "screen": "x"}

    with pytest.raises(ImproperlyConfigured, match="word_organization"):
        render_to_string("core/me.html", {"ui": ui}, request=asked)


def test_the_word_tag_reads_none_as_the_products_own_english(wrapped: None) -> None:
    asked = request()
    ui = {**presentation.browser_context(asked, None), "screen": "x"}

    body = render_to_string(
        "core/me.html", {"ui": ui, "word_organization": None}, request=asked
    )

    assert "<h1>My page</h1>" in body
    assert ">Sign out</button>" in body
