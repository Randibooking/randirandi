# SPDX-License-Identifier: AGPL-3.0-only
"""An unclassified file or removed licence header fails the source gate."""

import json
import subprocess
from pathlib import Path

from tools.source_licenses import problems


def test_source_gate_checks_new_files_and_headers(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    entries = {
        "source-licences.json": {"licence": "AGPL-3.0-only"},
        "script.py": {"licence": "AGPL-3.0-only", "header": True},
    }
    (tmp_path / "source-licences.json").write_text(json.dumps(entries))
    script = tmp_path / "script.py"
    script.write_text("# SPDX-License-Identifier: AGPL-3.0-only\n")
    assert problems(tmp_path) == []
    script.write_text("pass\n")
    assert problems(tmp_path) == ["Missing SPDX header: script.py"]
    (tmp_path / "new.py").write_text("pass\n")
    assert "Unclassified licence: new.py" in problems(tmp_path)
