# SPDX-License-Identifier: AGPL-3.0-only
"""Format 3 and the general model, starting from portable organization files.

These exercise floor plans, spots and groups as a setup file carries them,
and claims on spots. Calendar selection, pricing calculation and the choice
of spots are separate workflow steps, not exercised here.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import (
    IntegrityError,
    OperationalError,
    connection,
    models,
    transaction,
)
from django.db.models.query import QuerySet

from randirandi import setup_file as f
from randirandi.core import setup as setups
from randirandi.core import validator
from randirandi.core.models import (
    Asset,
    AssetInventory,
    BookingConfiguration,
    ConsumptionMode,
    Organization,
    PriceBookEntry,
    Space,
    Spot,
    SpotGroup,
    TakenTime,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_setup_file import FILES, SETUPS, SMALLEST, unpriced
from tests.test_setup_load import in_order

SCENARIOS = sorted((SETUPS / "scenarios").glob("*.toml"))
TODAY = date(2027, 3, 1)


def scenario(name: str) -> f.Setup:
    return f.read(SETUPS / "scenarios" / f"{name}.toml")


def text_of(name: str) -> str:
    return f.write(scenario(name))


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda path: path.stem)
def test_format_three_files_round_trip_without_a_database(path: Path) -> None:
    setup = f.read(path)
    assert setup.format == 3
    assert f.parse(f.write(setup)) == setup
    # Each file is as the writer writes it, under its own opening comments.
    assert path.read_text().endswith(f.write(setup))
    assert unpriced(setup) == []
    assert sum("@" in line for line in path.read_text().splitlines()) == 1


def test_the_scenarios_in_the_repository() -> None:
    assert [path.stem for path in SCENARIOS] == [
        "brew-lab",
        "cinema",
        "four-beds",
        "laundry",
        "old-mill",
        "riverside-hall",
        "sauna-palace",
        "supper-club",
    ]


# One change to Sunshine's file, and the fault it is refused with ("" when
# the change is read).
@pytest.mark.parametrize(
    ("old", "new", "fault"),
    [
        ('  "Bed 2",\n', '  "Bed 1",\n', "the name 'Bed 1' is given to more than one"),
        ('  "Bed 1",\n', "  4,\n", "spots: must be a number of spots"),
        ('  "Bed 1",\n', '  "",\n', ""),
        (
            'spots = [\n  "Bed 1",\n  "Bed 2",\n  "Bed 3",\n  "Bed 4",\n]',
            "spots = 0",
            "1 or more",
        ),
        (
            'spots = [\n  "Bed 1",\n  "Bed 2",\n  "Bed 3",\n  "Bed 4",\n]',
            "spots = 10001",
            "spots: must be at most 10,000, the most spots a floor plan holds.",
        ),
        (
            'spots = [\n  "Bed 1",\n  "Bed 2",\n  "Bed 3",\n  "Bed 4",\n]',
            "spots = []",
            "spots: must be",
        ),
        ('spot_word = "bed"', 'spot_word = ""', "spot_word: must not be empty"),
        ('spot_word = "bed"\n', "", "spot_word: is missing"),
        ('spot_word = "bed"', 'unit = "bed"', "unit: is not part"),
        (
            'space = "Sunshine"',
            'space = "Unknown"',
            "there is no space named 'Unknown'",
        ),
        (
            'space = "Sunshine"',
            'space = "Sunshine"\nwhole_space = "true"',
            "true or false",
        ),
        (
            'space = "Sunshine"',
            'space = "Sunshine"\nmaximum_occupancy = 0',
            "1 or more",
        ),
        (
            'floor_plan = "Beds"',
            'floor_plan = "Bunks"',
            "Sunshine has no floor plan named 'Bunks'",
        ),
        (
            'consumption_mode = "exclusive"',
            'consumption_mode = "exclusive"\nfloor_plan = "Beds"',
            "an exclusive container takes its whole asset and names no floor plan",
        ),
        (
            'consumption_mode = "shared"',
            'consumption_mode = "pooled"',
            "must be one of",
        ),
        ('consumption_mode = "shared"\n', "", "consumption_mode: is missing"),
        (
            'billing_basis = "per_capacity_unit"',
            'billing_basis = "per_person"',
            "one of",
        ),
        ('billing_basis = "per_capacity_unit"\n', "", "billing_basis: is missing"),
        (
            'name = "Sunshine"\n\n[[assets]]',
            'name = "Sunshine"\nbooking_page = "everything"\n\n[[assets]]',
            "booking_page: must be one of: one_asset, whole_space.",
        ),
    ],
)
def test_format_three_refuses_bad_properties_and_references(
    old: str, new: str, fault: str
) -> None:
    text = text_of("four-beds")
    assert text.count(old) == 1, old
    changed = text.replace(old, new, 1)
    if not fault:
        # A spot without a name among named ones is a spot like any other.
        assert f.parse(changed).assets[0].floor_plans[0].spots[0] == ""
        return
    with pytest.raises(f.SetupError, match=fault):
        f.parse(changed)


def test_an_asset_covering_its_whole_space_needs_one() -> None:
    text = text_of("four-beds").replace(
        'space = "Sunshine"\n', "whole_space = true\n", 1
    )
    with pytest.raises(f.SetupError, match="whole_space: needs a space"):
        f.parse(text)


def riverside_with(
    *, seated: f.FloorPlan | None = None, standing: f.FloorPlan | None = None
) -> f.Setup:
    setup = scenario("riverside-hall")
    hall = setup.assets[0]
    plans = (seated or hall.floor_plans[0], standing or hall.floor_plans[1])
    return replace(setup, assets=(replace(hall, floor_plans=plans),))


def changed_riverside(case: str) -> f.Setup:
    setup = scenario("riverside-hall")
    hall = setup.assets[0]
    seated, standing = hall.floor_plans
    front, back = seated.groups
    if case == "space-number":
        return replace(setup, spaces=(f.Space(1, "Hall"), f.Space(1, "Annex")))
    if case == "space-name":
        return replace(setup, spaces=(f.Space(1, "Hall"), f.Space(2, "Hall")))
    if case == "second-mark":
        return replace(
            setup,
            spaces=(f.Space(1, "Hall"),),
            assets=(
                replace(hall, space="Hall", whole_space=True),
                f.Asset(2, "Whole hall", "", space="Hall", whole_space=True),
            ),
        )
    if case == "unnamed-plans":
        return riverside_with(seated=replace(seated, name=""))
    if case == "same-plan-name":
        return riverside_with(standing=replace(standing, name="Seated"))
    if case == "group-name":
        return riverside_with(
            seated=replace(seated, groups=(front, replace(back, name="Front")))
        )
    if case == "no-such-spot":
        return riverside_with(
            seated=replace(seated, groups=(front, replace(back, spots=(5, 13))))
        )
    if case == "two-groups":
        return riverside_with(
            seated=replace(seated, groups=(front, replace(back, spots=(4, 5))))
        )
    assert case == "no-floor-plan"
    return replace(
        setup,
        booking_configurations=tuple(
            replace(item, floor_plan="") for item in setup.booking_configurations
        ),
    )


@pytest.mark.parametrize(
    ("case", "fault"),
    [
        ("space-number", "more than once"),
        ("space-name", "more than once"),
        ("second-mark", "this space already has an asset that covers it"),
        ("unnamed-plans", r"floor_plans\[1\].name: is missing"),
        ("same-plan-name", "the name 'Seated' is used more than once"),
        ("group-name", "the name 'Front' is used more than once"),
        ("no-such-spot", "there is no spot 13; the floor plan has 12"),
        ("two-groups", "spot 4 is in the group 'Front' already"),
        ("no-floor-plan", "is missing: a shared container names the floor plan"),
    ],
)
def test_floor_plan_and_space_identity_is_refused(case: str, fault: str) -> None:
    with pytest.raises(f.SetupError, match=fault):
        f.write(changed_riverside(case))


@pytest.mark.parametrize(
    "field",
    ['space = "Hall"', "whole_space = false", "maximum_occupancy = 3"],
)
def test_format_one_refuses_new_asset_fields(field: str) -> None:
    with pytest.raises(f.SetupError, match="is not part of the format"):
        f.parse(
            SMALLEST.replace('name = "Clubhouse"\n', f'name = "Clubhouse"\n{field}\n')
        )


def test_format_one_has_no_floor_plans() -> None:
    plan = '\n[[assets.floor_plans]]\nspot_word = "seat"\nspots = 4\n'
    with pytest.raises(f.SetupError, match=r"assets\[1\].floor_plans: is not part"):
        f.parse(
            SMALLEST.replace('name = "Clubhouse"\n', f'name = "Clubhouse"\n{plan}', 1)
        )


def test_writer_cannot_hide_floor_plan_data_by_changing_the_version() -> None:
    with pytest.raises(f.SetupError, match="format 1 would lose floor plan data"):
        f.write(replace(scenario("four-beds"), format=1))


def test_a_floor_plan_holds_at_most_ten_thousand_spots() -> None:
    for spots in (10_000, 10_001):
        text = text_of("riverside-hall").replace("spots = 40", f"spots = {spots}")
        if spots == 10_000:
            assert len(f.parse(text).assets[0].floor_plans[1].spots) == spots
            continue
        with pytest.raises(f.SetupError) as refused:
            f.parse(text)
        assert refused.value.faults == [
            "assets[1].floor_plans[2].spots: must be at most 10,000, the most "
            "spots a floor plan holds."
        ]
    standing = scenario("riverside-hall").assets[0].floor_plans[1]
    many = replace(standing, spots=("",) * 10_001)
    with pytest.raises(f.SetupError, match="at most 10,000"):
        f.write(riverside_with(standing=many))


NOT_EMPTY = "must not be empty, or start or end with a space."
A_TIME = "must be a time of day, written as 17:00."
A_LENGTH = 'must be a length with its unit, written as "4 h" or "90 min".'
A_WEEK = 'must be a calendar week, written as "2027-W01".'
NAMED_ONCE = "must be a list of one or more texts, each named once."
FIXED = "is set by the system for a fixed period. Leave it out."
UNNAMED = '"", "", "", "", "", "", "", "", "", ""'


def with_organization(setup: f.Setup, **changes: Any) -> f.Setup:
    return replace(setup, organization=replace(setup.organization, **changes))


def with_hall(setup: f.Setup, **changes: Any) -> f.Setup:
    return replace(setup, assets=(replace(setup.assets[0], **changes),))


def with_plan(setup: f.Setup, place: int, **changes: Any) -> f.Setup:
    plans = list(setup.assets[0].floor_plans)
    plans[place - 1] = replace(plans[place - 1], **changes)
    return with_hall(setup, floor_plans=tuple(plans))


def with_group(setup: f.Setup, place: int, **changes: Any) -> f.Setup:
    groups = list(setup.assets[0].floor_plans[0].groups)
    groups[place - 1] = replace(groups[place - 1], **changes)
    return with_plan(setup, 1, groups=tuple(groups))


def with_spans(
    setup: f.Setup, change: Callable[[f.AvailableSpan], f.AvailableSpan]
) -> f.Setup:
    """The spans of the calendar's first line, Monday to Thursday, changed."""
    calendar = setup.availability_calendars[0]
    spans = tuple(
        change(span) if span.end == 22 * 60 else span for span in calendar.spans
    )
    return replace(setup, availability_calendars=(replace(calendar, spans=spans),))


def with_configuration(setup: f.Setup, number: int, **changes: Any) -> f.Setup:
    return replace(
        setup,
        booking_configurations=tuple(
            replace(item, **changes) if item.number == number else item
            for item in setup.booking_configurations
        ),
    )


def with_entry(setup: f.Setup, **changes: Any) -> f.Setup:
    book = setup.price_books[0]
    entries = (replace(book.entries[0], **changes), *book.entries[1:])
    return replace(setup, price_books=(replace(book, entries=entries),))


def with_tier(setup: f.Setup, **changes: Any) -> f.Setup:
    tiers = setup.price_books[0].entries[0].tiers
    return with_entry(setup, tiers=(replace(tiers[0], **changes), *tiers[1:]))


# What the reader refuses, built in code and changed in Riverside Hall's file:
# the change to the setup, the text in the file and what it becomes, and the
# fault both are refused with.
AS_READ: list[Any] = [
    pytest.param(
        lambda setup: replace(setup, format=0),
        "format = 3",
        "format = 0",
        "format: must be 1 or more.",
        id="format 0",
    ),
    pytest.param(
        lambda setup: replace(setup, format=2),
        "format = 3",
        "format = 2",
        f.FORMAT_TWO,
        id="format 2",
    ),
    pytest.param(
        lambda setup: replace(setup, format=4),
        "format = 3",
        "format = 4",
        "format: is newer than this code, which reads format 3.",
        id="format 4",
    ),
    pytest.param(
        lambda setup: with_organization(setup, subdomain="Riverside Hall"),
        'subdomain = "riverside-hall"',
        'subdomain = "Riverside Hall"',
        "organization.subdomain: must be one part of a web address: a to z, 0 to "
        "9 and hyphens.",
        id="subdomain",
    ),
    pytest.param(
        lambda setup: with_organization(setup, time_zone="Europe/Nowhere"),
        'time_zone = "Europe/Oslo"',
        'time_zone = "Europe/Nowhere"',
        "organization.time_zone: must be a time zone, written as Europe/Oslo.",
        id="time zone",
    ),
    pytest.param(
        lambda setup: with_organization(setup, currency="GBP"),
        'currency = "NOK"',
        'currency = "GBP"',
        "organization.currency: must be one of: NOK, DKK, EUR.",
        id="currency",
    ),
    pytest.param(
        lambda setup: with_organization(setup, contact_email="bookings"),
        'contact_email = "bookings@riverside-hall.example"',
        'contact_email = "bookings"',
        "organization.contact_email: must be an email address.",
        id="email",
    ),
    pytest.param(
        lambda setup: with_organization(setup, standard_role=" Member"),
        'standard_role = "Member"',
        'standard_role = " Member"',
        f"organization.standard_role: {NOT_EMPTY}",
        id="standard role",
    ),
    pytest.param(
        lambda setup: with_organization(setup, member_retention_months=0),
        "member_retention_months = 12",
        "member_retention_months = 0",
        "organization.member_retention_months: must be 1 or more.",
        id="retention",
    ),
    pytest.param(
        lambda setup: replace(setup, spaces=(f.Space(1, "Hall", "", "everyone"),)),
        "\n[[assets]]\n",
        '\n[[spaces]]\nnumber = 1\nname = "Hall"\nbooking_page = "everyone"\n'
        "\n[[assets]]\n",
        "spaces[1].booking_page: must be one of: one_asset, whole_space.",
        id="booking page",
    ),
    pytest.param(
        lambda setup: replace(setup, spaces=(f.Space(0, "Hall"),)),
        "\n[[assets]]\n",
        '\n[[spaces]]\nnumber = 0\nname = "Hall"\n\n[[assets]]\n',
        "spaces[1].number: must be 1 or more.",
        id="space number",
    ),
    pytest.param(
        lambda setup: with_hall(setup, whole_space="yes"),
        "maximum_occupancy = 60",
        'whole_space = "yes"\nmaximum_occupancy = 60',
        "assets[1].whole_space: must be true or false.",
        id="whole space",
    ),
    pytest.param(
        lambda setup: with_hall(setup, maximum_occupancy=0),
        "maximum_occupancy = 60",
        "maximum_occupancy = 0",
        "assets[1].maximum_occupancy: must be 1 or more.",
        id="maximum occupancy 0",
    ),
    pytest.param(
        lambda setup: with_hall(setup, maximum_occupancy=-1),
        "maximum_occupancy = 60",
        "maximum_occupancy = -1",
        "assets[1].maximum_occupancy: must be 1 or more.",
        id="maximum occupancy -1",
    ),
    pytest.param(
        lambda setup: with_plan(setup, 2, spot_word=""),
        'spot_word = "place"',
        'spot_word = ""',
        f"assets[1].floor_plans[2].spot_word: {NOT_EMPTY}",
        id="empty spot word",
    ),
    pytest.param(
        lambda setup: with_plan(setup, 2, spots=()),
        "spots = 40",
        "spots = 0",
        "assets[1].floor_plans[2].spots: must be 1 or more.",
        id="no spots",
    ),
    pytest.param(
        lambda setup: with_plan(setup, 1, spots=("A", "A", *("",) * 10)),
        "spots = 12",
        f'spots = ["A", "A", {UNNAMED}]',
        "assets[1].floor_plans[1].spots: the name 'A' is given to more than one spot.",
        id="same spot name",
    ),
    pytest.param(
        lambda setup: with_plan(setup, 1, spots=("A", 1, *("",) * 10)),
        "spots = 12",
        f'spots = ["A", 1, {UNNAMED}]',
        "assets[1].floor_plans[1].spots: must be a number of spots, written as "
        '12, or a list of their names, written as ["Bed 1", "Bed 2"], with "" '
        "for a spot without a name, each name given once.",
        id="spot name not a text",
    ),
    pytest.param(
        lambda setup: with_group(setup, 1, name=""),
        'name = "Front"',
        'name = ""',
        f"assets[1].floor_plans[1].groups[1].name: {NOT_EMPTY}",
        id="empty group name",
    ),
    pytest.param(
        lambda setup: with_group(setup, 2, word=""),
        'name = "Back"\nword = "section"',
        'name = "Back"\nword = ""',
        f"assets[1].floor_plans[1].groups[2].word: {NOT_EMPTY}",
        id="empty group word",
    ),
    pytest.param(
        lambda setup: with_group(setup, 1, spots=(0, 1)),
        "spots = [1, 2, 3, 4]",
        "spots = [0, 1]",
        "assets[1].floor_plans[1].groups[1].spots: must be a list of spot "
        "numbers, each given once.",
        id="group spot 0",
    ),
    pytest.param(
        lambda setup: with_group(setup, 1, spots=("", 1)),
        "spots = [1, 2, 3, 4]",
        'spots = ["", 1]',
        "assets[1].floor_plans[1].groups[1].spots: must be a list of spot "
        "numbers, each given once.",
        id="group spot not a number",
    ),
    pytest.param(
        lambda setup: replace(
            setup,
            availability_calendars=(
                replace(
                    setup.availability_calendars[0],
                    start_date=datetime(2027, 3, 1, 10, tzinfo=UTC),
                ),
            ),
        ),
        'asset = "Riverside Hall"\nspans',
        'asset = "Riverside Hall"\nstart_date = 2027-03-01T10:00:00Z\nspans',
        "availability_calendars[1].start_date: must be a date, written as 2027-01-31.",
        id="calendar start a moment",
    ),
    pytest.param(
        lambda setup: with_spans(
            setup,
            lambda span: (
                replace(span, weekday="monday") if span.weekday == "mon" else span
            ),
        ),
        '{ weekdays = ["mon", ',
        '{ weekdays = ["monday", ',
        "availability_calendars[1].spans[1].weekdays: must name its weekdays "
        "as: mon, tue, wed, thu, fri, sat, sun.",
        id="weekday",
    ),
    pytest.param(
        lambda setup: replace(
            setup,
            availability_calendars=(
                replace(
                    setup.availability_calendars[0],
                    spans=(
                        *setup.availability_calendars[0].spans,
                        f.AvailableSpan("mon", 10 * 60, 22 * 60),
                    ),
                ),
            ),
        ),
        '{ weekdays = ["mon", ',
        '{ weekdays = ["mon", "mon", ',
        f"availability_calendars[1].spans[1].weekdays: {NAMED_ONCE}",
        id="weekday twice",
    ),
    pytest.param(
        lambda setup: with_spans(setup, lambda span: replace(span, start=24 * 60)),
        'start = "10:00", end = "22:00"',
        'start = "24:00", end = "22:00"',
        f"availability_calendars[1].spans[1].start: {A_TIME}",
        id="span start 24:00",
    ),
    pytest.param(
        lambda setup: with_spans(setup, lambda span: replace(span, start=22 * 60)),
        'start = "10:00", end = "22:00"',
        'start = "22:00", end = "22:00"',
        "availability_calendars[1].spans[1].end: must be later than the start.",
        id="span end not after start",
    ),
    pytest.param(
        lambda setup: replace(
            setup,
            booking_types=(
                replace(setup.booking_types[0], name=" Rehearsal hire"),
                *setup.booking_types[1:],
            ),
        ),
        'name = "Rehearsal hire"',
        'name = " Rehearsal hire"',
        f"booking_types[1].name: {NOT_EMPTY}",
        id="booking type name",
    ),
    pytest.param(
        lambda setup: replace(
            setup, roles=(replace(setup.roles[0], booking_categories=()),)
        ),
        'booking_categories = ["private"]\nadmin_access',
        "booking_categories = []\nadmin_access",
        f"roles[1].booking_categories: {NAMED_ONCE}",
        id="role without categories",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, period_type="open"),
        'period_type = "flexible"',
        'period_type = "open"',
        "booking_configurations[1].period_type: must be one of: fixed, flexible.",
        id="period type",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, period_start=24 * 60),
        'period_start = "10:00"',
        'period_start = "24:00"',
        f"booking_configurations[1].period_start: {A_TIME}",
        id="period start 24:00",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, end_day=-1),
        "end_day = 0",
        "end_day = -1",
        "booking_configurations[1].end_day: must be 0 or more.",
        id="end day",
    ),
    pytest.param(
        lambda setup: with_configuration(
            setup, 1, period_start_weekdays=("mon", "mon")
        ),
        'period_start_weekdays = ["mon", "tue", "wed", "thu"]',
        'period_start_weekdays = ["mon", "mon"]',
        f"booking_configurations[1].period_start_weekdays: {NAMED_ONCE}",
        id="start weekday twice",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, interval=0),
        'interval = "1 h"',
        'interval = "0 h"',
        f"booking_configurations[1].interval: {A_LENGTH}",
        id="interval 0",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, maximum_length=None),
        'maximum_length = "8 h"\n',
        "",
        "booking_configurations[1].maximum_length: is missing.",
        id="flexible without maximum",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 2, interval=60),
        'period_start_weekdays = ["fri"]',
        'period_start_weekdays = ["fri"]\ninterval = "1 h"',
        f"booking_configurations[2].interval: {FIXED}",
        id="fixed with an interval",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, start_date=date(2027, 3, 2)),
        'maximum_length = "8 h"',
        'maximum_length = "8 h"\nstart_week = "2027-W9"',
        f"booking_configurations[1].start_week: {A_WEEK}",
        id="start week not a Monday",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, stop_date=date(2027, 3, 1)),
        'maximum_length = "8 h"',
        'maximum_length = "8 h"\nstop_week = "W09"',
        f"booking_configurations[1].stop_week: {A_WEEK}",
        id="stop week not a Sunday",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, status="on"),
        'status = "enabled"',
        'status = "on"',
        "booking_configurations[1].status: must be one of: enabled, disabled.",
        id="status",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, consumption_mode="solo"),
        'consumption_mode = "exclusive"',
        'consumption_mode = "solo"',
        "booking_configurations[1].consumption_mode: must be one of: exclusive, "
        "shared.",
        id="consumption mode",
    ),
    pytest.param(
        lambda setup: with_configuration(setup, 1, every_weeks=53),
        'consumption_mode = "exclusive"',
        'consumption_mode = "exclusive"\nevery_weeks = 53',
        "booking_configurations[1].every_weeks: must be 52 or less.",
        id="every 53 weeks",
    ),
    pytest.param(
        lambda setup: with_entry(setup, tiers_go_by="hours"),
        'tiers_go_by = "length"',
        'tiers_go_by = "hours"',
        "price_books[1].entries[1].tiers_go_by: must be one of: length, time of day.",
        id="tiers go by",
    ),
    pytest.param(
        lambda setup: with_entry(setup, billing_basis="per_head"),
        'billing_basis = "per_booking"',
        'billing_basis = "per_head"',
        "price_books[1].entries[1].billing_basis: must be one of: per_booking, "
        "per_capacity_unit.",
        id="billing basis",
    ),
    pytest.param(
        lambda setup: with_entry(setup, tiers=()),
        'tiers = [\n  { up_to = "8 h", paid_per = "1 h", amount = 400 },\n]',
        "tiers = []",
        "price_books[1].entries[1].tiers: must hold one or more price tiers.",
        id="no tiers",
    ),
    pytest.param(
        lambda setup: with_tier(setup, paid_per=0),
        'paid_per = "1 h", amount = 400',
        'paid_per = "0 min", amount = 400',
        f"price_books[1].entries[1].tiers[1].paid_per: {A_LENGTH}",
        id="paid per 0",
    ),
    pytest.param(
        lambda setup: with_tier(setup, amount=-1),
        "amount = 400",
        "amount = -0.01",
        "price_books[1].entries[1].tiers[1].amount: must be zero or more.",
        id="negative amount",
    ),
    pytest.param(
        lambda setup: with_tier(setup, amount="400"),
        "amount = 400",
        'amount = "400"',
        "price_books[1].entries[1].tiers[1].amount: must be an amount, written "
        "as 250 or 249.50.",
        id="amount as a text",
    ),
]


@pytest.mark.parametrize(("change", "old", "new", "fault"), AS_READ)
@pytest.mark.django_db
def test_a_setup_built_in_code_meets_the_readers_rules(
    change: Callable[[f.Setup], f.Setup], old: str, new: str, fault: str
) -> None:
    built = change(scenario("riverside-hall"))
    with pytest.raises(f.SetupError) as written:
        f.write(built)
    assert fault in written.value.faults
    text = text_of("riverside-hall")
    assert old in text
    with pytest.raises(f.SetupError) as read:
        f.parse(text.replace(old, new, 1))
    assert fault in read.value.faults
    with pytest.raises(f.SetupError) as loaded:
        setups.load(built, today=TODAY)
    assert fault in loaded.value.faults
    assert not Organization.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", " Guest bedroom"),
        ("name", "Guest bedroom "),
        ("description", "\n A bedroom description\n"),
        ("unit", " bed"),
        ("unit", "\tbed\n"),
        ("spot", " Bed 1 "),
        ("plan", " Beds"),
        ("group", "Window side "),
    ],
)
def test_model_valid_capacity_text_round_trips_exactly(field: str, value: str) -> None:
    with application_login():
        org = setups.load(scenario("four-beds"), today=TODAY)
        plan = AssetInventory.objects.of(org).get()
        row: models.Model
        if field == "spot":
            row = Spot.objects.of(org).get(floor_plan=plan, number=1)
            row.name = value
        elif field == "group":
            row = SpotGroup(organization=org, floor_plan=plan, name=value, word="side")
        elif field in ("unit", "plan"):
            row = plan
            setattr(row, "name" if field == "plan" else "unit", value)
        else:
            row = Space.objects.of(org).get()
            setattr(row, field, value)
        row.full_clean()
        row.save()
        exported = setups.export(org)
        assert f.parse(f.write(exported)) == exported
        copied = replace(
            exported, organization=replace(exported.organization, subdomain="copy")
        )
        assert (
            setups.export(setups.load(f.parse(f.write(copied)), today=TODAY)) == copied
        )


@pytest.mark.django_db
def test_programmatic_load_refuses_what_the_reader_refuses() -> None:
    with pytest.raises(f.SetupError, match="spot 4 is in the group 'Front' already"):
        setups.load(changed_riverside("two-groups"), today=TODAY)
    assert not Organization.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["capacity", "new-asset"])
def test_export_captures_one_state_during_a_configuration_edit(change: str) -> None:
    with application_login():
        org = setups.load(f.read(SETUPS / "example.toml"), today=TODAY)
        asset = Asset.objects.of(org).order_by("number").first()
        assert asset is not None
        capture_reached, writer_started, writer_done = (
            threading.Event() for _ in range(3)
        )
        writer_pids: list[int] = []
        errors: list[Exception] = []
        original_select_related = QuerySet.select_related

        def edit() -> None:
            try:
                assert capture_reached.wait(10)
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid(), current_user")
                    pid, login = cursor.fetchone()
                    assert login == "randirandi_app"
                    writer_pids.append(pid)
                    writer_started.set()
                    if change == "capacity":
                        made_up.spots(made_up.floor_plan_of(asset), 8)
                    else:
                        Asset.objects.create(organization=org, name="New asset")
                    Asset.objects.of(org).filter(pk=asset.pk).update(
                        name="Changed asset"
                    )
            # Surface every worker failure in the main test thread.
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            finally:
                connection.close()
                writer_done.set()

        def during_capture(qs: QuerySet[Asset], *fields: str) -> QuerySet[Asset]:
            if qs.model is Asset and fields == ("space",):
                capture_reached.set()
                assert writer_started.wait(10)
                deadline = time.monotonic() + 10
                # The editor must have either committed or actually reached a
                # blocking lock; scheduling luck cannot satisfy this test.
                while not writer_done.is_set():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks "
                            "WHERE pid=%s AND NOT granted)",
                            (writer_pids[0],),
                        )
                        if cursor.fetchone()[0]:
                            break
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            return original_select_related(qs, *fields)

        worker = threading.Thread(target=edit)
        worker.start()
        try:
            with patch.object(QuerySet, "select_related", during_capture):
                exported = setups.export(org)
        finally:
            worker.join(timeout=15)
        assert not worker.is_alive()
        assert not errors
        written = f.parse(f.write(exported))
        clone = setups.load(
            replace(
                written, organization=replace(written.organization, subdomain="copy")
            ),
            today=TODAY,
        )
        copied_asset = Asset.objects.of(clone).get(number=asset.number)
        if change == "capacity":
            capacity = made_up.floor_plan_of(copied_asset).capacity
            assert (copied_asset.name, capacity) in (
                (asset.name, 1),
                ("Changed asset", 8),
            )
        else:
            original_count = len(f.read(SETUPS / "example.toml").assets)
            assert (copied_asset.name, Asset.objects.of(clone).count()) in (
                (asset.name, original_count),
                ("Changed asset", original_count + 1),
            )
        assert Asset.objects.of(org).get(pk=asset.pk).name == "Changed asset"


def share_locks() -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() "
            "AND mode='ShareLock' AND locktype='relation'"
        )
        count: int = cursor.fetchone()[0]
        return count


@pytest.mark.django_db(transaction=True)
def test_export_reloads_organization_and_releases_locks_after_success_or_refusal() -> (
    None
):
    with application_login():
        org = setups.load(scenario("four-beds"), today=TODAY)
        Organization.objects.filter(pk=org.pk).update(name="Changed organization")
        assert setups.export(org).organization.name == "Changed organization"
        refused = f.SetupError(["A floor plan the file cannot hold."])
        with (
            patch.object(setups, "_floor_plans", side_effect=refused),
            pytest.raises(f.SetupError),
        ):
            setups.export(org)
        assert share_locks() == 0
        # Within an outer transaction our reads include its pending edits,
        # and its configuration locks correctly last until the outer commit.
        with transaction.atomic():
            Organization.objects.filter(pk=org.pk).update(name="Uncommitted change")
            assert setups.export(org).organization.name == "Uncommitted change"
            assert share_locks() > 0
        assert share_locks() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", SCENARIOS, ids=lambda path: path.stem)
def test_application_load_check_export_and_reload_give_the_same_file(
    path: Path,
) -> None:
    with application_login():
        written = f.read(path)
        setups.check(written, today=TODAY)
        assert not Organization.objects.exists()
        org = setups.load(written, today=TODAY)
        exported = setups.export(org)
        assert exported == in_order(written)
        # The same file, with the one thing a load adds: the scenarios leave
        # each calendar's start date to the day of the load.
        undated = replace(
            exported,
            availability_calendars=tuple(
                replace(calendar, start_date=None)
                for calendar in exported.availability_calendars
            ),
        )
        assert path.read_text().endswith(f.write(undated))
        again = replace(
            exported, organization=replace(exported.organization, subdomain="second")
        )
        assert setups.export(setups.load(again, today=TODAY)) == again


@pytest.mark.django_db
@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_a_format_one_setup_exports_as_three_without_changing_its_values(
    path: Path,
) -> None:
    legacy = f.read(path)
    org = setups.load(legacy, today=TODAY)
    exported = setups.export(org)
    assert exported.format == 3
    assert not exported.spaces
    # Every asset has the floor plan it made for itself, which a file leaves
    # out: one spot without a name.
    assert all(asset.floor_plans == () for asset in exported.assets)
    assert all(
        made_up.floor_plan_of(asset).capacity == 1 for asset in Asset.objects.of(org)
    )
    assert exported == in_order(legacy)
    copied = replace(
        exported, organization=replace(exported.organization, subdomain="copy")
    )
    loaded = setups.load(f.parse(f.write(copied)), today=TODAY)
    assert setups.export(loaded) == copied


@pytest.mark.django_db
def test_empty_spaces_and_out_of_order_numbers_survive_export() -> None:
    original = scenario("four-beds")
    setup = replace(original, spaces=(f.Space(8, "Empty room"), *original.spaces))
    org = setups.load(setup, today=TODAY)
    assert Space.objects.create(organization=org, name="Next room").number == 9
    exported = setups.export(org)
    assert [s.number for s in exported.spaces] == [1, 8, 9]
    other = replace(
        exported, organization=replace(exported.organization, subdomain="other")
    )
    assert setups.export(setups.load(f.parse(f.write(other)), today=TODAY)) == other


@pytest.mark.django_db
def test_bad_load_rolls_back_all_configuration_and_reports_later_faults() -> None:
    setup = scenario("four-beds")
    room = setup.assets[0]
    setup = replace(
        setup,
        availability_calendars=(),
        booking_configurations=(),
        assets=(replace(room, floor_plans=(replace(room.floor_plans[0], spots=()),)),),
        price_books=(f.PriceBook(1, "X" * 201, ()),),
    )
    # The setup file's own checks refuse it first.
    with pytest.raises(f.SetupError, match=r"floor_plans\[1\].spots: must be 1 or"):
        setups.load(setup, today=TODAY)
    # Past them, a native database rule (capacity above 0) also has to be
    # presented as a setup fault, and a following validation failure must
    # still run.
    with (
        patch.object(f, "validate"),
        pytest.raises(f.SetupError) as error,
    ):
        setups.load(setup, today=TODAY)
    assert any(
        fault.startswith("assets[1].floor_plans[1]") and "inventory_positive" in fault
        for fault in error.value.faults
    ), error.value.faults
    assert any("price_books[1].name" in fault for fault in error.value.faults)
    assert not Organization.objects.exists()
    assert not Space.across_organizations.exists()
    assert not Asset.across_organizations.exists()
    assert not AssetInventory.across_organizations.exists()


@pytest.mark.django_db
def test_format_three_commands() -> None:
    file = SETUPS / "scenarios" / "sauna-palace.toml"
    call_command("load_setup", str(file), "--check", stdout=StringIO())
    assert not Organization.objects.exists()
    call_command("load_setup", str(file), stdout=StringIO())
    output = StringIO()
    call_command("export_setup", "sauna-palace", stdout=output)
    exported = f.parse(output.getvalue())
    assert exported.format == 3
    # The command loads on the day it runs.
    day = exported.availability_calendars[0].start_date
    assert day is not None
    assert exported == in_order(f.read(file), today=day)


@pytest.mark.django_db
def test_riverside_hall_loads_its_two_floor_plans_and_their_groups() -> None:
    org = setups.load(scenario("riverside-hall"), today=TODAY)
    hall = Asset.objects.of(org).get()
    assert hall.maximum_occupancy == 60
    seated, standing = AssetInventory.objects.of(org).order_by("id")
    assert (seated.name, seated.unit, seated.capacity) == ("Seated", "seat", 12)
    assert (standing.name, standing.unit, standing.capacity) == (
        "Standing",
        "place",
        40,
    )
    groups = {
        group.name: (
            group.word,
            sorted(
                Spot.objects.of(org)
                .filter(group=group)
                .values_list("number", flat=True)
            ),
        )
        for group in SpotGroup.objects.of(org)
    }
    assert groups == {
        "Front": ("section", [1, 2, 3, 4]),
        "Back": ("section", list(range(5, 13))),
    }
    assert not Spot.objects.of(org).filter(floor_plan=standing, group__isnull=False)
    containers = {
        config.booking_type.name: config.floor_plan
        for config in BookingConfiguration.objects.of(org).select_related(
            "booking_type", "floor_plan"
        )
    }
    assert containers == {
        "Rehearsal hire": None,
        "Concert": seated,
        "Hall hire": None,
        "Club night": standing,
    }


# The model page's quiz (2026-10-17, section 3), which stacks on the exclusive
# rehearsal hire since 2.97; and the two containers refused when saved.
QUIZ = """
[[booking_configurations]]
number = 5
asset = "Riverside Hall"
booking_type = "Concert"
period_type = "fixed"
period_start = "17:00"
period_end = "20:00"
end_day = 0
period_start_weekdays = ["thu"]
status = "enabled"
price_book = "Prices"
booking_categories = ["private"]
consumption_mode = "shared"
floor_plan = "Seated"
"""
# Standing places on the Friday of the seated concert: two floor plans of one
# asset at one time.
STANDING = """
[[booking_configurations]]
number = 5
asset = "Riverside Hall"
booking_type = "Club night"
period_type = "fixed"
period_start = "20:00"
period_end = "22:00"
end_day = 0
period_start_weekdays = ["fri"]
status = "enabled"
price_book = "Prices"
booking_categories = ["private"]
consumption_mode = "shared"
floor_plan = "Standing"
"""
BRUNCH = """
[[booking_configurations]]
number = 5
asset = "Riverside Hall"
booking_type = "Hall hire"
period_type = "fixed"
period_start = "11:00"
period_end = "14:00"
end_day = 0
period_start_weekdays = ["sun"]
status = "enabled"
price_book = "Prices"
booking_categories = ["private"]
consumption_mode = "exclusive"
"""


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("container", "fault"),
    [
        (
            STANDING,
            "booking_configurations[5].status: It covers the same time as "
            "container 002 on Riverside Hall; the two book different floor "
            "plans of the asset.",
        ),
        (
            BRUNCH,
            "booking_configurations[5]: Its date period that starts on Sunday "
            "7 March 2027 at 11:00 is not covered by the asset's availability: "
            "the asset is not available from 11:00 on Sunday 7 March 2027.",
        ),
    ],
    ids=["standing", "brunch"],
)
def test_riverside_halls_refused_containers_are_refused_by_the_loader(
    container: str, fault: str
) -> None:
    text = (SETUPS / "scenarios" / "riverside-hall.toml").read_text()
    head, prices = text.split("\n[[price_books]]", 1)
    with pytest.raises(f.SetupError) as error:
        setups.load(
            f.parse(head + container + "\n[[price_books]]" + prices), today=TODAY
        )
    assert fault in error.value.faults, error.value.faults
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_riverside_halls_quiz_stacks_on_the_rehearsal_hire() -> None:
    text = (SETUPS / "scenarios" / "riverside-hall.toml").read_text()
    head, prices = text.split("\n[[price_books]]", 1)
    org = setups.load(f.parse(head + QUIZ + "\n[[price_books]]" + prices), today=TODAY)
    rehearsal, quiz = (
        BookingConfiguration.objects.of(org).get(number=number) for number in (1, 5)
    )
    # Thursday 17:00 to 20:00 over the exclusive hire's 10:00 to 18:00.
    assert (rehearsal.consumption_mode, quiz.consumption_mode) == (
        ConsumptionMode.EXCLUSIVE,
        ConsumptionMode.SHARED,
    )
    assert rehearsal.asset_id == quiz.asset_id
    assert rehearsal.covers_the_same_time_as(quiz)
    assert validator.clashes(org, now=made_up.at(TODAY, 0)) == []


@pytest.mark.django_db
def test_sunshine_names_its_four_beds() -> None:
    org = setups.load(scenario("four-beds"), today=TODAY)
    beds = AssetInventory.objects.of(org).get()
    assert (beds.name, beds.unit) == ("Beds", "bed")
    assert list(
        Spot.objects.of(org).order_by("number").values_list("number", "name")
    ) == [(n, f"Bed {n}") for n in range(1, 5)]


@pytest.mark.django_db(transaction=True)
def test_laundry_machines_are_independent_but_whole_room_hire_conflicts() -> None:
    with application_login():
        org = setups.load(scenario("laundry"), today=TODAY)
        assets = {a.name: a for a in Asset.objects.of(org)}
        start, end = (made_up.at(TODAY, hour) for hour in (10, 11))

        def take(name: str) -> TakenTime:
            return TakenTime.objects.take(
                organization=org,
                asset=assets[name],
                starts_at=start,
                ends_at=end,
                state="held",
            )

        washer = take("Washer A")
        dryer = take("Dryer")
        with pytest.raises(IntegrityError):
            take("Whole laundry room")
        with pytest.raises(IntegrityError):
            take("Washer A")
        washer.state = "cancelled"
        washer.save()
        dryer.state = "expired"
        dryer.save()
        take("Whole laundry room")
        with pytest.raises(IntegrityError):
            take("Washer B")


@pytest.mark.django_db(transaction=True)
def test_four_beds_are_spots_and_the_private_room_needs_them_all() -> None:
    with application_login():
        org = setups.load(scenario("four-beds"), today=TODAY)
        room = Asset.objects.of(org).get(name="Sunshine")
        beds = made_up.floor_plan_of(room)
        start = made_up.at(TODAY, 17)
        end = made_up.at(TODAY + timedelta(days=1), 8)

        def take(*numbers: int, state: str = "held") -> TakenTime:
            return TakenTime.objects.take(
                organization=org,
                asset=room,
                starts_at=start,
                ends_at=end,
                mode="shared" if numbers else "exclusive",
                quantity=len(numbers) or 1,
                state=state,
                units=made_up.spots_of(beds, *numbers) if numbers else None,
            )

        three = take(1, 2, 3)
        with pytest.raises(IntegrityError):
            take(3)
        fourth = take(4, state="confirmed")
        three.state = "expired"
        three.save()
        with pytest.raises(IntegrityError):
            take()
        fourth.state = "cancelled"
        fourth.save()
        private = take()
        claim = private.allocations.get()
        assert (claim.spot_id, claim.inventory_id) == (None, None)
        assert private.starts_at == start and private.ends_at == end


@pytest.mark.django_db(transaction=True)
def test_sauna_time_modes_prices_and_eighteen_oclock_boundary_are_stored() -> None:
    with application_login():
        org = setups.load(scenario("sauna-palace"), today=TODAY)
        # One space, and no asset covers it: each is booked on its own.
        assert Space.objects.of(org).count() == 1
        assert not Asset.objects.of(org).filter(whole_space=True).exists()
        assets = {a.name: a for a in Asset.objects.of(org)}
        assert [
            made_up.floor_plan_of(assets[f"Sauna {n}"]).capacity for n in (1, 2, 3)
        ] == [8, 8, 8]
        assert made_up.floor_plan_of(assets["Gym"]).capacity == 20
        configs = BookingConfiguration.objects.of(org).filter(asset=assets["Sauna 1"])
        shared, private = (
            configs.get(consumption_mode="shared"),
            configs.get(consumption_mode="exclusive"),
        )
        assert (
            shared.period_start,
            shared.period_end,
            shared.period_start_weekdays,
        ) == (600, 1080, [0, 1, 2, 3, 4])
        assert (private.period_start, private.period_end) == (1080, 1320)
        price = PriceBookEntry.objects.of(org).get(booking_configuration=shared)
        assert price.billing_basis == "per_capacity_unit"
        assert list(
            price.tiers.order_by("up_to").values_list("up_to", "paid_per", "amount")
        ) == [(60, 60, 10000), (480, 60, 5000)]
        assert (
            PriceBookEntry.objects.of(org)
            .get(booking_configuration=private)
            .billing_basis
            == "per_booking"
        )
        start, boundary, end = (made_up.at(TODAY, hour) for hour in (17, 18, 22))
        TakenTime.objects.take(
            organization=org,
            asset=assets["Sauna 1"],
            starts_at=start,
            ends_at=boundary,
            mode=shared.consumption_mode,
            quantity=8,
            state="held",
            units=made_up.spots_of(made_up.floor_plan_of(assets["Sauna 1"])),
        )
        evening = TakenTime.objects.take(
            organization=org,
            asset=assets["Sauna 1"],
            starts_at=boundary,
            ends_at=end,
            mode=private.consumption_mode,
            state="held",
        )
        with pytest.raises(IntegrityError):
            evening.revise(
                starts_at=boundary - timedelta(minutes=1),
                ends_at=end,
                mode="exclusive",
                quantity=1,
            )
        for name in ("Sauna 2", "Sauna 3", "Gym"):
            TakenTime.objects.take(
                organization=org,
                asset=assets[name],
                starts_at=start,
                ends_at=end,
                state="held",
            )


@pytest.mark.django_db(transaction=True)
def test_loaded_sauna_has_one_winner_for_its_last_place() -> None:
    with application_login():
        org = setups.load(scenario("sauna-palace"), today=TODAY)
        asset = Asset.objects.of(org).get(name="Sauna 1")
        start, end = (made_up.at(TODAY, hour) for hour in (10, 12))
        TakenTime.objects.take(
            organization=org,
            asset=asset,
            starts_at=start,
            ends_at=end,
            mode="shared",
            quantity=7,
            state="confirmed",
            units=made_up.spots_of(made_up.floor_plan_of(asset), *range(1, 8)),
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def purchase() -> None:
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SELECT current_user")
                    assert cursor.fetchone() == ("randirandi_app",)
                    cursor.execute(
                        "SET CONSTRAINTS capacity_no_double_booking DEFERRED"
                    )
                    TakenTime.objects.take(
                        organization=org,
                        asset=asset,
                        starts_at=start,
                        ends_at=end,
                        mode="shared",
                        state="held",
                        quantity=1,
                        units=made_up.spots_of(made_up.floor_plan_of(asset), 8),
                    )
                    barrier.wait(timeout=10)
                outcomes.append("committed")
            except (IntegrityError, OperationalError) as error:
                assert getattr(error.__cause__, "sqlstate", None) in ("23P01", "40P01")
                outcomes.append("refused")
            finally:
                connection.close()

        workers = [threading.Thread(target=purchase) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert not worker.is_alive()
        assert sorted(outcomes) == ["committed", "refused"]
        assert sum(TakenTime.objects.of(org).values_list("quantity", flat=True)) == 8
