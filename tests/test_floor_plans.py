# SPDX-License-Identifier: AGPL-3.0-only
"""Floor plans, spots, groups and spaces, stored .

A floor plan is an asset's spots, numbered from 1 and never renumbered. A
group holds spots of its own floor plan. A shared container books one of its
asset's floor plans; an exclusive one names none. An asset may have several
floor plans, and its space is its own . Deleting an asset
deletes its floor plans, with their spots and groups.
"""

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import QuerySet, RestrictedError

from randirandi import setup_file
from randirandi.core import calendar, setup
from randirandi.core.models import (
    Asset,
    AssetInventory,
    Block,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingPage,
    BookingState,
    CapacityAllocation,
    ConsumptionMode,
    Space,
    Spot,
    SpotGroup,
    Status,
)
from randirandi.core.ordering import BookingRequest, create_order
from randirandi.limits import MOST_SPOTS, TOO_MANY_SPOTS
from randirandi.setup_file import SetupError
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, at, scenario
from tests.test_setup_file import SETUPS
from tests.test_validator import (
    AFTER,
    BEFORE,
    asset,
    container,
    other_plan,
    refused,
    shared,
)


def constraint_of(error: pytest.ExceptionInfo[IntegrityError]) -> str:
    cause = error.value.__cause__
    assert cause is not None
    name: str = cause.diag.constraint_name  # type: ignore[attr-defined]
    return name


def database_refuses(rows: QuerySet[Any], **change: Any) -> str:
    """The rule the database names when it refuses the change."""
    with pytest.raises(IntegrityError) as refusal, transaction.atomic():
        rows.update(**change)
    return constraint_of(refusal)


def refused_at_commit(write: Callable[[], object]) -> str:
    """The rule the database names when it refuses the write at commit; the
    write itself goes through."""
    written = False
    with pytest.raises(IntegrityError) as refusal, transaction.atomic():
        write()
        written = True
    assert written
    return constraint_of(refusal)


def floor_plan(
    org: Any, capacity: int, unit: str, name: str = "Hall"
) -> AssetInventory:
    """A new asset's floor plan of `capacity` spots."""
    made = AssetInventory(capacity=capacity, unit=unit)
    Asset(organization=org, name=name).save(floor_plan=made)
    return made


def numbers(floor_plan: AssetInventory) -> list[tuple[int, str]]:
    return list(
        Spot.objects.of(floor_plan.organization_id)
        .filter(floor_plan=floor_plan)
        .order_by("number")
        .values_list("number", "name")
    )


@pytest.mark.django_db
def test_an_asset_made_without_a_floor_plan_gets_one_with_one_spot() -> None:
    org = made_up.organization()
    room = Asset.objects.create(organization=org, name="Meeting room")

    floor_plan = made_up.floor_plan_of(room)
    assert (floor_plan.asset, floor_plan.capacity, floor_plan.unit) == (
        room,
        1,
        "booking",
    )
    assert numbers(floor_plan) == [(1, "")]
    assert (floor_plan.name, room.space, room.maximum_occupancy) == ("", None, None)


@pytest.mark.django_db
def test_a_floor_plan_is_made_with_its_spots_and_its_capacity_counts_them() -> None:
    org = made_up.organization()
    seats = floor_plan(org, 3, "seat")
    assert numbers(seats) == [(1, ""), (2, ""), (3, "")]

    seats.capacity = 5
    with pytest.raises(ValidationError) as error:
        seats.save()
    assert error.value.message_dict == {
        "capacity": ["It is the number of its spots, 3."]
    }

    added = Spot.objects.create(organization=org, floor_plan=seats, name="Aisle")
    seats.refresh_from_db()
    assert (added.number, seats.capacity, seats.last_spot_number) == (4, 4, 4)


@pytest.mark.django_db
def test_a_floor_plan_holds_at_most_ten_thousand_spots() -> None:
    org = made_up.organization()
    hall = floor_plan(org, MOST_SPOTS, "seat")
    # One bigger is refused, checked or made, in the setup file's words.
    bigger = AssetInventory(
        organization=org,
        asset=hall.asset,
        name="Standing",
        capacity=MOST_SPOTS + 1,
        unit="place",
    )
    with pytest.raises(ValidationError) as checked:
        bigger.full_clean()
    assert checked.value.message_dict["capacity"] == [TOO_MANY_SPOTS]
    with pytest.raises(ValidationError) as made:
        bigger.save()
    assert made.value.message_dict == {"capacity": [TOO_MANY_SPOTS]}
    # Grown a spot at a time through the application, it stops there.
    with pytest.raises(ValidationError) as grown:
        Spot.objects.create(organization=org, floor_plan=hall)
    assert grown.value.message_dict == {"floor_plan": [TOO_MANY_SPOTS]}
    hall.refresh_from_db()
    assert (hall.capacity, hall.last_spot_number) == (MOST_SPOTS, MOST_SPOTS)
    assert Spot.objects.of(org).filter(floor_plan=hall).count() == MOST_SPOTS
    assert AssetInventory.objects.of(org).get() == hall


@pytest.mark.django_db(transaction=True)
def test_the_database_holds_the_capacity_to_the_spots_at_every_commit() -> None:
    org = made_up.organization()
    seats = floor_plan(org, 3, "seat")
    plans = AssetInventory.objects.of(org).filter(pk=seats.pk)
    spots = Spot.objects.of(org).filter(floor_plan=seats)

    assert refused_at_commit(lambda: plans.update(capacity=7)) == (
        "floor_plan_counts_its_spots"
    )
    with connection.cursor() as cursor:
        for number, rule in (
            (3, "floor_plan_counts_its_spots"),
            (2, "spot_previous_place"),
        ):
            delete = partial(
                cursor.execute,
                "DELETE FROM core_spot WHERE floor_plan_id = %s AND number = %s",
                [seats.pk, number],
            )
            assert refused_at_commit(delete) == rule
    # Lower without its spots, and it is refused at once.
    assert database_refuses(plans, capacity=2) == "spot_place_within_capacity"
    assert spots.count() == seats.capacity == 3

    # A spot taken off with the capacity in one transaction goes through.
    with transaction.atomic():
        spots.filter(number=3).delete()
        plans.update(capacity=2)
    seats.refresh_from_db()
    assert (seats.capacity, numbers(seats)) == (2, [(1, ""), (2, "")])


@pytest.mark.django_db(transaction=True)
def test_a_floor_plan_goes_with_its_spots_and_groups() -> None:
    org = made_up.organization()
    hall = floor_plan(org, 4, "seat")
    table = SpotGroup.objects.create(organization=org, floor_plan=hall, name="Table A")
    Spot.objects.of(org).filter(floor_plan=hall, number__lte=2).update(group=table)

    with pytest.raises(RestrictedError):
        table.delete()
    hall.delete()

    assert not Spot.objects.of(org).exists()
    assert not SpotGroup.objects.of(org).exists()


@pytest.mark.django_db
def test_a_removed_spot_leaves_a_gap_and_its_number_is_not_given_again() -> None:
    org = made_up.organization()
    beds = floor_plan(org, 3, "bed")
    made_up.fewer_spots(beds, 2)

    Spot.objects.create(organization=org, floor_plan=beds)

    beds.refresh_from_db()
    assert numbers(beds) == [(1, ""), (2, ""), (4, "")]
    assert beds.capacity == 3
    spot = Spot.objects.of(org).get(floor_plan=beds, number=4)
    spot.name = "By the door"
    spot.save()
    # The number and the floor plan are the spot's own.
    assert (
        database_refuses(Spot.objects.of(org).filter(pk=spot.pk), number=0)
        == "spot_numbered_from_one"
    )


@pytest.mark.django_db
def test_a_spot_name_is_once_per_floor_plan_when_it_is_given() -> None:
    org = made_up.organization()
    hall, studio = (floor_plan(org, 2, "seat", name) for name in ("Hall", "Studio"))
    Spot.objects.of(org).filter(floor_plan=studio, number=1).update(name="Window")
    # Unnamed spots, and the same name on another floor plan.
    Spot.objects.of(org).filter(floor_plan=hall, number=1).update(name="Window")

    assert (
        database_refuses(
            Spot.objects.of(org).filter(floor_plan=hall, number=2), name="Window"
        )
        == "spot_name_once"
    )


@pytest.mark.django_db
def test_a_group_holds_spots_of_its_own_floor_plan_only() -> None:
    org = made_up.organization()
    hall, studio = (floor_plan(org, 8, "seat", name) for name in ("Hall", "Studio"))
    first = SpotGroup.objects.create(organization=org, floor_plan=hall, name="Table A")
    second = SpotGroup.objects.create(
        organization=org, floor_plan=hall, name="Table B", word="table"
    )
    assert [(group.number, group.word) for group in (first, second)] == [
        (1, "group"),
        (2, "table"),
    ]
    seat = Spot.objects.of(org).get(floor_plan=hall, number=1)
    seat.group = second
    seat.save()
    assert list(second.spots.all()) == [seat]

    assert (
        database_refuses(
            Spot.objects.of(org).filter(floor_plan=studio, number=1), group=first
        )
        == "spot_group_on_its_floor_plan"
    )
    assert (
        database_refuses(SpotGroup.objects.of(org).filter(pk=second.pk), name="Table A")
        == "spotgroup_name_once"
    )
    with pytest.raises(IntegrityError) as refusal, transaction.atomic():
        SpotGroup.objects.create(organization=org, floor_plan=studio, name="")
    assert constraint_of(refusal) == "spotgroup_numbered_and_named"


@pytest.mark.django_db
def test_an_asset_may_have_several_floor_plans_and_each_is_an_assets() -> None:
    org = made_up.organization()
    room = Asset.objects.create(organization=org, name="Meeting room")
    tables = AssetInventory.objects.create(
        organization=org, asset=room, capacity=2, unit="table"
    )
    assert list(room.floor_plans.order_by("pk")) == [
        made_up.floor_plan_of(room),
        tables,
    ]
    assert numbers(tables) == [(1, ""), (2, "")]
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetInventory.objects.create(organization=org, unit="seat")


@pytest.mark.django_db(transaction=True)
def test_an_asset_deleted_deletes_its_floor_plans_spots_and_groups() -> None:
    org = made_up.organization()
    room = Asset.objects.create(organization=org, name="Meeting room")
    tables = AssetInventory.objects.create(
        organization=org, asset=room, capacity=4, unit="seat"
    )
    group = SpotGroup.objects.create(organization=org, floor_plan=tables, name="Front")
    Spot.objects.of(org).filter(floor_plan=tables, number__lte=2).update(group=group)
    kept = Asset.objects.create(organization=org, name="Studio")

    room.delete()

    assert list(AssetInventory.objects.of(org)) == [made_up.floor_plan_of(kept)]
    assert list(Spot.objects.of(org).values_list("floor_plan", flat=True)) == [
        made_up.floor_plan_of(kept).pk
    ]
    assert not SpotGroup.objects.of(org).exists()
    # A delete past the application leaves no floor plan without its asset.
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM core_asset WHERE id = %s", [kept.pk])
    assert AssetInventory.objects.of(org).filter(asset=kept).exists()


@pytest.mark.django_db
def test_an_asset_has_its_own_space_and_may_cover_it() -> None:
    org = made_up.organization()
    loft = Space.objects.create(organization=org, name="Loft")
    whole = Asset.objects.create(
        organization=org, name="Whole loft", space=loft, whole_space=True
    )
    assert (whole.space, whole.whole_space, whole.scope_key) == (loft, True, loft.pk)
    room = Asset.objects.create(organization=org, name="Meeting room")
    # Without a space its key is its own, which no space has.
    assert room.scope_key == -room.pk
    assert not hasattr(made_up.floor_plan_of(whole), "space_id")


@pytest.mark.django_db
def test_the_mark_needs_a_space() -> None:
    org = made_up.organization()
    with pytest.raises(ValidationError) as error:
        Asset.objects.create(organization=org, name="Whole hall", whole_space=True)
    assert error.value.message_dict == {
        "whole_space": [
            "It needs a space: an asset without one cannot cover its whole space."
        ]
    }
    room = Asset.objects.create(organization=org, name="Meeting room")
    room.whole_space = True
    with pytest.raises(ValidationError):
        room.save()
    # The database: an asset that covers its whole space keeps a space.
    loft = Space.objects.create(organization=org, name="Loft")
    whole = Asset.objects.create(
        organization=org, name="Whole loft", space=loft, whole_space=True
    )
    assert (
        database_refuses(Asset.objects.of(org).filter(pk=whole.pk), space=None)
        == "asset_mark_needs_a_space"
    )


@pytest.mark.django_db
def test_the_maximum_occupancy_is_information_only() -> None:
    org = made_up.organization()
    hall = Asset.objects.create(organization=org, name="Hall")
    hall.maximum_occupancy = 40
    hall.save()
    hall.refresh_from_db()
    # It books nothing: the floor plan's spots are the asset's capacity.
    assert (hall.maximum_occupancy, made_up.floor_plan_of(hall).capacity) == (40, 1)
    assert (
        database_refuses(Asset.objects.of(org).filter(pk=hall.pk), maximum_occupancy=0)
        == "asset_occupancy_above_zero"
    )


@pytest.mark.django_db
def test_a_space_shows_one_asset_at_a_time_unless_it_shows_the_whole_space() -> None:
    org = made_up.organization()
    laundry = Space.objects.create(organization=org, name="Laundry room")
    assert laundry.booking_page == BookingPage.ONE_ASSET

    laundry.booking_page = BookingPage.WHOLE_SPACE
    laundry.save()

    laundry.refresh_from_db()
    assert laundry.booking_page == BookingPage.WHOLE_SPACE
    assert (
        database_refuses(
            Space.objects.of(org).filter(pk=laundry.pk), booking_page="every_asset"
        )
        == "space_booking_page_known"
    )


@pytest.mark.django_db
def test_a_container_names_a_floor_plan_exactly_when_it_is_shared() -> None:
    org = made_up.organization()
    sauna = asset(org, "Sauna")
    other = asset(org, "Plunge pool")
    floor_plan = made_up.floor_plan_of(sauna)
    exclusive, shared = (
        "It is left out: an exclusive container uses the whole asset.",
        "It is one of the asset's floor plans: a shared container books its spots.",
    )

    assert refused(container(sauna, floor_plan=floor_plan)) == {
        "floor_plan": [exclusive]
    }
    assert refused(container(sauna, consumption_mode=ConsumptionMode.SHARED)) == {
        "floor_plan": [shared]
    }
    assert refused(
        container(
            sauna,
            consumption_mode=ConsumptionMode.SHARED,
            floor_plan=made_up.floor_plan_of(other),
        )
    ) == {"floor_plan": [shared]}
    visit = container(
        sauna, consumption_mode=ConsumptionMode.SHARED, floor_plan=floor_plan
    )
    visit.save(now=BEFORE)

    rows = BookingConfiguration.objects.of(org).filter(pk=visit.pk)
    assert (
        database_refuses(rows, floor_plan=None)
        == "configuration_floor_plan_when_shared"
    )
    assert (
        database_refuses(rows, consumption_mode=ConsumptionMode.EXCLUSIVE)
        == "configuration_floor_plan_when_shared"
    )
    assert (
        database_refuses(rows, floor_plan=made_up.floor_plan_of(other))
        == "configuration_floor_plan_of_its_asset"
    )


@pytest.mark.django_db
def test_every_new_booking_and_block_names_its_container() -> None:
    made = made_up.everything()
    assert made.booking.booking_configuration == made.configuration
    block = made.block(16, 17)
    assert block.booking_configuration == made.configuration

    elsewhere = asset(made.organization, "Annex")
    with pytest.raises(ValueError, match=r"The container is on another asset\."):
        Booking.objects.take(
            organization=made.organization,
            asset=elsewhere,
            booking_configuration=made.configuration,
            starts_at=made_up.at(made.day, 18),
            ends_at=made_up.at(made.day, 19),
            state=BookingState.CONFIRMED,
            order=made.spare_order,
            member_id=made.spare_order.member_id,
            booking_type=made.booking_type,
            booking_category=made.private,
        )
    with pytest.raises(ValueError, match=r"The container is on another asset\."):
        Block.objects.take(
            organization=made.organization,
            asset=elsewhere,
            booking_configuration=made.configuration,
            starts_at=made_up.at(made.day, 18),
            ends_at=made_up.at(made.day, 19),
            made_by=made.administrator,
        )


@pytest.mark.django_db(transaction=True)
def test_an_order_writes_the_container_it_was_booked_under() -> None:
    org, member = scenario("scenarios/laundry.toml")
    washer = BookingConfiguration.objects.of(org).get(number=1)
    private = BookingCategory.objects.of(org).get(name="private")

    order = create_order(
        member=member,
        requests=[BookingRequest(washer.pk, private.pk, at(DAY, 14), at(DAY, 16))],
        now=at(DAY - timedelta(days=1)),
        accepted_version=made_up.accepted(member),
    )

    assert [booking.booking_configuration for booking in order.bookings.all()] == [
        washer
    ]


def test_the_calendar_asks_the_same_function_of_the_claims_it_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A claim held in memory, as the cart's are, on the asset that covers
    # the washer's space: it holds the whole washer while `kept_whole` says so.
    washer = Asset(pk=1, organization_id=1, space_id=1)
    start = at(hour=10)
    claim = CapacityAllocation(
        asset_id=2,
        whole_space=True,
        mode=ConsumptionMode.SHARED,
        spot_id=21,
        starts_at=start,
        ends_at=start + timedelta(hours=2),
    )
    assert calendar._held(washer, start, claim.ends_at, [claim]) is None
    monkeypatch.setattr(calendar, "kept_whole", lambda marked, other: False)
    assert calendar._held(washer, start, claim.ends_at, [claim]) == set()


@pytest.mark.django_db
def test_a_disabled_container_with_a_block_to_come_keeps_its_time() -> None:
    made = made_up.everything()
    annex = asset(made.organization, "Annex")
    hire = shared(annex, "Hire")
    hire.save(now=BEFORE)
    Block.objects.take(
        organization=made.organization,
        asset=annex,
        booking_configuration=hire,
        starts_at=at(DAY, 12),
        ends_at=at(DAY, 13),
        units=made_up.spots_of(made_up.floor_plan_of(annex), 1),
        made_by=made.administrator,
    )
    hire.status = Status.DISABLED
    hire.save(now=BEFORE)

    meeting = shared(annex, "Meeting", other_plan(annex))
    assert refused(meeting) == {
        "status": [
            "It covers the same time as container 002 on Annex, which is disabled "
            "and has a booking or a block still to come; the two book different "
            "floor plans of the asset."
        ]
    }
    # The block has passed.
    meeting.save(now=AFTER)
    assert (
        BookingConfiguration.objects.of(made.organization)
        .filter(pk=meeting.pk)
        .exists()
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", sorted(SETUPS.rglob("*.toml")), ids=lambda path: path.stem
)
def test_the_loader_gives_every_asset_its_floor_plans(path: Path) -> None:
    written = setup_file.read(path)
    org = setup.load(written, today=DAY)

    # An asset the file gives no floor plan has one spot without a name.
    default = (setup_file.FloorPlan(name="", spot_word="booking", spots=("",)),)
    for each in written.assets:
        plans = Asset.objects.of(org).get(number=each.number).floor_plans
        assert [
            (plan.name, plan.unit, numbers(plan)) for plan in plans.order_by("id")
        ] == [
            (plan.name, plan.spot_word, list(enumerate(plan.spots, start=1)))
            for plan in each.floor_plans or default
        ]
    shared = {
        item.number: item.floor_plan
        for item in written.booking_configurations
        if item.consumption_mode == ConsumptionMode.SHARED
    }
    for config in BookingConfiguration.objects.of(org).select_related("floor_plan"):
        assert (config.floor_plan.name if config.floor_plan else None) == shared.get(
            config.number
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "holding",
    [
        "spot name",
        "group",
        "floor plan name",
        "whole space page",
        "occupancy",
        "second floor plan",
    ],
)
def test_format_3_holds_what_only_floor_plans_hold(holding: str) -> None:
    org = setup.load(setup_file.read(SETUPS / "example.toml"), today=DAY)
    asset = Asset.objects.of(org).order_by("number").first()
    assert asset is not None
    floor_plan = made_up.floor_plan_of(asset)
    plain = setup.export(org)
    if holding == "spot name":
        Spot.objects.of(org).filter(floor_plan=floor_plan).update(name="By the stage")
    elif holding == "group":
        SpotGroup.objects.create(organization=org, floor_plan=floor_plan, name="Stage")
    elif holding == "floor plan name":
        floor_plan.name = "Concert seating"
        floor_plan.save()
    elif holding == "whole space page":
        Space.objects.create(
            organization=org, name="Hall", booking_page=BookingPage.WHOLE_SPACE
        )
    elif holding == "occupancy":
        Asset.objects.of(org).filter(pk=asset.pk).update(maximum_occupancy=120)
    else:
        floor_plan.name = "Seated"
        floor_plan.save()
        AssetInventory.objects.create(organization=org, asset=asset, name="Standing")

    exported = setup.export(org)
    assert exported != plain
    assert setup_file.parse(setup_file.write(exported)) == exported


@pytest.mark.django_db
def test_a_file_cannot_hold_two_floor_plans_without_names() -> None:
    org = setup.load(setup_file.read(SETUPS / "example.toml"), today=DAY)
    asset = Asset.objects.of(org).get(number=1)
    AssetInventory.objects.create(organization=org, asset=asset)
    with pytest.raises(SetupError, match="an asset with more than one floor plan"):
        setup_file.write(setup.export(org))


@pytest.mark.django_db(transaction=True)
def test_the_application_changes_only_what_may_change() -> None:
    with application_login():
        made = made_up.everything()
        spot = Spot.objects.of(made.organization).get()
        group = SpotGroup.objects.of(made.organization).get()
        floor_plan = made_up.floor_plan_of(made.asset)
        space = Space.objects.of(made.organization).get()
        allocation = CapacityAllocation.objects.of(made.organization).filter(
            taken_time=made.booking.taken_time_id
        )[0]
        allowed = [
            ("core_spot", "name = 'Front row'", spot.pk),
            ("core_spot", "group_id = NULL", spot.pk),
            # removing a spot moves the last spot into its place.
            ("core_spot", "place = 1", spot.pk),
            ("core_spotgroup", "name = 'Front', word = 'row'", group.pk),
            ("core_assetinventory", "name = 'Concert seating'", floor_plan.pk),
            ("core_space", "booking_page = 'whole_space'", space.pk),
            ("core_asset", "maximum_occupancy = 120", made.asset.pk),
            ("core_asset", "whole_space = false", made.asset.pk),
            ("core_asset", "space_id = NULL", made.asset.pk),
            ("core_takentime", "state = 'cancelled'", made.booking.taken_time_id),
        ]
        refused_columns = [
            ("core_spot", "number = 9", spot.pk),
            ("core_spot", f"floor_plan_id = {floor_plan.pk}", spot.pk),
            ("core_spot", "floor_plan_capacity = 1", spot.pk),
            ("core_spotgroup", "number = 9", group.pk),
            ("core_spotgroup", f"floor_plan_id = {floor_plan.pk}", group.pk),
            ("core_asset", "number = 9", made.asset.pk),
            ("core_assetinventory", f"asset_id = {made.asset.pk}", floor_plan.pk),
            ("core_takentime", "scope_key = 1", made.booking.taken_time_id),
            ("core_takentime", "whole_space = true", made.booking.taken_time_id),
            ("core_capacityallocation", "spot_id = NULL", allocation.pk),
            ("core_capacityallocation", "state = 'held'", allocation.pk),
        ]
        with connection.cursor() as cursor:
            for table, change, pk in allowed:
                cursor.execute(f"UPDATE {table} SET {change} WHERE id = %s", [pk])  # noqa: S608
            for table, change, pk in refused_columns:
                with pytest.raises(DatabaseError) as error, transaction.atomic():
                    cursor.execute(f"UPDATE {table} SET {change} WHERE id = %s", [pk])  # noqa: S608
                assert isinstance(
                    error.value.__cause__, psycopg.errors.InsufficientPrivilege
                ), (table, change)
        # The model's own writes go through as the application too.
        Spot.objects.create(organization=made.organization, floor_plan=floor_plan)
        SpotGroup.objects.create(
            organization=made.organization, floor_plan=floor_plan, name="Balcony"
        )
        floor_plan.refresh_from_db()
        assert (floor_plan.capacity, floor_plan.last_group_number) == (2, 2)
