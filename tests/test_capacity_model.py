# SPDX-License-Identifier: AGPL-3.0-only
"""The rule against double booking by spot , through the real
models and an application login."""

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    connection,
    transaction,
)
from django.db.models import ProtectedError

from randirandi import setup_file
from randirandi.core.models import (
    Asset,
    BillingBasis,
    Block,
    BookingConfiguration,
    BookingLine,
    BookingState,
    CapacityAllocation,
    ConsumptionMode,
    Space,
    Spot,
    Status,
    TakenTime,
)
from tests import made_up
from tests.capacity_app import application_login

pytestmark = pytest.mark.django_db(transaction=True)


def _claim(
    configuration: BookingConfiguration, start: Any, end: Any, *numbers: int
) -> TakenTime:
    """Taken time on the container's asset holding these spot numbers; the
    whole asset without numbers."""
    assert configuration.floor_plan is not None or not numbers
    return TakenTime.objects.take(
        organization_id=configuration.organization_id,
        asset=configuration.asset,
        starts_at=start,
        ends_at=end,
        state=BookingState.HELD,
        mode=ConsumptionMode.SHARED if numbers else ConsumptionMode.EXCLUSIVE,
        quantity=len(numbers) or 1,
        units=made_up.spots_of(configuration.floor_plan, *numbers)
        if numbers and configuration.floor_plan
        else None,
    )


def test_creation_and_permitted_saves_work_as_the_application() -> None:
    with application_login():
        made = made_up.everything()
        assert connection.connection is not None
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("randirandi_app",)
        made.asset.name = "Renamed room"
        made.asset.save()
        floor_plan = made_up.floor_plan_of(made.asset)
        floor_plan.unit = "visit"
        floor_plan.save()
        made.booking.taken_time.state = BookingState.CANCELLED
        made.booking.taken_time.save()
        # An exclusive claim holds the whole asset and names no spot.
        claim = made.book(10, 12).taken_time.allocations.get()
        assert (claim.spot_id, claim.inventory_id) == (None, None)
        block = made.block(16, 18)
        block.delete()
        made.book(16, 18)


def test_spots_of_shared_claims_and_the_whole_asset_compete() -> None:
    with application_login():
        made = made_up.everything()
        beds = made.shared("Guest room", 4, unit="bed")
        start, end = (
            made_up.at(made.day, 17),
            made_up.at(made.day + timedelta(days=1), 8),
        )
        two = _claim(beds, start, end, 1, 2)
        _claim(beds, start, end, 3)
        with pytest.raises(IntegrityError):
            _claim(beds, start, end, 2)  # One spot sold twice.
        with pytest.raises(IntegrityError):
            _claim(beds, start, end)  # The whole asset beside a spot.
        two.state = BookingState.CANCELLED
        two.save()
        assert list(two.allocations.values_list("state", flat=True)) == [
            "cancelled",
            "cancelled",
        ]
        _claim(beds, start, end, 2)


def test_a_marked_asset_and_the_other_assets_of_its_space_compete() -> None:
    with application_login():
        made = made_up.everything()
        wing = Space.objects.create(organization=made.organization, name="Wing")
        whole = made.shared("Whole wing", 2, space=wing, whole_space=True)
        study = made.shared("Study", 1, space=wing, status=Status.DISABLED)
        start, end = made_up.at(made.day, 10), made_up.at(made.day, 12)
        _claim(whole, start, end, 1)
        _claim(whole, start, end, 2)  # Its own other spot stays free.
        with pytest.raises(IntegrityError):
            _claim(study, start, end, 1)
        later, last = made_up.at(made.day, 14), made_up.at(made.day, 16)
        _claim(study, later, last)
        with pytest.raises(IntegrityError):
            _claim(whole, later, last, 1)


def test_a_revision_replaces_the_spots_in_one_transaction() -> None:
    with application_login():
        made = made_up.everything()
        pool = made.shared("Pool", 2)
        start, middle, end = (made_up.at(made.day, h) for h in (10, 11, 12))
        _claim(pool, start, middle, 1)
        row = _claim(pool, middle, end, 1, 2)
        row.revise(
            starts_at=end,
            ends_at=end + timedelta(hours=2),
            mode=ConsumptionMode.EXCLUSIVE,
            quantity=1,
        )
        assert row.allocations.get().spot_id is None
        with pytest.raises(IntegrityError):
            row.revise(
                starts_at=start,
                ends_at=end,
                mode=ConsumptionMode.SHARED,
                quantity=1,
                units=made_up.spots_of(made_up.floor_plan_of(pool.asset), 1),
            )
        row.refresh_from_db()
        assert row.starts_at == end
        assert row.allocations.get().spot_id is None


# Direct writes as the application, past the models.
CLAIM = """
    WITH claim AS (
        INSERT INTO core_takentime (organization_id, asset_id, scope_key,
            whole_space, mode, quantity, state, starts_at, ends_at)
        SELECT organization_id, id, {scope_key}, {whole_space}, %(mode)s, 1,
            'held', %(start)s, %(end)s
        FROM core_asset WHERE id = %(asset)s RETURNING *
    )
    INSERT INTO core_capacityallocation (organization_id, taken_time_id,
        ordinal, asset_id, inventory_id, spot_id, scope_key, whole_space, mode,
        quantity, state, starts_at, ends_at)
    SELECT organization_id, id, 1, asset_id, %(floor_plan)s, %(spot)s,
        scope_key, whole_space, mode, quantity, state, starts_at, ends_at
    FROM claim
"""


@pytest.mark.parametrize(
    "case",
    [
        "one-spot-twice",
        "exclusive-beside-a-spot",
        "marked-beside-its-space",
        "marked-exclusive-beside-its-space",
        "beside-a-marked-claim",
        "exclusive-beside-a-marked-claim",
        "stale-space",
        "stale-mark",
        "move-into-a-clash",
        "allocation-copy",
        "copy-update",
        "missing-allocation",
    ],
)
def test_direct_writes_cannot_get_past_the_rule(case: str) -> None:
    with application_login():
        made = made_up.everything()
        wing = Space.objects.create(organization=made.organization, name="Wing")
        whole = made.shared("Whole wing", 2, space=wing, whole_space=True)
        study = made.shared("Study", 2, space=wing, status=Status.DISABLED)
        lounge = made.shared("Lounge", 2)
        start, end = made_up.at(made.day, 16), made_up.at(made.day, 18)
        held = _claim(study, start, end, 1)
        _claim(lounge, start, end, 1)
        study_spot = made_up.spots_of(made_up.floor_plan_of(study.asset), 1)[0]
        whole_spot = made_up.spots_of(made_up.floor_plan_of(whole.asset), 1)[0]
        values: dict[str, Any] = {
            "mode": "shared",
            "start": start,
            "end": end,
            "asset": study.asset_id,
            "floor_plan": study_spot.floor_plan_id,
            "spot": study_spot.pk,
        }
        scope_key, whole_space = "scope_key", "whole_space"
        if case == "exclusive-beside-a-spot":
            values |= {"mode": "exclusive", "floor_plan": None, "spot": None}
        elif case == "marked-beside-its-space":
            values |= {
                "asset": whole.asset_id,
                "floor_plan": whole_spot.floor_plan_id,
                "spot": whole_spot.pk,
            }
        elif case == "marked-exclusive-beside-its-space":
            values |= {
                "asset": whole.asset_id,
                "mode": "exclusive",
                "floor_plan": None,
                "spot": None,
            }
        elif case in ("beside-a-marked-claim", "exclusive-beside-a-marked-claim"):
            later, last = made_up.at(made.day, 20), made_up.at(made.day, 21)
            _claim(whole, later, last, 1)
            values |= {"start": later, "end": last}
            if case.startswith("exclusive"):
                values |= {"mode": "exclusive", "floor_plan": None, "spot": None}
        elif case == "stale-space":
            # The lounge's own space key: the study's claim elsewhere.
            values |= {
                "start": made_up.at(made.day, 20),
                "end": made_up.at(made.day, 21),
            }
            scope_key = str(lounge.asset.scope_key)
        elif case == "stale-mark":
            values |= {
                "start": made_up.at(made.day, 20),
                "end": made_up.at(made.day, 21),
            }
            whole_space = "true"
        statements = {
            # Into the wing, covering it once the whole wing gives up the
            # mark: beside the study's claim.
            "move-into-a-clash": (
                "UPDATE core_asset SET space_id = %(space)s, whole_space = true "
                "WHERE id = %(asset)s",
                {"space": wing.pk, "asset": lounge.asset_id},
            ),
            "allocation-copy": (
                "UPDATE core_capacityallocation SET state = 'cancelled' "
                "WHERE taken_time_id = %(id)s",
                {"id": held.pk},
            ),
            "copy-update": (
                "UPDATE core_takentime SET scope_key = -1 WHERE id = %(id)s",
                {"id": held.pk},
            ),
            "missing-allocation": (
                "DELETE FROM core_capacityallocation WHERE taken_time_id = %(id)s",
                {"id": held.pk},
            ),
        }
        if case == "move-into-a-clash":
            whole.asset.whole_space = False
            whole.asset.save()
        statement = statements.get(
            case,
            (CLAIM.format(scope_key=scope_key, whole_space=whole_space), values),
        )
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(*statement)
        held.refresh_from_db()
        assert held.allocations.get().state == BookingState.HELD
        with pytest.raises(IntegrityError):
            _claim(study, start, end, 1)


def test_a_second_marked_asset_in_a_space_is_refused() -> None:
    """One asset per space carries the mark; different whole-space offers are
    its containers. Refused by the loader, the model and the database."""
    laundry = Path("setups/scenarios/laundry.toml").read_text()
    with pytest.raises(
        setup_file.SetupError, match="this space already has an asset that covers it"
    ):
        setup_file.parse(
            laundry.replace(
                'space = "Laundry room"\n',
                'space = "Laundry room"\nwhole_space = true\n',
                1,
            )
        )
    with application_login():
        made = made_up.everything()
        wing = Space.objects.create(organization=made.organization, name="Wing")
        made.shared("Whole wing", 2, space=wing, whole_space=True)
        study = made.shared("Study", 2, space=wing, status=Status.DISABLED).asset
        study.whole_space = True
        with pytest.raises(ValidationError, match="already covers this space"):
            study.save()
        with pytest.raises(ValidationError, match="already covers this space"):
            Asset.objects.create(
                organization=made.organization,
                name="Second whole wing",
                space=wing,
                whole_space=True,
            )
        with (
            pytest.raises(IntegrityError) as refused,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE core_asset SET whole_space = true WHERE id = %s", [study.pk]
            )
        cause = refused.value.__cause__
        assert cause is not None
        assert cause.diag.constraint_name == "asset_whole_space_once"  # type: ignore[attr-defined]


def test_moving_an_asset_carries_its_claims_or_is_refused() -> None:
    with application_login():
        made = made_up.everything()
        wing = Space.objects.create(organization=made.organization, name="Wing")
        # Disabled, so the validator lets the lounge in; the rule does not.
        whole = made.shared(
            "Whole wing", 1, space=wing, whole_space=True, status=Status.DISABLED
        )
        lounge = made.shared("Lounge", 1)
        start, end = made_up.at(made.day, 10), made_up.at(made.day, 12)
        _claim(whole, start, end, 1)
        claim = _claim(lounge, start, end, 1)
        later = made.day + timedelta(days=7)
        lounge.asset.space = wing
        with pytest.raises(ValidationError) as refused:
            # Past claims only, so the validator lets it move.
            lounge.asset.save(now=made_up.at(later, 0))
        assert refused.value.message_dict == {
            "space": [
                "Its bookings or blocks, also past ones, would then overlap "
                "others in its space; make a new asset in that space instead."
            ]
        }
        claim.refresh_from_db()
        assert claim.scope_key == -lounge.asset_id
        other = Space.objects.create(organization=made.organization, name="Annex")
        lounge.asset.refresh_from_db()
        lounge.asset.space = other
        lounge.asset.save(now=made_up.at(later, 0))
        claim.refresh_from_db()
        assert claim.scope_key == other.pk
        assert claim.allocations.get().scope_key == other.pk
        # The mark is refused the same way over past claims beside it.
        shed = made.shared("Shed", 1, space=other, status=Status.DISABLED)
        _claim(shed, start, end, 1)
        lounge.asset.refresh_from_db()
        lounge.asset.whole_space = True
        with pytest.raises(ValidationError) as refused:
            lounge.asset.save(now=made_up.at(later, 0))
        assert refused.value.message_dict == {
            "whole_space": [
                "Its bookings or blocks, also past ones, would then overlap "
                "others in its space; make a new asset with the mark instead."
            ]
        }


def test_a_booking_keeps_its_spots() -> None:
    with application_login():
        made = made_up.everything()
        beds = made.shared("Dormitory", 4, unit="bed")
        floor_plan = made_up.floor_plan_of(beds.asset)
        booking = made.book(
            10, 12, configuration=beds, units=made_up.spots_of(floor_plan, 2, 3)
        )
        held = list(
            booking.taken_time.allocations.order_by("ordinal").values_list(
                "spot_id", flat=True
            )
        )
        made_up.spots(floor_plan, 6)
        made_up.fewer_spots(floor_plan, 3)  # Spot 4 goes, held by no one.
        assert (
            list(
                booking.taken_time.allocations.order_by("ordinal").values_list(
                    "spot_id", flat=True
                )
            )
            == held
        )
        with pytest.raises(ProtectedError):
            Spot.objects.of(made.organization).filter(pk=held[0]).delete()
        with (
            pytest.raises(IntegrityError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("DELETE FROM core_spot WHERE id = %s", [held[1]])
        booking.taken_time.state = BookingState.CANCELLED
        booking.taken_time.save()
        with pytest.raises(ProtectedError):
            Spot.objects.of(made.organization).filter(pk=held[0]).delete()


def test_capacity_and_price_snapshots_remain_separate() -> None:
    with application_login():
        made = made_up.everything()
        original = made.booking.lines.get()
        line = BookingLine.objects.create(
            organization=made.organization,
            booking=made.booking,
            booking_configuration=made.configuration,
            price_book=made.price_book,
            up_to=60,
            quantity=2,
            amount=5000,
            configuration_number=made.configuration.number,
            price_book_number=made.price_book.number,
            billing_basis=BillingBasis.PER_CAPACITY_UNIT,
            capacity_quantity=3,
            capacity_unit="place",
        )
        assert line.total == 30000
        assert original.total == 25000
        made_up.spots(made_up.floor_plan_of(made.asset), 8)
        line.refresh_from_db()
        assert (line.capacity_quantity, line.capacity_unit, line.total) == (
            3,
            "place",
            30000,
        )
        with pytest.raises(IntegrityError):
            made.book(10, 12)  # The exclusive claim covers the new spots.


def test_bookings_and_blocks_take_spots_on_a_shared_container() -> None:
    with application_login():
        made = made_up.everything()
        pool = made.shared("Pool", 3)
        floor_plan = made_up.floor_plan_of(pool.asset)
        booking = made.book(
            16, 18, configuration=pool, units=made_up.spots_of(floor_plan, 1, 2)
        )
        block = made.block(
            16, 18, configuration=pool, units=made_up.spots_of(floor_plan, 3)
        )
        assert booking.taken_time.quantity == 2
        assert block.taken_time.state == "blocked"
        assert (
            CapacityAllocation.objects.of(made.organization)
            .filter(taken_time=block.taken_time)
            .get()
            .spot_id
            == made_up.spots_of(floor_plan, 3)[0].pk
        )
        with pytest.raises(ValueError, match="never a spot"):
            made.block(
                18, 19, units=made_up.spots_of(made_up.floor_plan_of(made.asset), 1)
            )
        with pytest.raises(ValueError, match="spots of its floor plan"):
            made.book(18, 19, configuration=pool, units=[])
        with pytest.raises(ValueError, match="container's floor plan"):
            made.book(
                18,
                19,
                configuration=pool,
                units=made_up.spots_of(made_up.floor_plan_of(made.asset), 1),
            )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        # The container's date period is 10:00 to 22:00 local time, an hour
        # after UTC, by the hour.
        (made_up.at(made_up.A_DAY, 8), made_up.at(made_up.A_DAY, 9)),
        (made_up.at(made_up.A_DAY, 20), made_up.at(made_up.A_DAY, 22)),
        (
            made_up.at(made_up.A_DAY, 16) + timedelta(minutes=30),
            made_up.at(made_up.A_DAY, 17),
        ),
        # A Tuesday, with no date period.
        (
            made_up.at(made_up.A_DAY + timedelta(days=1), 12),
            made_up.at(made_up.A_DAY + timedelta(days=1), 13),
        ),
    ],
)
def test_a_block_takes_whole_cells_of_its_container(start: Any, end: Any) -> None:
    with application_login():
        made = made_up.everything()
        with pytest.raises(ValueError, match="whole cells"):
            Block.objects.take(
                organization=made.organization,
                asset=made.asset,
                booking_configuration=made.configuration,
                starts_at=start,
                ends_at=end,
                made_by=made.administrator,
            )
        # Several whole cells: the whole date period of the next Monday.
        week = made.day + timedelta(days=7)
        Block.objects.take(
            organization=made.organization,
            asset=made.asset,
            booking_configuration=made.configuration,
            starts_at=made_up.at(week, 9),
            ends_at=made_up.at(week, 21),
            made_by=made.administrator,
        ).delete()


@pytest.mark.parametrize("second_spot", [1, 2])
def test_concurrent_application_logins_obey_the_spots(second_spot: int) -> None:
    with application_login():
        made = made_up.everything()
        pool = made.shared("Pool", 2)
        start, end = made_up.at(made.day, 10), made_up.at(made.day, 12)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def purchase(number: int) -> None:
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SELECT current_user")
                    assert cursor.fetchone() == ("randirandi_app",)
                    cursor.execute(
                        "SET CONSTRAINTS capacity_no_double_booking DEFERRED"
                    )
                    _claim(pool, start, end, number)
                    barrier.wait(timeout=10)
                outcomes.append("committed")
            except (IntegrityError, OperationalError) as error:
                assert getattr(error.__cause__, "sqlstate", None) in ("23P01", "40P01")
                outcomes.append("refused")
            finally:
                connection.close()

        workers = [
            threading.Thread(target=purchase, args=(number,))
            for number in (1, second_spot)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert not worker.is_alive()
        assert sorted(outcomes) == (
            ["committed", "refused"] if second_spot == 1 else ["committed", "committed"]
        )
        assert TakenTime.objects.of(made.organization).filter(
            asset=pool.asset
        ).count() == (1 if second_spot == 1 else 2)
