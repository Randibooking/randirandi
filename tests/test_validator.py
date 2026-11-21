# SPDX-License-Identifier: AGPL-3.0-only
"""The validator .

A container is refused when it is shared and covers the same time as another
shared container on its asset that books a different floor plan, or when the
asset's availability does not cover one of its date periods; a container with
a booking still to come keeps its fixed fields. Any other containers stack.
Written past it, two containers that clash leave the time they share
unoffered for both, and a command lists them.
"""

import re
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections

from randirandi import setup_file
from randirandi.core import setup, validator
from randirandi.core.calendar import bookable_intervals
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    AvailableSpan,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    ConsumptionMode,
    Organization,
    PeriodType,
    PriceBook,
    Space,
    Status,
    TakenTime,
    Weekday,
)
from randirandi.core.models.base import DAY as WHOLE_DAY
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from randirandi.setup_file import SetupError, parse
from tests import made_up
from tests.test_calendar import DAY, at, scenario
from tests.test_setup_file import SETUPS

# Before the made-up organization's booking on Monday 1 March 2027.
BEFORE = at(DAY - timedelta(days=28))
# After it.
AFTER = at(DAY + timedelta(days=9))


def kind(org: Organization, name: str) -> BookingType:
    return BookingType.objects.of(org).get_or_create(organization=org, name=name)[0]


def asset(
    org: Organization,
    name: str,
    *,
    space: Space | None = None,
    whole_space: bool = False,
    spans: tuple[tuple[int, int], ...] = ((0, WHOLE_DAY),),
    days: range = range(7),
    from_day: date = DAY,
) -> Asset:
    """An asset with an availability calendar from a day: the same spans on
    each of the weekdays."""
    made = Asset.objects.create(
        organization=org, name=name, space=space, whole_space=whole_space
    )
    available(made, from_day, spans, days)
    return made


def available(
    on: Asset,
    from_day: date,
    spans: tuple[tuple[int, int], ...],
    days: range = range(7),
) -> None:
    calendar = AvailabilityCalendar.objects.create(
        organization_id=on.organization_id, asset=on, start_date=from_day
    )
    for weekday in days:
        for first, last in spans:
            AvailableSpan.objects.create(
                organization_id=on.organization_id,
                availability_calendar=calendar,
                weekday=weekday,
                start_time=first,
                end_time=last,
            )


def container(on: Asset, name: str = "Meeting", **fields: Any) -> BookingConfiguration:
    """An enabled flexible container on Mondays, 12:00 to 14:00, unsaved."""
    values: dict[str, Any] = {
        "organization_id": on.organization_id,
        "asset": on,
        "booking_type": kind(on.organization, name),
        "period_type": PeriodType.FLEXIBLE,
        "period_start": 12 * 60,
        "period_end": 14 * 60,
        "period_start_weekdays": [Weekday.MONDAY],
        "interval": 60,
        "maximum_length": 120,
        "status": Status.ENABLED,
        "price_book": PriceBook.objects.of(on.organization_id).first()
        or PriceBook.objects.create(organization_id=on.organization_id, name="Prices"),
    }
    values.update(fields)
    return BookingConfiguration(**values)


def shared(
    on: Asset,
    name: str = "Meeting",
    plan: AssetInventory | None = None,
    **fields: Any,
) -> BookingConfiguration:
    """A shared container like `container`, booking the floor plan `plan`, by
    default the asset's first."""
    return container(
        on,
        name,
        consumption_mode=ConsumptionMode.SHARED,
        floor_plan=plan or made_up.floor_plan_of(on),
        **fields,
    )


def other_plan(on: Asset, name: str = "Standing") -> AssetInventory:
    """A second floor plan of the asset, with one spot."""
    return AssetInventory.objects.create(
        organization_id=on.organization_id, asset=on, name=name
    )


def refused(config: BookingConfiguration, now: datetime = BEFORE) -> dict[str, Any]:
    with pytest.raises(ValidationError) as error:
        config.save(now=now)
    return error.value.message_dict


@pytest.mark.django_db
def test_two_booking_types_on_one_asset_at_one_time_stack() -> None:
    made = made_up.everything()
    # The made-up organization's own container is exclusive, 10:00 to 22:00.
    container(made.asset).save(now=BEFORE)
    shared(made.asset, "Seats").save(now=BEFORE)
    shared(made.asset, "More seats").save(now=BEFORE)
    container(made.asset, status=Status.DISABLED).save(now=BEFORE)
    assert (
        BookingConfiguration.objects.of(made.organization)
        .filter(asset=made.asset, status=Status.ENABLED)
        .count()
        == 4
    )


@pytest.mark.django_db
def test_two_shared_containers_on_different_floor_plans_are_refused() -> None:
    org = made_up.organization()
    sauna = asset(org, "Sauna")
    shared(sauna, "Seated").save(now=BEFORE)
    standing = shared(sauna, "Standing", other_plan(sauna))
    assert refused(standing) == {
        "status": [
            "It covers the same time as container 001 on Sauna; the two book "
            "different floor plans of the asset."
        ]
    }
    # Back to back is not the same time; an exclusive container, a disabled
    # one and one on the same floor plan stack.
    shared(
        sauna, "Morning", standing.floor_plan, period_start=8 * 60, period_end=10 * 60
    ).save(now=BEFORE)
    container(sauna, "Private").save(now=BEFORE)
    shared(sauna, "Standing", standing.floor_plan, status=Status.DISABLED).save(
        now=BEFORE
    )
    shared(sauna, "Seated again").save(now=BEFORE)
    assert BookingConfiguration.objects.of(org).count() == 5


@pytest.mark.django_db
@pytest.mark.parametrize("whole_first", [False, True])
def test_a_whole_space_container_stacks_with_one_in_its_space(
    whole_first: bool,
) -> None:
    org = made_up.organization()
    room = Space.objects.create(organization=org, name="Laundry room")
    whole = asset(org, "Whole room", space=room, whole_space=True)
    washer = asset(org, "Washer", space=room)
    first, second = (whole, washer) if whole_first else (washer, whole)
    container(first, "Use").save(now=BEFORE)
    container(second, "Hire").save(now=BEFORE)
    assert (
        BookingConfiguration.objects.of(org).filter(status=Status.ENABLED).count() == 2
    )


@pytest.mark.django_db
def test_assets_in_one_space_without_the_mark_are_independent() -> None:
    org = made_up.organization()
    room = Space.objects.create(organization=org, name="Laundry room")
    for name in ("Washer", "Dryer"):
        container(asset(org, name, space=room), "Use").save(now=BEFORE)
    # The whole room at other times, and an asset elsewhere at the same time.
    whole = asset(org, "Whole room", space=room, whole_space=True)
    container(whole, "Hire", period_start=8 * 60, period_end=10 * 60).save(now=BEFORE)
    shed = Space.objects.create(organization=org, name="Garden shed")
    container(asset(org, "Whole shed", space=shed, whole_space=True), "Hire").save(
        now=BEFORE
    )
    assert BookingConfiguration.objects.of(org).count() == 4


@pytest.mark.django_db
def test_a_disabled_container_keeps_its_time_while_it_has_a_booking_to_come() -> None:
    made = made_up.everything()
    seated = shared(made.asset, "Seated")
    seated.save(now=BEFORE)
    made.book(12, 13, configuration=seated)
    seated.status = Status.DISABLED
    seated.save(now=BEFORE)
    standing = shared(made.asset, "Standing", other_plan(made.asset))
    assert refused(standing) == {
        "status": [
            "It covers the same time as container 002 on Function room, which is "
            "disabled and has a booking or a block still to come; the two book "
            "different floor plans of the asset."
        ]
    }
    # The booking has passed.
    standing.save(now=AFTER)
    assert (
        BookingConfiguration.objects.of(made.organization)
        .filter(pk=standing.pk)
        .exists()
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "fault"),
    [
        (
            # Friday 16:00 to Saturday 12:00, under 10:00 to 24:00 both days:
            # the night 00:00 to 10:00 is closed.
            {
                "period_type": PeriodType.FIXED,
                "period_start": 16 * 60,
                "period_end": 12 * 60,
                "end_day": 1,
                "period_start_weekdays": [Weekday.FRIDAY],
            },
            "Its date period that starts on Friday 9 April 2027 at 16:00 is not "
            "covered by the asset's availability: the asset is not available from "
            "00:00 on Saturday 10 April 2027.",
        ),
        (
            {"period_start": 8 * 60, "period_end": 12 * 60, "maximum_length": 240},
            "Its date period that starts on Monday 5 April 2027 at 08:00 is not "
            "covered by the asset's availability: the asset is not available from "
            "08:00 on Monday 5 April 2027.",
        ),
        (
            {
                "period_type": PeriodType.FIXED,
                "period_start": 9 * 60,
                "period_end": 11 * 60,
                "period_start_weekdays": [Weekday.WEDNESDAY],
            },
            "Its date period that starts on Wednesday 7 April 2027 at 09:00 is not "
            "covered by the asset's availability: the asset is not available from "
            "09:00 on Wednesday 7 April 2027.",
        ),
    ],
    ids=["over midnight", "flexible", "fixed"],
)
def test_a_period_the_availability_does_not_cover_is_refused(
    fields: dict[str, Any], fault: str
) -> None:
    org = made_up.organization()
    # Around the clock from 1 March, and from Monday 5 April 10:00 to 24:00.
    house = asset(org, "Guest house")
    available(house, date(2027, 4, 5), ((10 * 60, WHOLE_DAY),))
    assert refused(container(house, **fields)) == {"__all__": [fault]}
    # Its last calendar week before the second availability calendar.
    container(house, **fields, stop_date=date(2027, 4, 4)).save(now=BEFORE)


@pytest.mark.django_db
def test_a_period_over_midnight_is_covered_when_both_days_meet() -> None:
    org = made_up.organization()
    # Friday from 16:00 and Saturday until 12:00, and nothing else.
    house = asset(org, "Guest house", spans=((16 * 60, WHOLE_DAY),), days=range(4, 5))
    calendar = AvailabilityCalendar.objects.of(org).get(asset=house)
    AvailableSpan.objects.create(
        organization=org,
        availability_calendar=calendar,
        weekday=Weekday.SATURDAY,
        start_time=0,
        end_time=12 * 60,
    )
    friday_night: dict[str, Any] = {
        "period_type": PeriodType.FIXED,
        "period_start": 16 * 60,
        "period_end": 12 * 60,
        "end_day": 1,
        "period_start_weekdays": [Weekday.FRIDAY],
    }
    container(house, **friday_night).save(now=BEFORE)
    longer = container(house, **{**friday_night, "period_end": 13 * 60})
    assert "__all__" in refused(longer)


@pytest.mark.django_db
def test_a_past_clash_and_periods_before_the_first_calendar_are_not_checked() -> None:
    made = made_up.everything()
    shared(made.asset, "Seated").save(now=BEFORE)
    # Only on Monday 1 March, over the seated container, on another floor plan.
    once = shared(
        made.asset,
        "Standing",
        other_plan(made.asset),
        start_date=DAY,
        stop_date=DAY + timedelta(days=6),
    )
    assert "status" in refused(once)
    once.save(now=AFTER)
    assert (
        BookingConfiguration.objects.of(made.organization).filter(pk=once.pk).exists()
    )
    # Nothing covers Tuesdays before 1 March, when the asset's first
    # availability calendar starts; from then on, a Tuesday is checked.
    tuesday: dict[str, Any] = {"period_start_weekdays": [Weekday.TUESDAY]}
    february = container(
        made.asset, **tuesday, start_date=date(2027, 2, 1), stop_date=date(2027, 2, 28)
    )
    february.save(now=BEFORE)
    march = container(made.asset, **tuesday, start_date=date(2027, 2, 22))
    assert "__all__" in refused(march)


@pytest.mark.django_db
def test_alternating_weeks_are_accepted() -> None:
    org = made_up.organization()
    sauna = asset(org, "Sauna")
    standing = other_plan(sauna)
    shared(sauna, "Odd", start_date=DAY, every_weeks=2).save(now=BEFORE)
    shared(sauna, "Even", start_date=DAY + timedelta(days=7), every_weeks=2).save(
        now=BEFORE
    )
    third = shared(
        sauna,
        "Third",
        standing,
        start_date=DAY + timedelta(days=14),
        every_weeks=2,
    )
    assert refused(third) == {
        "status": [
            "It covers the same time as container 001 on Sauna; the two book "
            "different floor plans of the asset."
        ]
    }
    # In the other weeks it covers the same time as container 002.
    third.start_date = DAY + timedelta(days=7)
    assert refused(third)["status"][0].startswith(
        "It covers the same time as container 002"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        {"period_start": 9 * 60},
        {"period_end": 23 * 60},
        {"period_start_weekdays": [Weekday.MONDAY, Weekday.TUESDAY]},
        {"interval": 30},
        # A shared container books its asset's floor plan.
        {"consumption_mode": "shared", "floor_plan": "its own"},
        {"period_type": PeriodType.FIXED},
        {"start_date": DAY},
        {"start_date": DAY, "every_weeks": 2},
        {"asset": "other"},
    ],
    ids=lambda change: "+".join(change),
)
def test_a_fixed_field_is_refused_while_a_booking_is_to_come(
    change: dict[str, Any],
) -> None:
    made = made_up.everything()
    AvailableSpan.objects.create(
        organization=made.organization,
        availability_calendar=AvailabilityCalendar.objects.of(made.organization).get(
            asset=made.asset
        ),
        weekday=Weekday.TUESDAY,
        start_time=0,
        end_time=WHOLE_DAY,
    )
    config = made.configuration
    for field, value in change.items():
        setattr(
            config,
            field,
            asset(made.organization, "Other")
            if value == "other"
            else made_up.floor_plan_of(made.asset)
            if value == "its own"
            else value,
        )
    faults = refused(config)
    reference = made.order.reference
    assert set(change) <= set(faults)
    words = {
        "period_start": "period start",
        "period_end": "period end",
        "period_start_weekdays": "period start weekdays",
        "interval": "interval",
        "consumption_mode": "consumption mode",
        "period_type": "period type",
        "start_date": "first calendar week",
        "asset": "asset",
    }[next(iter(change))]
    assert faults[next(iter(change))] == [
        f"The {words} stays as it is while the container has a booking still to "
        f"come: order {reference}, "
        "from Monday 1 March 2027 at 11:00."
    ]
    # Once the booking has passed, it may change.
    config.save(now=AFTER)


@pytest.mark.django_db
def test_what_a_booking_keeps_may_change_while_it_is_to_come() -> None:
    made = made_up.everything()
    config = made.configuration
    config.price_book = PriceBook.objects.create(
        organization=made.organization, name="Prices 2028"
    )
    config.booking_type = kind(made.organization, "Party")
    config.minimum_length = 120
    config.maximum_length = 600
    config.save(now=BEFORE)
    # The stop week may move, but not before the week of the booking.
    config.stop_date = DAY + timedelta(days=6)
    config.save(now=BEFORE)
    config.stop_date = DAY - timedelta(days=1)
    reference = made.order.reference
    assert refused(config) == {
        "stop_date": [
            "The last calendar week is not before the week of the last booking "
            f"still to come: order {reference}, "
            "in the date period that starts on Monday 1 "
            "March 2027."
        ]
    }
    config.status = Status.DISABLED
    config.stop_date = None
    config.save(now=BEFORE)
    stored = BookingConfiguration.objects.of(made.organization).get(pk=config.pk)
    assert stored.price_book is not None
    assert (stored.price_book.name, stored.booking_type.name) == (
        "Prices 2028",
        "Party",
    )


@pytest.mark.django_db
def test_a_later_availability_change_holds_up_only_what_asks_for_more_time() -> None:
    org = made_up.organization()
    room = asset(org, "Studio", spans=((10 * 60, 18 * 60),))
    config = container(room, "Practice")
    config.save(now=BEFORE)
    # Availability always wins: Mondays now open at 13:00, under the container.
    AvailableSpan.objects.of(org).filter(
        availability_calendar__asset=room, weekday=Weekday.MONDAY
    ).update(start_time=13 * 60)
    config.price_book = PriceBook.objects.create(organization=org, name="Prices 2028")
    config.maximum_length = 60
    config.save(now=BEFORE)
    gap = (
        "Its date period that starts on Monday 1 March 2027 at 12:00 is not "
        "covered by the asset's availability: the asset is not available from "
        "12:00 on Monday 1 March 2027."
    )
    # A change to the time it covers, or enabling it again, asks for it all.
    config.period_end = 15 * 60
    assert refused(config) == {"__all__": [gap]}
    config.refresh_from_db()
    config.status = Status.DISABLED
    config.save(now=BEFORE)
    config.status = Status.ENABLED
    assert refused(config) == {"__all__": [gap]}


@pytest.mark.django_db
def test_setting_the_mark_where_containers_stack_is_allowed() -> None:
    org = made_up.organization()
    laundry = Space.objects.create(organization=org, name="Laundry")
    washer = asset(org, "Washer", space=laundry)
    dryer = asset(org, "Dryer", space=laundry)
    container(washer, "Wash").save(now=BEFORE)
    container(dryer, "Dry").save(now=BEFORE)

    validator.check_place(washer, space_id=laundry.pk, whole_space=True, now=BEFORE)
    # The mark is written through the asset's save .
    washer.whole_space = True
    washer.save(now=BEFORE)
    assert Asset.objects.of(org).get(pk=washer.pk).whole_space
    assert (
        BookingConfiguration.objects.of(org).filter(status=Status.ENABLED).count() == 2
    )


@pytest.mark.django_db
def test_the_mark_and_the_space_stay_while_a_booking_is_to_come() -> None:
    made = made_up.everything()
    made.asset.whole_space = True
    with pytest.raises(ValidationError) as error:
        made.asset.save(now=BEFORE)
    assert error.value.message_dict == {
        "whole_space": [
            "Function room has a booking still to come, so its space and whether "
            "it covers its whole space stay as they are."
        ]
    }
    claims = TakenTime.objects.of(made.organization).filter(asset=made.asset)
    # Once the booking has passed, the mark changes, and the copies its
    # bookings and blocks keep follow it.
    made.asset.save(now=AFTER)
    assert set(claims.values_list("whole_space", flat=True)) == {True}
    made.asset.whole_space = False
    annex = Space.objects.create(organization=made.organization, name="Annex")
    made.asset.space = annex
    with pytest.raises(ValidationError) as error:
        made.asset.save(now=BEFORE)
    assert set(error.value.message_dict) == {"space"}
    made.asset.save(now=AFTER)
    assert made.asset.scope_key == annex.pk
    assert set(claims.values_list("scope_key", "whole_space")) == {(annex.pk, False)}


@pytest.mark.django_db
def test_moving_into_a_space_where_containers_stack_is_allowed() -> None:
    org = made_up.organization()
    hall = Space.objects.create(organization=org, name="Hall")
    whole = asset(org, "Whole hall", space=hall, whole_space=True)
    container(whole, "Hire").save(now=BEFORE)
    stage = asset(org, "Stage")
    container(stage, "Rehearsal").save(now=BEFORE)
    stage.space = hall
    stage.save(now=BEFORE)
    assert Asset.objects.of(org).get(pk=stage.pk).space_id == hall.pk


@pytest.mark.django_db(transaction=True)
def test_two_saves_at_once_are_one_refused() -> None:
    org = made_up.organization()
    sauna = asset(org, "Sauna")
    standing = other_plan(sauna)
    rows = [shared(sauna, "Visit"), shared(sauna, "Stand-up sauna", standing)]
    barrier = Barrier(2)
    real = validator._uncovered

    def slow(*arguments: Any) -> Any:
        # Without the lock, both would pass their checks before either saves.
        clock.sleep(0.5)
        return real(*arguments)

    def save(row: BookingConfiguration) -> str:
        try:
            barrier.wait(timeout=10)
            row.save(now=BEFORE)
            return "saved"
        except ValidationError as error:
            return error.message_dict["status"][0]
        finally:
            connections.close_all()

    with (
        mock.patch.object(validator, "_uncovered", slow),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        outcomes = sorted(pool.map(save, rows))
    assert outcomes == [
        "It covers the same time as container 001 on Sauna; the two book "
        "different floor plans of the asset.",
        "saved",
    ]
    assert BookingConfiguration.objects.of(org).count() == 1


def laundry_text() -> str:
    return (SETUPS / "scenarios" / "laundry.toml").read_text()


@pytest.mark.django_db
def test_the_loader_refuses_a_container_the_availability_does_not_cover() -> None:
    text = (SETUPS / "fjordside.toml").read_text()
    every_day = (
        '{ weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], '
        'start = "00:00", end = "24:00" },'
    )
    assert text.count(every_day) == 1
    # An availability with the weekend's 10:00 to 14:00 left out, under a
    # weekend container from 10:00.
    gap = (
        '{ weekdays = ["mon", "tue", "wed", "thu", "fri"], '
        'start = "00:00", end = "24:00" },\n'
        '  { weekdays = ["sat", "sun"], start = "00:00", end = "10:00" },\n'
        '  { weekdays = ["sat", "sun"], start = "14:00", end = "24:00" },'
    )
    before = text.replace(every_day, gap)
    with pytest.raises(SetupError) as error:
        setup.load(parse(before), today=DAY)
    assert error.value.faults == [
        "booking_configurations[3]: Its date period that starts on Saturday 6 "
        "March 2027 at 10:00 is not covered by the asset's availability: the "
        "asset is not available from 10:00 on Saturday 6 March 2027."
    ]
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_the_loader_takes_the_beds_and_the_room_on_the_same_nights() -> None:
    text = (SETUPS / "scenarios" / "four-beds.toml").read_text()
    every_night = re.sub(
        r"period_start_weekdays = \[.*\]",
        'period_start_weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]',
        text,
    )
    org = setup.load(parse(every_night), today=DAY)
    beds, room = BookingConfiguration.objects.of(org).order_by("number")
    assert (beds.consumption_mode, room.consumption_mode) == ("shared", "exclusive")
    assert beds.asset_id == room.asset_id
    assert validator.clashes(org, now=at(DAY)) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", sorted(SETUPS.rglob("*.toml")), ids=lambda path: path.stem
)
def test_every_setup_file_loads(path: Path) -> None:
    organization = setup.load(setup_file.read(path), today=DAY)
    assert validator.clashes(organization, now=at(DAY)) == []


@pytest.mark.django_db(transaction=True)
def test_a_clash_written_past_the_validator_is_offered_by_neither_and_listed() -> None:
    org, member = scenario("scenarios/riverside-hall.toml")
    concert, club = (
        BookingConfiguration.objects.of(org)
        .select_related("asset", "booking_type")
        .get(number=number)
        for number in (2, 4)
    )
    friday = DAY + timedelta(days=4)
    zone = ZoneInfo("Europe/Oslo")

    def offered(config: BookingConfiguration, day: date) -> set[tuple[int, int]]:
        return {
            (
                offer.starts_at.astimezone(zone).hour,
                offer.ends_at.astimezone(zone).hour,
            )
            for offer in bookable_intervals(
                member=member,
                asset=config.asset,
                booking_type=config.booking_type,
                start_date=day,
                end_date=day + timedelta(days=1),
                now=at(DAY - timedelta(days=1)),
            )
        }

    assert offered(concert, friday) == {(19, 22)}
    # The club night on Friday 19:00 to 22:00 as well, on the standing floor
    # plan: the concert's seats and the standing places cannot be compared.
    BookingConfiguration.objects.of(org).filter(pk=club.pk).update(
        period_start=19 * 60,
        period_end=22 * 60,
        end_day=0,
        period_start_weekdays=[4],
        interval=180,
        minimum_length=180,
        maximum_length=180,
    )
    assert offered(concert, friday) == set()
    assert offered(club, friday) == set()
    private = BookingCategory.objects.of(org).get(name="private")
    request = BookingRequest(concert.pk, private.pk, at(friday, 19), at(friday, 22))
    with pytest.raises(OrderUnavailableError):
        create_order(
            member=member,
            requests=[request],
            now=at(DAY),
            accepted_version=made_up.accepted(member),
        )
    output = StringIO()
    call_command("list_clashes", "riverside-hall", stdout=output)
    assert output.getvalue() == (
        "Container 002 on Riverside Hall and container 004 on Riverside Hall "
        "book different floor plans of one asset at the same time, so neither "
        "offers the time they share.\n"
    )
    # A later save of either is refused.
    stored = BookingConfiguration.objects.of(org).get(pk=club.pk)
    assert "status" in refused(stored, now=at(DAY))


@pytest.mark.django_db
def test_the_list_of_clashes_says_when_there_is_none() -> None:
    setup.load(setup_file.read(SETUPS / "scenarios" / "laundry.toml"), today=DAY)
    output = StringIO()
    call_command("list_clashes", "laundry", stdout=output)
    assert output.getvalue() == "No containers clash.\n"


@pytest.mark.django_db
def test_the_list_of_clashes_names_an_organization_it_does_not_know() -> None:
    with pytest.raises(
        CommandError, match="no organization with the subdomain nowhere"
    ):
        call_command("list_clashes", "nowhere", stdout=StringIO())
