# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from randirandi.core.models import Member, MemberStatus, Organization, Origin, Role


class Command(BaseCommand):
    help = (
        "Add the owner's own sandbox mail addresses "
        "(RANDIRANDI_SANDBOX_MAIL_TO) as members of one sandbox organization."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("subdomain")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.ENVIRONMENT != "sandbox" or not settings.SWITCHES.sandbox_mail_to:
            raise CommandError(
                "Owner members can only be added in a sandbox that has "
                "RANDIRANDI_SANDBOX_MAIL_TO."
            )
        added = 0
        with transaction.atomic():
            try:
                org = Organization.objects.select_for_update().get(
                    subdomain=options["subdomain"]
                )
            except Organization.DoesNotExist:
                raise CommandError("No organization has that subdomain.") from None
            standard = (
                Role.objects.of(org).filter(pk=org.standard_role_id).first()
                if org.standard_role_id is not None
                else None
            )
            if standard is None or standard.admin_access:
                raise CommandError(
                    "The organization needs a standard role without admin access."
                )
            for email in settings.SWITCHES.sandbox_mail_to:
                # An address that is already a member is left as it is.
                _, created = Member.objects.of(org).get_or_create(
                    email=email,
                    defaults={
                        "organization": org,
                        "role": standard,
                        "status": MemberStatus.ACTIVE,
                        # An import never disables or changes this member.
                        "origin": Origin.ADMINISTRATOR,
                    },
                )
                added += created
        self.stdout.write(
            f"Added {added}; {len(settings.SWITCHES.sandbox_mail_to) - added} "
            "were members already."
        )
