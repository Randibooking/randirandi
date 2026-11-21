# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from randirandi.core import validator
from randirandi.core.models import Organization


class Command(BaseCommand):
    help = (
        "Lists an organization's shared containers on one asset that book "
        "different floor plans and cover the same time from now on: one of "
        "each pair was written past the validator. Neither offers the time "
        "they share."
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
        found = validator.clashes(organization, now=timezone.now())
        if not found:
            self.stdout.write("No containers clash.")
        for clash in found:
            self.stdout.write(
                f"Container {clash.one} on {clash.one.asset} and container "
                f"{clash.other} on {clash.other.asset} book different floor "
                "plans of one asset at the same time, so neither offers the "
                "time they share."
            )
