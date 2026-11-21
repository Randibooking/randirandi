# SPDX-License-Identifier: AGPL-3.0-only
"""Closures in the setup file and the export after edits on the availability
screens : written, read, loaded and exported again to the same
setup; a faulty closure is refused with every fault listed."""

from dataclasses import replace
from datetime import date

import pytest

from randirandi import setup_file
from randirandi.core import availability_admin as admin
from randirandi.core import setup as setups
from randirandi.core.models import Asset, AvailabilityClosure
from tests.test_asset_screens import TODAY, admin_for, reloaded
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
HALL = SETUPS / "scenarios/riverside-hall.toml"
FULL = [[0, 1440]]


def hall() -> setup_file.Setup:
    return setup_file.read(HALL)


def test_the_export_after_availability_edits_reloads_to_the_same_setup() -> None:
    org = setups.load(hall(), today=TODAY)
    who = admin_for(org)
    asset = Asset.objects.of(org).order_by("pk").first()
    assert asset is not None
    week = [FULL for _ in range(7)]
    admin.apply(
        who,
        asset.pk,
        {"kind": "week-make", "start_date": "2027-06-07", "week": week},
        now=None,
    )
    for first, last, reason in (
        ("2027-06-24", "2027-06-24", "holiday"),
        ("2027-07-05", "2027-07-18", "repair"),
        ("2027-09-01", "2027-09-01", ""),
    ):
        admin.apply(
            who,
            asset.pk,
            {
                "kind": "closure-make",
                "first_date": first,
                "last_date": last,
                "reason": reason,
            },
            now=None,
        )
    exported, again = reloaded(org, "second")
    assert again == exported
    assert [(c.first_date, c.last_date, c.reason) for c in exported.closures] == [
        (date(2027, 6, 24), date(2027, 6, 24), "holiday"),
        (date(2027, 7, 5), date(2027, 7, 18), "repair"),
        (date(2027, 9, 1), date(2027, 9, 1), ""),
    ]
    assert {c.asset for c in exported.closures} == {asset.name}
    assert date(2027, 6, 7) in [c.start_date for c in exported.availability_calendars]


def test_a_closure_is_loaded_as_a_row_and_exported_in_order() -> None:
    setup = hall()
    name = setup.assets[0].name
    closures = (
        setup_file.Closure(name, date(2027, 8, 3), date(2027, 8, 4), "repair"),
        setup_file.Closure(name, date(2027, 7, 1), date(2027, 7, 1), ""),
    )
    org = setups.load(replace(setup, closures=closures), today=TODAY)
    rows = AvailabilityClosure.objects.of(org).order_by("first_date")
    assert [(r.first_date, r.reason) for r in rows] == [
        (date(2027, 7, 1), ""),
        (date(2027, 8, 3), "repair"),
    ]
    assert [c.first_date for c in setups.export(org).closures] == [
        date(2027, 7, 1),
        date(2027, 8, 3),
    ]


def faults(setup: setup_file.Setup) -> list[str]:
    with pytest.raises(setup_file.SetupError) as refused:
        setup_file.validate(setup)
    return refused.value.faults


def test_a_faulty_closure_is_refused_with_every_fault() -> None:
    name = hall().assets[0].name
    found = faults(
        replace(
            hall(),
            closures=(
                setup_file.Closure("Nowhere", date(2027, 1, 1), date(2027, 1, 1)),
                setup_file.Closure(name, date(2027, 2, 2), date(2027, 2, 1)),
                setup_file.Closure(name, date(1999, 1, 1), date(1999, 1, 1)),
                setup_file.Closure(name, date(2027, 2, 2), date(2027, 2, 2), "party"),
            ),
        )
    )
    text = "\n".join(found)
    assert "closures[1].asset" in text
    assert "closures[2]" in text and "closures[3]" in text and "closures[4]" in text
    assert len(found) >= 4


def test_a_file_in_an_older_format_cannot_hold_closures() -> None:
    name = hall().assets[0].name
    one = setup_file.Closure(name, date(2027, 2, 2), date(2027, 2, 2))
    found = faults(replace(hall(), format=1, closures=(one,)))
    assert any("format" in fault for fault in found)


def test_closures_are_written_and_read_back_and_nothing_else_is_known() -> None:
    setup = hall()
    name = setup.assets[0].name
    with_closures = replace(
        setup,
        closures=(
            setup_file.Closure(name, date(2027, 5, 17), date(2027, 5, 17), "holiday"),
        ),
    )
    text = setup_file.write(with_closures)
    assert "[[closures]]" in text
    assert setup_file.parse(text) == with_closures
    bad = text.replace('reason = "holiday"', 'reason = "holiday"\ncolour = "red"')
    with pytest.raises(setup_file.SetupError, match="colour"):
        setup_file.parse(bad)
    bad_date = text.replace("2027-05-17", "17 May")
    with pytest.raises(setup_file.SetupError):
        setup_file.parse(bad_date)
