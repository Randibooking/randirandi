# SPDX-License-Identifier: AGPL-3.0-only
"""Made-up .xlsx files for the tests, built from nothing but XML text.

Nothing here is a real member list: the people, addresses and units are
invented by the tests that call it.
"""

import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"


def _letters(index: int) -> str:
    text = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        text = chr(65 + rest) + text
    return text


def sheet_xml(rows: list[list[object]], *, shared: list[str] | None = None) -> str:
    """The sheet's XML: text cells as shared strings when `shared` is given,
    else inline; numbers as number cells; None leaves a cell out."""
    body = []
    for number, row in enumerate(rows, start=1):
        cells = []
        for index, value in enumerate(row):
            if value is None:
                continue
            ref = f"{_letters(index)}{number}"
            if isinstance(value, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
            elif isinstance(value, int | float):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif shared is not None:
                shared.append(str(value))
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            else:
                text = escape(str(value))
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        body.append(f'<row r="{number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    )


def workbook(
    rows: list[list[object]],
    *,
    sheets: int = 1,
    shared: bool = False,
    extra: dict[str, bytes | str] | None = None,
    sheet_text: str | None = None,
    workbook_extra: str = "",
    workbook_rels_extra: str = "",
    sheet_rels: str | None = None,
) -> bytes:
    """An .xlsx with `sheets` sheets, all holding `rows`; `extra` adds or
    replaces parts, `sheet_text` replaces the first sheet's XML."""
    strings: list[str] | None = [] if shared else None
    first = sheet_text if sheet_text is not None else sheet_xml(rows, shared=strings)
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"></Types>'
        ),
        "xl/workbook.xml": (
            f'<?xml version="1.0"?><workbook xmlns="{MAIN}" xmlns:r="{RELS}"><sheets>'
            + "".join(
                f'<sheet name="Sheet{n}" sheetId="{n}" r:id="rId{n}"/>'
                for n in range(1, sheets + 1)
            )
            + f"</sheets>{workbook_extra}</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<?xml version="1.0"?><Relationships xmlns="{PACKAGE}">'
            + "".join(
                f'<Relationship Id="rId{n}" Type="{RELS}/worksheet" '
                f'Target="worksheets/sheet{n}.xml"/>'
                for n in range(1, sheets + 1)
            )
            + f"{workbook_rels_extra}</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": first,
    }
    for n in range(2, sheets + 1):
        parts[f"xl/worksheets/sheet{n}.xml"] = sheet_xml(rows)
    if strings is not None:
        parts["xl/sharedStrings.xml"] = (
            f'<?xml version="1.0"?><sst xmlns="{MAIN}">'
            + "".join(f"<si><t>{escape(text)}</t></si>" for text in strings)
            + "</sst>"
        )
    if sheet_rels is not None:
        parts["xl/worksheets/_rels/sheet1.xml.rels"] = sheet_rels
    parts.update(extra or {})
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()
