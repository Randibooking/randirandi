# SPDX-License-Identifier: AGPL-3.0-only
import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from randirandi.core.member_import import import_members
from randirandi.core.models import ImportOutcome, Organization
from randirandi.member_parsers import MADE_UP


class Command(BaseCommand):
    help = "Import the bundled made-up members into one sandbox organization."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.ENVIRONMENT != "sandbox" or not settings.SWITCHES.made_up_members:
            raise CommandError("Made-up members can only be loaded in a sandbox.")
        try:
            data = (settings.BASE_DIR / "dummy-members.csv").read_bytes()
        except OSError:
            raise CommandError(
                "The bundled dummy member file cannot be read."
            ) from None
        # The import takes the organization's lock itself, so two first
        # invocations do not compete for the same input.
        with transaction.atomic():
            try:
                org = Organization.objects.select_for_update().get(
                    subdomain=options["subdomain"]
                )
            except Organization.DoesNotExist:
                raise CommandError("No organization has that subdomain.") from None
            run = import_members(
                org,
                MADE_UP,
                MADE_UP.read(data),
                file_name="dummy-members.csv",
                file_size=len(data),
            )
        self.stdout.write(
            f"Import {run.pk}: {run.outcome}; created={run.created}, "
            f"changed={run.changed}, "
            f"granted={run.granted}, revoked={run.revoked}, skipped={run.skipped}."
        )
        self.stdout.write(json.dumps(run.report, sort_keys=True))
        if run.outcome != ImportOutcome.APPLIED:
            raise CommandError(
                "The member import did not apply; inspect the recorded report."
            )
