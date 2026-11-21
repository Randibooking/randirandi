# SPDX-License-Identifier: AGPL-3.0-only
"""A failed, stale or partial metadata read must never report a good backup."""

import io
import logging
import urllib.error
import urllib.request
import urllib.response
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any

import pytest
from django.test import override_settings

from randirandi.ops.backups import BackupCheck, RecoveryKind
from randirandi.ops.jobs import Job, JobRunner, configured_jobs

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
INSTANCE = "ccc1fb0d-3be8-4766-af76-efbc9c5e1a9a"
STAND_IN = "public-test-stand-in"


def backup(**changes: Any) -> dict[str, Any]:
    return {
        "id": "one",
        "instance_id": INSTANCE,
        "database_name": "randirandi",
        "status": "ready",
        "created_at": (NOW - timedelta(hours=24)).isoformat(),
        "expires_at": (NOW + timedelta(days=29)).isoformat(),
        "size": 183940,
    } | changes


def snapshot(**changes: Any) -> dict[str, Any]:
    result = backup(size=5_000_000_000)
    del result["database_name"]
    return result | changes


def reader(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    snapshots: list[dict[str, Any]] | None = None,
) -> BackupCheck:
    def read(self: BackupCheck, page: int, kind: RecoveryKind) -> dict[str, Any]:
        assert page == 1
        points = rows if kind == "backups" else snapshots or []
        field = "database_backups" if kind == "backups" else "snapshots"
        return {field: points, "total_count": len(points)}

    monkeypatch.setattr(
        BackupCheck,
        "read_page",
        read,
    )
    return BackupCheck(INSTANCE, "randirandi", STAND_IN)


@pytest.mark.parametrize("hours,good", [(24, True), (30, True), (30.001, False)])
def test_the_newest_usable_backup_has_a_30_hour_limit(
    monkeypatch: pytest.MonkeyPatch, hours: float, good: bool
) -> None:
    check = reader(
        monkeypatch, [backup(created_at=(NOW - timedelta(hours=hours)).isoformat())]
    )
    assert check.assess(NOW).startswith("ok:") is good


@pytest.mark.parametrize(
    "changes",
    [
        {"database_name": "randirandi_restore_20261011"},
        {"status": "creating"},
        {"status": "error"},
        {"expires_at": NOW.isoformat()},
        {"size": 0},
    ],
)
def test_an_unusable_or_other_database_backup_does_not_hide_an_old_backup(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    old = backup(id="old", created_at=(NOW - timedelta(hours=31)).isoformat())
    check = reader(monkeypatch, [backup(**changes), old])
    assert check.assess(NOW) == "failed: newest backup older than 30 hours"


def test_no_backups_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader(monkeypatch, []).assess(NOW) == "failed: no usable backup"


@pytest.mark.parametrize(
    "created", ["bad-date", "2026-10-11T10:00:00", "2026-10-12T10:00:00Z"]
)
def test_invalid_or_future_dates_fail(
    monkeypatch: pytest.MonkeyPatch, created: str
) -> None:
    with pytest.raises(ValueError):
        reader(monkeypatch, [backup(created_at=created)]).assess(NOW)


def test_pages_are_read_to_completion_and_the_newest_may_be_on_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[int] = []

    def read(self: BackupCheck, page: int, kind: RecoveryKind) -> dict[str, Any]:
        if kind == "snapshots":
            return {"snapshots": [], "total_count": 0}
        pages.append(page)
        rows = (
            [backup(id=str(i), status="error") for i in range(100)]
            if page == 1
            else [backup()]
        )
        return {"database_backups": rows, "total_count": 101}

    monkeypatch.setattr(BackupCheck, "read_page", read)
    assert BackupCheck(INSTANCE, "randirandi", STAND_IN).assess(NOW).startswith("ok:")
    assert pages == [1, 2]


@pytest.mark.parametrize("count", [2, -1, "1", True])
def test_partial_or_invalid_listings_fail(
    monkeypatch: pytest.MonkeyPatch, count: object
) -> None:
    monkeypatch.setattr(
        BackupCheck,
        "read_page",
        lambda self, page, kind: {"database_backups": [backup()], "total_count": count},
    )
    with pytest.raises(ValueError):
        BackupCheck(INSTANCE, "randirandi", STAND_IN).assess(NOW)


def test_provider_exceptions_and_credential_values_never_enter_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(self: BackupCheck, page: int, kind: RecoveryKind) -> dict[str, Any]:
        raise OSError(STAND_IN + " provider response")

    monkeypatch.setattr(BackupCheck, "read_page", fail)
    check = BackupCheck(INSTANCE, "randirandi", STAND_IN)
    check.run()
    assert "backup check: failed: metadata read" in caplog.text
    assert "provider response" not in caplog.text
    assert STAND_IN not in caplog.text + repr(check)
    assert not any(record.exc_info for record in caplog.records)


def test_missing_credentials_are_reported_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unexpected(*args: Any) -> None:
        pytest.fail("Network called without a credential")

    monkeypatch.setattr(BackupCheck, "read_page", unexpected)
    BackupCheck(INSTANCE, "randirandi", "").run()
    assert "backup check: failed: credential missing" in caplog.text


@pytest.mark.parametrize("kind", ["backups", "snapshots"])
def test_credential_is_sent_only_to_fixed_https_get_and_redirect_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    kind: RecoveryKind,
) -> None:
    calls: list[str] = []

    def send(self: urllib.request.HTTPSHandler, request: urllib.request.Request) -> Any:
        assert request.full_url.startswith(
            f"https://api.scaleway.com/rdb/v1/regions/fr-par/{kind}?"
        )
        assert request.get_method() == "GET"
        assert request.get_header("X-auth-token") == STAND_IN
        calls.append(request.full_url)
        headers = Message()
        headers["Location"] = "https://another-host.example/steal"
        response = urllib.response.addinfourl(
            io.BytesIO(b""), headers, request.full_url, 302
        )
        response.msg = "Found"  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", send)
    monkeypatch.setenv("https_proxy", "http://unexpected-proxy.example:8000")
    with pytest.raises(ValueError, match="Backup read refused"):
        BackupCheck(INSTANCE, "randirandi", STAND_IN).read_page(1, kind)
    assert len(calls) == 1


def test_recent_instance_snapshot_prevents_the_stale_manual_dump_false_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_dump = backup(created_at=(NOW - timedelta(hours=32)).isoformat())
    recent = snapshot(created_at=(NOW - timedelta(hours=3)).isoformat())
    check = reader(monkeypatch, [old_dump], [recent])
    assert check.assess(NOW) == "ok: newest backup age 10800 seconds"


@pytest.mark.parametrize("hours,good", [(24, True), (30, True), (30.001, False)])
def test_snapshots_alone_obey_the_same_freshness_limit(
    monkeypatch: pytest.MonkeyPatch,
    hours: float,
    good: bool,
) -> None:
    check = reader(
        monkeypatch,
        [],
        [snapshot(created_at=(NOW - timedelta(hours=hours)).isoformat())],
    )
    assert check.assess(NOW).startswith("ok:") is good


@pytest.mark.parametrize(
    "changes",
    [
        {"instance_id": "11111111-1111-4111-8111-111111111111"},
        {"status": "creating"},
        {"status": "error"},
        {"expires_at": NOW.isoformat()},
        {"size": 0},
    ],
)
def test_unusable_or_other_instance_snapshot_cannot_hide_stale_protection(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    old = snapshot(id="old", created_at=(NOW - timedelta(hours=31)).isoformat())
    check = reader(monkeypatch, [], [snapshot(**changes), old])
    assert check.assess(NOW) == "failed: newest backup older than 30 hours"


def test_dump_of_another_instance_is_not_protection_for_this_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = reader(
        monkeypatch,
        [backup(instance_id="11111111-1111-4111-8111-111111111111")],
    )
    assert check.assess(NOW) == "failed: no usable backup"


def test_recent_dump_still_qualifies_when_there_are_no_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = reader(monkeypatch, [backup()], [])
    assert check.assess(NOW) == "ok: newest backup age 86400 seconds"


def test_snapshot_pagination_is_complete_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[tuple[RecoveryKind, int]] = []

    def read(self: BackupCheck, page: int, kind: RecoveryKind) -> dict[str, Any]:
        pages.append((kind, page))
        if kind == "backups":
            return {"database_backups": [], "total_count": 0}
        rows = (
            [snapshot(id=str(i), status="error") for i in range(100)]
            if page == 1
            else [snapshot()]
        )
        return {"snapshots": rows, "total_count": 101}

    monkeypatch.setattr(BackupCheck, "read_page", read)
    assert BackupCheck(INSTANCE, "randirandi", STAND_IN).assess(NOW).startswith("ok:")
    assert pages == [("backups", 1), ("snapshots", 1), ("snapshots", 2)]


@pytest.mark.parametrize(
    "problem", ["refused", "partial", "duplicate", "missing_field"]
)
def test_snapshot_read_failures_are_not_hidden_by_a_recent_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    problem: str,
) -> None:
    def read(self: BackupCheck, page: int, kind: RecoveryKind) -> dict[str, Any]:
        if kind == "backups":
            return {"database_backups": [backup()], "total_count": 1}
        if problem == "refused":
            raise OSError(STAND_IN + " snapshot read refused")
        if problem == "partial":
            return {"snapshots": [snapshot()], "total_count": 2}
        if problem == "duplicate":
            return {"snapshots": [snapshot(), snapshot()], "total_count": 2}
        return {"snapshots": [{"id": "missing-fields"}], "total_count": 1}

    monkeypatch.setattr(BackupCheck, "read_page", read)
    BackupCheck(INSTANCE, "randirandi", STAND_IN).run()
    assert "backup check: failed: metadata read" in caplog.text
    assert "backup check: ok:" not in caplog.text
    assert STAND_IN not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"created_at": "bad-date"},
        {"created_at": "2026-10-11T10:00:00"},
        {"created_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"expires_at": (NOW - timedelta(days=2)).isoformat()},
        {"size": True},
        {"size": "5000000000"},
        {"size": -1},
        {"size": None},
    ],
)
def test_malformed_ready_snapshot_is_a_failed_metadata_read(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    check = reader(monkeypatch, [], [snapshot(**changes)])
    with pytest.raises((ValueError, TypeError)):
        check.assess(NOW)


@pytest.mark.parametrize("environment", ["sandbox", "production"])
@override_settings(
    BACKUP_CHECK_INSTANCE_ID=INSTANCE,
    BACKUP_CHECK_SECRET_KEY=STAND_IN,
)
def test_configured_worker_runs_once_an_hour_and_laptop_does_not(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    environment: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "randirandi.ops.jobs.JOBS",
        (Job("retention", timedelta(hours=1), lambda: None),),
    )
    monkeypatch.setattr(BackupCheck, "run", lambda self: calls.append("read"))
    clock = [0.0]
    with override_settings(ENVIRONMENT=environment):
        runner = JobRunner(configured_jobs(), clock=lambda: clock[0])
    with caplog.at_level(logging.INFO):
        runner.run_due()
        clock[0] = 3599
        runner.run_due()
        clock[0] = 3600
        runner.run_due()
    assert calls == ["read", "read"]
    with override_settings(BACKUP_CHECK_INSTANCE_ID=""):
        assert [job.name for job in configured_jobs()] == ["retention"]


@pytest.mark.parametrize("credential", [STAND_IN, ""])
@override_settings(ENVIRONMENT="production", BACKUP_CHECK_INSTANCE_ID=INSTANCE)
def test_production_job_assesses_its_own_recovery_points_or_reports_missing_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    credential: str,
) -> None:
    from django.conf import settings

    seen: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr("randirandi.ops.jobs.JOBS", ())

    def page(
        self: BackupCheck, number: int, kind: RecoveryKind = "backups"
    ) -> dict[str, Any]:
        assert number == 1
        seen.append((self.instance, self.database, self.key, kind))
        rows = (
            [
                snapshot(
                    created_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
                )
            ]
            if kind == "snapshots"
            else []
        )
        return {
            "snapshots" if kind == "snapshots" else "database_backups": rows,
            "total_count": len(rows),
        }

    monkeypatch.setattr(BackupCheck, "read_page", page)
    with override_settings(BACKUP_CHECK_SECRET_KEY=credential):
        [job] = configured_jobs()
        assert job.name == "backup-check" and job.every == timedelta(hours=1)
        with caplog.at_level(logging.INFO):
            job.run()
    if credential:
        assert seen == [
            (INSTANCE, str(settings.DATABASES["default"]["NAME"]), STAND_IN, kind)
            for kind in ("backups", "snapshots")
        ]
        assert "backup check: ok:" in caplog.text
    else:
        assert seen == []
        assert "backup check: failed: credential missing" in caplog.text
    assert STAND_IN not in caplog.text
