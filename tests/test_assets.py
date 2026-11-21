# SPDX-License-Identifier: AGPL-3.0-only
"""A local image build reuses only byte-matching source and browser output."""

import json
import shutil
from pathlib import Path

from tools.assets import OUTPUT, STAMP, current, stamp, state


def test_source_and_output_edits_invalidate_the_build(tmp_path: Path) -> None:
    for name in (
        "frontend/a.jsx",
        "randirandi/locale/en.json",
        "bin/styles",
        "tools/assets.py",
        str(OUTPUT / "app.js"),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original")
    stamp = tmp_path / OUTPUT / STAMP
    assert not current(tmp_path)
    stamp.write_text(json.dumps(state(tmp_path)))
    assert current(tmp_path)
    for name in ("frontend/a.jsx", str(OUTPUT / "app.js")):
        path = tmp_path / name
        path.write_text("changed")
        assert not current(tmp_path)
        path.write_text("original")
        assert current(tmp_path)
    (tmp_path / OUTPUT / "app.js").unlink()
    assert not current(tmp_path)


def test_an_edit_during_compilation_cannot_stamp_old_output(tmp_path: Path) -> None:
    source, built = tmp_path / "source", tmp_path / "built"
    for name in (
        "frontend/a.jsx",
        "randirandi/locale/en.json",
        "bin/styles",
        "tools/assets.py",
        str(OUTPUT / "app.js"),
    ):
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("original")
    shutil.copytree(source, built)
    (source / "frontend/a.jsx").write_text("edited while compiling")
    assert not stamp(source, built)
    assert not current(source)
    (source / "frontend/a.jsx").write_text("original")
    assert stamp(source, built)
    assert current(source)
