# SPDX-License-Identifier: AGPL-3.0-only
from typing import Any

from django.core.management.base import BaseCommand

from randirandi.core.retention import purge_expired


class Command(BaseCommand):
    help = "Erase expired member data and records under organization retention rules."

    def handle(self, *args: Any, **options: Any) -> None:
        purge_expired()
        self.stdout.write("Retention completed.")
