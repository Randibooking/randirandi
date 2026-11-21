# SPDX-License-Identifier: AGPL-3.0-only
"""Parsers: code that reads one organization's member list in its own layout.

An administrator picks a parser, gives it a file and starts the import.
The parser turns the file into a `MemberTable` and says how to read it
(column map and layout, see core.member_layout). Reconciliation (core.member_import)
does the rest.

Reading an uploaded .xlsx is reading untrusted input. `read_xlsx` uses the
standard library only (no new dependency) and refuses: an upload over
`MAX_UPLOAD`, an archive that expands past `MAX_EXPANDED` or holds more than
`MAX_PARTS` parts, a workbook with other than one sheet, any external
reference, and any XML part with a DTD or an entity declaration. A refusal is
a fixed problem code on the table; no cell is copied into a message.
"""

import posixpath
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from xml.parsers import expat

from randirandi.member_file import MemberTable, read_csv

MAX_UPLOAD = 5 * 1024 * 1024
MAX_EXPANDED = 25 * 1024 * 1024
MAX_PARTS = 500
MAX_ROWS = 20_000
MAX_COLUMNS = 100
MAX_CELL = 1_000
MAX_ELEMENTS = 300_000
MAX_NUMBER_DIGITS = 30

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WORKSHEET = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
)
DANGEROUS = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
REFERENCE = re.compile(r"([A-Z]{1,3})([0-9]+)")


class UnreadableXlsxError(Exception):
    """The file cannot be read; `code` is the fixed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Parser:
    id: str
    name: str
    # One line for the screen: what the parser reads.
    reads: str
    # The file extension it takes, with the dot.
    accept: str
    read: Callable[[bytes], MemberTable]
    column_map: dict[str, str] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)


def _read_part(archive: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    """One part of the archive, as XML bytes with no DTD or entity; the bytes
    read are counted against `budget`, whatever the headers say."""
    try:
        with archive.open(name) as part:
            data = part.read(budget[0] + 1)
    except KeyError, zipfile.BadZipFile, RuntimeError, OSError, EOFError:
        raise UnreadableXlsxError("invalid_xlsx") from None
    budget[0] -= len(data)
    if budget[0] < 0:
        raise UnreadableXlsxError("xlsx_too_large")
    # UTF-8 only: another encoding could hide a declaration from the check.
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise UnreadableXlsxError("invalid_xlsx") from None
    if DANGEROUS.search(data):
        raise UnreadableXlsxError("invalid_xlsx")
    return data


def _refuse(*_: object) -> None:
    raise UnreadableXlsxError("invalid_xlsx")


def _name(name: str) -> str:
    namespace, separator, local = name.rpartition("}")
    return f"{{{namespace}}}{local}" if separator else name


def _xml(data: bytes) -> ElementTree.Element:
    """The part parsed by expat with its DTD and entity handlers refusing: the
    check in `_read_part` is the first guard, this the second."""
    builder = ElementTree.TreeBuilder()
    parser: Any = expat.ParserCreate(namespace_separator="}")
    elements = [0]

    def start(tag: str, attrs: dict[str, str]) -> None:
        # The budget counts bytes; each element costs far more memory than
        # its bytes, so the elements are counted too.
        elements[0] += 1
        if elements[0] > MAX_ELEMENTS:
            raise UnreadableXlsxError("xlsx_too_large")
        builder.start(_name(tag), {_name(k): v for k, v in attrs.items()})

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda tag: builder.end(_name(tag))
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    try:
        parser.Parse(data, True)
        return builder.close()
    except expat.ExpatError:
        raise UnreadableXlsxError("invalid_xlsx") from None


def _target(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


def _external(relationships: ElementTree.Element) -> bool:
    return any(
        item.get("TargetMode", "").lower() == "external"
        for item in relationships.iter(f"{RELATIONSHIPS}Relationship")
    )


def _shared_strings(archive: zipfile.ZipFile, budget: list[int]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(_read_part(archive, "xl/sharedStrings.xml", budget))
    return [_plain(item) for item in root.iter(f"{MAIN}si")]


def _plain(element: ElementTree.Element) -> str:
    """The text of a string item: its own text and its runs', never the
    phonetic hints (rPh) beside it."""
    parts: list[str] = []
    for child in element:
        if child.tag == f"{MAIN}t":
            parts.append(child.text or "")
        elif child.tag == f"{MAIN}r":
            parts.extend(t.text or "" for t in child.findall(f"{MAIN}t"))
    return "".join(parts)


def _number(text: str) -> str:
    """A number cell as the text a person would have typed: a whole number
    without its exponent or decimals (a phone number kept as a number)."""
    if len(text) > MAX_CELL:
        return text
    try:
        value = Decimal(text)
    except InvalidOperation:
        return text
    # int() of 1E+20000000 would never finish: a value past this size stays text.
    if not value.is_finite() or abs(value.adjusted()) > MAX_NUMBER_DIGITS:
        return text
    if value == value.to_integral_value():
        return str(int(value))
    return text


def _cell_text(cell: ElementTree.Element, strings: list[str]) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        inline = cell.find(f"{MAIN}is")
        return _plain(inline) if inline is not None else ""
    value = cell.find(f"{MAIN}v")
    text = (value.text or "") if value is not None else ""
    if kind == "s":
        try:
            index = int(text)
            if index < 0:
                raise IndexError(index)
            return strings[index]
        except ValueError, IndexError:
            raise UnreadableXlsxError("invalid_xlsx") from None
    if kind == "n":
        return _number(text)
    if kind == "e":
        return ""
    return text


def _column(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def read_xlsx(data: bytes) -> MemberTable:
    """The one sheet of an .xlsx as a table of text. Row numbers are the
    sheet's own: the table's first row is row 1, empty rows kept."""
    if len(data) > MAX_UPLOAD:
        return MemberTable((), (), "file_too_large")
    try:
        return _read(data)
    except UnreadableXlsxError as problem:
        return MemberTable((), (), problem.code)
    except zipfile.BadZipFile, RuntimeError, OSError, EOFError, ValueError:
        return MemberTable((), (), "invalid_xlsx")


def _read(data: bytes) -> MemberTable:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise UnreadableXlsxError("invalid_xlsx") from None
    with archive:
        names = archive.namelist()
        if len(names) > MAX_PARTS:
            raise UnreadableXlsxError("xlsx_too_large")
        if any(
            info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            for info in archive.infolist()
        ):
            raise UnreadableXlsxError("invalid_xlsx")
        if sum(info.file_size for info in archive.infolist()) > MAX_EXPANDED:
            raise UnreadableXlsxError("xlsx_too_large")
        if "xl/workbook.xml" not in names:
            raise UnreadableXlsxError("invalid_xlsx")
        if any(name.startswith("xl/externalLinks/") for name in names):
            raise UnreadableXlsxError("external_reference")
        budget = [MAX_EXPANDED]
        workbook = _xml(_read_part(archive, "xl/workbook.xml", budget))
        if workbook.find(f"{MAIN}externalReferences") is not None:
            raise UnreadableXlsxError("external_reference")
        sheets = list(workbook.iter(f"{MAIN}sheet"))
        if len(sheets) != 1:
            raise UnreadableXlsxError("not_one_sheet")
        rels_name = "xl/_rels/workbook.xml.rels"
        if rels_name not in names:
            raise UnreadableXlsxError("invalid_xlsx")
        relationships = _xml(_read_part(archive, rels_name, budget))
        if _external(relationships):
            raise UnreadableXlsxError("external_reference")
        wanted = sheets[0].get(f"{OFFICE}id")
        path = ""
        for item in relationships.iter(f"{RELATIONSHIPS}Relationship"):
            if item.get("Id") == wanted and item.get("Type") == WORKSHEET:
                path = _target("xl/workbook.xml", item.get("Target", ""))
        if not path.startswith("xl/") or path not in names:
            raise UnreadableXlsxError("invalid_xlsx")
        sheet_rels = f"{posixpath.dirname(path)}/_rels/{posixpath.basename(path)}.rels"
        if sheet_rels in names and _external(
            _xml(_read_part(archive, sheet_rels, budget))
        ):
            raise UnreadableXlsxError("external_reference")
        strings = _shared_strings(archive, budget)
        sheet = _xml(_read_part(archive, path, budget))
    return _table(sheet, strings)


def _table(sheet: ElementTree.Element, strings: list[str]) -> MemberTable:
    cells: dict[tuple[int, int], str] = {}
    last_row = 0
    width = 0
    row_number = 0
    for row in sheet.iter(f"{MAIN}row"):
        row_number = int(row.get("r") or row_number + 1)
        if not 1 <= row_number <= MAX_ROWS:
            raise UnreadableXlsxError("xlsx_too_large")
        column_number = -1
        for cell in row.findall(f"{MAIN}c"):
            reference = REFERENCE.fullmatch(cell.get("r", ""))
            column_number = (
                _column(reference.group(1)) if reference else column_number + 1
            )
            if column_number >= MAX_COLUMNS:
                raise UnreadableXlsxError("xlsx_too_large")
            text = _cell_text(cell, strings)
            if len(text) > MAX_CELL:
                raise UnreadableXlsxError("xlsx_too_large")
            if text.strip():
                cells[(row_number, column_number)] = text
                last_row = max(last_row, row_number)
                width = max(width, column_number + 1)
    rows = tuple(
        tuple(cells.get((number, index), "") for index in range(width))
        for number in range(1, last_row + 1)
    )
    if not rows:
        return MemberTable((), ())
    return MemberTable(rows[0], rows[1:])


# --- The parsers ---

# One row per unit, with up to two people; headings are in row 1.
UNIT_MEMBERS_COLUMN_MAP = {
    "membership": "Unit",
    "section": "Entrance",
    "full_name": "Name {n}",
    "email": "Email {n}",
    "phone": "Phone {n}",
}
UNIT_MEMBERS_LAYOUT: dict[str, Any] = {
    "header_row": 1,
    "country_code": "+47",
    "people_per_row": 2,
    # "2 Elm Street, H0201": the address, then the flat.
    "split": {
        "column": "Address and flat",
        "separator": ",",
        "fields": ["address", "address_unit"],
    },
}

UNIT_MEMBERS = Parser(
    id="unit-members-xlsx",
    name="Unit members, Excel file",
    reads=(
        "An Excel file (.xlsx), one row per unit: unit, address and flat, "
        "entrance, and up to two people with name, email and phone."
    ),
    accept=".xlsx",
    read=read_xlsx,
    column_map=UNIT_MEMBERS_COLUMN_MAP,
    layout=UNIT_MEMBERS_LAYOUT,
)

# What the product offers an administrator, in the order shown.
PARSERS: tuple[Parser, ...] = (UNIT_MEMBERS,)

# Compatibility for existing import links and recorded import history.
LEGACY_UNIT_MEMBERS = replace(UNIT_MEMBERS, id="torshov-kv8-excel")


def _read_csv_bytes(data: bytes) -> MemberTable:
    try:
        return read_csv(data.decode("utf-8"))
    except UnicodeDecodeError:
        return MemberTable((), (), "invalid_csv")


# The sandbox's made-up members (`import_dummy_members`): headings are the
# canonical field names, one person per row. Not offered to administrators.
MADE_UP = Parser(
    id="made-up-members",
    name="Made-up members, CSV file (sandbox)",
    reads="The bundled made-up members.",
    accept=".csv",
    read=_read_csv_bytes,
)


def parser_by_id(identifier: str) -> Parser | None:
    """A parser an administrator can pick."""
    return next(
        (
            parser
            for parser in (*PARSERS, LEGACY_UNIT_MEMBERS)
            if parser.id == identifier
        ),
        None,
    )


def parser_named(identifier: str) -> Parser | None:
    """Any parser that has ever run, to word an old run."""
    return next(
        (
            parser
            for parser in (*PARSERS, LEGACY_UNIT_MEMBERS, MADE_UP)
            if parser.id == identifier
        ),
        None,
    )
