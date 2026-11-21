# SPDX-License-Identifier: AGPL-3.0-only
import logging
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection

logger = logging.getLogger(__name__)

# What the owner of the tables gives the app's user. Every table the owner
# makes from now on can be read and written by the app, and nothing more; a
# migration may take some of that away again for a table that is only ever
# added to. The app makes nothing in the schema.
GRANTS = (
    "ALTER DEFAULT PRIVILEGES FOR ROLE randirandi_owner IN SCHEMA public "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO randirandi_app",
    "ALTER DEFAULT PRIVILEGES FOR ROLE randirandi_owner IN SCHEMA public "
    "GRANT USAGE, SELECT ON SEQUENCES TO randirandi_app",
    "GRANT USAGE ON SCHEMA public TO randirandi_app",
    "REVOKE CREATE ON SCHEMA public FROM randirandi_app",
)


class Command(BaseCommand):
    help = (
        "Gives the app's database user its rights on what the owner of the "
        "tables makes. Run as the owner of the tables, before the migrations. "
        "It is safe to run again."
    )
    # It runs before the first migration, when the tables are not there yet.
    requires_system_checks = []  # noqa: RUF012

    def handle(self, *args: Any, **options: Any) -> None:
        with connection.cursor() as cursor:
            for grant in GRANTS:
                cursor.execute(grant)
        logger.info("grants: %d given", len(GRANTS))
