# SPDX-License-Identifier: AGPL-3.0-only
"""Reading a member list in any layout: the part before reconciliation.

A source's `column_map` names the heading of each field; its `layout` says how
the table is laid out. Both are configuration; nothing here knows an
organization. A person's headings hold "{n}" when a row lists several people.
Problems are fixed reason codes; nothing here copies a cell into a message.

"""

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from randirandi.member_file import MemberTable

PERSON_FIELDS = ("full_name", "first_name", "last_name", "email", "phone")
MEMBERSHIP_FIELDS = (
    "membership",
    "address",
    "address_unit",
    "section",
    "postal_code",
    "city",
    "country",
)
FIELDS = (*PERSON_FIELDS, *MEMBERSHIP_FIELDS)
LIMITS = {
    "full_name": 300,
    "first_name": 150,
    "last_name": 150,
    "email": 254,
    "phone": 16,
    "membership": 100,
    "address": 200,
    "address_unit": 50,
    "section": 100,
    "postal_code": 20,
    "city": 100,
    "country": 2,
}
LAYOUT_KEYS = {
    "header_row",
    "empty_values",
    "country_code",
    "people_per_row",
    "split",
    "people_count",
    "fixed",
}
NUMBER = "{n}"


@dataclass(frozen=True)
class Layout:
    header_row: int
    empty_values: frozenset[str]
    country_code: str
    people_per_row: int
    split_column: str
    split_separator: str
    split_fields: tuple[str, ...]
    people_count: str
    fixed: dict[str, str]


class Problem(NamedTuple):
    """Why a table cannot be read: a fixed reason code and, for a missing
    column, the heading the parser looked for. The heading comes from the
    parser's own configuration, never from the file."""

    code: str
    heading: str = ""


@dataclass(frozen=True, repr=False)
class Plan:
    first_row: int
    data: tuple[tuple[str, ...], ...]
    empty_values: frozenset[str]
    country_code: str
    fixed: dict[str, str]
    people: tuple[dict[str, int], ...]
    membership: dict[str, int]
    split: tuple[int, str, tuple[str, ...]] | None
    count: int | None


@dataclass(repr=False)
class Row:
    number: int
    membership: dict[str, str]
    people: list[dict[str, str]]
    stated: str


def _whole(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _text(value: Any, longest: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= longest


def parse_layout(raw: Any) -> Layout | None:
    """The layout, or None when it is not valid."""
    if not isinstance(raw, dict) or set(raw) - LAYOUT_KEYS:
        return None
    header_row = raw.get("header_row", 1)
    people = raw.get("people_per_row", 1)
    empties = raw.get("empty_values", [])
    code = raw.get("country_code", "")
    count = raw.get("people_count", "")
    split = raw.get("split")
    given = raw.get("fixed", {})
    if not isinstance(given, dict):
        return None
    fixed: dict[str, str] = {}
    for name, value in given.items():
        # A fixed value is a membership field other than the identifier.
        if name not in MEMBERSHIP_FIELDS or name == "membership":
            return None
        if not _text(value, LIMITS[name]):
            return None
        fixed[name] = value.strip().upper() if name == "country" else value.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in fixed[name]):
            return None
    if "country" in fixed and not re.fullmatch(r"[A-Z]{2}", fixed["country"]):
        return None
    if not (_whole(header_row, 1, 200) and _whole(people, 1, 20)):
        return None
    if not (
        isinstance(empties, list)
        and len(empties) <= 20
        and all(_text(item, 50) for item in empties)
    ):
        return None
    if not isinstance(code, str) or (
        code and not re.fullmatch(r"\+[1-9][0-9]{0,3}", code)
    ):
        return None
    if count != "" and not _text(count, 200):
        return None
    column = separator = ""
    fields: tuple[str, ...] = ()
    if split is not None:
        if not isinstance(split, dict) or set(split) != {
            "column",
            "separator",
            "fields",
        }:
            return None
        column, separator = split["column"], split["separator"]
        listed = split["fields"]
        if not (
            _text(column, 200)
            and isinstance(separator, str)
            and 1 <= len(separator) <= 5
            and isinstance(listed, list)
            and len(listed) == 2
            and listed[0] != listed[1]
            and all(item in MEMBERSHIP_FIELDS for item in listed)
        ):
            return None
        fields = tuple(listed)
    return Layout(
        header_row,
        frozenset(item.strip() for item in empties),
        code,
        people,
        column.strip(),
        separator,
        fields,
        count.strip(),
        fixed,
    )


def compile_plan(table: MemberTable, mapping: Any, raw_layout: Any) -> Plan | Problem:
    """A plan for reading the table, or the problem that ends the run."""
    if table.problem:
        return Problem(table.problem)
    if (
        not isinstance(mapping, dict)
        or set(mapping) - set(FIELDS)
        or any(not isinstance(v, str) or not v.strip() for v in mapping.values())
    ):
        return Problem("invalid_column_map")
    layout = parse_layout(raw_layout)
    if layout is None:
        return Problem("invalid_layout")
    many = layout.people_per_row > 1
    mapped = {name: heading.strip() for name, heading in mapping.items()}
    for name, heading in mapped.items():
        # Person headings number their people exactly when a row has several.
        if (NUMBER in heading) != (many and name in PERSON_FIELDS):
            return Problem("invalid_layout")
    if NUMBER in layout.split_column or NUMBER in layout.people_count:
        return Problem("invalid_layout")
    if set(layout.split_fields) & set(mapped):
        return Problem("invalid_layout")
    # A fixed value stands in for a column; it never sits beside one.
    if set(layout.fixed) & (set(mapped) | set(layout.split_fields)):
        return Problem("invalid_layout")

    rows = (table.columns, *table.rows)
    if layout.header_row > len(rows):
        return Problem("invalid_columns")
    header = tuple(cell.strip() for cell in rows[layout.header_row - 1])
    if not header:
        return Problem("invalid_columns")
    positions: dict[str, list[int]] = {}
    for index, heading in enumerate(header):
        positions.setdefault(heading, []).append(index)

    # (field, person slot or 0, heading, whether the source named it)
    wanted: list[tuple[str, int, str, bool]] = []
    for slot in range(1, layout.people_per_row + 1):
        for name in PERSON_FIELDS:
            if name in mapped:
                heading = mapped[name].replace(NUMBER, str(slot))
                wanted.append((name, slot, heading, True))
            elif not many:
                wanted.append((name, slot, name, False))
    for name in MEMBERSHIP_FIELDS:
        if name not in layout.split_fields and name not in layout.fixed:
            wanted.append((name, 0, mapped.get(name, name), name in mapped))
    headings = [heading for _, _, heading, _ in wanted]
    extra = [layout.split_column, layout.people_count]
    headings.extend(heading for heading in extra if heading)
    if len(set(headings)) != len(headings):
        return Problem("invalid_column_map")
    if any(len(positions.get(heading, ())) > 1 for heading in headings):
        return Problem("invalid_columns")
    for heading in headings[len(wanted) :]:
        if heading not in positions:
            return Problem("missing_column", heading)

    people: list[dict[str, int]] = [{} for _ in range(layout.people_per_row)]
    membership: dict[str, int] = {}
    for name, slot, heading, named in wanted:
        if heading not in positions:
            if named:
                return Problem("missing_column", heading)
            continue
        (people[slot - 1] if slot else membership)[name] = positions[heading][0]
    if "membership" not in membership:
        return Problem("missing_column", mapped.get("membership", "membership"))
    for slot, columns in enumerate(people, start=1):
        if "email" not in columns:
            return Problem(
                "missing_column",
                mapped.get("email", "email").replace(NUMBER, str(slot)),
            )
        if not ("full_name" in columns or {"first_name", "last_name"} <= set(columns)):
            return Problem(
                "missing_column",
                mapped.get("full_name", "full_name").replace(NUMBER, str(slot)),
            )
    split = None
    if layout.split_column:
        split = (
            positions[layout.split_column][0],
            layout.split_separator,
            layout.split_fields,
        )
    count = positions[layout.people_count][0] if layout.people_count else None

    data = rows[layout.header_row :]
    if not data:
        return Problem("empty_list")
    if any(len(row) != len(header) for row in data):
        return Problem("invalid_row_width")
    return Plan(
        layout.header_row + 1,
        tuple(data),
        layout.empty_values,
        layout.country_code,
        layout.fixed,
        tuple(people),
        membership,
        split,
        count,
    )


def _phone(value: str, code: str) -> str:
    if not code:
        return value.replace(" ", "")
    value = "".join(c for c in value if c not in " -.()[]")
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value or value.startswith("+"):
        return value
    return code + value.removeprefix("0")


def _names(values: dict[str, str]) -> None:
    """Make the missing ones of full, first and last name ."""
    full, first, last = values["full_name"], values["first_name"], values["last_name"]
    if not full:
        values["full_name"] = " ".join(part for part in (first, last) if part)
        return
    words = full.split()
    # A one-word full name is the first name (builder's pick).
    values["first_name"] = first or " ".join(words[:-1] or words)
    values["last_name"] = last or (words[-1] if len(words) > 1 else "")


def read_rows(plan: Plan) -> list[Row]:
    """The file's rows as people and one membership each. A row with neither
    a person nor a stated count is left out."""
    rows = []
    for number, cells in enumerate(plan.data, start=plan.first_row):

        def cell(index: int, cells: tuple[str, ...] = cells) -> str:
            value = cells[index].strip()
            return "" if value in plan.empty_values else value

        people = []
        for columns in plan.people:
            values = {
                name: cell(columns[name]) if name in columns else ""
                for name in PERSON_FIELDS
            }
            if not any(values.values()):
                continue
            values["email"] = values["email"].lower()
            values["phone"] = _phone(values["phone"], plan.country_code)
            _names(values)
            people.append(values)
        membership = {
            name: cell(plan.membership[name]) if name in plan.membership else ""
            for name in MEMBERSHIP_FIELDS
        }
        membership.update(plan.fixed)
        if plan.split:
            index, separator, (first, second) = plan.split
            text = cell(index)
            if separator in text:
                head, _, tail = text.rpartition(separator)
                membership[first], membership[second] = head.strip(), tail.strip()
            else:
                membership[first], membership[second] = text, ""
        membership["country"] = membership["country"].upper()
        stated = cell(plan.count) if plan.count is not None else ""
        if people or stated:
            rows.append(Row(number, membership, people, stated))
    return rows
