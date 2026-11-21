# SPDX-License-Identifier: AGPL-3.0-only
"""The time zone and currency migration : the asset's zone and the
price book's currency, filled from the organization, both ways, with rows in
it. Fictional places."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import Asset, PriceBook
from tests.test_calendar import Example, at
from tests.test_calendar import made as made

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0026_booking_emails")]
AFTER = [("core", "0027_time_zone_and_currency")]


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def migrate_to(target: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)


def rows(query: str, *values: Any) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query, values)
        return list(cursor.fetchall())


def columns(table: str) -> set[str]:
    return {
        name
        for (name,) in rows(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            table,
        )
    }


def populate(made: Example) -> None:
    """A booking, a price and a payment-free order's worth of rows."""
    made.claim(at(hour=10), at(hour=12))


def test_undoing_it_drops_the_columns_and_redoing_it_fills_them_from_the_organization(
    made: Example,
) -> None:
    populate(made)
    migrate_to(BEFORE)
    assert "time_zone" not in columns("core_asset")
    assert "currency" not in columns("core_pricebook")
    assert "currency" not in columns("core_pricebookentry")
    assert "time_zone" not in columns("core_takentime")
    assert rows("SELECT count(*) FROM core_takentime") == [(1,)]
    # While the schema is the old one, the organization changes its own.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_organization SET time_zone = 'America/New_York', "
            "currency = 'EUR' WHERE id = %s",
            [made.org.pk],
        )
    migrate_to(AFTER)
    assert rows("SELECT DISTINCT time_zone FROM core_asset") == [("America/New_York",)]
    assert rows("SELECT DISTINCT currency FROM core_pricebook") == [("EUR",)]
    assert rows("SELECT DISTINCT currency FROM core_pricebookentry") == [("EUR",)]
    assert rows("SELECT DISTINCT time_zone FROM core_takentime") == [
        ("America/New_York",)
    ]


def test_undoing_it_is_refused_naming_the_rows_that_differ(made: Example) -> None:
    populate(made)
    deck = Asset.objects.create(
        organization=made.org, name="Harbor Deck", time_zone="America/New_York"
    )
    euros = PriceBook.objects.create(
        organization=made.org, name="Euro prices", currency="EUR"
    )
    with pytest.raises(RuntimeError) as caught:
        migrate_to(BEFORE)
    message = str(caught.value)
    assert "price books with their own currency: 1" in message
    assert f"{made.org.subdomain}: Euro prices (EUR)" in message
    assert "assets with their own time_zone: 1" in message
    assert f"{made.org.subdomain}: Harbor Deck (America/New_York)" in message
    # Nothing was undone.
    assert "time_zone" in columns("core_asset")
    assert "currency" in columns("core_pricebook")
    # Put back as the organization's, it is undone, and done again.
    Asset.objects.of(made.org).filter(pk=deck.pk).update(time_zone="Europe/Oslo")
    PriceBook.objects.of(made.org).filter(pk=euros.pk).update(currency="NOK")
    migrate_to(BEFORE)
    assert "time_zone" not in columns("core_asset")
    migrate_to(AFTER)
    assert set(rows("SELECT time_zone FROM core_asset")) == {("Europe/Oslo",)}
    assert rows("SELECT count(*) FROM core_takentime") == [(1,)]
