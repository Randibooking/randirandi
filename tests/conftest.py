# SPDX-License-Identifier: AGPL-3.0-only
"""Request tests use source assets; delivery tests select collected storage."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from randirandi import labels


@pytest.fixture(autouse=True)
def source_static_assets() -> Iterator[None]:
    # Error pages share the product shell and now resolve static URLs too.
    # Unit tests do not build a production manifest. Tests of collected,
    # hashed/gzip delivery override this with the production storage explicitly.
    with override_settings(
        STORAGES={
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            }
        }
    ):
        yield


@pytest.fixture(autouse=True)
def reversals_of_older_migrations_start_without_authenticators(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 0044 refuses its reversal while an authenticator exists . The tests of
    older migrations reverse past it with the made-up
    organization's administrator in place, so their reversal clears the table
    first. A test of 0044 itself, or of what survives a refusal, marks
    `keeps_authenticators` and meets the refusal."""
    if request.node.get_closest_marker("keeps_authenticators"):
        return
    migrate = MigrationExecutor.migrate

    def clearing(
        self: MigrationExecutor, targets: Any, plan: Any = None, **kwargs: Any
    ) -> Any:
        plan = self.migration_plan(targets) if plan is None else plan
        if any(back and step.name.startswith("0044_") for step, back in plan):
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM core_authenticator")
        return migrate(self, targets, plan=plan, **kwargs)

    monkeypatch.setattr(MigrationExecutor, "migrate", clearing)


CONDITIONS_COLUMNS = (
    "ALTER TABLE core_organization ADD COLUMN IF NOT EXISTS "
    "free_cancellation_days smallint NOT NULL DEFAULT 5",
    "ALTER TABLE core_order ADD COLUMN IF NOT EXISTS conditions_id bigint NULL",
    "ALTER TABLE core_order ADD COLUMN IF NOT EXISTS "
    "conditions_accepted_at timestamptz NULL",
)
CONDITIONS_DROP = (
    "ALTER TABLE core_organization DROP COLUMN IF EXISTS free_cancellation_days",
    "ALTER TABLE core_order DROP COLUMN IF EXISTS conditions_id",
    "ALTER TABLE core_order DROP COLUMN IF EXISTS conditions_accepted_at",
)


@pytest.fixture(autouse=True)
def reversals_of_older_migrations_keep_the_current_columns(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 0053  adds columns to the organization and the order,
    which the code selects always. A test of an older migration reverses past
    0053 and then runs the current code (an audit record, an order read) on the
    older schema, also after a refusal of its own migration that left 0053
    already reversed. Such a reversal keeps the three columns in place, and the
    way forward drops them first. A test of 0053 itself marks
    `reverses_conditions` and meets its true reversal."""
    if request.node.get_closest_marker("reverses_conditions"):
        return
    migrate = MigrationExecutor.migrate

    def keeping(
        self: MigrationExecutor, targets: Any, plan: Any = None, **kwargs: Any
    ) -> Any:
        plan = self.migration_plan(targets) if plan is None else plan
        steps = [(step.name, back) for step, back in plan]
        forward = any(name.startswith("0053_") and not back for name, back in steps)
        back = any(name.startswith("0053_") and back for name, back in steps)
        if back:
            # Orders of the older tests accepted the conditions; the refusal of
            # 0053's own reversal is for its own test only.
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE core_order SET conditions_id = NULL, "
                    "conditions_accepted_at = NULL"
                )
                cursor.execute("DELETE FROM core_bookingconditions")
        if forward:
            with connection.cursor() as cursor:
                for statement in CONDITIONS_DROP:
                    cursor.execute(statement)
        try:
            return migrate(self, targets, plan=plan, **kwargs)
        finally:
            if back:
                with connection.cursor() as cursor:
                    for statement in CONDITIONS_COLUMNS:
                        cursor.execute(statement)

    monkeypatch.setattr(MigrationExecutor, "migrate", keeping)


LANGUAGE_COLUMN = (
    "ALTER TABLE core_organization ADD COLUMN IF NOT EXISTS "
    "language varchar(16) NOT NULL DEFAULT 'en'"
)
LANGUAGE_DROP = "ALTER TABLE core_organization DROP COLUMN IF EXISTS language"


@pytest.fixture(autouse=True)
def reversals_of_older_migrations_keep_the_language_column(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 0054 (S1.3a) adds the organization's language, which the code
    selects always. A test of an older migration reverses past 0054 and then runs
    the current code on the older schema, also after a refusal of its own
    migration that left 0054 already reversed. Such a reversal keeps the column
    in place, and the way forward drops it first. A test of 0054 itself marks
    `reverses_language` and meets its true reversal."""
    if request.node.get_closest_marker("reverses_language"):
        return
    migrate = MigrationExecutor.migrate

    def keeping(
        self: MigrationExecutor, targets: Any, plan: Any = None, **kwargs: Any
    ) -> Any:
        plan = self.migration_plan(targets) if plan is None else plan
        steps = [(step.name, back) for step, back in plan]
        forward = any(name.startswith("0054_") and not back for name, back in steps)
        back = any(name.startswith("0054_") and back for name, back in steps)
        if forward:
            with connection.cursor() as cursor:
                cursor.execute(LANGUAGE_DROP)
        try:
            return migrate(self, targets, plan=plan, **kwargs)
        finally:
            if back:
                with connection.cursor() as cursor:
                    cursor.execute(LANGUAGE_COLUMN)

    monkeypatch.setattr(MigrationExecutor, "migrate", keeping)


@pytest.fixture
def second_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second catalogue, `xx`, from the fixture file beside the tests: a few
    keys, one with other placeholders than its English text, most left out."""
    path = Path(__file__).parent / "locale_fixture" / "xx.json"
    monkeypatch.setitem(
        labels.CATALOGUES, "xx", json.loads(path.read_text(encoding="utf-8"))
    )
