# SPDX-License-Identifier: AGPL-3.0-only
"""The job runner: scheduled work, done by one process beside the app.

A job is a name, how often it runs, and what it does. A configured worker
also checks its backups. Product jobs arrive with their own milestones.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings

from randirandi.core.booking_emails import (
    send_cancellations,
    send_confirmations,
    send_reminders,
)
from randirandi.core.event_admin import top_up
from randirandi.core.event_notices import send_event_notices
from randirandi.core.login_delivery import send_pending_codes
from randirandi.core.payment_reconciliation import reconcile_requested
from randirandi.core.payment_refunds import alert_completed
from randirandi.core.payouts import read_reports
from randirandi.core.retention import purge_expired
from randirandi.core.security_notices import send_security_notices
from randirandi.ops.backups import BackupCheck

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    name: str
    every: timedelta
    run: Callable[[], None]


def make_event_days() -> None:
    top_up()


JOBS: tuple[Job, ...] = (
    Job("login-email", timedelta(seconds=5), send_pending_codes),
    Job("payments", timedelta(seconds=5), reconcile_requested),
    Job("payment-refund-alerts", timedelta(seconds=5), alert_completed),
    Job("payouts", timedelta(minutes=10), read_reports),
    Job("security-notices", timedelta(seconds=30), send_security_notices),
    Job("booking-confirmations", timedelta(seconds=5), send_confirmations),
    Job("booking-reminders", timedelta(minutes=1), send_reminders),
    Job("booking-cancellations", timedelta(seconds=5), send_cancellations),
    Job("retention", timedelta(hours=1), purge_expired),
    # The next days of every repeating event, a year ahead , and
    # the emails about the days it skipped.
    Job("event-days", timedelta(days=1), make_event_days),
    Job("event-notices", timedelta(seconds=30), send_event_notices),
)


def configured_jobs() -> tuple[Job, ...]:
    if not settings.BACKUP_CHECK_INSTANCE_ID:
        return JOBS
    check = BackupCheck(
        settings.BACKUP_CHECK_INSTANCE_ID,
        str(settings.DATABASES["default"]["NAME"]),
        settings.BACKUP_CHECK_SECRET_KEY,
    )
    return (*JOBS, Job("backup-check", timedelta(hours=1), check.run))


class JobRunner:
    """Runs each job when it is due. One job failing does not stop the rest."""

    def __init__(
        self, jobs: Sequence[Job], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._jobs = tuple(jobs)
        self._clock = clock
        # Every job is due at once when the runner starts.
        self._due_at = {job.name: clock() for job in self._jobs}

    def run_due(self) -> None:
        for job in self._jobs:
            started = self._clock()
            if started < self._due_at[job.name]:
                continue
            self._due_at[job.name] = started + job.every.total_seconds()
            try:
                job.run()
            except Exception:
                # Whatever a job raises, the next one runs.
                logger.exception("job failed: %s", job.name)
            else:
                milliseconds = round((self._clock() - started) * 1000)
                logger.info("job done: %s in %d ms", job.name, milliseconds)
