# SPDX-License-Identifier: AGPL-3.0-only
"""an order's reference, six random characters people read
, and migration 0034 that gave every order one."""

import re
from collections.abc import Iterator

import pytest
from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from randirandi.core.models import Order, Organization
from randirandi.core.models import orders as order_models
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)
CODE = re.compile(r"[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}")
BEFORE = [("core", "0033_member_last_login_at")]


def make(made: made_up.MadeUp) -> Order:
    return Order.objects.create(
        organization=made.organization, member=made.member, total=0
    )


def test_an_order_gets_six_characters_from_the_alphabet_when_it_is_made() -> None:
    made = made_up.everything()
    assert order_models.REFERENCE_ALPHABET == "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    assert len(order_models.REFERENCE_ALPHABET) == 31
    assert not set("01OIL") & set(order_models.REFERENCE_ALPHABET)
    orders = [make(made) for _ in range(30)]
    assert all(CODE.fullmatch(order.reference) for order in orders)
    assert len({order.reference for order in orders}) == 30
    stored = Order.objects.of(made.organization).get(pk=orders[0].pk)
    assert stored.reference == orders[0].reference
    assert str(stored) == f"order {stored.reference}"


def test_a_clash_draws_again_and_the_number_is_not_wasted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = made_up.everything()
    first = make(made)
    draws: Iterator[str] = iter([first.reference, first.reference, "AAAAAA"])
    monkeypatch.setattr(order_models, "draw_reference", lambda: next(draws))
    # Two draws clash with the first order; the database refuses each and the
    # third draw is kept.
    second = make(made)
    assert second.reference == "AAAAAA"
    assert second.number == first.number + 1
    organization = Organization.objects.get(pk=made.organization.pk)
    assert organization.last_order_number == second.number


def test_the_reference_is_unique_per_organization_and_only_a_code() -> None:
    made = made_up.everything()
    order = make(made)
    with pytest.raises(IntegrityError), transaction.atomic():
        Order.objects.of(made.organization).filter(pk=make(made).pk).update(
            reference=order.reference
        )
    for bad in ("0OIL1X", "abcdef", "ABC", "ABCDEFG"):
        with pytest.raises((IntegrityError, DataError)), transaction.atomic():
            Order.objects.of(made.organization).filter(pk=order.pk).update(
                reference=bad
            )
    order.refresh_from_db()
    assert CODE.fullmatch(order.reference)


def test_a_reference_is_never_changed() -> None:
    made = made_up.everything()
    order = make(made)
    kept = order.reference
    order.reference = "ZZZZZZ" if kept != "ZZZZZZ" else "YYYYYY"
    with pytest.raises(ValidationError, match="cannot be changed"):
        order.save()
    order.reference = kept
    order.total = 0
    order.save()
    assert Order.objects.of(made.organization).get(pk=order.pk).reference == kept


@pytest.fixture
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def column_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'core_order' AND column_name = 'reference'"
        )
        return cursor.fetchone() is not None


@pytest.mark.usefixtures("restore_current_schema")
def test_the_migration_gives_every_order_one_and_reverses() -> None:
    made = made_up.everything()
    kept = {order.pk: order.number for order in Order.objects.of(made.organization)}
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    assert not column_exists()
    old = executor.loader.project_state(BEFORE).apps.get_model("core", "Order")
    for number in (50, 51, 52):
        old._default_manager.create(
            organization_id=made.organization.pk,
            member_id=made.member.pk,
            number=number,
            total=0,
        )
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert column_exists()
    found = list(Order.objects.of(made.organization))
    assert len(found) == len(kept) + 3
    assert all(CODE.fullmatch(order.reference) for order in found)
    assert len({order.reference for order in found}) == len(found)
    assert {order.pk: order.number for order in found if order.pk in kept} == kept
    # Back again: the references go, the numbers stay.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BEFORE)
    assert not column_exists()
