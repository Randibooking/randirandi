# SPDX-License-Identifier: AGPL-3.0-only
"""The label set: every word the member pages show, by key ."""

import json
import re
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from randirandi import labels
from randirandi.core import booking_words
from randirandi.core.models import Organization
from tests import made_up

FILE = Path(labels.__file__).parent / "locale" / "en.json"
# The namespaces of the words the server writes for a member (`day-limit`:
# the refusal's counted texts, `booking_words.DAY_LIMIT_TEXTS`).
WRITTEN = ("calendar", "email", "event", "conditions", "login", "day-limit")


def organization(words: object, language: str = "en") -> Organization:
    """An organization, to the resolver: its language and its own words."""
    return Organization(language=language, labels=words)


def test_the_label_set_is_one_file_of_keys_and_their_default_english_texts() -> None:
    held = json.loads(FILE.read_text(encoding="utf-8"))

    assert held == labels.DEFAULTS
    assert held
    for key, text in held.items():
        assert isinstance(key, str)
        assert key == key.strip() and " " not in key, key
        assert isinstance(text, str)
        assert text and text == text.strip(), key
        assert len(text) <= labels.LONGEST, key


def test_a_default_is_a_text_the_reader_accepts_for_its_own_key() -> None:
    for key, text in labels.DEFAULTS.items():
        assert labels.fault("en", key, text) is None, key


def test_a_placeholder_is_a_name_in_braces() -> None:
    assert labels.placeholders("From {time} for {length} or {time}") == {
        "time",
        "length",
    }
    assert labels.placeholders("No names, { braces } or {1}") == frozenset()


def test_the_booking_words_default_texts_are_the_files() -> None:
    keys = [
        booking_words.NOT_OFFERED,
        booking_words.TAKEN,
        booking_words.BESIDE_CART,
        booking_words.STARTED,
        booking_words.PRICE_CHANGED,
        booking_words.CART_FULL,
        booking_words.OTHER_CURRENCY,
        booking_words.ORDER_HELD,
        booking_words.NOT_READY,
        booking_words.CART_EMPTY,
        booking_words.TOO_LARGE,
        booking_words.BUSY,
        booking_words.TOO_FAR_AHEAD,
        booking_words.CONDITIONS_NOT_ACCEPTED,
        booking_words.CONDITIONS_CHANGED,
        booking_words.HELD,
        booking_words.CONFIRMED,
        booking_words.CANCELLED,
        booking_words.NOT_COMPLETED,
        booking_words.SETTLING,
    ]

    assert sorted(booking_words.DEFAULT_TEXT) == sorted(keys)
    for key in keys:
        assert booking_words.DEFAULT_TEXT[key] == labels.DEFAULTS[key]
        assert booking_words.text(key) == labels.DEFAULTS[key]
    # An unknown key, and the state "ok", have no text.
    assert booking_words.text("ok") == ""
    assert booking_words.text("place.book") == ""


def test_the_keys_without_a_dot_are_the_servers_refusal_and_state_keys() -> None:
    # The frontend test reads the server's keys this way.
    assert sorted(key for key in labels.DEFAULTS if "." not in key) == sorted(
        booking_words.DEFAULT_TEXT
    )


def test_the_calendar_and_email_keys_are_the_words_the_server_writes() -> None:
    # The frontend test leaves these namespaces to the server.
    code = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(labels.__file__).parent / "core").glob("*.py")
    )
    # The pages the server draws read their words with `{% word "key" %}`.
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(labels.__file__).parent / "core" / "templates").rglob(
            "*.html"
        )
    )
    used = set(
        re.findall(r'labels\.word\(\s*[^,()]+,\s*"([a-z][a-z-]*\.[a-z0-9._-]+)"', code)
    )
    assert used <= set(labels.DEFAULTS)
    # A key the code holds in a table (the error pages' keys by status) is
    # written too, whatever the call that reads it.
    # The browser's sign-in card reads its own keys (`login.*`).
    card = "\n".join(
        (Path(labels.__file__).parent.parent / "frontend" / "src" / name).read_text(
            encoding="utf-8"
        )
        for name in ("main.jsx", "login/screen.js")
    )
    quoted = set(
        re.findall(r"""["']([a-z][a-z-]*\.[a-z0-9._-]+)["']""", code + templates + card)
    )
    written = {key for key in labels.DEFAULTS if key.split(".")[0] in WRITTEN}
    assert written and written <= used | quoted


def test_the_page_gets_only_the_texts_the_rule_allows() -> None:
    held = {
        "place.book": "Reserve",
        "no.such": "x",
        "place.me": "",
        "stack.from": "From",
        "cart.title": 3,
    }

    assert labels.valid(organization(held)) == {"place.book": "Reserve"}
    assert labels.valid(organization(["place.book"])) == {}
    assert labels.valid(organization(None)) == {}


def test_the_default_floor_plan_word_is_no_word() -> None:
    assert booking_words.spot_word("booking") is None
    assert booking_words.spot_word(None) is None
    assert booking_words.spot_word("seat") == "seat"


@pytest.mark.parametrize(
    ("key", "text", "fault"),
    [
        (
            "no.such",
            "x",
            "is not a label key. The keys are in randirandi/locale/en.json.",
        ),
        (3, "x", "is not a label key. The keys are in randirandi/locale/en.json."),
        ("place.book", 3, "must be a text."),
        ("place.book", "", "must not be empty, or start or end with a space."),
        ("place.book", "Reserve ", "must not be empty, or start or end with a space."),
        ("place.book", "x" * 201, "must not be longer than 200 characters."),
        (
            "place.book",
            "Reserve {x}",
            "must not use placeholders: its default has none.",
        ),
        (
            "stack.from",
            "From now",
            "must use exactly the placeholders of its default: {time}.",
        ),
    ],
)
def test_the_rule_for_an_organizations_text(
    key: object, text: object, fault: str
) -> None:
    assert labels.fault("en", key, text) == fault


def test_the_rule_accepts_a_text_that_uses_its_placeholders() -> None:
    assert labels.fault("en", "stack.from", "Starting {time}") is None
    assert labels.fault("en", "place.book", "x" * 200) is None


@pytest.mark.django_db
def test_an_organization_holds_only_texts_the_set_allows() -> None:
    org = made_up.organization("labelled")
    assert org.labels == {}
    org.labels = {"place.book": "Reserve"}
    org.full_clean()

    org.labels = {"place.book": "Reserve", "no.such": "x"}
    with pytest.raises(ValidationError, match=r"no\.such: is not a label key"):
        org.full_clean()
    org.labels = {"stack.from": "From"}
    with pytest.raises(ValidationError, match=r"stack\.from: must use exactly"):
        org.full_clean()
    org.labels = ["place.book"]
    with pytest.raises(ValidationError, match="Must be label keys"):
        org.full_clean()


def test_a_closed_day_says_closed_with_the_reason_of_its_closure() -> None:
    from randirandi.core.models import ClosureReason

    # One label for each reason a closure can give, worded as the reason is.
    for reason in ClosureReason:
        assert labels.DEFAULTS[f"closure.{reason.value}"] == reason.label
        assert labels.fault("en", f"closure.{reason.value}", "Stengt for ferie") is None
    assert labels.DEFAULTS["day.closed"] == "Closed"
    assert labels.DEFAULTS["day.closed-why"] == "Closed: {reason}"
    assert labels.placeholders(labels.DEFAULTS["day.closed-why"]) == {"reason"}
    assert labels.fault("en", "day.closed-why", "Stengt") is not None
    assert labels.fault("en", "day.closed-why", "Stengt: {reason}") is None
    # The tile's old "closed" is gone: an empty day that is not closed is not worded so.
    assert "tile.closed" not in labels.DEFAULTS


def test_a_refusal_text_is_in_the_organizations_language(second_language: None) -> None:
    speaking = organization({}, "xx")

    assert booking_words.text("taken", org=speaking) == "taken xx"
    assert booking_words.text("taken", org=organization({})) == "taken"
    assert booking_words.text("taken") == "taken"
    # A key the xx catalogue lacks is English.
    assert booking_words.text("cart-full", org=speaking) == labels.DEFAULTS["cart-full"]


def test_the_resolver_takes_an_organization_or_none_only() -> None:
    for wrong in ({}, {"place.book": "x"}, "en", 3):
        with pytest.raises(TypeError):
            labels.word(wrong, "place.book")  # type: ignore[arg-type]
