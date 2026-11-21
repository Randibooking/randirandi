# SPDX-License-Identifier: AGPL-3.0-only
"""A disposable, loopback-only example with its own Compose resources."""

import argparse
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "randirandi-example"
LABEL = "org.randirandi.local-example"
KEYS = (
    "RANDIRANDI_SECRET_KEY",
    "RANDIRANDI_AUTHENTICATOR_KEY",
    "RANDIRANDI_DB_OWNER_PASSWORD",
    "RANDIRANDI_DB_APP_PASSWORD",
)
SERVICES = ("app", "jobs", "migrate", "caddy", "db", "db-roles")
VOLUMES = ("db-data", "db-socket", "caddy-data", "caddy-config")
SEED = """
from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from randirandi.core.models import Organization, Member
if settings.ENVIRONMENT != 'sandbox' or not settings.SWITCHES.made_up_members:
    raise RuntimeError('The example requires a fictional sandbox')
with transaction.atomic():
    if Organization.objects.exists():
        expected = Organization.objects.filter(
            subdomain='harbourview', name='Harbour View Housing Co-op'
        )
        if Organization.objects.count() != 1 or not expected.exists():
            raise RuntimeError('The example database contains another organization')
        print('The existing fictional example is ready.')
    else:
        call_command('load_setup', 'setups/example.toml')
        call_command('import_dummy_members', 'harbourview')
        org = Organization.objects.get(subdomain='harbourview')
        admin = Member.objects.of(org).get(email='ola.nordmann@example.com')
        call_command('first_administrator', 'harbourview', admin.pk, 'Administrator')
"""


class RefusedError(Exception):
    pass


def run(
    command: Sequence[str],
    *,
    environ: Mapping[str, str],
    text: str | None = None,
    capture: bool = False,
) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed executables and argument lists
        command,
        cwd=ROOT,
        env=environ,
        input=text,
        text=True,
        capture_output=capture,
        check=True,
    )
    return result.stdout if capture else ""


def resources(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """Inspect only this Compose project; never borrow an existing stack."""
    found = []
    for kind in ("container", "volume", "network"):
        args = ["docker", kind, "ls", "--quiet"]
        if kind == "container":
            args.append("--all")
        args.extend(["--filter", f"label=com.docker.compose.project={PROJECT}"])
        names = set(run(args, environ=environ, capture=True).split())
        # Compose may reuse an unlabelled volume/network with its expected name.
        # Inspect exact names as well as labelled resources before any mutation.
        expected = (
            {f"{PROJECT}-{service}-1" for service in SERVICES}
            if kind == "container"
            else {
                f"{PROJECT}_{name}"
                for name in (VOLUMES if kind == "volume" else ("web", "db"))
            }
        )
        all_names = [
            "docker",
            kind,
            "ls",
            "--format",
            "{{.Names}}" if kind == "container" else "{{.Name}}",
        ]
        if kind == "container":
            all_names.append("--all")
        names.update(
            set(run(all_names, environ=environ, capture=True).split()) & expected
        )
        if not names:
            continue
        entries = json.loads(
            run(["docker", kind, "inspect", *names], environ=environ, capture=True)
        )
        for entry in entries:
            labels = (
                entry["Config"].get("Labels")
                if kind == "container"
                else entry.get("Labels")
            ) or {}
            if (
                labels.get(LABEL) != "1"
                or labels.get("com.docker.compose.project") != PROJECT
            ):
                raise RefusedError(
                    "A resource with the example project name is not owned by this "
                    "launcher. Nothing was changed."
                )
            found.append({"kind": kind, "data": entry})
    return found


def environment(
    inherited: Mapping[str, str], existing: Sequence[dict[str, Any]]
) -> dict[str, str]:
    """Keep generated keys in the existing local containers, never a key file."""
    env = {
        key: value
        for key, value in inherited.items()
        if not key.startswith(("RANDIRANDI_", "COMPOSE_"))
    }
    previous: dict[str, str] = {}
    for item in existing:
        if item["kind"] != "container":
            continue
        config = item["data"]["Config"]
        if config["Labels"].get("com.docker.compose.service") == "app":
            previous = dict(
                value.split("=", 1) for value in config["Env"] if "=" in value
            )
    if any(item["kind"] == "volume" for item in existing) and not all(
        previous.get(key) for key in KEYS[:2]
    ):
        raise RefusedError(
            "Example volumes exist without their application keys. Use reset for "
            "a fresh fictional example; no data was changed."
        )
    env.update({key: previous.get(key) or secrets.token_hex(32) for key in KEYS})
    env.update(
        RANDIRANDI_ENVIRONMENT="sandbox",
        RANDIRANDI_ALLOWED_HOSTS="harbourview.localhost,localhost",
        RANDIRANDI_ORGANIZATION_DOMAIN="localhost",
        RANDIRANDI_SENDER="RandiRandi <post@localhost>",
        RANDIRANDI_SITE_ADDRESS="harbourview.localhost",
        RANDIRANDI_TLS="internal",
        RANDIRANDI_BIND="127.0.0.1",
        RANDIRANDI_HTTP_PORT="8085",
        RANDIRANDI_HTTPS_PORT="8445",
        RANDIRANDI_EMAIL_TO_CONSOLE="1",
        RANDIRANDI_MADE_UP_MEMBERS="1",
        RANDIRANDI_IMAGE_TAG="example",
    )
    return env


def overlay() -> str:
    # JSON is a YAML subset, so no YAML dependency is needed by the launcher.
    labelled = {"labels": {LABEL: "1"}}
    return json.dumps(
        {
            "services": dict.fromkeys(SERVICES, labelled),
            "volumes": dict.fromkeys(VOLUMES, labelled),
            "networks": dict.fromkeys(("web", "db"), labelled),
        }
    )


def compose(
    args: Sequence[str], environ: Mapping[str, str], *, capture: bool = False
) -> str:
    return run(
        [
            str(ROOT / "bin/dev"),
            "--project-name",
            PROJECT,
            "--file",
            "compose.yaml",
            "--file",
            "-",
            *args,
        ],
        environ=environ,
        text=overlay(),
        capture=capture,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=(
            "start",
            "stop",
            "reset",
            "destroy",
            "logs",
            "status",
            "certificate",
        ),
    )
    args = parser.parse_args(argv)
    try:
        existing = resources(os.environ)
        # Reset/destroy deliberately discard only verified example resources.
        if args.action in {"reset", "destroy"}:
            env = environment(os.environ, [])
            compose(["down", "--volumes", "--remove-orphans"], env)
            existing = []
            if args.action == "destroy":
                return 0
        env = environment(os.environ, existing)
        if args.action in {"start", "reset"}:
            compose(["up", "--build", "--wait", "--wait-timeout", "120"], env)
            compose(
                ["exec", "-T", "app", "python", "manage.py", "shell", "-c", SEED], env
            )
            sys.stdout.write(
                "Example ready: https://harbourview.localhost:8445/\n"
                "Administrator: ola.nordmann@example.com; "
                "member: mari.testesen@example.com\n"
                "Read sign-in codes with bin/example logs. "
                "Administrator MFA stays enabled.\n"
                "Trust the local CA as described in docs/running.md; "
                "bin/example certificate prints it.\n"
                "Choose Function room, internal, a future weekday "
                "and a free 1-12-hour booking.\n"
            )
        elif args.action == "stop":
            compose(["stop"], env)
        elif args.action == "logs":
            compose(["logs", "--follow", "app"], env)
        elif args.action == "status":
            compose(["ps"], env)
        elif args.action == "certificate":
            certificate = compose(
                [
                    "exec",
                    "-T",
                    "caddy",
                    "cat",
                    "/data/caddy/pki/authorities/local/root.crt",
                ],
                env,
                capture=True,
            )
            sys.stdout.write(certificate)
    except RefusedError as error:
        sys.stderr.write(str(error) + "\n")
        return 1
    except subprocess.CalledProcessError:
        sys.stderr.write(
            "The example command failed. "
            "Existing resources remain available for inspection.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
