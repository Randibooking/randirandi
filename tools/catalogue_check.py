# SPDX-License-Identifier: AGPL-3.0-only
"""Every shipped language catalogue is complete .

    python -m tools.catalogue_check            randirandi/locale/*.json
    python -m tools.catalogue_check <folder>   the catalogues in a folder

`en.json` in the folder is the reference. Each catalogue (English too) must
hold exactly its keys, a text for every key, the same placeholders per key and
both forms (`.one`, `.other`) of a counted key, so that a language that ships
shows no English by a missing key and no placeholder is lost or made up.
`bin/check` runs it; the faults are named by file and key.
"""

import json
import sys
import time
from pathlib import Path

from randirandi import labels


def check(folder: Path) -> list[str]:
    """The faults of every catalogue in `folder`, one line each, by file."""
    reference_file = folder / "en.json"
    if not reference_file.is_file():
        return [f"{folder}: has no en.json, the reference."]
    reference = json.loads(reference_file.read_text(encoding="utf-8"))
    faults = []
    for path in sorted(folder.glob("*.json")):
        try:
            texts = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            faults.append(f"{path.name}: is not JSON: {error}")
            continue
        faults += [
            f"{path.name}: {fault}"
            for fault in labels.catalogue_faults(texts, reference)
        ]
    return faults


def main(argv: list[str]) -> int:
    folder = Path(argv[0]) if argv else labels.LOCALE
    started = time.monotonic()
    faults = check(folder)
    for fault in faults:
        sys.stderr.write(f"{fault}\n")
    shipped = len(list(folder.glob("*.json")))
    sys.stdout.write(
        f"{shipped} catalogue(s) checked in {time.monotonic() - started:.3f} s: "
        f"{len(faults)} fault(s).\n"
    )
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
