# SPDX-License-Identifier: AGPL-3.0-only
"""A complete member table; CSV and future sheet readers feed the same import."""

import csv
from dataclasses import dataclass
from io import StringIO


@dataclass(frozen=True, repr=False)
class MemberTable:
    # Personal values are transient input, never an object representation or log.
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    problem: str = ""


def read_csv(text: str) -> MemberTable:
    try:
        reader = csv.reader(StringIO(text.removeprefix("\ufeff")), strict=True)
        columns = tuple(next(reader, ()))
        return MemberTable(columns, tuple(tuple(row) for row in reader))
    except csv.Error:
        return MemberTable((), (), "invalid_csv")
