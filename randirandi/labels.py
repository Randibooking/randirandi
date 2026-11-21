# SPDX-License-Identifier: AGPL-3.0-only
"""The label set: every word the member pages show, by key, per language.

A language's catalogue is `locale/<language>.json`: each key and its text in
that language. English, `locale/en.json`, is the product's own language and
holds every key; another catalogue may lack keys, and a key it lacks shows its
English text. An organization has one language (`Organization.language`) and
sets its own text for a key in its setup file's `[labels]` table;
the page shows that text and, for a key it did not set, the catalogue's. A
text may hold named placeholders such as `{free}`; they belong to the key, and
an organization's text uses exactly the ones the key's text uses in that
language's catalogue.

The files live inside `randirandi/` so that the image, which copies no
`frontend/`, has them, and the frontend imports `en.json` from here.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.utils.safestring import SafeString

    from randirandi.core.models import Organization

LOCALE = Path(__file__).with_name("locale")

# The product's own language: it holds every key and answers for a missing one.
DEFAULT_LANGUAGE = "en"

# Language code to its catalogue (key to text). The code ships these; a test
# adds a fixture catalogue here.
CATALOGUES: dict[str, dict[str, str]] = {
    path.stem: json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(LOCALE.glob("*.json"))
}

DEFAULTS: dict[str, str] = CATALOGUES[DEFAULT_LANGUAGE]

LONGEST = 200

# The same pattern the frontend fills placeholders by (frontend/src/book/labels.js).
_PLACEHOLDER = re.compile(r"\{([a-z][A-Za-z0-9_]*)\}")


def placeholders(text: str) -> frozenset[str]:
    """The names of the placeholders in a text."""
    return frozenset(_PLACEHOLDER.findall(text))


def languages() -> tuple[str, ...]:
    """The languages the code ships a catalogue for, English first."""
    return (DEFAULT_LANGUAGE, *sorted(set(CATALOGUES) - {DEFAULT_LANGUAGE}))


# Each shipped language's name in itself, for the admin's choice; a code
# without one (a test fixture) is named by its code.
NAMES = {"en": "English", "nb": "Norsk bokmål"}


def language_name(language: str) -> str:
    """A language's name in itself."""
    return NAMES.get(language, language)


def language_fault(language: object) -> str | None:
    """Why a language code is refused, or None: it must be a catalogue the
    code ships."""
    if type(language) is str and language in CATALOGUES:
        return None
    return f"must be a language the code ships: {', '.join(languages())}."


def catalogue_text(language: str, key: str) -> str:
    """A key's text in a language's catalogue, English where it lacks the key."""
    return CATALOGUES.get(language, {}).get(key) or DEFAULTS[key]


def own_words(held: object, language: str) -> dict[str, str]:
    """The organization's own words, in its `language`, that the rule allows: a
    stored value that is not a table, an unknown key or a refused text is left
    out."""
    if not isinstance(held, dict):
        return {}
    return {
        key: text for key, text in held.items() if fault(language, key, text) is None
    }


def language_of(organization: Organization | None) -> str:
    """The organization's language; English for none, and for one the code
    ships no catalogue for. Anything but an organization or None is an error."""
    from randirandi.core.models import Organization

    if organization is None:
        return DEFAULT_LANGUAGE
    if not isinstance(organization, Organization):
        raise TypeError(f"Expected an Organization or None, got {type(organization)}.")
    return (
        organization.language
        if organization.language in CATALOGUES
        else DEFAULT_LANGUAGE
    )


def valid(organization: Organization | None) -> dict[str, str]:
    """The words the page needs besides the English defaults it ships, for an
    organization: its language's catalogue (nothing for English, which the page
    holds) with its own words for that language over it."""
    language = language_of(organization)
    own = own_words(None if organization is None else organization.labels, language)
    if language == DEFAULT_LANGUAGE:
        return own
    return {**CATALOGUES[language], **own}


def _raw(organization: Organization | None, key: str) -> str:
    """An organization's text for a key, placeholders unfilled: its own for its
    language when the rule allows it, otherwise its language's catalogue,
    otherwise English."""
    language = language_of(organization)
    own = {} if organization is None else own_words(organization.labels, language)
    return own.get(key) or catalogue_text(language, key)


def word(organization: Organization | None, key: str, /, **values: object) -> str:
    """An organization's text for a key: its own for its language when the rule
    allows it, otherwise its language's catalogue, otherwise English. Its
    placeholders are filled in one pass as the page fills them; what the server
    writes for a member (the calendar file, the emails). A placeholder without a
    value stays as written. No organization (None) is the product's own English.
    The text is plain text: a plain-text mail carries it as it is; an HTML page
    escapes it (a template does; `word_html` is for a text with HTML in it)."""
    return _PLACEHOLDER.sub(
        lambda found: str(values[found[1]]) if found[1] in values else found[0],
        _raw(organization, key),
    )


def _shown(value: object) -> str:
    """A value as text; a value that is already safe HTML stays so."""
    return value if isinstance(value, str) else str(value)


def word_html(
    organization: Organization | None, key: str, /, **values: object
) -> SafeString:
    """The same text for an HTML page, escaped first and filled after: the text
    is plain text, so `<` and `&` in it are shown as they are written, and a
    value is escaped as it goes in unless it is already safe HTML (a link the
    caller built). A placeholder without a value stays as written."""
    # Django is not loaded until a page needs it: the setup file reader imports
    # this module and reads without Django.
    from django.utils.html import conditional_escape, escape
    from django.utils.safestring import mark_safe

    return mark_safe(  # noqa: S308 - the text and every value are escaped
        _PLACEHOLDER.sub(
            lambda found: (
                str(conditional_escape(_shown(values[found[1]])))
                if found[1] in values
                else found[0]
            ),
            str(escape(_raw(organization, key))),
        )
    )


def plural_form(count: int) -> str:
    """The form of a counted text: `one` for exactly 1, `other` for the rest
    (0 and 2 and more). The rule English and Norwegian share; a language with
    other forms adds its rule here and its forms to the check."""
    return "one" if count == 1 else "other"


def plural(
    organization: Organization | None, key: str, count: int, /, **values: object
) -> str:
    """A counted text: `<key>.one` or `<key>.other`, with `{count}` filled."""
    return word(organization, f"{key}.{plural_form(count)}", count=count, **values)


def date_words(
    organization: Organization | None, day: date | datetime, *, year: bool = False
) -> str:
    """ "Tuesday 20 October", or with the year, in the organization's language:
    the pattern is the catalogue's `date.words` (Norwegian puts a dot after the
    day), the names its `weekdays.*` and `months.*`. For no
    organization, the product's own English: what an administrator reads
    (the administrator's screens stay English)."""
    text = word(
        organization,
        "date.words",
        weekday=word(organization, f"weekdays.{WEEKDAYS[day.weekday()]}"),
        day=day.day,
        month=word(organization, f"months.{day.month}"),
    )
    return f"{text} {day.year}" if year else text


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def placeholders_fault(key: str, text: str, wanted: frozenset[str]) -> bool:
    """Whether a text uses other placeholders than the key's. The `.one` form of
    a counted key may leave out `{count}`: a language may write its singular
    without the number ("one hour"); every other placeholder must match."""
    found = placeholders(text)
    if found == wanted:
        return False
    return not (key.endswith(".one") and found == wanted - {"count"})


def fault(language: object, key: object, text: object) -> str | None:
    """Why an organization's text for a key in a language is refused, or None.
    The reader of the setup file and the model say it the same way."""
    if type(language) is not str or language not in CATALOGUES:
        return language_fault(language)
    if key not in DEFAULTS:
        return "is not a label key. The keys are in randirandi/locale/en.json."
    if type(text) is not str:
        return "must be a text."
    if not text or text != text.strip():
        return "must not be empty, or start or end with a space."
    if len(text) > LONGEST:
        return f"must not be longer than {LONGEST} characters."
    wanted = placeholders(catalogue_text(language, str(key)))
    if placeholders_fault(str(key), text, wanted):
        if not wanted:
            return "must not use placeholders: its default has none."
        names = ", ".join("{" + name + "}" for name in sorted(wanted))
        return f"must use exactly the placeholders of its default: {names}."
    return None


PLURAL_FORMS = ("one", "other")


def catalogue_faults(
    texts: object, reference: dict[str, str] | None = None
) -> list[str]:
    """What is wrong with a language's catalogue, one line each; none when it
    is complete: exactly the keys of the reference (English), a text for every
    key, the same placeholders per key, and both forms of a counted key."""
    if not isinstance(texts, dict):
        return ["is not a table of keys and texts."]
    reference = DEFAULTS if reference is None else reference
    faults = []
    for key in sorted(set(reference) - set(texts)):
        faults.append(f"lacks the key {key}.")
    for key in sorted(set(texts) - set(reference)):
        faults.append(f"has the key {key}, which English does not.")
    for key in sorted(set(texts) & set(reference)):
        text = texts[key]
        if type(text) is not str or not text.strip():
            faults.append(f"has no text for {key}.")
        elif placeholders_fault(key, text, placeholders(reference[key])):
            faults.append(f"uses other placeholders than English for {key}.")
    stems = {
        key.rpartition(".")[0]
        for key in (*texts, *reference)
        if key.rpartition(".")[2] in PLURAL_FORMS
    }
    for stem in sorted(stems):
        for form in PLURAL_FORMS:
            if f"{stem}.{form}" not in texts:
                faults.append(f"lacks the form {form} of {stem}.")
    return faults
