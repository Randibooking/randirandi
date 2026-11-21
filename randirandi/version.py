# SPDX-License-Identifier: AGPL-3.0-only
"""Build-time release identity; development builds do not claim a public tag."""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

METADATA = Path(__file__).with_name("release.json")
REPOSITORY = "https://github.com/Randibooking/randirandi"


@dataclass(frozen=True)
class Release:
    version: str = "dev"
    revision: str = ""

    @property
    def source_url(self) -> str:
        return f"{REPOSITORY}/tree/{self.version}" if self.revision else ""


def release(version: str, revision: str) -> Release:
    if version == "dev" and not revision:
        return Release()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", version):
        raise ValueError("Release version must be a v-prefixed semantic version")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Release revision must be a full Git commit")
    return Release(version, revision)


def current() -> Release:
    if not METADATA.exists():
        return Release()
    data = json.loads(METADATA.read_text())
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) for key in ("version", "revision")
    ):
        raise ValueError("Invalid release metadata")
    return release(data["version"], data["revision"])


if __name__ == "__main__":
    stamped = release(sys.argv[1], sys.argv[2])
    METADATA.write_text(
        json.dumps({"version": stamped.version, "revision": stamped.revision}) + "\n"
    )
