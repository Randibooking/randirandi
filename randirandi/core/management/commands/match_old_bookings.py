# SPDX-License-Identifier: AGPL-3.0-only
import json
import sys
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi.core.models import Organization
from randirandi.core.old_bookings import holders


class Command(BaseCommand):
    help = (
        "Reads bookings from an organization's earlier calendar, a JSON list "
        "of objects with a 'title' and, when someone else holds it, a "
        "'holder' name to match instead or a 'holder_email' that names the "
        "member exactly, and prints for each, in the same "
        "order, how it fits the organization's members, their ids and whether "
        "it is resolved. Reads only; prints no names."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain", help="The organization's subdomain.")
        parser.add_argument("file", help="The JSON file, or - for standard input.")

    def handle(self, *args: Any, **options: Any) -> None:
        subdomain: str = options["subdomain"]
        try:
            organization = Organization.objects.get(subdomain=subdomain)
        except Organization.DoesNotExist:
            raise CommandError(
                f"There is no organization with the subdomain {subdomain}."
            ) from None
        try:
            if options["file"] == "-":
                entries = json.load(sys.stdin)
            else:
                entries = json.loads(Path(options["file"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            # The error's text may quote the file; it is not repeated.
            raise CommandError(
                f"The file cannot be read as JSON ({type(error).__name__})."
            ) from None
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("title"), str)
            and isinstance(entry.get("holder", ""), str)
            and isinstance(entry.get("holder_email", ""), str)
            for entry in entries
        ):
            raise CommandError(
                "The file must be a list of objects, each with a 'title' and "
                "optionally a 'holder' and a 'holder_email', all text."
            )
        found = holders(
            organization,
            (
                (entry.get("holder") or entry["title"], entry.get("holder_email", ""))
                for entry in entries
            ),
        )
        self.stdout.write(
            json.dumps(
                [
                    {
                        "fit": one.fit,
                        "member_ids": list(one.member_ids),
                        "resolved": one.found,
                    }
                    for one in found
                ]
            )
        )
