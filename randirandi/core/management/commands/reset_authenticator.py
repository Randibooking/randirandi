# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi.core.models import Organization
from randirandi.core.second_factor import reset_authenticator
from randirandi.core.security_notices import send_security_notices


class Command(BaseCommand):
    help = "Operator fallback: reset an authenticator, with audit and alerts."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain")
        parser.add_argument("member_id", type=int)
        parser.add_argument(
            "--no-other-administrator-available",
            action="store_true",
            help="Operator confirms no other administrator is available.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            org = Organization.objects.get(subdomain=options["subdomain"])
            reset_authenticator(
                organization=org,
                member_id=options["member_id"],
                no_other_administrator_available=options[
                    "no_other_administrator_available"
                ],
            )
        except ObjectDoesNotExist, PermissionDenied, ValueError:
            raise CommandError(
                "Reset refused: check organization/member and confirm "
                "no other administrator is available."
            ) from None
        send_security_notices()
        self.stdout.write(
            f"Authenticator reset for member {options['member_id']}; "
            "audited and administrator alerts queued."
        )
