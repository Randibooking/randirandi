# SPDX-License-Identifier: AGPL-3.0-only
import logging
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from randirandi.ops.calls import PORT, Calls, serve
from randirandi.ops.jobs import JobRunner, configured_jobs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Answers the platform's calls in the second container: its schedule, "
        "and the one run of a deploy."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        jobs = configured_jobs()
        calls = Calls(JobRunner(jobs), settings.OUTSIDE_CHECK_ADDRESS)
        logger.info("answering calls on port %d, with %d jobs", PORT, len(jobs))
        serve(calls)
        logger.info("answering calls stopped")
