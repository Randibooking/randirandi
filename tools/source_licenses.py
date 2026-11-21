# SPDX-License-Identifier: AGPL-3.0-only
"""Verify explicit licensing and headers without modifying protected assets."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "source-licences.json"


def problems(root: Path) -> list[str]:
    entries = json.loads((root / MANIFEST).read_text())
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode()
    files = {
        p
        for p in output.split("\0")
        if p
        and (root / p).is_file()
        and not p.startswith("randirandi/core/static/core/ui/")
        and p != "randirandi/release.json"
    }
    found = [f"Unclassified licence: {p}" for p in sorted(files - entries.keys())]
    for name in sorted(files & entries.keys()):
        item = entries[name]
        if not item.get("licence"):
            found.append(f"Missing licence: {name}")
        if item.get("header"):
            lines = (root / name).read_text().splitlines()
            if not any(
                "SPDX-License-Identifier: " + item["licence"] in s for s in lines[:3]
            ):
                found.append(f"Missing SPDX header: {name}")
    return found


if __name__ == "__main__":
    errors = problems(ROOT)
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        raise SystemExit(1)
    sys.stdout.write("Source licences and protected-asset annotations are complete.\n")
