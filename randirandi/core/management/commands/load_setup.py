# SPDX-License-Identifier: AGPL-3.0-only
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from randirandi import setup_file
from randirandi.core import setup as setups


class Command(BaseCommand):
    help = (
        "Makes a new organization from a setup file, or from standard input "
        "when the file is `-`. A file that breaks a rule is not loaded, in "
        "whole or in part."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "file", type=Path, help="The setup file, or `-` for standard input."
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Put the file through every check, and load nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        file: Path = options["file"]
        from_stdin = str(file) == "-"
        name = "standard input" if from_stdin else str(file)
        try:
            if from_stdin:
                setup = setup_file.parse(sys.stdin.read())
            else:
                setup = setup_file.read(file)
            today = timezone.localdate(timezone=ZoneInfo(setup.organization.time_zone))
            if options["check"]:
                setups.check(setup, today=today)
            else:
                setups.load(setup, today=today)
        except OSError as error:
            raise CommandError(f"{name} cannot be read: {error.strerror}.") from None
        except UnicodeDecodeError:
            raise CommandError(f"{name} is not UTF-8 text.") from None
        except setup_file.SetupError as error:
            faults = "\n".join(f"  {fault}" for fault in error.faults)
            raise CommandError(f"{name} is not loaded:\n{faults}") from None

        subdomain = setup.organization.subdomain
        holds = (
            f"{_counted(len(setup.assets), 'asset')}, "
            f"{_counted(len(setup.booking_configurations), 'booking configuration')} "
            f"and {_counted(len(setup.price_books), 'price book')}"
        )
        if options["check"]:
            self.stdout.write(f"{name} passes every check: {holds}. Nothing is loaded.")
        else:
            self.stdout.write(f"Loaded the organization {subdomain}: {holds}.")


def _counted(count: int, what: str) -> str:
    return f"{count} {what}" if count == 1 else f"{count} {what}s"
