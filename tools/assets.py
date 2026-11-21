# SPDX-License-Identifier: AGPL-3.0-only
"""Cache only an exact frontend input and output match for local image builds."""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("randirandi/core/static/core/ui")
STAMP = ".build.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs(root: Path) -> dict[str, str]:
    files = [
        p
        for p in (root / "frontend").rglob("*")
        if p.is_file() and not {"node_modules", ".vite"}.intersection(p.parts)
    ]
    files += [
        root / p for p in ("randirandi/locale/en.json", "bin/styles", "tools/assets.py")
    ]
    return {str(p.relative_to(root)): digest(p) for p in sorted(files)}


def state(root: Path) -> dict[str, dict[str, str]]:
    outputs = [p for p in (root / OUTPUT).glob("*") if p.is_file() and p.name != STAMP]
    return {
        "inputs": inputs(root),
        "outputs": {str(p.relative_to(root)): digest(p) for p in sorted(outputs)},
    }


def current(root: Path) -> bool:
    saved = root / OUTPUT / STAMP
    if not saved.is_file():
        return False
    try:
        actual = state(root)
        return bool(actual["outputs"]) and json.loads(saved.read_text()) == actual
    except OSError, ValueError:
        return False


def stamp(root: Path, built: Path) -> bool:
    """Identify the actual compiled copy, rather than later live-source edits."""
    expected = inputs(built)
    if inputs(root) != expected:
        (root / OUTPUT / STAMP).unlink(missing_ok=True)
        return False
    actual = state(root)
    actual["inputs"] = expected
    (root / OUTPUT / STAMP).write_text(json.dumps(actual, sort_keys=True) + "\n")
    return True


if __name__ == "__main__":
    if sys.argv[1] == "current":
        raise SystemExit(0 if current(ROOT) else 1)
    if sys.argv[1] != "stamp":
        raise SystemExit("Use current or stamp")
    if not stamp(ROOT, Path(sys.argv[2])):
        raise SystemExit("Frontend source changed during compilation; build again")
