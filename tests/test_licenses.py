# SPDX-License-Identifier: AGPL-3.0-only
"""The licence check : free licences pass, anything else is refused,
a stale notices file is refused, and a commit runs the check only when a staged
path can change a licence."""

import json
from pathlib import Path

import pytest

from tools import licenses
from tools.licenses import Package

REPOSITORY = Path(__file__).resolve().parents[1]


def a_package(
    licence: str | None, *, kind: str = "python", server: bool = True
) -> Package:
    return Package(kind, "thing", "1.0", licence, server)


@pytest.mark.parametrize(
    "expression",
    [
        "MIT",
        "Apache-2.0 OR BSD-2-Clause",
        "(MIT OR GPL-3.0-only) AND ISC",
        "MIT OR GPL-3.0-only",
        "PSF-2.0",
        "MPL-2.0",
        "OFL-1.1",
        "CC0-1.0",
        "0BSD",
        "Unlicense",
    ],
)
def test_free_licences_pass(expression: str) -> None:
    assert licenses.is_free(expression, server=False)


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-3.0-only",
        "AGPL-3.0-or-later",
        "MIT AND GPL-3.0-only",
        "GPL-2.0-only WITH Classpath-exception-2.0",
        "Proprietary",
        "BSD License",
        "(MIT",
        "MIT OR",
        "",
    ],
)
def test_other_licences_are_refused(expression: str) -> None:
    assert not licenses.is_free(expression, server=True)


def test_lgpl_is_free_on_the_server_only() -> None:
    assert licenses.is_free("LGPL-3.0-only", server=True)
    assert not licenses.is_free("LGPL-3.0-only", server=False)
    assert licenses.refused([a_package("LGPL-3.0-only")], {}) == []
    assert licenses.refused([a_package("LGPL-3.0-only", kind="npm", server=False)], {})


def test_a_refused_licence_is_named() -> None:
    [line] = licenses.refused([a_package("GPL-3.0-only")], {})
    assert "python thing 1.0" in line
    assert "GPL-3.0-only" in line
    assert "licenses/accepted.toml" in line


def test_unknown_terms_are_refused() -> None:
    [line] = licenses.refused([a_package(None)], {})
    assert "no licence named" in line


def test_an_accepted_entry_passes_for_that_version_only() -> None:
    known = {("thing", "1.0"): "Custom but free"}
    assert licenses.refused([a_package(None), a_package("GPL-3.0-only")], known) == []
    newer = Package("python", "thing", "1.1", None, True)
    assert licenses.refused([newer], known)


def write(root: Path, name: str, text: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text)


def test_an_incomplete_accepted_entry_is_a_problem(tmp_path: Path) -> None:
    write(
        tmp_path, "licenses/accepted.toml", '[[accepted]]\nname = "a"\nversion = "1"\n'
    )
    known, problems = licenses.accepted(tmp_path)
    assert known == {}
    assert "lacks ['licence', 'reason']" in problems[0]


def test_a_new_image_is_not_accepted(tmp_path: Path) -> None:
    write(
        tmp_path,
        "images.env",
        "# comment\nPYTHON_IMAGE=python:3.14-slim@sha256:" + "a" * 64 + "\n"
        "CADDY_IMAGE=ghcr.io/new/thing:2@sha256:" + "b" * 64 + "\n",
    )
    images = licenses.image_packages(tmp_path)
    assert [(i.name, i.version) for i in images] == [
        ("python", "3.14-slim"),
        ("ghcr.io/new/thing", "2"),
    ]
    known = {("python", "3.14-slim"): "PSF-2.0"}
    [line] = licenses.refused(images, known)
    assert "ghcr.io/new/thing" in line


def test_a_new_action_is_not_accepted(tmp_path: Path) -> None:
    write(
        tmp_path,
        ".github/workflows/check.yml",
        "steps:\n"
        f"  - uses: actions/checkout@{'1' * 40} # v4\n"
        f"  - uses: someone/new-action@{'2' * 40} # v1\n"
        "  - uses: ./.github/actions/check-setup\n",
    )
    actions = licenses.action_packages(tmp_path)
    assert [(a.name, a.version) for a in actions] == [
        ("actions/checkout", "v4"),
        ("someone/new-action", "v1"),
    ]
    [line] = licenses.refused(actions, {("actions/checkout", "v4"): "MIT"})
    assert "someone/new-action" in line


def test_npm_packages_come_from_the_lock_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        "frontend/package-lock.json",
        json.dumps(
            {
                "packages": {
                    "": {"name": "root"},
                    "node_modules/a": {"version": "1.0.0", "license": "MIT"},
                    "node_modules/a/node_modules/@b/c": {
                        "version": "2.0.0",
                        "license": "GPL-3.0",
                    },
                    "node_modules/d": {"version": "3.0.0"},
                    "node_modules/e": {
                        "version": "4.0.0",
                        "license": "LGPL-3.0-only",
                        "dev": True,
                    },
                }
            }
        ),
    )
    found = licenses.npm_packages(tmp_path)
    assert [(p.name, p.licence) for p in found] == [
        ("a", "MIT"),
        ("@b/c", "GPL-3.0"),
        ("d", None),
        ("e", "LGPL-3.0-only"),
    ]
    assert [p.server for p in found] == [False, False, False, True]
    assert len(licenses.refused(found, {})) == 2


def a_notices_tree(root: Path, packages: dict[str, object]) -> None:
    write(root, "frontend/package-lock.json", json.dumps({"packages": packages}))
    write(root, str(licenses.NOTICES), "Notices.\n")


def test_notices_are_current_after_the_stamp(tmp_path: Path) -> None:
    a_notices_tree(tmp_path, {"": {}, "node_modules/a": {"version": "1.0.0"}})
    assert licenses.stale_notices(tmp_path)
    licenses.stamp(tmp_path)
    assert licenses.stale_notices(tmp_path) == []
    licenses.stamp(tmp_path)
    notices = (tmp_path / licenses.NOTICES).read_text()
    assert notices.count(licenses.STAMP) == 1


def test_notices_are_stale_when_a_browser_package_changes(tmp_path: Path) -> None:
    a_notices_tree(tmp_path, {"": {}, "node_modules/a": {"version": "1.0.0"}})
    licenses.stamp(tmp_path)
    a_notices_tree(tmp_path, {"": {}, "node_modules/a": {"version": "1.0.1"}})
    (tmp_path / licenses.NOTICES).write_text(
        "Notices.\n" + licenses.STAMP + "0" * 64 + "\n"
    )
    [line] = licenses.stale_notices(tmp_path)
    assert "bin/licenses stamp" in line


def test_build_tools_do_not_change_the_notices(tmp_path: Path) -> None:
    a_notices_tree(tmp_path, {"node_modules/a": {"version": "1.0.0"}})
    licenses.stamp(tmp_path)
    a_notices_tree(
        tmp_path,
        {
            "node_modules/a": {"version": "1.0.0"},
            "node_modules/vite": {"version": "9.0.0", "dev": True},
        },
    )
    (tmp_path / licenses.NOTICES).write_text(
        "Notices.\n" + licenses.STAMP + licenses.browser_stamp(tmp_path) + "\n"
    )
    assert licenses.stale_notices(tmp_path) == []


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "frontend/package.json",
        "frontend/package-lock.json",
        "Dockerfile",
        "images.env",
        ".github/workflows/check.yml",
        ".github/actions/check-setup/action.yml",
        "licenses/accepted.toml",
        "randirandi/core/static/core/brand/logo.svg",
        "randirandi/core/static/core/ui/inter-latin-400-normal.woff2",
        "randirandi/core/static/core/third-party-notices.txt",
        "frontend/brand/logo-package-v2/logo.svg",
        "somewhere/new-font.woff2",
        "somewhere/favicon.ico",
    ],
)
def test_the_commit_check_runs_for_a_licence_input(path: str) -> None:
    assert licenses.touches_licences(["README.md", path])


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "randirandi/core/models.py",
        "tests/test_calendar.py",
        "frontend/src/app.jsx",
        "docs/adr/ADR-0024.md",
    ],
)
def test_the_commit_check_skips_other_changes(path: str) -> None:
    assert not licenses.touches_licences([path])
    assert not licenses.touches_licences([])


def test_the_repository_passes() -> None:
    count, problems = licenses.check(REPOSITORY)
    assert problems == []
    assert count > 50
