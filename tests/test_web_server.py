# SPDX-License-Identifier: AGPL-3.0-only
"""How gunicorn runs the app and writes its access log (controls C18, C31)."""

import runpy
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"


def test_the_access_log_has_no_query_and_no_network_address() -> None:
    config = runpy.run_path(str(CONFIG))

    line = config["access_log_format"]
    # The request line and the query can hold what a person typed. The
    # caller's network address is personal data.
    for atom in ("%(r)s", "%(q)s", "%(h)s", "%(f)s", "%(a)s"):
        assert atom not in line, atom
    assert "%(U)s" in line


def test_gunicorn_logs_through_the_ids_only_formatter() -> None:
    config = runpy.run_path(str(CONFIG))

    logging_config = config["logconfig_dict"]
    assert set(logging_config["formatters"]) == {"ids_only"}
    for name in ("gunicorn.error", "gunicorn.access"):
        assert logging_config["loggers"][name]["handlers"] == ["stderr"]


def test_the_app_is_loaded_once_before_the_workers_are_made() -> None:
    config = runpy.run_path(str(CONFIG))

    # The secrets are fetched once for each start. A worker that is replaced
    # while the app runs does not ask Infisical.
    assert config["preload_app"] is True


def test_one_process_answers_with_four_threads() -> None:
    config = runpy.run_path(str(CONFIG))

    # Each process holds its own copy of the app; the sandbox has 256 MB.
    assert (config["workers"], config["worker_class"], config["threads"]) == (
        1,
        "gthread",
        4,
    )
