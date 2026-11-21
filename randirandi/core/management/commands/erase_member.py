# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi.core.member_data import operator_erase_member
from randirandi.core.models import Organization


class Command(BaseCommand):
    help = "Operator: erase a member's personal data on their written request."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain")
        parser.add_argument("member_id", type=int)
        parser.add_argument(
            "--written-request-received",
            action="store_true",
            help="Operator confirms the member asked in writing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["written_request_received"]:
            raise CommandError(
                "Erase refused: confirm the member's written request with "
                "--written-request-received."
            )
        try:
            org = Organization.objects.get(subdomain=options["subdomain"])
            last_administrator = operator_erase_member(
                organization=org, member_id=options["member_id"]
            )
        except ObjectDoesNotExist:
            raise CommandError(
                "Erase refused: unknown organization or member."
            ) from None
        except ValueError:
            raise CommandError(
                "Erase refused: a positive member identifier is required."
            ) from None
        except PermissionDenied as refusal:
            raise CommandError(f"Erase refused: {refusal}") from None
        self.stdout.write(f"Erased member {options['member_id']}.")
        if last_administrator:
            self.stdout.write(
                "That was the organization's last active administrator. Nobody "
                "can administer it until you name a new one with "
                "`manage.py first_administrator`."
            )
