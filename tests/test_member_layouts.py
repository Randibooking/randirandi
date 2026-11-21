# SPDX-License-Identifier: AGPL-3.0-only
"""Member lists in any layout, for a made-up rowing club ."""

import json
from typing import Any

import pytest
from django.db import connection

from randirandi.core.models import (
    ImportOutcome,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    Role,
)
from randirandi.member_file import MemberTable, read_csv
from tests import made_up
from tests.made_up import import_members

pytestmark = pytest.mark.django_db

HEADER = (
    "Boat",
    "Address",
    "Zone",
    "Adults",
    "Name 1",
    "Email 1",
    "Mobile 1",
    "Name 2",
    "Email 2",
    "Mobile 2",
    "Notes",
    "",
    "Notes",
)
MAP = {
    "membership": "Boat",
    "section": "Zone",
    "full_name": "Name {n}",
    "email": "Email {n}",
    "phone": "Mobile {n}",
}
LAYOUT: dict[str, Any] = {
    "header_row": 2,
    "empty_values": ["-"],
    "country_code": "+44",
    "people_per_row": 2,
    "split": {
        "column": "Address",
        "separator": ",",
        "fields": ["address", "address_unit"],
    },
    "people_count": "Adults",
    "fixed": {"postal_code": "AB1 2CD", "city": "Riverton", "country": "gb"},
}


def line(*cells: str) -> tuple[str, ...]:
    return (*cells, *("",) * (len(HEADER) - len(cells)))


def club(*rows: tuple[str, ...]) -> MemberTable:
    title = ("Riverside Rowing Club, member list",) + ("",) * (len(HEADER) - 1)
    return MemberTable(title, (HEADER, *rows))


@pytest.fixture
def source() -> made_up.Source:
    org = made_up.organization()
    role = Role.objects.create(organization=org, name="Members")
    org.standard_role = role
    org.save(update_fields=["standard_role"])
    return made_up.Source(org, column_map=MAP, layout=LAYOUT)


def codes(run: MemberImport) -> dict[str, list[list[int]]]:
    found: dict[str, list[list[int]]] = {}
    for item in run.report["findings"]:
        found.setdefault(item["code"], []).append(item["rows"])
    return found


def test_a_family_list_with_two_heading_rows_is_read_whole(
    source: made_up.Source,
) -> None:
    run = import_members(
        source,
        club(
            line(
                "B1",
                "1 Quay Road, Locker 12",
                "North",
                "2",
                "Ann Marie Hill",
                "Ann@Example.com",
                "07911 123456",
                "Tom Hill",
                "tom@example.com",
                "07911-123457",
                "x",
                "",
                "y",
            ),
            line(
                "B2",
                "2 Quay Road",
                "South",
                "3",
                "Jo Lake",
                "jo@example.com",
                "-",
                "-",
                "-",
                "-",
            ),
            line(
                "B3",
                "3 Quay Road, Locker 3",
                "-",
                "2",
                "Kim Pond",
                "-",
                "+44 7911 123458",
                "Lee Pond",
                "lee@example.com",
                "0044 7911 123459",
            ),
            line(
                "B4",
                "4 Quay Road",
                "East",
                "1",
                "Ann Marie Hill",
                "ann@example.com",
                "07911123456",
                "-",
                "-",
                "-",
            ),
        ),
    )
    assert run.outcome == ImportOutcome.APPLIED
    # Row 5 holds a person with no email: skipped, and said so by file row.
    assert (run.created, run.skipped) == (4, 1)
    assert codes(run) == {
        "invalid_member": [[5]],
        "people_count_differs": [[4]],
    }
    members = Member.objects.of(source.organization_id)
    ann = members.get(email="ann@example.com")
    assert (ann.full_name, ann.first_name, ann.last_name) == (
        "Ann Marie Hill",
        "Ann Marie",
        "Hill",
    )
    assert ann.phone == "+447911123456"
    assert members.get(email="tom@example.com").phone == "+447911123457"
    assert members.get(email="lee@example.com").phone == "+447911123459"
    assert members.get(email="jo@example.com").phone == ""
    assert not members.filter(full_name="Kim Pond").exists()
    assert (
        MemberMembership.objects.of(source.organization_id).filter(member=ann).count()
        == 2
    )
    first = Membership.objects.of(source.organization_id).get(identifier="B1")
    assert (first.address, first.address_unit, first.section) == (
        "1 Quay Road",
        "Locker 12",
        "North",
    )
    assert (first.postal_code, first.city, first.country) == (
        "AB1 2CD",
        "Riverton",
        "GB",
    )
    second = Membership.objects.of(source.organization_id).get(identifier="B2")
    assert (second.address, second.address_unit, second.section) == (
        "2 Quay Road",
        "",
        "South",
    )
    report = json.dumps(run.report)
    for value in ("Hill", "Pond", "example.com", "Quay"):
        assert value not in report


def test_a_changed_unit_or_section_is_updated_by_the_next_import(
    source: made_up.Source,
) -> None:
    first = line(
        "B1", "1 Quay Road, Locker 12", "North", "1", "Ann Hill", "ann@example.com"
    )
    import_members(source, club(first))
    moved = line(
        "B1", "1 Quay Road, Locker 13", "South", "1", "Ann Hill", "ann@example.com"
    )
    run = import_members(source, club(moved))
    assert (run.created, run.changed) == (0, 1)
    membership = Membership.objects.of(source.organization_id).get(identifier="B1")
    assert (membership.address_unit, membership.section) == ("Locker 13", "South")
    assert import_members(source, club(moved)).changed == 0


def test_two_rows_giving_one_membership_two_values_are_skipped(
    source: made_up.Source,
) -> None:
    run = import_members(
        source,
        club(
            line(
                "B1",
                "1 Quay Road, Locker 1",
                "North",
                "1",
                "Ann Hill",
                "ann@example.com",
            ),
            line(
                "B1",
                "1 Quay Road, Locker 2",
                "North",
                "1",
                "Tom Hill",
                "tom@example.com",
            ),
            line("B2", "2 Quay Road", "North", "1", "Jo Lake", "jo@example.com"),
        ),
    )
    assert run.created == 1 and run.skipped == 2
    assert codes(run)["inconsistent_membership"] == [[3], [4]]
    assert run.report["removals_deferred"]


@pytest.mark.parametrize(
    "given,expected",
    [
        ({"full_name": "Ann Marie Hill"}, ("Ann Marie Hill", "Ann Marie", "Hill")),
        ({"full_name": "Ann"}, ("Ann", "Ann", "")),
        ({"first_name": "Ann", "last_name": "Hill"}, ("Ann Hill", "Ann", "Hill")),
        ({"first_name": "Ann", "last_name": ""}, ("Ann", "Ann", "")),
        (
            {"full_name": "Dr Ann Hill", "first_name": "Ann", "last_name": "Hill"},
            ("Dr Ann Hill", "Ann", "Hill"),
        ),
    ],
)
def test_the_missing_names_are_made(
    source: made_up.Source, given: dict[str, str], expected: tuple[str, str, str]
) -> None:
    source.column_map = {}
    source.layout = {}
    names = ("full_name", "first_name", "last_name")
    columns = ("membership", "email", *given)
    run = import_members(
        source, MemberTable(columns, (("B1", "ann@example.com", *given.values()),))
    )
    assert run.created == 1
    person = Member.objects.of(source.organization_id).get()
    assert tuple(getattr(person, name) for name in names) == expected


def test_a_list_without_any_name_or_phone_column_rules(source: made_up.Source) -> None:
    source.column_map = {}
    source.layout = {}
    run = import_members(
        source, MemberTable(("membership", "email"), (("B1", "ann@example.com"),))
    )
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] == "missing_column"
    assert run.report["column"] == "full_name"
    # Full name alone, no phone column: enough.
    run = import_members(
        source,
        MemberTable(
            ("membership", "email", "full_name"),
            (("B1", "ann@example.com", "Ann Hill"),),
        ),
    )
    assert run.created == 1


def test_only_mapped_headings_must_be_unique_and_are_compared_trimmed(
    source: made_up.Source,
) -> None:
    source.column_map = {}
    source.layout = {}
    columns = (" membership ", "email", "full_name", "", "", "notes", "notes")
    row = ("B1", "ann@example.com", "Ann Hill", "a", "b", "c", "d")
    assert import_members(source, MemberTable(columns, (row,))).created == 1
    twice = MemberTable((*columns, "email "), (*[(*row, "z")],))
    stopped = import_members(source, twice)
    assert stopped.report["reason"] == "invalid_columns"


def test_cell_values_such_as_dashes_read_as_empty_but_only_those_named(
    source: made_up.Source,
) -> None:
    source.column_map = {}
    source.layout = {"empty_values": ["-", "n/a"]}
    columns = ("membership", "email", "full_name", "phone", "section")
    rows = (
        ("B1", "ann@example.com", "Ann Hill", "n/a", "-"),
        ("B2", "tom@example.com", "Tom Hill", "+441234567", "none"),
    )
    assert import_members(source, MemberTable(columns, rows)).created == 2
    assert (
        Member.objects.of(source.organization_id).get(email="ann@example.com").phone
        == ""
    )
    assert (
        Membership.objects.of(source.organization_id).get(identifier="B2").section
        == "none"
    )


def test_a_list_with_no_people_stops_and_changes_nothing(
    source: made_up.Source,
) -> None:
    import_members(
        source,
        club(line("B1", "1 Quay Road", "North", "1", "Ann Hill", "ann@example.com")),
    )
    run = import_members(
        source, club(line("B9", "9 Quay Road", "North", "0", "-", "-"))
    )
    assert run.outcome == ImportOutcome.STOPPED
    assert run.report["reason"] == "empty_list"


@pytest.mark.parametrize(
    "layout,reason",
    [
        ({"header_row": 0}, "invalid_layout"),
        ({"header_row": True}, "invalid_layout"),
        ({"header_row": 1.5}, "invalid_layout"),
        ({"people_per_row": 0}, "invalid_layout"),
        ({"unknown": 1}, "invalid_layout"),
        ({"country_code": "47"}, "invalid_layout"),
        ({"empty_values": "-"}, "invalid_layout"),
        ({"empty_values": [""]}, "invalid_layout"),
        ({"people_count": 3}, "invalid_layout"),
        ({"split": {"column": "Address", "separator": ","}}, "invalid_layout"),
        (
            {
                "split": {
                    "column": "Address",
                    "separator": "",
                    "fields": ["address", "section"],
                }
            },
            "invalid_layout",
        ),
        (
            {
                "split": {
                    "column": "Address",
                    "separator": ",",
                    "fields": ["address", "email"],
                }
            },
            "invalid_layout",
        ),
        # The split fills a field that the column map also names.
        (
            {
                "split": {
                    "column": "Address",
                    "separator": ",",
                    "fields": ["section", "address"],
                }
            },
            "invalid_layout",
        ),
        # Several people per row, but a person heading has no number.
        ({**LAYOUT, "people_per_row": 3}, "missing_column"),
        ([], "invalid_layout"),
        ("text", "invalid_layout"),
        ({**LAYOUT, "header_row": 9}, "invalid_columns"),
        (
            {
                "split": {
                    "column": "Nowhere",
                    "separator": ",",
                    "fields": ["address", "address_unit"],
                },
                "people_per_row": 2,
            },
            "missing_column",
        ),
    ],
)
def test_an_invalid_layout_stops_the_run(
    source: made_up.Source, layout: Any, reason: str
) -> None:
    data = club(line("B1", "1 Quay Road", "North", "1", "Ann Hill", "ann@example.com"))
    assert import_members(source, data).created == 1
    source.layout = layout
    run = import_members(source, data)
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] == reason or (
        reason == "invalid_layout" and run.report["reason"] == "invalid_layout"
    )
    assert Member.objects.of(source.organization_id).count() == 1


def test_numbered_headings_are_needed_exactly_when_a_row_has_several_people(
    source: made_up.Source,
) -> None:
    data = club(line("B1", "1 Quay Road", "North", "1", "Ann Hill", "ann@example.com"))
    source.column_map = {**MAP, "email": "Email 1"}
    assert import_members(source, data).report["reason"] == "invalid_layout"
    source.column_map = MAP
    source.layout = {**LAYOUT, "people_per_row": 1}
    assert import_members(source, data).report["reason"] == "invalid_layout"


def test_a_csv_with_title_rows_is_read_by_the_same_import(
    source: made_up.Source,
) -> None:
    text = "Riverside Rowing Club\nBoat,Name,Mail\nB1,Ann Hill,ann@example.com\n"
    source.column_map = {"membership": "Boat", "full_name": "Name", "email": "Mail"}
    source.layout = {"header_row": 2}
    run = import_members(source, read_csv(text))
    assert run.created == 1
    assert Member.objects.of(source.organization_id).get().full_name == "Ann Hill"


def test_a_bad_row_far_down_is_reported_by_the_files_row_number(
    source: made_up.Source,
) -> None:
    rows = [
        line(
            f"B{i}", f"{i} Quay Road", "North", "1", f"Ann{i} Hill", f"a{i}@example.com"
        )
        for i in range(10)
    ]
    rows[6] = line("B6", "6 Quay Road", "North", "1", "Ann Six", "bad")
    run = import_members(source, club(*rows))
    assert codes(run) == {"invalid_member": [[9]]}


@pytest.mark.django_db(transaction=True)
def test_migration_fills_full_names_and_refuses_a_reverse_that_loses_values(
    source: made_up.Source,
) -> None:
    from django.db.migrations.executor import MigrationExecutor

    previous = [("core", "0020_payment_create_phone")]
    try:
        MigrationExecutor(connection).migrate(previous)
        state = MigrationExecutor(connection).loader.project_state(previous).apps
        old_role = state.get_model("core", "Role")._base_manager.create(
            organization_id=source.organization_id, name="Old"
        )
        old = state.get_model("core", "Member")._base_manager.create(
            organization_id=source.organization_id,
            role_id=old_role.pk,
            first_name="Ann",
            last_name="Hill",
            email="ann@example.com",
            access="granted",
            origin="member import",
        )
        single = state.get_model("core", "Member")._base_manager.create(
            organization_id=source.organization_id,
            role_id=old_role.pk,
            first_name="Jo",
            email="jo@example.com",
            access="granted",
            origin="member import",
        )
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())
        members = Member.objects.of(source.organization_id)
        assert members.get(pk=old.pk).full_name == "Ann Hill"
        assert members.get(pk=single.pk).full_name == "Jo"
        # A source with a layout cannot exist here: part 0040 took the sources
        # away, so that loss is no longer reachable from the current schema.
        for lose in ("unit", "name"):
            if lose == "unit":
                Membership.objects.create(
                    organization_id=source.organization_id,
                    identifier="B1",
                    section="North",
                )
            if lose == "name":
                Membership.objects.of(source.organization_id).delete()
                members.filter(pk=old.pk).update(full_name="Dr Ann Hill")
            with pytest.raises(RuntimeError, match="Cannot reverse"):
                MigrationExecutor(connection).migrate(previous)
        members.filter(pk=old.pk).update(full_name="Ann Hill")
        # A blank full name, as one made outside an import has, loses nothing.
        members.filter(pk=single.pk).update(full_name="")
        MigrationExecutor(connection).migrate(previous)
    finally:
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())


def test_a_postcode_column_and_fixed_city_and_country(source: made_up.Source) -> None:
    source.column_map = {"membership": "Boat", "email": "Mail", "postal_code": "Post"}
    source.layout = {"fixed": {"city": " Riverton ", "country": "gb"}}
    columns = ("Boat", "Mail", "Post", "full_name")
    rows = (("B1", "ann@example.com", "AB1 2CD", "Ann Hill"),)
    assert import_members(source, MemberTable(columns, rows)).created == 1
    flat = Membership.objects.of(source.organization_id).get(identifier="B1")
    assert (flat.postal_code, flat.city, flat.country) == ("AB1 2CD", "Riverton", "GB")
    # Set and updated like the other membership values.
    moved = (("B1", "ann@example.com", "ZZ9 9ZZ", "Ann Hill"),)
    assert import_members(source, MemberTable(columns, moved)).changed == 1
    flat.refresh_from_db()
    assert flat.postal_code == "ZZ9 9ZZ"


def test_a_country_column_is_stored_upper_case_and_checked(
    source: made_up.Source,
) -> None:
    source.column_map = {"membership": "Boat", "email": "Mail", "country": "Land"}
    source.layout = {}
    columns = ("Boat", "Mail", "Land", "full_name")
    ok = (("B1", "ann@example.com", "no", "Ann Hill"),)
    assert import_members(source, MemberTable(columns, ok)).created == 1
    assert Membership.objects.of(source.organization_id).get().country == "NO"
    bad = (("B1", "ann@example.com", "Norway", "Ann Hill"),)
    run = import_members(source, MemberTable(columns, bad))
    assert codes(run) == {"invalid_member": [[2]]}


def test_two_rows_with_two_cities_for_one_membership_are_skipped(
    source: made_up.Source,
) -> None:
    source.column_map = {"membership": "Boat", "email": "Mail", "city": "Town"}
    source.layout = {}
    columns = ("Boat", "Mail", "Town", "full_name")
    rows = (
        ("B1", "ann@example.com", "Riverton", "Ann Hill"),
        ("B1", "tom@example.com", "Lakeside", "Tom Hill"),
    )
    run = import_members(source, MemberTable(columns, rows))
    assert run.created == 0 and run.skipped == 2
    assert codes(run) == {"inconsistent_membership": [[2], [3]]}


@pytest.mark.parametrize(
    "mapping,layout",
    [
        ({"city": "Town"}, {"fixed": {"city": "Riverton"}}),
        ({}, {"fixed": {"membership": "B1"}}),
        ({}, {"fixed": {"email": "ann@example.com"}}),
        ({}, {"fixed": {"full_name": "Ann Hill"}}),
        ({}, {"fixed": {"country": "Norway"}}),
        ({}, {"fixed": {"country": "N1"}}),
        ({}, {"fixed": {"city": "x" * 101}}),
        ({}, {"fixed": {"city": ""}}),
        ({}, {"fixed": {"city": 5}}),
        ({}, {"fixed": {"nowhere": "x"}}),
        ({}, {"fixed": ["city"]}),
        (
            {},
            {
                "fixed": {"address": "1 Quay Road"},
                "split": {
                    "column": "Town",
                    "separator": ",",
                    "fields": ["address", "section"],
                },
            },
        ),
    ],
)
def test_an_invalid_fixed_value_stops_the_run(
    source: made_up.Source, mapping: dict[str, str], layout: dict[str, Any]
) -> None:
    source.column_map = mapping
    source.layout = layout
    columns = ("membership", "email", "full_name", "Town")
    run = import_members(
        source, MemberTable(columns, (("B1", "ann@example.com", "Ann Hill", "x"),))
    )
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] == "invalid_layout"
    assert not Member.objects.of(source.organization_id).exists()


def test_an_unmapped_field_reads_a_column_of_its_own_name_unless_fixed(
    source: made_up.Source,
) -> None:
    source.column_map = {}
    source.layout = {}
    columns = ("membership", "email", "full_name", "city")
    rows = (("B1", "ann@example.com", "Ann Hill", "Lakeside"),)
    assert import_members(source, MemberTable(columns, rows)).created == 1
    assert Membership.objects.of(source.organization_id).get().city == "Lakeside"
    source.layout = {"fixed": {"city": "Riverton"}}
    assert import_members(source, MemberTable(columns, rows)).changed == 1
    assert Membership.objects.of(source.organization_id).get().city == "Riverton"
