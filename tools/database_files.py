# SPDX-License-Identifier: AGPL-3.0-only
"""Refuse a database, a dump or a member list in Git .

Member data lives in the managed database, never in Git. `bin/check` runs
this on every tracked file; `bin/check --commit` on the staged files only.

    python -m tools.database_files            every tracked file
    python -m tools.database_files --staged   the staged files, as staged

A file is refused by its name, by its first bytes or by a COPY block of a
plain-text dump. The only `.csv` allowed is the made-up `dummy-members.csv`;
a `.sql` file is allowed in `docker/db/` (roles) and `tests/proofs/`.
"""

import re
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

DATABASE_NAMES = (".sqlite", ".sqlite3", ".db", ".dump", ".pgdump", ".bak", ".sql.gz")
SQL_ALLOWED_IN = ("docker/db/", "tests/proofs/")
MEMBER_LIST = ".csv"
ALLOWED_LIST = "dummy-members.csv"
# The start of a file is read for the headers; a plain-text dump is looked
# through up to the limit (a dump is large only when it holds data, and its
# first COPY block comes before most of it).
HEADER_BYTES = 16
SCAN_LIMIT = 4 * 1024 * 1024
POSTGRES_DUMP = b"PGDMP"
SQLITE = b"SQLite format 3"
COPY_BLOCK = re.compile(
    rb"^[ \t]*COPY\s+\S.*\sFROM\s+stdin", re.MULTILINE | re.IGNORECASE
)


def by_name(path: str) -> str | None:
    """Why the name alone is refused, or None."""
    lower = path.lower()
    if lower.endswith(DATABASE_NAMES):
        return "its name says it is a database or a dump"
    if lower.endswith(".sql") and not path.startswith(SQL_ALLOWED_IN):
        return "a .sql file belongs only in docker/db/ or tests/proofs/"
    if lower.endswith(MEMBER_LIST) and path != ALLOWED_LIST:
        return f"a .csv file may be a member list; only {ALLOWED_LIST} is allowed"
    return None


def by_content(content: bytes) -> str | None:
    """Why the content is refused, or None."""
    if content.startswith(POSTGRES_DUMP):
        return "it starts like a PostgreSQL dump"
    if content.startswith(SQLITE):
        return "it starts like a SQLite database"
    if COPY_BLOCK.search(content):
        return "it holds a COPY ... FROM stdin block, the data of a dump"
    return None


def refusal(path: str, content: bytes) -> str | None:
    """The plain-words refusal of one file, or None when it may stay."""
    why = by_name(path) or by_content(content)
    if why is None:
        return None
    return (
        f"{path}: refused, {why}. Member data goes into the managed database, "
        "not Git. Remove the file from Git (and from the history if it was "
        "pushed)."
    )


def _git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(  # noqa: S603 — fixed Git executable, fixed arguments
        ["git", *args],  # noqa: S607
        input=data,
        check=True,
        capture_output=True,
    ).stdout


def _read_staged(paths: list[str]) -> Iterable[tuple[str, bytes]]:
    """The staged content of each path, in one `git cat-file` process."""
    request = b"".join(f":{path}\n".encode() for path in paths)
    out = _git("cat-file", "--batch", data=request)
    at = 0
    for path in paths:
        end = out.index(b"\n", at)
        header = out[at:end].split()
        at = end + 1
        if len(header) != 3:  # ":path missing"
            continue
        size = int(header[2])
        yield path, out[at : at + min(size, SCAN_LIMIT)]
        at += size + 1


def _read_tracked(paths: list[str]) -> Iterable[tuple[str, bytes]]:
    for path in paths:
        try:
            with Path(path).open("rb") as handle:
                yield path, handle.read(SCAN_LIMIT)
        except OSError:
            continue  # tracked but absent or a directory (a submodule)


def check(staged: bool) -> list[str]:
    if staged:
        listing = _git(
            "diff", "--cached", "--name-only", "--no-renames", "-z", "--diff-filter=d"
        )
    else:
        listing = _git("ls-files", "-z")
    paths = [name.decode() for name in listing.split(b"\0") if name]
    if not paths:
        return []
    files = _read_staged(paths) if staged else _read_tracked(paths)
    return [found for path, content in files if (found := refusal(path, content))]


def main(argv: list[str]) -> int:
    started = time.perf_counter()
    refused = check(staged="--staged" in argv)
    for line in refused:
        sys.stderr.write(f"{line}\n")
    if "--time" in argv:
        sys.stdout.write(f"{time.perf_counter() - started:.3f} s\n")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
