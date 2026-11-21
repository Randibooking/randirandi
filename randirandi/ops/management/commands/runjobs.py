# SPDX-License-Identifier: AGPL-3.0-only
import logging
import signal
import tempfile
import threading
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from randirandi.ops.jobs import JOBS, JobRunner

logger = logging.getLogger(__name__)

TICK_SECONDS = 5.0
# Touched at every tick. Docker reads its age to know the runner is alive.
HEARTBEAT = Path(tempfile.gettempdir()) / "randirandi-jobs.heartbeat"


class Command(BaseCommand):
    help = "Runs the scheduled jobs until it is told to stop."

    def handle(self, *args: Any, **options: Any) -> None:
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())

        runner = JobRunner(JOBS)
        logger.info("job runner started with %d jobs", len(JOBS))
        while not stop.is_set():
            runner.run_due()
            close_old_connections()
            HEARTBEAT.touch()
            stop.wait(TICK_SECONDS)
        logger.info("job runner stopped")
