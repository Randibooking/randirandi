# SPDX-License-Identifier: AGPL-3.0-only
"""Keep application brand copies identical to the approved product package."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_or_sync(root: Path, *, sync: bool = False) -> int:
    """Verify the package before checking or restoring declared consumer copies."""
    manifest = json.loads((root / "frontend/brand/asset-sources.json").read_text())
    exports = manifest["runtime_exports"]
    for entry in manifest["files"]:
        if entry["path"] in exports:
            continue
        path = root / entry["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Approved brand asset changed: {entry['path']}")
    for destination, source in exports.items():
        if sync:
            shutil.copyfile(root / source, root / destination)
        if (root / destination).read_bytes() != (root / source).read_bytes():
            raise ValueError(f"Brand copy differs from approved export: {destination}")
    return len(exports)


def main() -> None:
    """Check by default; sync only on an explicit command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "sync"))
    args = parser.parse_args()
    count = check_or_sync(ROOT, sync=args.command == "sync")
    sys.stdout.write(f"{count} runtime brand exports match the approved package.\n")


if __name__ == "__main__":
    main()
