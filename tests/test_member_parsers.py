# SPDX-License-Identifier: AGPL-3.0-only
"""The member parsers: the .xlsx reader on made-up files, and the one parser
(Unit members, Excel file) through the import."""

import zipfile
from io import BytesIO

import pytest

from randirandi.core.member_import import import_members
from randirandi.core.models import (
    ImportOutcome,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    Organization,
    Role,
)
from randirandi.member_parsers import (
    LEGACY_UNIT_MEMBERS,
    MADE_UP,
    MAX_EXPANDED,
    MAX_UPLOAD,
    PARSERS,
    UNIT_MEMBERS,
    parser_by_id,
    parser_named,
    read_xlsx,
)
from tests import made_up
from tests.xlsx import MAIN, workbook

pytestmark = pytest.mark.django_db

EXTERNAL_REFERENCES = "<externalReferences><externalReference/></externalReferences>"
HEADINGS: list[object] = [
    "Unit",
    "Address and flat",
    "Entrance",
    "Name 1",
    "Email 1",
    "Phone 1",
    "Name 2",
    "Email 2",
    "Phone 2",
]


def unit(number: int, *, second: bool = True) -> list[object]:
    return [
        f"U-{number:03d}",
        f"{number} Elm Street, H0{number}01",
        f"Entrance {'AB'[number % 2]}",
        f"First{number} Last{number}",
        f"first{number}@example.com",
        91000000 + number,
        f"Second{number} Last{number}" if second else None,
        f"second{number}@example.com" if second else None,
        92000000 + number if second else None,
    ]


def run_of(org: Organization, data: bytes) -> MemberImport:
    return import_members(
        org, UNIT_MEMBERS, UNIT_MEMBERS.read(data), file_size=len(data)
    )


@pytest.fixture
def org() -> Organization:
    organization = made_up.organization()
    role = Role.objects.create(organization=organization, name="Members")
    organization.standard_role = role
    organization.save(update_fields=["standard_role"])
    return organization


def test_the_registry_holds_the_one_parser_with_its_name() -> None:
    assert [p.name for p in PARSERS] == ["Unit members, Excel file"]
    assert parser_by_id("unit-members-xlsx") is UNIT_MEMBERS
    assert UNIT_MEMBERS.accept == ".xlsx"
    # The sandbox's parser can word an old run but is never offered.
    assert parser_by_id(MADE_UP.id) is None
    assert parser_named(MADE_UP.id) is MADE_UP
    assert parser_by_id("nothing") is None


def test_the_right_layout_is_read_into_members_and_memberships(
    org: Organization,
) -> None:
    data = workbook([HEADINGS, unit(1), unit(2)])
    run = run_of(org, data)
    assert run.outcome == ImportOutcome.APPLIED
    assert (run.created, run.granted, run.skipped) == (4, 4, 0)
    assert run.parser == "unit-members-xlsx" and run.file_size == len(data)
    first = Member.objects.of(org).get(email="first1@example.com")
    # A phone kept as a number in the sheet is a Norwegian number.
    assert first.phone == "+4791000001"
    assert first.full_name == "First1 Last1"
    link = MemberMembership.objects.of(org).get(member=first)
    flat = Membership.objects.of(org).get(pk=link.membership_id)
    assert (flat.identifier, flat.address, flat.address_unit, flat.section) == (
        "U-001",
        "1 Elm Street",
        "H0101",
        "Entrance B",
    )
    assert Membership.objects.of(org).count() == 2
    assert Member.objects.of(org).count() == 4


def test_shared_strings_and_whole_numbers_are_read_as_text() -> None:
    data = workbook([HEADINGS, unit(3)], shared=True)
    table = read_xlsx(data)
    assert table.columns == tuple(HEADINGS)
    assert table.rows[0][5] == "91000003"
    # A whole number written with an exponent is still a whole number.
    text = (
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="1">'
        '<c r="A1"><v>9.1234567E7</v></c><c r="B1"><v>1.5</v></c>'
        "</row></sheetData></worksheet>"
    )
    assert read_xlsx(workbook([], sheet_text=text)).columns == ("91234567", "1.5")


def test_a_missing_heading_fails_the_run_and_names_the_heading(
    org: Organization,
) -> None:
    headings = [h for h in HEADINGS if h != "Email 1"]
    rows = [[v for h, v in zip(HEADINGS, unit(1), strict=True) if h != "Email 1"]]
    run = run_of(org, workbook([headings, *rows]))
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] == "missing_column"
    assert run.report["column"] == "Email 1"
    assert not Member.objects.of(org).exists()


def test_two_people_on_a_row_share_the_unit(org: Organization) -> None:
    run_of(org, workbook([HEADINGS, unit(4)]))
    links = MemberMembership.objects.of(org)
    assert links.count() == 2
    assert len({link.membership_id for link in links}) == 1


def test_an_empty_second_person_is_left_out(org: Organization) -> None:
    run = run_of(org, workbook([HEADINGS, unit(5, second=False)]))
    assert run.outcome == ImportOutcome.APPLIED
    assert (run.created, run.skipped) == (1, 0)
    assert run.report["findings"] == []


def test_row_numbers_are_the_sheets_own_and_empty_rows_are_kept(
    org: Organization,
) -> None:
    broken = unit(2)
    broken[4] = None  # no email on the person
    rows = [HEADINGS, unit(1), [], broken]
    table = read_xlsx(workbook(rows))
    assert len(table.rows) == 3
    run = run_of(org, workbook(rows))
    assert run.skipped == 1
    # The unit's row is sheet row 4: header, unit 1, the empty row, then this.
    assert {tuple(f["rows"]) for f in run.report["findings"]} == {(4,)}


def test_a_file_over_the_upload_cap_is_refused() -> None:
    table = read_xlsx(b"0" * (MAX_UPLOAD + 1))
    assert table.problem == "file_too_large"
    assert read_xlsx(b"0" * MAX_UPLOAD).problem == "invalid_xlsx"


def test_a_zip_that_expands_too_far_is_refused(org: Organization) -> None:
    data = workbook(
        [HEADINGS, unit(1)], extra={"xl/padding.bin": b"0" * (MAX_EXPANDED + 1)}
    )
    assert len(data) < MAX_UPLOAD
    assert read_xlsx(data).problem == "xlsx_too_large"
    run = run_of(org, data)
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] == "xlsx_too_large"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an excel file",
        b"Unit,Address and flat\nU-001,1 Elm Street\n",
        b"PK\x03\x04 broken",
    ],
)
def test_a_file_that_is_not_xlsx_fails_the_run(org: Organization, data: bytes) -> None:
    assert read_xlsx(data).problem == "invalid_xlsx"
    run = run_of(org, data)
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] == "invalid_xlsx"


def test_a_cut_off_file_and_a_zip_without_a_workbook_are_not_xlsx() -> None:
    assert read_xlsx(workbook([HEADINGS])[:-30]).problem == "invalid_xlsx"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hello")
    assert read_xlsx(buffer.getvalue()).problem == "invalid_xlsx"


def test_more_than_one_sheet_is_refused() -> None:
    assert read_xlsx(workbook([HEADINGS, unit(1)], sheets=2)).problem == "not_one_sheet"


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "workbook_rels_extra": (
                '<Relationship Id="rId9" Type="x" Target="http://example.test/a.xlsx" '
                'TargetMode="External"/>'
            )
        },
        {"workbook_extra": EXTERNAL_REFERENCES},
        {"extra": {"xl/externalLinks/externalLink1.xml": "<externalLink/>"}},
        {
            "sheet_rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Type="hyperlink" '
                'Target="file:///etc/passwd" TargetMode="External"/></Relationships>'
            )
        },
    ],
)
def test_an_external_reference_is_refused(arguments: dict[str, object]) -> None:
    data = workbook([HEADINGS, unit(1)], **arguments)  # type: ignore[arg-type]
    assert read_xlsx(data).problem == "external_reference"


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE sheet [<!ENTITY a "aaaa">]>',
        '<!DOCTYPE lolz SYSTEM "http://example.test/x.dtd">',
        '<!doctype x [<!entity b "bbbb">]>',
    ],
)
def test_a_dtd_or_an_entity_is_refused(declaration: str) -> None:
    text = (
        f'<?xml version="1.0"?>{declaration}<worksheet xmlns="{MAIN}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>&a;</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    assert read_xlsx(workbook([], sheet_text=text)).problem == "invalid_xlsx"


def test_a_part_in_another_encoding_is_refused() -> None:
    text = f'<?xml version="1.0" encoding="UTF-16"?><worksheet xmlns="{MAIN}"/>'
    data = workbook([], extra={"xl/worksheets/sheet1.xml": text.encode("utf-16")})
    assert read_xlsx(data).problem == "invalid_xlsx"


def test_the_problem_never_holds_a_cell() -> None:
    table = read_xlsx(workbook([HEADINGS, unit(1)], sheets=2))
    assert "first1@example.com" not in repr(table)
    assert table.columns == () and table.rows == ()


def _cell(value: str, kind: str = "") -> str:
    return (
        f'<?xml version="1.0"?><worksheet xmlns="{MAIN}"><sheetData><row r="1">'
        f'<c r="A1"{kind}><v>{value}</v></c></row></sheetData></worksheet>'
    )


def test_a_huge_exponent_stays_text_and_never_hangs() -> None:
    for text in ("1E+20000000", "1E-20000000", "-9E+999999999", "1" * 900):
        table = read_xlsx(workbook([], sheet_text=_cell(text)))
        assert table.problem == "" and table.columns == (text,)
    table = read_xlsx(workbook([], sheet_text=_cell("4.791234567E+10")))
    assert table.columns == ("47912345670",)


def test_a_million_empty_strings_is_refused_with_bounded_memory() -> None:
    import tracemalloc

    shared = (
        f'<?xml version="1.0"?><sst xmlns="{MAIN}">' + "<si/>" * 1_000_000 + "</sst>"
    )
    data = workbook([["x"]], extra={"xl/sharedStrings.xml": shared})
    assert len(data) < MAX_UPLOAD
    tracemalloc.start()
    try:
        table = read_xlsx(data)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert table.problem == "xlsx_too_large"
    assert peak < 120 * 1024 * 1024


def test_a_part_compressed_with_lzma_is_refused() -> None:
    good = workbook([["a"]])
    buffer = BytesIO()
    with (
        zipfile.ZipFile(BytesIO(good)) as source,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_LZMA) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, source.read(info))
    assert read_xlsx(good).problem == ""
    assert read_xlsx(buffer.getvalue()).problem == "invalid_xlsx"


def test_phonetic_text_is_left_out_of_a_shared_and_an_inline_string() -> None:
    shared = (
        f'<?xml version="1.0"?><sst xmlns="{MAIN}"><si><r><t>Ma</t></r>'
        '<r><t>ry</t></r><rPh sb="0" eb="1"><t>HINT</t></rPh></si></sst>'
    )
    sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{MAIN}"><sheetData><row r="1">'
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="inlineStr"><is><t>In</t><rPh><t>HINT</t></rPh></is></c>'
        "</row></sheetData></worksheet>"
    )
    table = read_xlsx(
        workbook([], sheet_text=sheet, extra={"xl/sharedStrings.xml": shared})
    )
    assert table.columns == ("Mary", "In")


@pytest.mark.parametrize("index", ["-1", "1", "x"])
def test_a_shared_string_index_outside_the_strings_is_refused(index: str) -> None:
    sheet = _cell(index, ' t="s"')
    shared = f'<?xml version="1.0"?><sst xmlns="{MAIN}"><si><t>only</t></si></sst>'
    table = read_xlsx(
        workbook([], sheet_text=sheet, extra={"xl/sharedStrings.xml": shared})
    )
    assert table.problem == "invalid_xlsx"


def test_legacy_parser_links_preserve_the_recorded_identity(org: Organization) -> None:
    old = LEGACY_UNIT_MEMBERS
    assert parser_by_id(old.id) is old
    assert parser_named(old.id) is old
    data = workbook([HEADINGS, unit(1)])
    run = import_members(org, old, old.read(data), file_size=len(data))
    run.refresh_from_db()
    assert run.outcome == ImportOutcome.APPLIED
    assert run.parser == old.id
    assert parser_named(run.parser) is old
    assert old.column_map == UNIT_MEMBERS.column_map
    assert old.layout == UNIT_MEMBERS.layout
