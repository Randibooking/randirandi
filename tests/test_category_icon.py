# SPDX-License-Identifier: AGPL-3.0-only
"""a booking category's icon, one of a short list or blank
: the model, the setup file, the export and the choices."""

import json
from dataclasses import replace

import pytest
from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from django.test import Client

from randirandi import setup_file
from randirandi.core import setup as setups
from randirandi.core.booking_choices import booking_choices
from randirandi.core.models import BookingCategory
from randirandi.limits import CATEGORY_ICONS
from randirandi.setup_file import SetupError, parse, validate
from tests import made_up
from tests.test_booking_choices import cover_twelve_hours
from tests.test_login_sessions import get, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_setup_file import SMALLEST, faults_of
from tests.test_setup_load import TODAY

ICONS = ["person", "work", "building", "home", "group"]


def with_icon(icon: str) -> str:
    return SMALLEST.replace(
        '[[booking_categories]]\nname = "private"\n',
        f'[[booking_categories]]\nname = "private"\nicon = "{icon}"\n',
    )


def test_the_list_is_the_five_the_owner_approved() -> None:
    assert list(CATEGORY_ICONS) == ICONS


@pytest.mark.django_db
@pytest.mark.parametrize("icon", ["", *ICONS])
def test_the_model_takes_blank_and_each_key(icon: str) -> None:
    made = made_up.everything()
    category = BookingCategory(organization=made.organization, name="x", icon=icon)
    category.full_clean()
    category.save()
    assert (
        BookingCategory.objects.of(made.organization).get(pk=category.pk).icon == icon
    )


@pytest.mark.django_db
def test_a_category_has_no_icon_until_one_is_set() -> None:
    assert made_up.everything().private.icon == ""


@pytest.mark.django_db
@pytest.mark.parametrize("icon", ["star", "Person", " person", "personal", "x" * 9])
def test_the_model_and_the_database_refuse_an_unknown_icon(icon: str) -> None:
    made = made_up.everything()
    category = BookingCategory(organization=made.organization, name="x", icon=icon)
    with pytest.raises(ValidationError) as raised:
        category.full_clean()
    assert "icon" in raised.value.message_dict
    with pytest.raises((IntegrityError, DataError)), transaction.atomic():
        category.save()


@pytest.mark.parametrize("icon", ICONS)
def test_the_setup_file_reads_each_key(icon: str) -> None:
    (category,) = parse(with_icon(icon)).booking_categories
    assert category.icon == icon


def test_the_setup_file_leaves_the_icon_out() -> None:
    (category,) = parse(SMALLEST).booking_categories
    assert category.icon == ""
    assert "icon" not in setup_file.write(parse(SMALLEST))


@pytest.mark.parametrize("icon", ["star", "", "Person"])
def test_the_setup_file_refuses_an_unknown_or_empty_icon(icon: str) -> None:
    (fault,) = faults_of(with_icon(icon))
    assert fault.startswith("booking_categories[1].icon: must be one of: person, ")


def test_the_setup_file_refuses_an_icon_that_is_not_a_text() -> None:
    (fault,) = faults_of(with_icon("person").replace('icon = "person"', "icon = 3"))
    assert fault.startswith("booking_categories[1].icon")


def test_a_setup_built_in_code_is_held_to_the_same_list() -> None:
    setup = parse(SMALLEST)
    bad = replace(
        setup, booking_categories=(setup_file.BookingCategory("private", "star"),)
    )
    with pytest.raises(SetupError) as raised:
        validate(bad)
    assert any(
        line.startswith("booking_categories[1].icon") for line in raised.value.faults
    )


@pytest.mark.django_db
def test_icons_load_export_and_write_back() -> None:
    written = parse(with_icon("home"))
    organization = setups.load(written, today=TODAY)
    assert BookingCategory.objects.of(organization).get().icon == "home"
    exported = setups.export(organization)
    assert exported.booking_categories == (
        setup_file.BookingCategory("private", "home"),
    )
    text = setup_file.write(exported)
    assert 'icon = "home"' in text
    assert parse(text).booking_categories == exported.booking_categories
    other = replace(
        exported, organization=replace(exported.organization, subdomain="second")
    )
    assert setups.export(setups.load(other, today=TODAY)).booking_categories == (
        exported.booking_categories
    )


@pytest.mark.django_db
def test_the_fjordside_scenario_has_the_approved_icons() -> None:
    from tests.test_setup_file import SETUPS

    categories = setup_file.read(SETUPS / "fjordside.toml").booking_categories
    assert [(c.name, c.icon) for c in categories] == [
        ("Private", "person"),
        ("Work", "work"),
        ("Internal", "building"),
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("icon", ["", "work"])
def test_the_choices_answer_carries_the_icon(icon: str) -> None:
    made = made_up.everything()
    cover_twelve_hours(made)
    BookingCategory.objects.of(made.organization).filter(pk=made.private.pk).update(
        icon=icon
    )
    (choice,) = booking_choices(made.member).categories
    assert choice.icon == icon
    client = Client()
    sign_in(client, made.member)
    body = json.loads(get(client, "/booking/choices/").content)
    assert body["categories"] == [
        {"id": made.private.pk, "name": "private", "icon": icon}
    ]


def test_the_page_maps_exactly_the_keys_the_server_allows() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent
        / "frontend"
        / "src"
        / "book"
        / "categories.js"
    ).read_text()
    listed = source.split("CATEGORY_ICON_KEYS = [", 1)[1].split("]", 1)[0]
    assert [key.strip(" '") for key in listed.split(",")] == list(CATEGORY_ICONS)
