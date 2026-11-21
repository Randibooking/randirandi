# SPDX-License-Identifier: AGPL-3.0-only
"""Check dependency licences and the browser notices fingerprint.

The locked packages, image pins and actions need declared free terms. The
browser notices retain their required attribution. Metadata acceptance does
not establish container redistribution compliance; see licenses/README.md.

    python -m tools.licenses check
    python -m tools.licenses check --commit
    python -m tools.licenses stamp
"""

import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCEPTED = Path("licenses/accepted.toml")
NOTICES = Path("randirandi/core/static/core/third-party-notices.txt")
STAMP = "Browser packages covered by these notices: "

# Free licences. No fee, no royalty, no source we must hand to anyone.
FREE = {
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "ISC",
    "PSF-2.0",
    "Python-2.0",
    "MPL-2.0",
    "OFL-1.1",
    "CC0-1.0",
    "0BSD",
    "Unlicense",
}
# Free only where the code runs on our hosting and goes to nobody.
SERVER_ONLY = {
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
}
# What a package says in its classifiers when it gives no licence expression.
CLASSIFIERS = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "Creative Commons Zero v1.0 Universal": "CC0-1.0",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
}
# A change to one of these can change a licence: the commit check runs then.
PATHS = (
    "uv.lock",
    "pyproject.toml",
    "frontend/package.json",
    "frontend/package-lock.json",
    "Dockerfile",
    "images.env",
    "bin/licenses",
    "tools/licenses.py",
    "randirandi/core/static/core/third-party-notices.txt",
)
PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    "licenses/",
    "frontend/brand/",
    "randirandi/core/static/core/brand/",
    "randirandi/core/static/core/ui/",
)
ASSET_SUFFIXES = (".woff", ".woff2", ".ttf", ".otf", ".ico", ".svg")


@dataclass(frozen=True)
class Package:
    """One thing we depend on. `licence` is None when it says nothing usable."""

    kind: str
    name: str
    version: str
    licence: str | None
    server: bool


def touches_licences(paths: list[str]) -> bool:
    """Whether a staged path is one of the inputs a licence can come from."""
    return any(
        path in PATHS or path.startswith(PREFIXES) or path.endswith(ASSET_SUFFIXES)
        for path in paths
    )


def is_free(expression: str, *, server: bool) -> bool:
    """Whether an SPDX expression is on the allowlist.

    `A OR B` needs one of them, `A AND B` both. An exception (`WITH`), a name
    that is not an SPDX id and a broken expression are all refused.
    """
    allowed = FREE | SERVER_ONLY if server else FREE
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    at = 0

    def either() -> bool:
        nonlocal at
        found = both()
        while at < len(tokens) and tokens[at].upper() == "OR":
            at += 1
            found = both() or found
        return found

    def both() -> bool:
        nonlocal at
        found = one()
        while at < len(tokens) and tokens[at].upper() == "AND":
            at += 1
            found = one() and found
        return found

    def one() -> bool:
        nonlocal at
        if at >= len(tokens):
            raise ValueError(expression)
        word = tokens[at]
        at += 1
        if word == "(":
            found = either()
            if at >= len(tokens) or tokens[at] != ")":
                raise ValueError(expression)
            at += 1
            return found
        return word in allowed

    try:
        return either() and at == len(tokens)
    except ValueError:
        return False


def python_packages(root: Path) -> list[Package]:
    """The locked packages that are installed here, with their own metadata.

    A locked package that is not installed belongs to another platform; the
    run on GitHub (Linux) sees what the laptop does not.
    """
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = {package["name"] for package in lock["package"]}
    found = []
    for distribution in distributions():
        meta = distribution.metadata
        name = re.sub(r"[-_.]+", "-", meta["Name"]).lower()
        if name not in locked or name == "randirandi":
            continue
        classifiers = [
            line.removeprefix("License :: OSI Approved :: ")
            for line in meta.get_all("Classifier") or []
            if line.startswith("License :: ")
        ]
        licence = meta.get("License-Expression")
        if not licence and classifiers:
            licence = " AND ".join(CLASSIFIERS.get(line, line) for line in classifiers)
        if not licence:
            licence = meta.get("License")
        found.append(Package("python", name, distribution.version, licence, True))
    return sorted(found, key=lambda package: package.name)


def npm_packages(root: Path) -> list[Package]:
    """Every package in the front end's lock file."""
    lock = json.loads((root / "frontend/package-lock.json").read_text())
    found = []
    for path, entry in lock["packages"].items():
        if not path or entry.get("link"):
            continue
        licence = entry.get("license")
        found.append(
            Package(
                "npm",
                path.rsplit("node_modules/", 1)[-1],
                entry["version"],
                licence if isinstance(licence, str) else None,
                # Build tools run on our machines only, like the server.
                entry.get("dev") is True,
            )
        )
    return found


def image_packages(root: Path) -> list[Package]:
    """The images in images.env, by name and tag; the digest changes weekly."""
    found = []
    for line in (root / "images.env").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        reference = line.split("=", 1)[1].split("@", 1)[0]
        name, _, tag = reference.rpartition(":")
        found.append(Package("image", name, tag, None, True))
    return found


def action_packages(root: Path) -> list[Package]:
    """The GitHub actions the workflows use, by name and the version comment."""
    use = re.compile(r"uses:\s*([^\s@./][^\s@]*)@(\S+)(?:\s+#\s*(\S+))?")
    found = {}
    for path in sorted(root.glob(".github/**/*.yml")):
        for match in use.finditer(path.read_text()):
            name, ref, version = match.groups()
            found[name, version or ref] = Package(
                "action", name, version or ref, None, True
            )
    return list(found.values())


def accepted(root: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
    """The hand-accepted entries by (name, version), and what is wrong in them."""
    entries = tomllib.loads((root / ACCEPTED).read_text()).get("accepted", [])
    known = {}
    problems = []
    for entry in entries:
        missing = [
            key
            for key in ("name", "version", "licence", "reason")
            if not entry.get(key)
        ]
        if missing:
            problems.append(f"{ACCEPTED}: {entry.get('name', '?')} lacks {missing}")
            continue
        known[entry["name"], entry["version"]] = entry["licence"]
    return known, problems


def refused(packages: list[Package], known: dict[tuple[str, str], str]) -> list[str]:
    """What is wrong with the licences of these packages, one line each."""
    problems = []
    for package in packages:
        if (package.name, package.version) in known:
            continue
        label = f"{package.kind} {package.name} {package.version}"
        how = f"Look at its terms, then add it to {ACCEPTED} with a reason."
        if package.licence is None:
            problems.append(f"{label}: no licence named. {how}")
        elif not is_free(package.licence, server=package.server):
            problems.append(
                f"{label}: {package.licence!r} is not a free licence. {how}"
            )
    return problems


def browser_stamp(root: Path) -> str:
    """A fingerprint of the npm packages the browser files are built from."""
    lock = json.loads((root / "frontend/package-lock.json").read_text())
    shipped = sorted(
        f"{path.rsplit('node_modules/', 1)[-1]} {entry['version']}"
        for path, entry in lock["packages"].items()
        if path and not entry.get("dev")
    )
    return hashlib.sha256("\n".join(shipped).encode()).hexdigest()


def stale_notices(root: Path) -> list[str]:
    """Why the notices file does not cover the locked browser packages."""
    lines = (root / NOTICES).read_text().splitlines()
    stamped = next((line for line in reversed(lines) if line.startswith(STAMP)), "")
    if stamped.removeprefix(STAMP) == browser_stamp(root):
        return []
    return [
        f"{NOTICES}: not current for the browser packages in "
        "frontend/package-lock.json. Add what the new packages' licences ask "
        "for, then run bin/licenses stamp."
    ]


def stamp(root: Path) -> None:
    """Record that the notices were reviewed for the locked browser packages."""
    lines = (root / NOTICES).read_text().splitlines()
    lines = [line for line in lines if not line.startswith(STAMP)]
    lines.append(STAMP + browser_stamp(root))
    (root / NOTICES).write_text("\n".join(lines) + "\n")


def check(root: Path) -> tuple[int, list[str]]:
    """How many things were looked at, and every refusal."""
    known, problems = accepted(root)
    packages = (
        python_packages(root)
        + npm_packages(root)
        + image_packages(root)
        + action_packages(root)
    )
    problems += refused(packages, known)
    problems += stale_notices(root)
    return len(packages), problems


def staged() -> list[str]:
    listing = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--no-renames"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout
    return listing.splitlines()


def main(argv: list[str]) -> int:
    started = time.perf_counter()
    if argv[:1] == ["stamp"]:
        stamp(ROOT)
        return 0
    if argv[:1] != ["check"]:
        sys.stderr.write("bin/licenses check [--commit] | stamp\n")
        return 2
    if "--commit" in argv and not touches_licences(staged()):
        sys.stdout.write("No licence input is staged; not checked.\n")
        return 0
    count, problems = check(ROOT)
    for line in problems:
        sys.stderr.write(f"{line}\n")
    if not problems:
        took = time.perf_counter() - started
        sys.stdout.write(f"{count} packages, images and actions: free. {took:.2f} s\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
