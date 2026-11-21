# SPDX-License-Identifier: AGPL-3.0-only
"""The commit hook runs the tests a commit's changes can reach; GitHub runs
every test ."""

import json
from pathlib import Path

import pytest

from tools.commit_tests import SECONDS, kept_times, record, select, shares

REPOSITORY = Path(__file__).resolve().parents[1]

FILES = {
    "randirandi/__init__.py": "",
    "randirandi/settings.py": 'ROOT_URLCONF = "randirandi.urls"\n',
    "randirandi/urls.py": "from randirandi import views\n",
    "randirandi/views.py": (
        '"""Pages. NOTES.md says why."""\n'
        "from randirandi.core.models import Thing\n"
        'PAGE = "pages/thing.html"\n'
    ),
    "randirandi/core/__init__.py": "",
    "randirandi/core/clock.py": "def now() -> None: ...\n",
    "randirandi/core/models/__init__.py": (
        "from randirandi.core.models.other import Other\n"
        "from randirandi.core.models.things import Thing\n"
    ),
    "randirandi/core/models/other.py": "class Other: ...\n",
    "randirandi/core/models/things.py": "class Thing: ...\n",
    "randirandi/core/migrations/__init__.py": "",
    "randirandi/core/migrations/0001_first.py": "",
    "randirandi/core/management/__init__.py": "",
    "randirandi/core/management/commands/__init__.py": "",
    "randirandi/core/management/commands/load_things.py": "",
    "tools/__init__.py": "",
    "tools/road.py": 'ROAD = "ROADMAP.toml"\n',
    "tests/__init__.py": "",
    "tests/conftest.py": "",
    "tests/test_things.py": "from randirandi.core.models import Thing\n",
    "tests/test_other.py": "from randirandi.core.models import Other\n",
    "tests/test_clock.py": 'TARGET = "randirandi.core.clock.now"\n',
    "tests/test_page.py": "def test_page(client) -> None: ...\n",
    "tests/test_road.py": "from tools import road\n",
    "tests/test_migrations.py": (
        "from django.db.migrations.executor import MigrationExecutor\n"
    ),
    "tests/test_command.py": 'COMMAND = "load_things"\n',
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name, text in FILES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return tmp_path


@pytest.mark.parametrize(
    ("changed", "picked"),
    [
        # Documentation alone, even when a docstring names it.
        (["README.md", "NOTES.md"], []),
        # Through the package to its submodule, and through the test client.
        (
            ["randirandi/core/models/things.py"],
            ["test_page.py", "test_things.py"],
        ),
        (["randirandi/core/models/other.py"], ["test_other.py"]),
        # A patch target is an import.
        (["randirandi/core/clock.py"], ["test_clock.py"]),
        # A file that code names, by its path or its file name.
        (["ROADMAP.toml"], ["test_road.py"]),
        (["randirandi/templates/pages/thing.html"], ["test_page.py"]),
        # A template no code names is still extended by those that are.
        (["randirandi/templates/base.html"], ["test_page.py"]),
        # A command runs by its name.
        (["randirandi/core/management/commands/load_things.py"], ["test_command.py"]),
        (["randirandi/core/migrations/0001_first.py"], ["test_migrations.py"]),
        (["tests/test_other.py"], ["test_other.py"]),
    ],
)
def test_a_change_picks_the_tests_that_reach_it(
    root: Path, changed: list[str], picked: list[str]
) -> None:
    assert select(root, changed) == [f"tests/{name}" for name in picked]


@pytest.mark.parametrize(
    "changed", ["tests/conftest.py", "randirandi/settings.py", "uv.lock"]
)
def test_what_every_test_reads_picks_every_test(root: Path, changed: str) -> None:
    assert len(select(root, [changed])) == 7


def test_the_hook_picks_and_github_runs_every_test() -> None:
    hook = (REPOSITORY / ".githooks/pre-commit").read_text()
    assert "python -m tools.commit_tests --shares" in hook
    assert "bin/test-shares" in hook
    # Each run keeps what every file took, for the next split by time.
    assert "python -m tools.commit_tests --record" in hook
    assert '--junitxml="$report"' in (REPOSITORY / "bin/test-shares").read_text()
    assert 'PYTEST_ADDOPTS="--collect-only -qq"' in hook
    assert "bin/check --commit" in hook
    # Its time is reported on every run, and over 4 minutes is a defect.
    assert "budget=240" in hook
    assert "trap report EXIT" in hook
    # Re-entry may replace the bootstrap, but must preserve the timed body.
    assert "exec " not in hook.split("trap report EXIT", 1)[1]
    workflow = (REPOSITORY / ".github/workflows/check.yml").read_text()
    assert "part: [0, 1, 2, 3, 4]" in workflow
    assert workflow.rstrip().endswith("- run: bin/ci-part ${{ matrix.part }} 4")
    parts = (REPOSITORY / "bin/ci-part").read_text()
    assert 'PYTEST_ADDOPTS="--collect-only -qq" exec bin/check\n' in parts
    # The four shares GitHub runs hold every test file once.
    every = sorted(
        path.relative_to(REPOSITORY).as_posix()
        for path in (REPOSITORY / "tests").glob("test_*.py")
    )
    # Every test file named is picked, as bin/check and bin/ci-part name them.
    assert select(REPOSITORY, every) == every
    found = shares(REPOSITORY, every, 4)
    assert len(found) == 4
    assert sorted(path for share in found for path in share) == every
    # Without the hook's selection, bin/check runs every test file, in eight
    # shares side by side; asked only to collect, it collects them here.
    check = (REPOSITORY / "bin/check").read_text()
    tests = check.split('step "Tests"', 1)[1]
    assert "*--collect-only*)" in tests
    assert 'uv run --locked --quiet pytest -o "cache_dir=$pytest_cache"' in tests
    assert "printf '%s\\n' tests/test_*.py \\\n" in tests
    assert "python -m tools.commit_tests --shares 8 \\\n" in tests
    assert '| bin/test-shares "$logs" || outcome=$?\n' in tests


def test_shares_hold_every_file_once_and_about_as_many_tests(tmp_path: Path) -> None:
    sizes = {"a": 9, "b": 5, "c": 4, "d": 3, "e": 1}
    for name, size in sizes.items():
        (tmp_path / f"test_{name}.py").write_text("def test_x() -> None: ...\n" * size)
    files = [f"test_{name}.py" for name in sizes]
    found = shares(tmp_path, files, 2)
    assert sorted(path for share in found for path in share) == files
    totals = sorted(sum(sizes[path[5]] for path in share) for share in found)
    # The largest first, each to the share with the fewest so far.
    assert totals == [10, 12]
    assert len(shares(tmp_path, files[:1], 6)) == 1


def test_shares_go_by_the_seconds_each_file_took(tmp_path: Path) -> None:
    # The migrations file has the fewest tests and the longest run (2026-10-21).
    sizes = {"migrations": 2, "a": 6, "b": 6, "c": 6, "d": 6}
    for name, size in sizes.items():
        (tmp_path / f"test_{name}.py").write_text("def test_x() -> None: ...\n" * size)
    files = [f"test_{name}.py" for name in sizes]
    took = {
        "test_migrations.py": 120.0,
        "test_a.py": 20.0,
        "test_b.py": 20.0,
        "test_c.py": 20.0,
    }
    timed = shares(tmp_path, files[:4], 2, took)
    assert timed == [["test_migrations.py"], ["test_a.py", "test_b.py", "test_c.py"]]
    # By the number of tests the migrations file shares with a full file.
    assert ["test_migrations.py"] not in shares(tmp_path, files[:4], 2)
    # test_d.py has no time: its 6 tests weigh 6 x 180 s / 20 tests = 54 s.
    assert shares(tmp_path, files, 2, took) == [
        ["test_migrations.py"],
        ["test_a.py", "test_b.py", "test_c.py", "test_d.py"],
    ]
    # Times of files that are not picked change nothing.
    assert shares(tmp_path, files, 2, {"test_gone.py": 1.0}) == shares(
        tmp_path, files, 2
    )


def test_record_keeps_what_each_file_took(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    for name in ("test_a", "test_b", "test_c"):
        (tmp_path / "tests" / f"{name}.py").write_text("")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "share-1.xml").write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_a" name="test_x" time="1.5"/>'
        '<testcase classname="tests.test_a.Group" name="test_y" time="2.0"/>'
        '<testcase classname="tests.test_b" name="test_z[1]" time="0.25"/>'
        '<testcase classname="tests.test_unknown" name="test_q" time="9"/>'
        "</testsuite></testsuites>"
    )
    (tmp_path / SECONDS).parent.mkdir()
    (tmp_path / SECONDS).write_text(
        '{"tests/test_b.py": 7.0, "tests/test_c.py": 4.0, "tests/test_gone.py": 3.0}'
    )
    assert record(tmp_path, logs) == {"tests/test_a.py": 3.5, "tests/test_b.py": 0.25}
    kept = json.loads((tmp_path / SECONDS).read_text())
    # A new time replaces the old; a file not run keeps its own; a removed one goes.
    assert kept == {
        "tests/test_a.py": 3.5,
        "tests/test_b.py": 0.25,
        "tests/test_c.py": 4.0,
    }


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[1, 2]",
        '"text"',
        '{"tests/test_a.py": "slow"}',
        '{"tests/test_a.py": -1}',
        '{"tests/test_a.py": Infinity}',
    ],
)
def test_damaged_times_split_by_the_number_of_tests(
    tmp_path: Path, content: str
) -> None:
    (tmp_path / SECONDS).parent.mkdir()
    (tmp_path / SECONDS).write_text(content)
    # Read as no times: the split counts tests and commits go on.
    assert kept_times(tmp_path) == {}
    (tmp_path / "test_a.py").write_text("def test_x() -> None: ...\n")
    assert shares(tmp_path, ["test_a.py"], 2, kept_times(tmp_path)) == [["test_a.py"]]


def test_a_broken_report_leaves_the_other_shares_times(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    logs = tmp_path / "logs"
    logs.mkdir()
    # Share 1 was killed while its report was written.
    (logs / "share-1.xml").write_text('<testsuites><testsuite><testcase classname="te')
    (logs / "share-2.xml").write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_a" name="test_x" time="2.5"/>'
        "</testsuite></testsuites>"
    )
    # A damaged times file is replaced, not a reason to keep nothing.
    (tmp_path / SECONDS).parent.mkdir()
    (tmp_path / SECONDS).write_text("{")
    assert record(tmp_path, logs) == {"tests/test_a.py": 2.5}
    assert json.loads((tmp_path / SECONDS).read_text()) == {"tests/test_a.py": 2.5}
