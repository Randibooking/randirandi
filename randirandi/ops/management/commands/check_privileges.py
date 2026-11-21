# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from randirandi.ops import privileges


class Command(BaseCommand):
    help = (
        "Compares the app's database user with the declared contract of its "
        "rights, and changes nothing. Prints table names and counts only. "
        "Exits 1 on a difference."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        with connection.cursor() as cursor:
            if not privileges.has_login(cursor):
                raise CommandError(f"The database has no user {privileges.APP}.")
            missing, extra = privileges.difference(cursor)
            wanted = len(privileges.declared(cursor))
        for name, count in privileges.counts(extra).items():
            self.stdout.write(f"beyond the contract: {name}: {count}")
        for name, count in privileges.counts(missing).items():
            self.stdout.write(f"missing: {name}: {count}")
        if missing or extra:
            raise CommandError(
                f"{len(extra)} rights beyond the contract, {len(missing)} missing."
            )
        self.stdout.write(f"privileges: {wanted} rights, as the contract.")
