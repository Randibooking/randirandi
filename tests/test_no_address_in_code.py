# SPDX-License-Identifier: AGPL-3.0-only
"""No email address is written into a page, a template, a label or the
frontend source . Every address a page shows is the organization's own
field. Tests, setup files and built or third-party files are not product pages."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
# Where the product writes pages: its Python (views, forms, emails), templates,
# the label set and the frontend source.
SUFFIXES = {
    ".py", ".html", ".txt", ".json", ".js", ".jsx", ".mjs", ".scss", ".css",
    ".svg", ".webmanifest",
}  # fmt: skip
SKIPPED = {"migrations", "node_modules", "__pycache__", ".vite"}
# What is built or vendored, by path: the vite bundle and the third-party notices.
BUILT = {
    "randirandi/core/static/core/ui/app.js",
    "randirandi/core/static/core/ui/app.js.map",
    "randirandi/core/static/core/ui/main.css",
    "randirandi/core/static/core/third-party-notices.txt",
}


def product_files() -> list[Path]:
    found: list[Path] = []
    for top in (ROOT / "randirandi", ROOT / "frontend" / "src"):
        for path in sorted(top.rglob("*")):
            relative = path.relative_to(ROOT).parts
            if (
                path.is_file()
                and path.suffix in SUFFIXES
                and not SKIPPED & set(relative)
                and path.relative_to(ROOT).as_posix() not in BUILT
                and not path.name.endswith((".test.js", ".test.jsx"))
            ):
                found.append(path)
    return found


def test_the_guard_reads_the_places_a_page_is_written() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in product_files()}
    assert "randirandi/locale/en.json" in names
    assert "randirandi/core/emails.py" in names
    assert "randirandi/core/templates/core/email/base.html" in names
    assert "frontend/src/main.jsx" in names
    assert "randirandi/core/static/core/manifest.webmanifest" in names
    assert "randirandi/core/static/core/ui/app.js" not in names
    assert not any("/migrations/" in name or ".test." in name for name in names)


@pytest.mark.parametrize(
    "text",
    [
        "name@domain.no",
        "a.b+c@sub.example.org",
        "placeholder: 'x@y.com'",
        "mailto:a@b.example",
    ],
)
def test_the_guard_sees_an_address(text: str) -> None:
    assert ADDRESS.search(text)


@pytest.mark.parametrize("text", ["name@domain", "@types/node", "x @ y", "{email}"])
def test_the_guard_leaves_generic_words_alone(text: str) -> None:
    assert not ADDRESS.search(text)


def test_no_address_is_written_into_a_page_a_template_a_label_or_the_frontend() -> None:
    found = [
        f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()[:80]}"
        for path in product_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ADDRESS.search(line)
    ]
    assert found == []
