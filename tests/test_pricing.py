# SPDX-License-Identifier: AGPL-3.0-only
"""Configured prices, line snapshots and coherent capture, using fictional data."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, date, timedelta
from threading import Event
from typing import Any

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from randirandi.core import pricing
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    ConfigurationCategory,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Space,
    TakenTime,
)
from randirandi.core.pricing import BookingPrice, PriceUnavailableError, price_booking
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, Example, at, offer_on, scenario
from tests.test_calendar import made as made

pytestmark = pytest.mark.django_db(transaction=True)


def tiers(
    made: Example, rows: list[tuple[int, int, int]], *, by: str = "length"
) -> None:
    made.price.tiers_go_by = by
    made.price.save()
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).delete()
    for up_to, paid_per, amount in rows:
        PriceTier.objects.create(
            organization=made.org,
            price_book_entry=made.price,
            up_to=up_to,
            paid_per=paid_per,
            amount=amount,
        )


def quote(made: Example, **changes: Any) -> BookingPrice:
    parameters: dict[str, Any] = {
        "configuration": made.config,
        "booking_category": made.category,
        "starts_at": at(hour=10),
        "ends_at": at(hour=12),
    }
    parameters.update(changes)
    return price_booking(**parameters)


@pytest.mark.parametrize(
    ("hours", "total", "quantities"),
    [
        (1, 25000, [1]),
        (4, 25000, [1]),
        (5, 35000, [1, 1]),
        (8, 35000, [1, 1]),
        (12, 45000, [1, 2]),
    ],
)
def test_length_tiers_round_each_started_part_and_add(
    made: Example, hours: int, total: int, quantities: list[int]
) -> None:
    made.configure(period_end=1320, maximum_length=720)
    tiers(made, [(240, 240, 25000), (720, 240, 10000)])
    result = quote(made, ends_at=at(hour=10 + hours))
    assert result.total == total
    assert [line.quantity for line in result.lines] == quantities
    assert [line.up_to for line in result.lines] == [240, 720][: len(quantities)]
    assert all(line.capacity_quantity == 1 for line in result.lines)
    assert result.ends_at == at(hour=10 + hours)
    assert TakenTime.objects.of(made.org).count() == 0
    assert BookingLine.objects.of(made.org).count() == 0


@pytest.mark.parametrize(
    ("first", "last", "total", "quantities"),
    [
        (10, 14, 50000, [1]),
        (10, 17, 100000, [2]),
        (16, 18, 150000, [1, 1]),
        (18, 20, 100000, [1]),
        (10, 22, 200000, [2, 1]),
        (12, 17, 100000, [2]),
    ],
)
def test_clock_tiers_round_from_each_part_start(
    made: Example, first: int, last: int, total: int, quantities: list[int]
) -> None:
    made.configure(period_end=1320, maximum_length=720)
    tiers(made, [(1020, 240, 50000), (1320, 300, 100000)], by="time of day")
    result = quote(made, starts_at=at(hour=first), ends_at=at(hour=last))
    assert result.total == total
    assert [line.quantity for line in result.lines] == quantities


def test_partial_paid_units_use_integer_ceiling_and_exact_minor_units(
    made: Example,
) -> None:
    made.configure(interval=1, minimum_length=1)
    tiers(made, [(240, 60, 101)])
    assert quote(made, ends_at=at(hour=11)).total == 101
    assert quote(made, ends_at=at(hour=11, minute=1)).total == 202
    tiers(made, [(240, 1, 2_000_000_001)])
    assert quote(made, ends_at=at(hour=11, minute=1)).total == 122_000_000_061


def test_zero_price_keeps_a_real_line(made: Example) -> None:
    tiers(made, [(240, 60, 0)])
    result = quote(made)
    assert result.total == 0
    assert len(result.lines) == 1
    assert result.lines[0].amount == 0
    assert result.lines[0].quantity == 2


@pytest.mark.parametrize("basis", ["per_booking", "per_capacity_unit"])
def test_shared_requested_quantity_is_distinct_from_paid_time(
    made: Example, basis: str
) -> None:
    made.capacity(8)
    made.price.billing_basis = basis
    made.price.save()
    tiers(made, [(60, 60, 10000), (240, 60, 5000)])
    result = quote(made, quantity=3)
    multiplier = 3 if basis == "per_capacity_unit" else 1
    assert result.quantity == 3
    assert [line.quantity for line in result.lines] == [1, 1]
    assert [line.capacity_quantity for line in result.lines] == [multiplier, multiplier]
    assert result.total == 15000 * multiplier


def test_an_exclusive_container_is_priced_per_booking_never_per_spot(
    made: Example,
) -> None:
    made.capacity(8, mode="exclusive")
    tiers(made, [(240, 60, 100)])
    result = quote(made)
    assert (result.quantity, result.total) == (1, 200)
    assert (result.lines[0].capacity_quantity, result.lines[0].capacity_unit) == (
        1,
        "booking",
    )
    made.price.billing_basis = "per_capacity_unit"
    made.price.save()
    with pytest.raises(PriceUnavailableError, match="no supported price"):
        quote(made)


def test_a_price_per_place_counts_only_its_own_floor_plan(made: Example) -> None:
    space = Space.objects.create(organization=made.org, name="Room")
    room = Asset(organization=made.org, name="Entire room", space=space)
    room.whole_space = True
    room.save(floor_plan=AssetInventory(capacity=4, unit="place"))
    Asset(organization=made.org, name="Machines", space=space).save(
        floor_plan=AssetInventory(capacity=20, unit="machine")
    )
    made = offer_on(made, room, consumption_mode="shared")
    made.price.billing_basis = "per_capacity_unit"
    made.price.save()
    tiers(made, [(240, 60, 100)])
    line = quote(made, quantity=4).lines[0]
    assert (line.capacity_quantity, line.capacity_unit) == (4, "place")
    with pytest.raises(PriceUnavailableError, match="quantity"):
        quote(made, quantity=5)


@pytest.mark.parametrize(
    "change",
    ["missing", "empty", "short", "wrong_book", "unknown_kind", "unlinked", "disabled"],
)
def test_missing_or_unusable_price_never_becomes_free(
    made: Example, change: str
) -> None:
    if change == "missing":
        made.price.delete()
    elif change == "empty":
        tiers(made, [])
    elif change == "short":
        tiers(made, [(60, 60, 100)])
    elif change == "wrong_book":
        book = PriceBook.objects.create(organization=made.org, name="Different book")
        made.configure(price_book=book)
    elif change == "unknown_kind":
        PriceBookEntry.objects.of(made.org).filter(pk=made.price.pk).update(
            tiers_go_by="unknown"
        )
    elif change == "unlinked":
        ConfigurationCategory.objects.of(made.org).filter(
            booking_configuration=made.config
        ).delete()
    else:
        made.configure(status="disabled")
    with pytest.raises(PriceUnavailableError):
        quote(made)


def test_foreign_category_and_configuration_cannot_select_another_price(
    made: Example,
) -> None:
    other = made_up.everything("foreign-pricing")
    with pytest.raises(PriceUnavailableError):
        quote(made, booking_category=other.private)
    with pytest.raises(PriceUnavailableError):
        quote(made, configuration=other.configuration)


def test_quote_is_immutable_and_current_prices_are_reloaded(made: Example) -> None:
    first = quote(made)
    line: Any = first.lines[0]
    with pytest.raises(FrozenInstanceError):
        line.amount = 1
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(
        amount=400
    )
    BookingCategory.objects.of(made.org).filter(pk=made.category.pk).update(
        name="Renamed"
    )
    second = quote(made)
    assert first.total == 200
    assert second.total == 800
    assert first.booking_category_name == "Private"
    assert second.booking_category_name == "Renamed"
    assert first.currency == "NOK"


def test_line_snapshots_fit_existing_storage_and_survive_configuration_edits() -> None:
    rows = made_up.everything()
    config = rows.configuration
    PriceTier.objects.of(rows.organization).filter(
        price_book_entry__booking_configuration=config
    ).update(up_to=config.maximum_length)
    # The fixture's own held booking does not prevent calculating its price.
    result = price_booking(
        configuration=config,
        booking_category=rows.private,
        starts_at=rows.booking.taken_time.starts_at,
        ends_at=rows.booking.taken_time.ends_at,
    )
    lines = []
    for snapshot in result.lines:
        line = BookingLine(organization=rows.organization, booking=rows.booking)
        for name, value in asdict(snapshot).items():
            setattr(line, name, value)
        line.save()
        lines.append(line)
    original = sum(line.total for line in lines)
    PriceTier.objects.of(rows.organization).update(amount=99000)
    for line in lines:
        line.refresh_from_db()
    assert sum(line.total for line in lines) == original == result.total


def test_price_does_not_claim_membership_availability_or_free_capacity(
    made: Example,
) -> None:
    AvailabilityCalendar.objects.of(made.org).delete()
    made.claim(at(hour=10), at(hour=14))
    assert quote(made).total == 200
    assert len(made.offers()) == 0


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "1"])
def test_quantity_must_be_a_positive_integer(made: Example, quantity: Any) -> None:
    with pytest.raises(ValueError, match="positive whole"):
        quote(made, quantity=quantity)


def test_demand_must_fit_mode_and_capacity(made: Example) -> None:
    with pytest.raises(PriceUnavailableError, match="quantity"):
        quote(made, quantity=2)
    made.capacity(4)
    with pytest.raises(PriceUnavailableError, match="quantity"):
        quote(made, quantity=5)
    made_up.fewer_spots(made_up.floor_plan_of(made.asset), 2)
    with pytest.raises(PriceUnavailableError, match="quantity"):
        quote(made, quantity=3)


@pytest.mark.parametrize(("first", "last"), [(9, 12), (10, 15), (12, 12), (12, 11)])
def test_invalid_interval_is_refused(made: Example, first: int, last: int) -> None:
    with pytest.raises(ValueError):
        quote(made, starts_at=at(hour=first), ends_at=at(hour=last))


def test_off_grid_partial_minute_and_naive_moments_are_refused(made: Example) -> None:
    with pytest.raises(PriceUnavailableError):
        quote(made, starts_at=at(hour=10, minute=30), ends_at=at(hour=11, minute=30))
    with pytest.raises(PriceUnavailableError):
        quote(made, ends_at=at(hour=12) + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="time zone"):
        quote(made, starts_at=at(hour=10).replace(tzinfo=None))


def test_weekday_dates_and_overnight_anchor_match_calendar(made: Example) -> None:
    made.configure(
        period_start=1320,
        period_end=120,
        end_day=1,
        period_start_weekdays=[0],
        start_date=DAY,
        stop_date=DAY + timedelta(days=6),
    )
    result = quote(
        made,
        starts_at=at(DAY + timedelta(days=1), 0),
        ends_at=at(DAY + timedelta(days=1), 1),
    )
    assert result.total == 100
    with pytest.raises(PriceUnavailableError):
        quote(
            made,
            starts_at=at(DAY + timedelta(days=2), 0),
            ends_at=at(DAY + timedelta(days=2), 1),
        )
    # The next Monday is after the last calendar week.
    with pytest.raises(PriceUnavailableError):
        quote(
            made,
            starts_at=at(DAY + timedelta(days=8), 0),
            ends_at=at(DAY + timedelta(days=8), 1),
        )
    tiers(made, [(1440, 60, 100)], by="time of day")
    with pytest.raises(PriceUnavailableError):
        quote(made, starts_at=at(DAY, 22), ends_at=at(DAY, 23))


@pytest.mark.parametrize("day", [date(2027, 3, 27), date(2027, 10, 30)])
def test_fixed_night_is_priced_as_one_configured_period_across_clock_change(
    made: Example, day: date
) -> None:
    made.configure(period_type="fixed", period_start=1020, period_end=480, end_day=1)
    tiers(made, [(900, 900, 25000)])
    result = quote(made, starts_at=at(day, 17), ends_at=at(day + timedelta(days=1), 8))
    assert result.total == 25000
    assert result.lines[0].quantity == 1
    assert (result.ends_at - result.starts_at) != timedelta(hours=15)
    with pytest.raises(PriceUnavailableError):
        quote(made, starts_at=at(day, 17), ends_at=at(day + timedelta(days=1), 7))


@pytest.mark.parametrize("day", [date(2027, 3, 28), date(2027, 10, 31)])
def test_flexible_length_charges_elapsed_time_across_clock_change(
    made: Example, day: date
) -> None:
    made.configure(period_start=0, period_end=360, maximum_length=360)
    tiers(made, [(360, 60, 100)])
    result = quote(made, starts_at=at(day, 1), ends_at=at(day, 4))
    assert result.lines[0].quantity == (2 if day.month == 3 else 4)


def test_flexible_clock_tiers_follow_repeated_local_time_and_round_each_part(
    made: Example,
) -> None:
    day = date(2027, 10, 31)
    made.configure(period_start=0, period_end=360, interval=30, maximum_length=360)
    tiers(made, [(150, 60, 100), (360, 60, 200)], by="time of day")
    result = quote(made, starts_at=at(day, 1, 30), ends_at=at(day, 3, 30))
    # 01:30-02:30 cheap (60m), first 02:30-clock rollback expensive (30m),
    # repeated 02:00-02:30 cheap (30m), then 02:30-03:30 expensive (60m).
    assert [(line.up_to, line.quantity, line.amount) for line in result.lines] == [
        (150, 1, 100),
        (360, 1, 200),
        (150, 1, 100),
        (360, 1, 200),
    ]
    assert result.total == 600


def test_spring_clock_tiers_charge_only_elapsed_parts(made: Example) -> None:
    day = date(2027, 3, 28)
    made.configure(period_start=0, period_end=360, interval=30, maximum_length=360)
    tiers(made, [(150, 60, 100), (180, 60, 9999), (360, 60, 200)], by="time of day")
    result = quote(made, starts_at=at(day, 1, 30), ends_at=at(day, 3, 30))
    assert [(line.up_to, line.quantity) for line in result.lines] == [
        (150, 1),
        (360, 1),
    ]
    assert result.total == 300


@pytest.mark.parametrize("day", [date(2027, 3, 28), date(2027, 10, 31)])
def test_fixed_clock_price_keeps_nominal_period_and_flexible_quotes_match_calendar(
    made: Example, day: date
) -> None:
    made.configure(period_type="fixed", period_start=60, period_end=240)
    tiers(made, [(240, 180, 300)], by="time of day")
    result = quote(made, starts_at=at(day, 1), ends_at=at(day, 4))
    assert result.total == 300
    made.configure(
        period_type="flexible",
        interval=30,
        minimum_length=30,
        maximum_length=180,
    )
    for offer in made.offers(day):
        result = quote(made, starts_at=offer.starts_at, ends_at=offer.ends_at)
        assert result.starts_at == offer.starts_at
        assert result.ends_at == offer.ends_at
        assert result.total > 0


@pytest.mark.parametrize("day", [date(2027, 3, 28), date(2027, 10, 31)])
def test_fixed_ambiguous_or_missing_start_is_not_given_a_price(
    made: Example, day: date
) -> None:
    made.configure(period_type="fixed", period_start=150, period_end=240)
    tiers(made, [(90, 90, 100)])
    with pytest.raises(PriceUnavailableError):
        quote(made, starts_at=at(day, 2, 30), ends_at=at(day, 4))


# The beds are places from Sunday to Thursday night, the room is hired whole
# on Friday and Saturday night.
@pytest.mark.parametrize(
    ("number", "quantity", "total", "days"),
    [(1, 3, 30000, 0), (2, 1, 40000, 4)],
)
def test_four_beds_price_places_and_the_whole_room(
    number: int, quantity: int, total: int, days: int
) -> None:
    org, _member = scenario("scenarios/four-beds.toml")
    day = DAY + timedelta(days=days)
    result = price_booking(
        configuration=BookingConfiguration.objects.of(org).get(number=number),
        booking_category=BookingCategory.objects.of(org).get(name="private"),
        starts_at=at(day, 17),
        ends_at=at(day + timedelta(days=1), 8),
        quantity=quantity,
    )
    assert result.total == total
    assert result.lines[0].quantity == 1


@pytest.mark.parametrize("manual", [False, True])
def test_quote_refuses_caller_owned_transactions(made: Example, manual: bool) -> None:
    if manual:
        transaction.set_autocommit(False)
        try:
            with pytest.raises(RuntimeError, match="own read-only"):
                quote(made)
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
    else:
        with transaction.atomic(), pytest.raises(RuntimeError, match="own read-only"):
            quote(made)


def test_captured_calculator_needs_no_queries_and_can_run_in_order_transaction(
    made: Example,
) -> None:
    with transaction.atomic():
        data = pricing._read(made.config, made.category)
        with CaptureQueriesContext(connection) as queries:
            result = pricing._calculate(data, at(hour=10), at(hour=12), 1)
        assert len(queries) == 0
        assert result.total == 200


def test_application_login_quote_releases_read_only_state(made: Example) -> None:
    with application_login():
        assert quote(made).total == 200
        PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(
            amount=200
        )
        assert quote(made).total == 400


def test_quote_is_coherent_during_concurrent_capacity_and_price_changes(
    made: Example,
) -> None:
    made.capacity(8)
    made.price.billing_basis = "per_capacity_unit"
    made.price.save()
    started, changed = Event(), Event()

    def read() -> BookingPrice:
        def pause(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
            result = execute(sql, params, many, context)
            if 'FROM "core_bookingconfiguration"' in sql and not started.is_set():
                started.set()
                assert changed.wait(10)
            return result

        try:
            with connection.execute_wrapper(pause):
                return quote(made, quantity=8)
        finally:
            connection.close()

    def edit() -> None:
        try:
            assert started.wait(10)
            with transaction.atomic():
                made_up.fewer_spots(made_up.floor_plan_of(made.asset), 4)
                PriceTier.objects.of(made.org).filter(
                    price_book_entry=made.price
                ).update(amount=300)
            changed.set()
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(read)
        editing = pool.submit(edit)
        old = reading.result(timeout=20)
        editing.result(timeout=20)
        fresh = quote(made, quantity=4)
    assert old.total == 8 * 2 * 100
    assert fresh.total == 4 * 2 * 300


@pytest.mark.parametrize(
    (
        "filename",
        "number",
        "category_name",
        "first",
        "last",
        "days",
        "quantity",
        "amount",
    ),
    [
        ("fjordside.toml", 2, "Work", 10, 22, 0, 1, 240000),
        ("fjordside.toml", 2, "Private", 10, 22, 0, 1, 50000),
        ("fjordside.toml", 3, "Private", 14, 22, 5, 1, 70000),
        ("scenarios/sauna-palace.toml", 1, "private", 12, 14, 0, 3, 45000),
    ],
)
def test_loaded_fixture_prices_use_the_same_general_function(
    filename: str,
    number: int,
    category_name: str,
    first: int,
    last: int,
    days: int,
    quantity: int,
    amount: int,
) -> None:
    org, _member = scenario(filename)
    config = BookingConfiguration.objects.of(org).get(number=number)
    category = BookingCategory.objects.of(org).get(name=category_name)
    day = DAY + timedelta(days=days)
    result = price_booking(
        configuration=config,
        booking_category=category,
        starts_at=at(day, first),
        ends_at=at(day, last),
        quantity=quantity,
    )
    assert result.total == amount
    assert result.starts_at.tzinfo == UTC


@pytest.mark.parametrize("fixed", [False, True])
def test_invalid_clock_bound_is_rejected_by_model_calendar_and_quote(
    made: Example, fixed: bool
) -> None:
    from django.core.exceptions import ValidationError

    if fixed:
        made.configure(period_type="fixed", interval=240, minimum_length=240)
    tiers(made, [(1500, 60, 100)], by="time of day")
    tier = PriceTier.objects.of(made.org).get(price_book_entry=made.price)
    with pytest.raises(ValidationError, match="24:00"):
        tier.full_clean()
    assert "24:00" in (made.price.what_its_tiers_leave_out() or "")
    assert made.offers() == ()
    with pytest.raises(PriceUnavailableError, match="tiers"):
        quote(made, ends_at=at(hour=14))
    # Midnight itself is a valid exclusive endpoint. Length tiers may
    # exceed a day; their unit is duration, not a time on the clock.
    tier.up_to = 1440
    tier.save()
    tier.full_clean()
    assert made.price.what_its_tiers_leave_out() is None
    assert made.offers()
    assert quote(made, ends_at=at(hour=14)).total == 400
    tiers(made, [(1500, 60, 100)])
    PriceTier.objects.of(made.org).get(price_book_entry=made.price).full_clean()
    assert made.offers()
    assert quote(made, ends_at=at(hour=14)).total == 400


@pytest.mark.parametrize("entry_id", [2147483647, None, "bad"])
def test_clock_tier_validation_preserves_missing_entry_field_error(
    made: Example,
    entry_id: Any,
) -> None:
    from django.core.exceptions import ValidationError

    tier = PriceTier(
        organization=made.org,
        price_book_entry_id=entry_id,
        up_to=60,
        paid_per=60,
        amount=100,
    )
    with pytest.raises(ValidationError) as refused:
        tier.full_clean()
    assert "price_book_entry" in refused.value.message_dict


@pytest.mark.parametrize("invalid", [None, "bad"])
def test_clock_tier_validation_preserves_invalid_scalar_field_error(
    made: Example,
    invalid: Any,
) -> None:
    from django.core.exceptions import ValidationError

    made.price.tiers_go_by = "time of day"
    made.price.save()
    tier = PriceTier(
        organization=made.org,
        price_book_entry=made.price,
        paid_per=60,
        amount=100,
    )
    tier.up_to = invalid
    with pytest.raises(ValidationError) as refused:
        tier.full_clean()
    assert "up_to" in refused.value.message_dict
