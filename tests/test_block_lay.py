# SPDX-License-Identifier: AGPL-3.0-only
"""How a stretch becomes blocks : the lay over fixed and flexible
periods, across days and date periods, stacked and shared containers, a
stretch with no whole cell, what a booking or a block in the way does, all or
none, a double submit, the race with a booking, and the member calendar before
and after."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from randirandi.core import audit, block_admin, booking_admin, member_calendar
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    Asset,
    AuditEvent,
    AvailabilityCalendar,
    AvailableSpan,
    Block,
    Booking,
    BookingConfiguration,
    BookingState,
    ConfigurationCategory,
    ConsumptionMode,
    Order,
    Spot,
    TakenTime,
    Weekday,
)
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import (
    MARCH_8,
    MARCH_15,
    MARCH_22,
    NOW,
    answer,
    call,
    deck,
    local,
    stretch,
)
from tests.test_admin_blocks import (
    made as made,
)
from tests.test_admin_blocks import (
    now as now,
)
from tests.test_login_sessions import page_settings as page_settings

pytestmark = pytest.mark.django_db(transaction=True)
FRIDAY = date(2027, 3, 12)


def lay(made: made_up.MadeUp, start: str, end: str, asset: Asset | None = None) -> Any:
    return answer(call(made, "lay", stretch(made, start, end, asset)))


def times(found: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    return [
        (p["start_date"], p["start_time"], p["end_date"], p["end_time"])
        for p in found["pieces"]
    ]


def blocks_of(made: made_up.MadeUp) -> int:
    return Block.objects.of(made.organization).count()


def container_like(
    made: made_up.MadeUp, asset: Asset, **changes: Any
) -> BookingConfiguration:
    """An exclusive flexible container on the asset, by the hour, with the
    made-up category; `changes` are its fields."""
    fields: dict[str, Any] = {
        "organization": made.organization,
        "asset": asset,
        "booking_type": made.booking_type,
        "period_type": "flexible",
        "period_start": 10 * 60,
        "period_end": 22 * 60,
        "end_day": 0,
        "period_start_weekdays": [Weekday.MONDAY],
        "interval": 60,
        "minimum_length": 60,
        "status": "enabled",
        "price_book": made.price_book,
        **changes,
    }
    if "maximum_length" not in fields:
        fields["maximum_length"] = min(
            fields["minimum_length"] * 12, fields["period_end"] - fields["period_start"]
        )
    found = BookingConfiguration(**fields)
    found.save()
    ConfigurationCategory.objects.create(
        organization=made.organization,
        booking_configuration=found,
        booking_category=made.private,
    )
    return found


def nights(made: made_up.MadeUp, *, touching: bool = False) -> BookingConfiguration:
    """A cabin whose date periods are fixed: Friday and Saturday 15:00 to the
    next day at 11:00 (20 hours), or to 15:00 (touching one another)."""
    cabin = Asset.objects.create(organization=made.organization, name="Cabin")
    end = 15 * 60 if touching else 11 * 60
    length = (24 if touching else 20) * 60
    return container_like(
        made,
        cabin,
        period_type="fixed",
        period_start=15 * 60,
        end_day=1,
        period_end=end,
        period_start_weekdays=[Weekday.FRIDAY, Weekday.SATURDAY],
        interval=length,
        minimum_length=length,
        maximum_length=length,
    )


# --- The lay -------------------------------------------------------------------


def test_a_flexible_container_lays_the_cells_inside_the_stretch(
    made: made_up.MadeUp,
) -> None:
    found = lay(made, local(MARCH_8, 18), local(MARCH_15, 12))
    # Monday evening, and Monday morning a week on: cells that touch are one
    # block, nothing is laid on the days between.
    assert times(found) == [
        ("2027-03-08", "18:00", "2027-03-08", "22:00"),
        ("2027-03-15", "10:00", "2027-03-15", "12:00"),
    ]
    assert {(p["container"], p["booking_type"]) for p in found["pieces"]} == {
        (1, "Room hire")
    }
    assert (found["booked"], found["blocked"], found["refusal"]) == ([], False, None)
    assert blocks_of(made) == 1


def test_only_whole_cells_are_laid(made: made_up.MadeUp) -> None:
    assert times(lay(made, local(MARCH_8, 10, 30), local(MARCH_8, 13, 30))) == [
        ("2027-03-08", "11:00", "2027-03-08", "13:00")
    ]
    # No whole cell: half an hour inside one, a day the container is closed,
    # and a stretch that ends before it starts.
    for start, end in (
        (local(MARCH_8, 10, 15), local(MARCH_8, 10, 45)),
        (
            local(MARCH_8 + timedelta(days=1), 10),
            local(MARCH_8 + timedelta(days=1), 18),
        ),
        (local(MARCH_8, 12), local(MARCH_8, 10)),
        (local(MARCH_8, 12), local(MARCH_8, 12)),
    ):
        found = lay(made, start, end)
        assert (found["pieces"], found["refusal"]) == ([], "nothing")
        refused = answer(call(made, "make", stretch(made, start, end)), 409)
        assert refused["error"] == "nothing"
    assert blocks_of(made) == 1


def test_a_stretch_over_fixed_date_periods_lays_each_period_whole(
    made: made_up.MadeUp,
) -> None:
    cabin = nights(made)
    # Friday noon to Monday: Friday and Saturday night, whole.
    found = lay(
        made, local(FRIDAY, 12), local(FRIDAY + timedelta(days=3), 0), cabin.asset
    )
    assert times(found) == [
        ("2027-03-12", "15:00", "2027-03-13", "11:00"),
        ("2027-03-13", "15:00", "2027-03-14", "11:00"),
    ]
    # Half a night is no cell at all.
    half = lay(
        made, local(FRIDAY, 12), local(FRIDAY + timedelta(days=1), 8), cabin.asset
    )
    assert (half["pieces"], half["refusal"]) == ([], "nothing")


def test_date_periods_that_touch_are_one_block(made: made_up.MadeUp) -> None:
    cabin = nights(made, touching=True)
    start, end = local(FRIDAY, 12), local(FRIDAY + timedelta(days=3), 0)
    found = lay(made, start, end, cabin.asset)
    assert times(found) == [("2027-03-12", "15:00", "2027-03-14", "15:00")]
    answer(call(made, "make", stretch(made, start, end, cabin.asset)))
    (block,) = Block.objects.of(made.organization).filter(booking_configuration=cabin)
    assert block.taken_time.starts_at == datetime(2027, 3, 12, 14, tzinfo=UTC)
    assert block.taken_time.ends_at == datetime(2027, 3, 14, 14, tzinfo=UTC)


def test_stacked_containers_never_take_the_same_time_twice(
    made: made_up.MadeUp,
) -> None:
    # Container 2 covers Monday 12:00 to 16:00 and Tuesday 10:00 to 12:00 on the
    # same asset as container 1, which covers Mondays 10:00 to 22:00.
    AvailableSpan.objects.create(
        organization=made.organization,
        availability_calendar=AvailabilityCalendar.objects.of(made.organization).get(
            asset=made.asset
        ),
        weekday=Weekday.TUESDAY,
        start_time=8 * 60,
        end_time=23 * 60,
    )
    second = container_like(
        made,
        made.asset,
        period_start=12 * 60,
        period_end=16 * 60,
        period_start_weekdays=[Weekday.MONDAY, Weekday.TUESDAY],
    )
    tuesday = MARCH_8 + timedelta(days=1)
    found = lay(made, local(MARCH_8, 10), local(tuesday, 16))
    # Monday is container 1's alone; container 2's cells there overlap its
    # cells, so they are skipped. Tuesday has only container 2's cells.
    assert [(p["container"], *times({"pieces": [p]})[0]) for p in found["pieces"]] == [
        (1, "2027-03-08", "10:00", "2027-03-08", "22:00"),
        (2, "2027-03-09", "12:00", "2027-03-09", "16:00"),
    ]
    made_ = answer(
        call(made, "make", stretch(made, local(MARCH_8, 10), local(tuesday, 16)))
    )
    taken = TakenTime.objects.of(made.organization).filter(
        pk__in=Block.objects.of(made.organization)
        .filter(pk__in=made_["blocks"])
        .values("taken_time")
    )
    spans = sorted((t.starts_at, t.ends_at) for t in taken)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))
    assert second.number == 2


def test_the_lower_numbered_container_keeps_the_time_it_shares(
    made: made_up.MadeUp,
) -> None:
    # Container 2 has Mondays 10:00 to 14:00 only inside container 1's.
    container_like(made, made.asset, period_end=14 * 60)
    found = lay(made, local(MARCH_8, 10), local(MARCH_8, 22))
    assert [
        (p["container"], p["start_time"], p["end_time"]) for p in found["pieces"]
    ] == [(1, "10:00", "22:00")]


def test_a_block_on_a_shared_container_holds_every_spot(
    made: made_up.MadeUp,
) -> None:
    boat = made.shared("Boat", 3)
    start, end = local(MARCH_15, 10), local(MARCH_15, 12)
    found = lay(made, start, end, boat.asset)
    assert (found["refusal"], times(found)) == (
        None,
        [("2027-03-15", "10:00", "2027-03-15", "12:00")],
    )
    made_ = answer(call(made, "make", stretch(made, start, end, boat.asset)))
    (block,) = Block.objects.of(made.organization).filter(pk__in=made_["blocks"])
    assert block.taken_time.mode == "shared" and block.taken_time.quantity == 3
    held = sorted(block.taken_time.allocations.values_list("spot__number", flat=True))
    assert held == [1, 2, 3]
    # No spot is free for a booking in that time; a booking is named in the way.
    again = answer(call(made, "make", stretch(made, start, end, boat.asset)), 409)
    assert again["error"] == "blocked"
    # Removing frees every spot.
    answer(call(made, "remove", block=block.pk))
    assert (
        not block.taken_time.__class__.objects.of(made.organization)
        .filter(pk=block.taken_time_id)
        .exists()
    )
    answer(call(made, "make", stretch(made, start, end, boat.asset)))


def test_no_spot_of_a_blocked_shared_container_can_be_booked(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(booking_admin, "_now", lambda: NOW - timedelta(days=30))
    boat = made.shared("Boat", 2)
    found = answer(
        call(
            made,
            "make",
            stretch(made, local(made.day, 10), local(made.day, 12), boat.asset),
        )
    )
    # 10:00 to 12:00 local is 09:00 to 11:00 UTC.
    for spot in (1, 2):
        with pytest.raises(IntegrityError), transaction.atomic():
            made.book(
                9,
                10,
                configuration=boat,
                units=made_up.spots_of(made_up.floor_plan_of(boat.asset), spot),
            )
    (pk,) = found["blocks"]
    answer(call(made, "remove", block=pk))
    made.book(9, 10, configuration=boat)


def none_left(
    monkeypatch: pytest.MonkeyPatch, *containers: BookingConfiguration
) -> None:
    """The containers' floor plans have no spot: the model cannot save such a
    plan (capacity is positive), so the spot lookup is made to find none."""
    plans = {c.floor_plan_id for c in containers}
    real = Spot.objects.of

    class Found:
        def __init__(self, rows: Any) -> None:
            self.rows = rows

        def filter(self, **kwargs: Any) -> Any:
            if kwargs.get("floor_plan_id") in plans:
                return self.rows.none()
            return Found(self.rows.filter(**kwargs))

        def exists(self) -> bool:
            return bool(self.rows.exists())

    monkeypatch.setattr(Spot.objects, "of", lambda org: Found(real(org)), raising=False)


def test_a_shared_container_without_spots_is_laid_as_nothing(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = container_like(
        made,
        made.asset,
        consumption_mode=ConsumptionMode.SHARED,
        floor_plan=made_up.floor_plan_of(made.asset),
        period_start=11 * 60,
        period_end=13 * 60,
    )
    lone = made.shared("Lone", 1)
    none_left(monkeypatch, empty, lone)
    start, end = local(MARCH_15, 10), local(MARCH_15, 14)
    found = lay(made, start, end)
    # Container 1 lays as usual; the empty one adds nothing.
    assert [
        (p["container"], p["start_time"], p["end_time"]) for p in found["pieces"]
    ] == [(1, "10:00", "14:00")]
    made_ = answer(call(made, "make", stretch(made, start, end)))
    assert len(made_["blocks"]) == 1
    # Alone on its asset it is nothing, in the preview and the make alike.
    alone = lay(made, start, end, lone.asset)
    assert (alone["pieces"], alone["refusal"]) == ([], "nothing")
    refused = answer(call(made, "make", stretch(made, start, end, lone.asset)), 409)
    assert refused["error"] == "nothing"


def test_a_disabled_container_lays_nothing(made: made_up.MadeUp) -> None:
    made.configuration.status = "disabled"
    made.configuration.save()
    assert lay(made, local(MARCH_8, 10), local(MARCH_8, 12))["refusal"] == "nothing"


def test_a_stretch_is_at_most_366_days(made: made_up.MadeUp) -> None:
    long = stretch(made, local(MARCH_8, 10), local(date(2028, 3, 9), 10))
    answer(call(made, "lay", long), 400)
    fits = stretch(made, local(MARCH_8, 10), local(date(2028, 3, 8), 10))
    assert len(lay(made, fits["start"], fits["end"])["pieces"]) > 40


def test_a_stretch_across_a_clock_change_lays_whole_cells(made: made_up.MadeUp) -> None:
    # Oslo changes its clocks on 28 March 2027; the Mondays before and after
    # are at UTC+1 and UTC+2.
    last, after = date(2027, 3, 22), date(2027, 3, 29)
    found = lay(made, local(last, 18), local(after, 12))
    assert times(found) == [
        ("2027-03-22", "18:00", "2027-03-22", "22:00"),
        ("2027-03-29", "10:00", "2027-03-29", "12:00"),
    ]
    answer(call(made, "make", stretch(made, local(last, 18), local(after, 12))))
    starts = sorted(
        TakenTime.objects.of(made.organization)
        .filter(starts_at__gt=datetime(2027, 3, 20, tzinfo=UTC))
        .values_list("starts_at", flat=True)
    )
    assert starts == [
        datetime(2027, 3, 22, 17, tzinfo=UTC),
        datetime(2027, 3, 29, 8, tzinfo=UTC),
    ]


def test_two_assets_on_different_clocks_lay_on_their_own(made: made_up.MadeUp) -> None:
    euro = deck(made)
    found = lay(made, local(MARCH_8, 9), local(MARCH_8, 15), euro.asset)
    assert times(found) == [("2027-03-08", "10:00", "2027-03-08", "14:00")]


# --- What is in the way ------------------------------------------------------------


def test_a_booking_in_the_way_is_named_and_refuses_the_stretch(
    made: made_up.MadeUp,
) -> None:
    # On 1 March, in Oslo's UTC+1: the made-up booking holds 11:00 to 13:00,
    # the made-up block 15:00 to 16:00. A held booking of another order holds
    # 17:00 to 19:00, and a cancelled one 20:00 to 21:00.
    held = made.spare_order
    made.book(16, 18, state=BookingState.HELD, order=held)
    cancelled = Order.objects.create(
        organization=made.organization, member=made.member, total=0
    )
    made.book(19, 20, state=BookingState.CANCELLED, order=cancelled)
    day = made.day
    start, end = local(day, 10), local(day, 22)
    found = lay(made, start, end)
    assert found["booked"] == sorted({made.order.number, held.number})
    assert found["booked_references"] == [
        order.reference for order in sorted((made.order, held), key=lambda o: o.number)
    ]
    assert found["refusal"] == "booked"
    refused = answer(call(made, "make", stretch(made, start, end)), 409)
    assert refused == {"error": "booked", "booked": found["booked"], "events": []}
    # Only the bookings holding time inside the stretch are named; a
    # cancelled one holds nothing.
    assert lay(made, local(day, 17), local(day, 19))["booked"] == [held.number]
    assert lay(made, local(day, 19), local(day, 22))["booked"] == []
    assert blocks_of(made) == 1


def test_a_block_in_the_way_is_blocked_already(made: made_up.MadeUp) -> None:
    # The made-up block is 14:00 to 15:00 UTC on 1 March: 15:00 to 16:00 local.
    found = lay(made, local(made.day, 14), local(made.day, 17))
    assert (found["booked"], found["blocked"], found["refusal"]) == (
        [],
        True,
        "blocked",
    )
    refused = answer(
        call(made, "make", stretch(made, local(made.day, 14), local(made.day, 17))), 409
    )
    assert refused == {"error": "blocked", "booked": [], "events": []}
    # The cells either side of it are free.
    assert lay(made, local(made.day, 17), local(made.day, 20))["refusal"] is None


def test_all_blocks_of_a_stretch_or_none(made: made_up.MadeUp) -> None:
    # Two pieces: 8 March is free, 15 March has a booking.
    booking = made_up.at(MARCH_15, 9)
    order = Order.objects.create(
        organization=made.organization, member=made.member, total=0
    )
    Booking.objects.take(
        organization=made.organization,
        asset=made.asset,
        booking_configuration=made.configuration,
        starts_at=booking,
        ends_at=booking + timedelta(hours=1),
        state=BookingState.CONFIRMED,
        order=order,
        member_id=order.member_id,
        booking_type=made.booking_type,
        booking_category=made.private,
        booking_type_name="Room hire",
        booking_category_name="private",
    )
    before = (blocks_of(made), TakenTime.objects.of(made.organization).count())
    data = stretch(made, local(MARCH_8, 10), local(MARCH_15, 22))
    assert answer(call(made, "make", data), 409)["error"] == "booked"
    assert (blocks_of(made), TakenTime.objects.of(made.organization).count()) == before
    assert (
        not AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.MADE_BLOCK)
        .exclude(pk=made.rows[AuditEvent].pk)
        .exists()
    )


def test_a_double_submit_makes_the_blocks_once(made: made_up.MadeUp) -> None:
    data = stretch(made, local(MARCH_8, 18), local(MARCH_15, 12), note="Painting")
    first = answer(call(made, "make", data))
    assert len(first["blocks"]) == 2
    again = answer(call(made, "make", data), 409)
    assert again["error"] == "blocked"
    assert blocks_of(made) == 3
    assert (
        AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.MADE_BLOCK)
        .count()
        == 3
    )


def test_a_failed_event_leaves_no_block(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = audit.record_event
    calls = 0

    def fail_on_the_second(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("no event")
        return real(**kwargs)

    monkeypatch.setattr("randirandi.core.block_admin.record_event", fail_on_the_second)
    data = stretch(made, local(MARCH_8, 18), local(MARCH_15, 12))
    with pytest.raises(RuntimeError, match="no event"):
        call(made, "make", data)
    assert blocks_of(made) == 1


# --- The race with a booking -------------------------------------------------------


def priced(made: made_up.MadeUp) -> None:
    """The container bookable as the made-up prices reach: up to four hours."""
    made.configuration.maximum_length = 4 * 60
    made.configuration.save()


def booking_request(made: made_up.MadeUp, day: date) -> BookingRequest:
    return BookingRequest(
        made.configuration.pk,
        made.private.pk,
        made_up.at(day, 9),
        made_up.at(day, 11),
    )


def test_a_booking_and_a_block_for_the_same_time_end_with_one(
    made: made_up.MadeUp,
) -> None:
    priced(made)
    with application_login():
        # The booking path works here: it takes a Monday no block asks for.
        create_order(
            member=made.member,
            requests=[booking_request(made, MARCH_22 + timedelta(days=28))],
            now=NOW - timedelta(days=30),
            accepted_version=made_up.accepted(made.member),
        )
    outcomes: list[tuple[str, bool]] = []
    for week, day in enumerate((MARCH_8, MARCH_15, MARCH_22)):
        ready = Barrier(2)

        def book(day: date = day, ready: Barrier = ready) -> bool:
            try:
                ready.wait(timeout=15)
                create_order(
                    member=made.member,
                    requests=[booking_request(made, day)],
                    now=NOW - timedelta(days=30),
                    accepted_version=made_up.accepted(made.member),
                )
                return True
            except OrderUnavailableError:
                return False
            finally:
                connection.close()

        def block(day: date = day, ready: Barrier = ready) -> bool:
            try:
                ready.wait(timeout=15)
                block_admin.make(
                    made.administrator,
                    made.asset.pk,
                    made_up.at(day, 9),
                    made_up.at(day, 11),
                    "",
                )
                return True
            except block_admin.BlockRefusedError:
                return False
            finally:
                connection.close()

        with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
            booked, blocked = pool.submit(book), pool.submit(block)
            results = (booked.result(timeout=60), blocked.result(timeout=60))
        outcomes.append(("booking" if results[0] else "block", sum(results) == 1))
        in_that_time = TakenTime.objects.of(made.organization).filter(
            asset=made.asset, starts_at=made_up.at(day, 9)
        )
        assert in_that_time.count() == 1, (week, results)
        assert in_that_time.get().state in {"blocked", "held", "confirmed"}
    assert all(one for _, one in outcomes)


def test_a_booking_that_lands_while_the_block_is_made_is_met_by_a_retry(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The booking commits after the block has laid its stretch: the database
    refuses the block's time, the block lays again and is refused as booked."""
    real = block_admin.lay
    calls = 0

    def lay_then_book(asset: Asset, start: datetime, end: datetime) -> Any:
        nonlocal calls
        laid = real(asset, start, end)
        calls += 1
        if calls == 1:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(book_directly).result(timeout=30)
        return laid

    # Made now: the order's number takes the organization's row, which the
    # block holds while it lays.
    order = Order.objects.create(
        organization=made.organization, member=made.member, total=0
    )

    def book_directly() -> None:
        try:
            Booking.objects.take(
                organization=made.organization,
                asset=made.asset,
                booking_configuration=made.configuration,
                starts_at=made_up.at(MARCH_15, 9),
                ends_at=made_up.at(MARCH_15, 11),
                state=BookingState.CONFIRMED,
                order=order,
                member_id=order.member_id,
                booking_type=made.booking_type,
                booking_category=made.private,
                booking_type_name="Room hire",
                booking_category_name="private",
            )
        finally:
            connection.close()

    monkeypatch.setattr(block_admin, "lay", lay_then_book)
    with application_login(), pytest.raises(block_admin.BlockRefusedError) as refused:
        block_admin.make(
            made.administrator,
            made.asset.pk,
            made_up.at(MARCH_15, 9),
            made_up.at(MARCH_15, 11),
            "",
        )
    assert refused.value.code == "booked"
    assert calls == 2
    assert blocks_of(made) == 1


# --- The members' calendar ---------------------------------------------------------


def calendar_of(made: made_up.MadeUp, day: date) -> Any:
    with application_login():
        found = _calendar(made, day)
    (container,) = found.containers
    (period,) = container.periods
    return period


def _calendar(made: made_up.MadeUp, day: date) -> Any:
    return member_calendar.member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        space_id=None,
        booking_type_id=None,
        booking_category_id=made.private.pk,
        quantity=1,
        length=60,
        start_date=day,
        end_date=day + timedelta(days=1),
        now=NOW - timedelta(days=30),
        cart=(),
    )


def free_rows(period: Any) -> set[int]:
    return {start.row for start in period.starts}


def test_a_blocks_time_is_not_free_to_members_and_is_after_its_removal(
    made: made_up.MadeUp,
) -> None:
    priced(made)
    before = calendar_of(made, MARCH_15)
    assert before.taken == ()
    assert free_rows(before) == set(range(12))
    # 12:00 to 14:00 local: rows 2 and 3 of the period that starts at 10:00.
    found = answer(
        call(made, "make", stretch(made, local(MARCH_15, 12), local(MARCH_15, 14)))
    )
    during = calendar_of(made, MARCH_15)
    assert [(t.first, t.last, t.state) for t in during.taken] == [(2, 3, "block")]
    assert free_rows(during) == set(range(12)) - {2, 3}
    (pk,) = found["blocks"]
    answer(call(made, "remove", block=pk))
    after = calendar_of(made, MARCH_15)
    assert after.taken == ()
    assert free_rows(after) == set(range(12))
