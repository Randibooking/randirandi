# SPDX-License-Identifier: AGPL-3.0-only
"""The example cannot inherit provider access or reset another Compose project."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from tools import example


def test_example_is_local_and_drops_inherited_service_credentials() -> None:
    env = example.environment(
        {
            "PATH": "/usr/bin",
            "RANDIRANDI_ENVIRONMENT": "production",
            "RANDIRANDI_BIND": "0.0.0.0",  # noqa: S104 -- inherited value must be ignored
            "RANDIRANDI_INFISICAL_PROJECT": "private-project",
            "RANDIRANDI_SMTP_PASSWORD": "fictional-provider-value",
            "RANDIRANDI_SANDBOX_MAIL_TO": "someone@example.test",
            "COMPOSE_PROJECT_NAME": "another-stack",
        },
        [],
    )
    assert env["PATH"] == "/usr/bin"
    assert env["RANDIRANDI_ENVIRONMENT"] == "sandbox"
    assert env["RANDIRANDI_BIND"] == "127.0.0.1"
    assert env["RANDIRANDI_EMAIL_TO_CONSOLE"] == "1"
    assert env["RANDIRANDI_MADE_UP_MEMBERS"] == "1"
    assert "COMPOSE_PROJECT_NAME" not in env
    assert "RANDIRANDI_SANDBOX_MAIL_TO" not in env
    assert not any("INFISICAL" in key or "SMTP" in key for key in env)
    assert all(len(env[key]) == 64 for key in example.KEYS)


def test_restart_keeps_session_and_authenticator_keys_from_its_own_container() -> None:
    existing = [
        {
            "kind": "container",
            "data": {
                "Config": {
                    "Labels": {"com.docker.compose.service": "app", example.LABEL: "1"},
                    "Env": [
                        "RANDIRANDI_SECRET_KEY=existing-session",
                        "RANDIRANDI_AUTHENTICATOR_KEY=existing-mfa",
                    ],
                }
            },
        },
        {"kind": "volume", "data": {}},
    ]
    env = example.environment({}, existing)
    assert env["RANDIRANDI_SECRET_KEY"] == "existing-session"  # noqa: S105 -- fixture
    assert env["RANDIRANDI_AUTHENTICATOR_KEY"] == "existing-mfa"


def test_start_refuses_orphaned_data_instead_of_losing_authenticator_keys() -> None:
    with pytest.raises(example.RefusedError, match="without their application keys"):
        example.environment({}, [{"kind": "volume", "data": {}}])


@pytest.mark.parametrize("foreign_kind", ["container", "volume", "network"])
def test_reset_refuses_every_kind_of_unowned_resource_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    foreign_kind: str,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        *,
        environ: Mapping[str, str],
        text: str | None = None,
        capture: bool = False,
    ) -> str:
        calls.append(list(command))
        assert command[0] == "docker"
        assert command[2] in {"ls", "inspect"}
        if command[2] == "ls":
            if "--filter" in command:
                assert f"label=com.docker.compose.project={example.PROJECT}" in command
                return "foreign\n" if command[1] == foreign_kind else ""
            return ""
        item: dict[str, Any] = {"Labels": {}}
        return json.dumps([{"Config": item} if foreign_kind == "container" else item])

    monkeypatch.setattr(example, "run", fake_run)
    assert example.main(["reset"]) == 1
    assert calls


def test_all_example_resources_carry_the_ownership_label() -> None:
    config = json.loads(example.overlay())
    assert set(config["services"]) == set(example.SERVICES)
    assert set(config["volumes"]) == set(example.VOLUMES)
    for group in config.values():
        assert all(item["labels"] == {example.LABEL: "1"} for item in group.values())


@pytest.mark.parametrize("action", ["start", "reset", "destroy"])
def test_unlabelled_named_volume_is_never_adopted_or_erased(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        environ: Mapping[str, str],
        text: str | None = None,
        capture: bool = False,
    ) -> str:
        assert command[0] == "docker" and command[2] in {"ls", "inspect"}
        if command[2] == "inspect":
            assert command[1] == "volume"
            return json.dumps([{"Labels": {}}])
        if command[1] == "volume" and "--format" in command:
            return f"{example.PROJECT}_db-data\n"
        return ""

    monkeypatch.setattr(example, "run", fake_run)
    assert example.main([action]) == 1
