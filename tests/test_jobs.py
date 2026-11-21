# SPDX-License-Identifier: AGPL-3.0-only
"""The job runner."""

import logging
from datetime import timedelta

import pytest

from randirandi.ops.jobs import Job, JobRunner


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_job_runs_at_start_and_then_when_it_is_due() -> None:
    clock = Clock()
    runs: list[float] = []
    job = Job("erase", timedelta(minutes=10), lambda: runs.append(clock.now))
    runner = JobRunner([job], clock=clock)

    runner.run_due()
    clock.now += 599
    runner.run_due()
    clock.now += 1
    runner.run_due()

    assert runs == [1000.0, 1600.0]


def test_a_job_that_fails_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ran: list[str] = []

    def fail() -> None:
        raise RuntimeError("the payment provider did not answer")

    runner = JobRunner(
        [
            Job("expire-holds", timedelta(minutes=1), fail),
            Job("erase", timedelta(minutes=1), lambda: ran.append("erase")),
        ],
        clock=Clock(),
    )

    with caplog.at_level(logging.INFO, logger="randirandi.ops.jobs"):
        runner.run_due()

    assert ran == ["erase"]
    assert "job failed: expire-holds" in caplog.text
    assert "job done: erase" in caplog.text


def test_a_job_that_failed_waits_for_its_next_time() -> None:
    clock = Clock()
    tries: list[float] = []

    def fail() -> None:
        tries.append(clock.now)
        raise RuntimeError

    runner = JobRunner([Job("import", timedelta(hours=1), fail)], clock=clock)

    runner.run_due()
    clock.now += 60
    runner.run_due()

    assert tries == [1000.0]
