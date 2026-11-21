# SPDX-License-Identifier: AGPL-3.0-only
"""The language setting and the resolver : one language per
organization, its catalogue first, the organization's own words over it,
English for a key the catalogue lacks."""

from dataclasses import replace

import pytest
from django.core.exceptions import ValidationError

from randirandi import labels, setup_file
from randirandi.core import setup as setups
from randirandi.core.models import Organization
from randirandi.setup_file import SetupError, parse
from tests import made_up
from tests.test_setup_file import FORMAT_THREE, faults_of
from tests.test_setup_load import TODAY


def org(language: str = "en", **words: str) -> Organization:
    return Organization(language=language, labels=words)


def with_language(text: str, language: str) -> str:
    return text.replace(
        "[organization]\n", f'[organization]\nlanguage = "{language}"\n', 1
    )


def test_the_code_ships_english_and_norwegian_and_english_holds_every_key() -> None:
    assert labels.languages() == ("en", "nb")
    assert labels.word(org("nb"), "place.book") == "Book"
    assert labels.word(org("nb"), "place.me") == "Min side"
    assert labels.DEFAULTS is labels.CATALOGUES["en"]
    assert labels.word(org(), "place.book") == "Book"
    assert labels.word(None, "place.book") == "Book"


@pytest.mark.django_db
def test_an_organization_speaks_english_unless_it_says_otherwise() -> None:
    organization = made_up.organization("plain")

    assert organization.language == "en"
    assert labels.word(organization, "place.book") == "Book"
    organization.full_clean()


@pytest.mark.django_db
def test_an_organization_is_refused_a_language_the_code_does_not_ship(
    second_language: None,
) -> None:
    organization = made_up.organization("odd")
    organization.language = "zz"
    with pytest.raises(ValidationError, match="must be a language the code ships"):
        organization.full_clean()
    organization.language = "xx"
    organization.full_clean()


def test_an_empty_language_is_refused_in_a_file_and_in_a_setup_built_in_code() -> None:
    assert faults_of(with_language(FORMAT_THREE, "")) == [
        "organization.language: must not be empty, or start or end with a space."
    ]
    with pytest.raises(SetupError, match=r"organization\.language: must not be empty"):
        parse(with_language(FORMAT_THREE, ""))
    setup = parse(FORMAT_THREE)
    with pytest.raises(SetupError, match=r"organization\.language"):
        setup_file.validate(
            replace(setup, organization=replace(setup.organization, language=""))
        )


def test_a_setup_names_its_language_and_one_without_a_catalogue_is_refused(
    second_language: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert parse(FORMAT_THREE).organization.language == "en"
    setup = parse(with_language(FORMAT_THREE, "xx"))
    assert setup.organization.language == "xx"
    # Written only when it is not English, and read back as the same setup.
    assert 'language = "xx"' in setup_file.write(setup)
    assert "language" not in setup_file.write(parse(FORMAT_THREE))
    assert parse(setup_file.write(setup)) == setup

    assert faults_of(with_language(FORMAT_THREE, "zz")) == [
        "organization.language: must be a language the code ships: en, nb, xx."
    ]
    monkeypatch.delitem(labels.CATALOGUES, "xx")
    assert faults_of(with_language(FORMAT_THREE, "xx")) == [
        "organization.language: must be a language the code ships: en, nb."
    ]
    with pytest.raises(SetupError, match=r"organization\.language"):
        setup_file.validate(
            replace(setup, organization=replace(setup.organization, language="zz"))
        )


def test_the_resolver_takes_the_catalogue_of_the_language_and_falls_back_to_english(
    second_language: None,
) -> None:
    speaking = org("xx")

    assert labels.word(speaking, "place.book") == "Reserve xx"
    assert labels.word(speaking, "stack.needs", length="2 hours") == "Needs 2 hours xx"
    # A key the xx catalogue lacks shows its English text.
    assert "stack.from" not in labels.CATALOGUES["xx"]
    assert labels.word(speaking, "stack.from", time="10:00") == "From 10:00"
    assert labels.word(org("en"), "place.book") == "Book"
    # A language without a catalogue is English.
    assert labels.word(org("zz"), "place.book") == "Book"


def test_the_organizations_own_words_come_before_the_catalogue(
    second_language: None,
) -> None:
    assert labels.word(org("en", **{"place.book": "Mine"}), "place.book") == "Mine"
    assert labels.word(org("xx", **{"place.book": "Mine"}), "place.book") == "Mine"
    assert labels.word(org("xx", **{"place.book": " "}), "place.book") == ("Reserve xx")


def test_a_text_is_checked_against_the_placeholders_of_its_languages_catalogue(
    second_language: None,
) -> None:
    both = "Needs {length} free from {time}"
    assert labels.fault("en", "stack.needs", both) is None
    assert labels.fault("xx", "stack.needs", both) == (
        "must use exactly the placeholders of its default: {length}."
    )
    assert labels.fault("xx", "stack.needs", "Trenger {length}") is None
    assert labels.fault("en", "stack.needs", "Trenger {length}") is not None
    # A key the catalogue lacks is checked against the English text.
    assert labels.fault("xx", "stack.from", "Fra {time}") is None
    assert labels.fault("xx", "stack.from", "Fra") is not None
    assert labels.fault("zz", "place.book", "x") == (
        "must be a language the code ships: en, nb, xx."
    )


@pytest.mark.django_db
def test_the_model_checks_the_words_against_its_languages_catalogue(
    second_language: None,
) -> None:
    organization = made_up.organization("checked")
    organization.language = "xx"
    organization.labels = {"stack.needs": "Trenger {length}"}
    organization.full_clean()
    organization.labels = {"stack.needs": "Needs {length} free from {time}"}
    with pytest.raises(ValidationError, match=r"stack\.needs: must use exactly"):
        organization.full_clean()


@pytest.mark.django_db
def test_a_setup_with_a_language_loads_and_exports_it(second_language: None) -> None:
    written = parse(
        with_language(FORMAT_THREE, "xx")
        + '\n[labels]\n"stack.needs" = "Trenger {length}"\n'
    )

    organization = setups.load(written, today=TODAY)

    assert organization.language == "xx"
    assert labels.word(organization, "stack.needs", length="2 t") == "Trenger 2 t"
    exported = setups.export(organization)
    assert exported.organization.language == "xx"
    assert exported.labels == written.labels
    assert parse(setup_file.write(exported)).labels == written.labels
