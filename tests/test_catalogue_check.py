# SPDX-License-Identifier: AGPL-3.0-only
"""Language catalogues preserve English keys, placeholders and plural forms."""

import json
import shutil
import time
from pathlib import Path

import pytest

from randirandi import labels
from tools import catalogue_check

FIXTURE = Path(__file__).parent / "catalogue_fixture"
REFERENCE = json.loads((FIXTURE / "en.json").read_text(encoding="utf-8"))


def faults_of(name: str) -> list[str]:
    texts = json.loads((FIXTURE / f"{name}.json").read_text(encoding="utf-8"))
    return labels.catalogue_faults(texts, REFERENCE)


def test_a_complete_catalogue_has_no_fault() -> None:
    assert faults_of("complete") == []
    assert faults_of("en") == []


@pytest.mark.parametrize(
    ("name", "fault"),
    [
        ("missing-key", "lacks the key welcome."),
        ("extra-key", "has the key farewell, which English does not."),
        ("empty-text", "has no text for greeting."),
        ("other-placeholder", "uses other placeholders than English for welcome."),
        ("lacks-plural-form", "lacks the form other of box."),
    ],
)
def test_each_fault_is_found_and_named(name: str, fault: str) -> None:
    assert fault in faults_of(name)


def test_the_one_form_may_leave_out_the_count_and_nothing_else() -> None:
    # "en eske": a language may write its singular without the number.
    assert faults_of("one-without-count") == []
    # The other form keeps {count}, and every other placeholder must match.
    assert "uses other placeholders than English for box.other." in faults_of(
        "other-without-count"
    )
    assert "uses other placeholders than English for box.one." in faults_of(
        "one-other-placeholder"
    )


def test_an_organizations_own_singular_may_leave_out_the_count() -> None:
    assert labels.fault("en", "length.hours.one", "one hour") is None
    assert labels.fault("en", "length.hours.one", "{count} hour") is None
    assert labels.fault("en", "length.hours.one", "{hours} hour") is not None
    assert labels.fault("en", "length.hours.other", "many hours") is not None


def test_a_text_that_is_not_a_text_is_a_fault() -> None:
    texts = {**REFERENCE, "greeting": 3, "welcome": None}

    found = labels.catalogue_faults(texts, REFERENCE)

    assert "has no text for greeting." in found
    assert "has no text for welcome." in found
    assert labels.catalogue_faults(["greeting"], REFERENCE)


def test_the_tool_passes_a_folder_of_complete_catalogues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("en", "complete"):
        shutil.copy(FIXTURE / f"{name}.json", tmp_path / f"{name}.json")

    assert catalogue_check.main([str(tmp_path)]) == 0
    assert "0 fault(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name",
    [
        "missing-key",
        "extra-key",
        "empty-text",
        "other-placeholder",
        "lacks-plural-form",
    ],
)
def test_the_tool_fails_a_folder_with_a_faulty_catalogue_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    for each in ("en", "complete", name):
        shutil.copy(FIXTURE / f"{each}.json", tmp_path / f"{each}.json")

    assert catalogue_check.main([str(tmp_path)]) == 1
    assert f"{name}.json: " in capsys.readouterr().err


def test_the_tool_fails_a_folder_without_the_reference(tmp_path: Path) -> None:
    shutil.copy(FIXTURE / "complete.json", tmp_path / "complete.json")

    assert catalogue_check.check(tmp_path) == [
        f"{tmp_path}: has no en.json, the reference."
    ]


def test_the_catalogues_the_code_ships_are_complete_and_fast() -> None:
    started = time.monotonic()
    found = catalogue_check.check(labels.LOCALE)

    assert found == []
    assert time.monotonic() - started < 1


def test_english_holds_both_forms_of_every_counted_key() -> None:
    stems = {
        key.rpartition(".")[0]
        for key in labels.DEFAULTS
        if key.endswith((".one", ".other"))
    }
    assert {"line.places", "filter.quantity-chip", "length.hours"} <= stems
    for stem in stems:
        assert f"{stem}.one" in labels.DEFAULTS and f"{stem}.other" in labels.DEFAULTS
