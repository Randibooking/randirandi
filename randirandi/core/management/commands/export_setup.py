# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi import setup_file
from randirandi.core import setup as setups
from randirandi.core.models import Organization


class Command(BaseCommand):
    help = (
        "Writes an organization's setup, as it is now, as a setup file in "
        "format 3. It holds no members and no secrets."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain", help="The organization's subdomain.")

    def handle(self, *args: Any, **options: Any) -> None:
        subdomain: str = options["subdomain"]
        try:
            organization = Organization.objects.get(subdomain=subdomain)
        except Organization.DoesNotExist:
            raise CommandError(
                f"There is no organization with the subdomain {subdomain}."
            ) from None
        try:
            written = setup_file.write(setups.export(organization))
        except setup_file.SetupError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(written, ending="")
