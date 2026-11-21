# SPDX-License-Identifier: AGPL-3.0-only
"""Migration 0054 (S1.3a): the organization's language, English for every one
that exists. Reversing is refused while an organization has another language
and is clean otherwise. The graph's leaf nodes are restored after every test,
refused reverse or not."""

from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import Organization
from tests import made_up

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.reverses_language,
]
BEFORE = [("core", "0053_booking_conditions")]


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def reverse() -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BEFORE)


def has_language_column() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'core_organization' AND column_name = 'language'"
        )
        return cursor.fetchone() is not None


def test_reversing_is_refused_while_an_organization_has_another_language() -> None:
    org = made_up.organization("speaker")
    Organization.objects.filter(pk=org.pk).update(language="nb")

    with pytest.raises(RuntimeError, match=r"it would be lost.*speaker"):
        reverse()

    assert has_language_column()
    assert Organization.objects.get(pk=org.pk).language == "nb"


def test_reversing_is_clean_with_english_only_and_forward_makes_english_again() -> None:
    org = made_up.organization("plain")
    assert Organization.objects.get(pk=org.pk).language == "en"

    reverse()
    assert not has_language_column()

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert has_language_column()
    assert Organization.objects.get(pk=org.pk).language == "en"
