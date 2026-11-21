# SPDX-License-Identifier: AGPL-3.0-only
"""No database, dump or member list in Git : by name, by content, in
the staged files and in every tracked file."""

import subprocess
from pathlib import Path

import pytest

from tools import database_files

REPOSITORY = Path(__file__).resolve().parents[1]
# Put together here, so that this file holds no dump line itself.
COPY = "COPY " + "members (name) FROM " + "stdin;\nAnn\n\\.\n"
DUMP = b"PGD" + b"MP\x01\x0e\x00"
SQLITE = b"SQLite " + b"format 3\x00" + b"\x00" * 8


@pytest.mark.parametrize(
    "path",
    [
        "a.sqlite",
        "a.sqlite3",
        "data/a.db",
        "a.dump",
        "a.pgdump",
        "a.bak",
        "a.sql.gz",
        "A.DB",
        "dump.sql",
        "docker/other/roles.sql",
        "members.csv",
        "docker/db/members.csv",
        "export/dummy-members.csv.bak",
        "export/dummy-members.csv",
    ],
)
def test_names_are_refused(path: str) -> None:
    assert database_files.refusal(path, b"harmless") is not None


@pytest.mark.parametrize(
    "path",
    [
        "dummy-members.csv",
        "docker/db/roles.sql",
        "tests/proofs/capacity.sql",
        "randirandi/core/models.py",
        "notes.md",
        "database.py",
    ],
)
def test_allowed_names_stay(path: str) -> None:
    assert database_files.refusal(path, b"harmless") is None


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (DUMP, "PostgreSQL dump"),
        (SQLITE, "SQLite"),
        (b"-- dump\nSET x = 1;\n" + COPY.encode(), "COPY"),
        (b"-- dump\n  " + COPY.encode(), "COPY"),
    ],
)
def test_content_is_refused_under_any_name(content: bytes, why: str) -> None:
    message = database_files.refusal("notes.txt", content)
    assert message is not None
    assert why in message
    assert "managed database" in message


def test_prose_about_a_dump_stays() -> None:
    text = b"Use COPY to load; the dump starts with PGDMP and FROM stdin follows.\n"
    assert database_files.refusal("notes.md", text) is None


def _git(*args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed Git executable, test arguments
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],  # noqa: S607
        check=True,
    )


def _repository(path: Path, files: dict[str, bytes]) -> None:
    _git("init", "--quiet", str(path))
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_bytes(content)
    _git("-C", str(path), "add", "-A")


def test_the_full_run_reads_every_tracked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(
        tmp_path,
        {"ok.txt": b"fine", "dummy-members.csv": b"a,b\n", "x/hidden.txt": SQLITE},
    )
    monkeypatch.chdir(tmp_path)
    found = database_files.check(staged=False)
    assert len(found) == 1
    assert found[0].startswith("x/hidden.txt: refused")


def test_a_commit_reads_the_staged_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path, {"old.csv": b"a\n", "notes.txt": b"fine"})
    monkeypatch.chdir(tmp_path)
    _git("-C", str(tmp_path), "commit", "--quiet", "-m", "start")
    # Staged as a dump, then changed again in the working tree: the staged
    # content counts.
    (tmp_path / "notes.txt").write_bytes(DUMP)
    _git("-C", str(tmp_path), "add", "notes.txt")
    (tmp_path / "notes.txt").write_bytes(b"fine again")
    (tmp_path / "unstaged.db").write_bytes(b"x")
    found = database_files.check(staged=True)
    assert [line.split(":")[0] for line in found] == ["notes.txt"]
    # The full run sees the old list; the commit run only what it adds.
    assert [line.split(":")[0] for line in database_files.check(staged=False)] == [
        "old.csv"
    ]


def test_a_commit_that_removes_a_file_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path, {"members.csv": b"a\n"})
    monkeypatch.chdir(tmp_path)
    _git("-C", str(tmp_path), "commit", "--quiet", "-m", "start")
    _git("-C", str(tmp_path), "rm", "--quiet", "members.csv")
    assert database_files.check(staged=True) == []


def test_the_exit_code_and_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repository(tmp_path, {"a.sqlite": b"x"})
    monkeypatch.chdir(tmp_path)
    assert database_files.main(["--staged"]) == 1
    assert "a.sqlite: refused" in capsys.readouterr().err


def test_this_repository_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPOSITORY)
    assert database_files.check(staged=False) == []


def test_bin_check_runs_the_guard_on_staged_files_for_a_commit() -> None:
    check = (REPOSITORY / "bin/check").read_text()
    assert "python -m tools.database_files --staged\n" in check
    assert "python -m tools.database_files\n" in check
    assert check.index("tools.database_files") < check.index(
        'step "Secrets in the files"'
    )
