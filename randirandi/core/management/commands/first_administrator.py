# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi.core.member_admin import first_administrator
from randirandi.core.models import Organization


class Command(BaseCommand):
    help = "Name the first administrator by active member ID and admin role."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain")
        parser.add_argument("member_id", type=int)
        parser.add_argument(
            "role", help="The organization's existing role with admin access."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            org = Organization.objects.get(subdomain=options["subdomain"])
            member = first_administrator(org, options["member_id"], options["role"])
        except ObjectDoesNotExist:
            raise CommandError(
                "Organization, member or admin role not found in that organization."
            ) from None
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from None
        self.stdout.write(f"Member {member.pk} is the first administrator.")
