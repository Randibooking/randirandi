# SPDX-License-Identifier: AGPL-3.0-only
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi.core import old_booking_import
from randirandi.core.models import Organization


class Command(BaseCommand):
    help = (
        "Brings in an organization's bookings from its earlier calendar, with "
        "their blocks and events, from a JSON document (old_booking_import). "
        "Without --bring-in it says what it would do and saves nothing. Prints "
        "each entry's key, kind and outcome; never a name."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain", help="The organization's subdomain.")
        parser.add_argument("file", help="The JSON document, or - for standard input.")
        parser.add_argument(
            "--administrator",
            required=True,
            help="The email of the active administrator who brings them in.",
        )
        parser.add_argument(
            "--bring-in",
            action="store_true",
            help="Save what the rules allow; without it nothing is saved.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        subdomain: str = options["subdomain"]
        organization = Organization.objects.filter(subdomain=subdomain).first()
        if organization is None:
            raise CommandError(
                f"There is no organization with the subdomain {subdomain}."
            )
        try:
            if options["file"] == "-":
                raw = json.load(sys.stdin)
            else:
                raw = json.loads(Path(options["file"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            # The error's text may quote the file; it is not repeated.
            raise CommandError(
                f"The file cannot be read as JSON ({type(error).__name__})."
            ) from None
        try:
            document = old_booking_import.read(raw)
            administrator = old_booking_import.administrator_of(
                organization, options["administrator"]
            )
        except (old_booking_import.DocumentError, LookupError) as error:
            raise CommandError(str(error)) from None
        run = (
            old_booking_import.bring_in
            if options["bring_in"]
            else old_booking_import.check
        )
        outcomes = run(organization, administrator, document)
        self.stdout.write(
            json.dumps(
                {
                    "saved": bool(options["bring_in"]),
                    "counts": dict(
                        sorted(
                            Counter(f"{o.kind}: {o.result}" for o in outcomes).items()
                        )
                    ),
                    "entries": [
                        {
                            "key": o.key,
                            "kind": o.kind,
                            "result": o.result,
                            "detail": list(o.detail),
                        }
                        for o in outcomes
                    ],
                },
                ensure_ascii=False,
                indent=1,
            )
        )
