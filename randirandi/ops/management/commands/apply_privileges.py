# SPDX-License-Identifier: AGPL-3.0-only
import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from randirandi.ops import privileges

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Brings the app's database user to the declared contract of its rights "
        "(randirandi/ops/privileges.py): takes away what is beyond it and gives "
        "what is missing, in one transaction. Run as the owner of the tables, "
        "after the migrations. It is safe to run again."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        with transaction.atomic(), connection.cursor() as cursor:
            if not privileges.has_login(cursor):
                raise CommandError(f"The database has no user {privileges.APP}.")
            missing, extra = privileges.difference(cursor)
            beyond = privileges.counts(extra)
            lacking = privileges.counts(missing)
            taken, given = privileges.bring_to_contract(cursor)
        for name, count in beyond.items():
            logger.info("privileges: %s: %d beyond the contract, taken", name, count)
        for name, count in lacking.items():
            logger.info("privileges: %s: %d missing, given", name, count)
        logger.info("privileges: %d taken, %d given", taken, given)
