# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic checkout rules and real PostgreSQL races with fictional members."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from randirandi.core import ordering
from randirandi.core.booking_quote import Choice, cart_quotes
from randirandi.core.models import (
    Asset,
    Block,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    CapacityAllocation,
    Member,
    Order,
    Payment,
    PriceTier,
    RoleCategory,
    Space,
    TakenTime,
)
from randirandi.core.ordering import (
    BookingRequest,
    OrderContendedError,
    OrderLimitError,
    OrderUnavailableError,
    create_order,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, Example, at, offer_on
from tests.test_calendar import made as made
from tests.test_pricing import tiers

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))


def request(made: Example, **changes: Any) -> BookingRequest:
    return replace(
        BookingRequest(made.config.pk, made.category.pk, at(hour=10), at(hour=12)),
        **changes,
    )


def checkout(made: Example, *requests: BookingRequest) -> Order:
    return create_order(
        member=made.member,
        requests=requests or [request(made)],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )


def assert_empty(made: Example) -> None:
    for model in (Order, Booking, BookingLine, TakenTime, CapacityAllocation):
        assert model.objects.of(made.org).count() == 0
    made.org.refresh_from_db()
    assert made.org.last_order_number == 0


def test_paid_checkout_persists_complete_current_snapshots_as_application(
    made: Example,
) -> None:
    made.capacity(8)
    made.price.billing_basis = "per_capacity_unit"
    made.price.save()
    tiers(made, [(60, 60, 10000), (240, 60, 5000)])
    with application_login():
        order = checkout(made, request(made, quantity=3))
        booking = order.bookings.select_related("taken_time").get()
        assert order.total == 45000
        assert order.member_id == booking.member_id == made.member.pk
        assert order.number == 1
        assert order.created_at == NOW
        assert order.hold_expires_at == NOW + timedelta(minutes=10)
        assert booking.taken_time.state == "held"
        assert booking.taken_time.quantity == 3
        assert booking.taken_time.allocations.count() == 3
        assert booking.booking_type_name == made.kind.name
        assert booking.booking_category_name == made.category.name
        lines = list(booking.lines.order_by("pk"))
        assert [line.total for line in lines] == [30000, 15000]
        assert all(line.capacity_quantity == 3 for line in lines)
        assert all(line.configuration_number == made.config.number for line in lines)
        PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(
            amount=1
        )
        assert sum(line.total for line in booking.lines.all()) == order.total
        assert Payment.objects.of(made.org).count() == 0


def test_zero_total_confirms_with_real_lines_and_no_payment(made: Example) -> None:
    tiers(made, [(240, 60, 0)])
    order = checkout(made)
    booking = order.bookings.select_related("taken_time").get()
    assert order.total == 0 and order.hold_expires_at is None
    assert booking.taken_time.state == "confirmed"
    assert booking.lines.count() == 1
    assert booking.lines.get().amount == 0
    assert Payment.objects.of(made.org).count() == 0


def test_last_cart_request_failure_rolls_back_prior_bookings_lines_and_number(
    made: Example,
) -> None:
    with pytest.raises(OrderUnavailableError, match="capacity"):
        checkout(made, request(made), request(made, starts_at=at(hour=11)))
    assert_empty(made)
    assert checkout(made).number == 1


def test_adjacent_cart_intervals_both_succeed(made: Example) -> None:
    order = checkout(
        made, request(made), request(made, starts_at=at(hour=12), ends_at=at(hour=14))
    )
    assert order.total == 400 and order.bookings.count() == 2


@pytest.mark.parametrize(
    "fault",
    ["revoked", "role", "disabled", "category", "price", "closed", "past", "grid"],
)
def test_current_rules_are_rechecked_before_any_hold(made: Example, fault: str) -> None:
    wanted = request(made)
    if fault == "revoked":
        Member.objects.of(made.org).filter(pk=made.member.pk).update(status="disabled")
    elif fault == "role":
        RoleCategory.objects.of(made.org).all().delete()
    elif fault == "disabled":
        BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
            status="disabled"
        )
    elif fault == "category":
        extra = BookingCategory.objects.create(organization=made.org, name="Other")
        wanted = replace(wanted, booking_category_id=extra.pk)
    elif fault == "price":
        PriceTier.objects.of(made.org).all().delete()
    elif fault == "closed":
        made.available([(600, 660), (661, 840)])
    elif fault == "past":
        wanted = replace(
            wanted,
            starts_at=at(DAY - timedelta(days=2), hour=10),
            ends_at=at(DAY - timedelta(days=2), hour=12),
        )
    else:
        wanted = replace(wanted, starts_at=at(hour=10, minute=1))
    with pytest.raises(OrderUnavailableError):
        checkout(made, wanted)
    assert_empty(made)


@pytest.mark.parametrize("foreign", ["configuration", "category", "member"])
def test_foreign_identifiers_never_authorize_a_checkout(
    made: Example, foreign: str
) -> None:
    other = made_up.everything("other")
    wanted = request(made)
    member = made.member
    if foreign == "configuration":
        wanted = replace(wanted, configuration_id=other.configuration.pk)
    elif foreign == "category":
        wanted = replace(wanted, booking_category_id=other.private.pk)
    else:
        member = other.member
    with pytest.raises(OrderUnavailableError):
        create_order(
            member=member,
            requests=[wanted],
            now=NOW,
            accepted_version=made_up.accepted(member),
        )
    assert_empty(made)


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_invalid_quantities_leave_nothing(made: Example, quantity: Any) -> None:
    with pytest.raises(ValueError):
        checkout(made, request(made, quantity=quantity))
    assert_empty(made)


def test_order_bounds_and_amount_range_leave_nothing(made: Example) -> None:
    with pytest.raises(OrderLimitError):
        create_order(
            member=made.member,
            requests=[],
            now=NOW,
            accepted_version=made_up.accepted(made.member),
        )
    with pytest.raises(OrderLimitError):
        checkout(made, *([request(made)] * 51))
    tiers(made, [(240, 1, 2147483647)])
    with pytest.raises(OrderLimitError, match="amount"):
        checkout(made)
    assert_empty(made)


def test_dense_calendar_grid_does_not_prevent_one_valid_checkout(made: Example) -> None:
    made.configure(interval=1, minimum_length=1)
    order = checkout(made)
    assert order.total == 200


def numbers(order: Order) -> list[int | None]:
    """The spot numbers of the order's one booking, by ordinal."""
    return numbers_of(order.bookings.get())


def numbers_of(booking: Booking) -> list[int | None]:
    rows = booking.taken_time.allocations.order_by("ordinal")
    return [row.spot.number if row.spot else None for row in rows]


def test_a_booking_keeps_one_spot_for_its_whole_time(made: Example) -> None:
    made.capacity(2)
    made.claim(at(hour=10), at(hour=11), mode="shared", units=(1,))
    made.claim(at(hour=11), at(hour=12), mode="shared", units=(2,))
    # Each spot is free for one of the two hours: no spot is free for both,
    # and a booking never moves from one to another.
    with pytest.raises(OrderUnavailableError, match="no longer available"):
        checkout(made)
    assert Order.objects.of(made.org).count() == 0
    made.capacity(3)
    order = checkout(made)
    (row,) = order.bookings.get().taken_time.allocations.all()
    assert row.spot is not None
    assert (row.ordinal, row.spot.number, row.starts_at, row.ends_at) == (
        1,
        3,
        at(hour=10),
        at(hour=12),
    )


def test_the_lowest_free_spots_are_taken_without_a_pick(made: Example) -> None:
    made.capacity(4)
    made.claim(at(hour=10), at(hour=12), mode="shared", units=(1, 3))
    assert numbers(checkout(made, request(made, quantity=2))) == [2, 4]
    with pytest.raises(OrderUnavailableError, match="no longer available"):
        checkout(made)


def test_a_picked_spot_is_taken_and_a_taken_one_is_refused(made: Example) -> None:
    made.capacity(4)
    made.claim(at(hour=10), at(hour=12), mode="shared", units=(1,))
    assert numbers(checkout(made, request(made, quantity=2, spots=(4, 2)))) == [4, 2]
    for taken in (1, 4):
        with pytest.raises(OrderUnavailableError, match="picked spot is not free"):
            checkout(made, request(made, spots=(taken,)))
    with pytest.raises(OrderUnavailableError, match="picked spot is not free"):
        checkout(made, request(made, spots=(9,)))
    with pytest.raises(ValueError, match="different spot numbers"):
        checkout(made, request(made, quantity=2, spots=(3, 3)))
    with pytest.raises(ValueError, match="different spot numbers"):
        checkout(made, request(made, quantity=2, spots=(3,)))
    assert Order.objects.of(made.org).count() == 1


def test_an_exclusive_booking_takes_no_pick(made: Example) -> None:
    with pytest.raises(OrderUnavailableError, match="never a spot"):
        checkout(made, request(made, spots=(1,)))
    assert_empty(made)


@pytest.mark.parametrize("tuesday_taken", [False, True])
def test_the_next_date_period_in_the_cart_keeps_the_spot_when_it_is_free(
    made: Example, tuesday_taken: bool
) -> None:
    made.capacity(3)
    tuesday = DAY + timedelta(days=1)
    made.claim(at(hour=10), at(hour=12), mode="shared", units=(1,))
    if tuesday_taken:
        made.claim(at(tuesday, 10), at(tuesday, 12), mode="shared", units=(2,))
    # Given in the other order: an order takes its bookings by start (W18).
    order = checkout(
        made,
        request(made, starts_at=at(tuesday, 10), ends_at=at(tuesday, 12)),
        request(made),
    )
    by_start = {
        booking.taken_time.starts_at: numbers_of(booking)[0]
        for booking in order.bookings.select_related("taken_time")
    }
    # Monday's booking takes spot 2, the lowest free; Tuesday's keeps it
    # while it is free, though spot 1 is free then, and else takes the lowest.
    assert by_start == {at(hour=10): 2, at(tuesday, 10): 1 if tuesday_taken else 2}


def test_keeping_the_spot_gives_way_when_it_would_refuse_a_cart_that_fits(
    made: Example,
) -> None:
    made.capacity(2)
    tuesday = DAY + timedelta(days=1)
    made.claim(at(hour=10), at(hour=12), mode="shared", units=(1,))
    made.claim(at(tuesday, 13), at(tuesday, 15), mode="shared", units=(1,))
    times = [
        (at(hour=10), at(hour=12)),
        (at(tuesday, 10), at(tuesday, 12)),
        (at(tuesday, 11), at(tuesday, 14)),
    ]
    # Keeping Monday's spot 2 on Tuesday 10-12 would leave 11-14 no spot;
    # lowest first, Tuesday 10-12 takes spot 1 and 11-14 spot 2.
    quotes = cart_quotes(
        member=made.member,
        cart=[
            Choice(made.config.pk, made.category.pk, 1, start, end)
            for start, end in times
        ],
        now=NOW,
    )
    assert [quote.reason for quote in quotes] == [None, None, None]
    order = checkout(
        made,
        *(request(made, starts_at=start, ends_at=end) for start, end in times),
    )
    by_start = {
        booking.taken_time.starts_at: numbers_of(booking)[0]
        for booking in order.bookings.select_related("taken_time")
    }
    assert by_start == {
        start: number for (start, _), number in zip(times, [2, 1, 2], strict=True)
    }


def test_independent_machines_share_space_but_whole_room_takes_all(
    made: Example,
) -> None:
    room = Space.objects.create(organization=made.org, name="Laundry")
    made.asset.space = room
    made.asset.save()
    machine = offer_on(
        made, Asset.objects.create(organization=made.org, name="Machine 2", space=room)
    )
    order = checkout(made, request(made), request(machine))
    assert order.bookings.count() == 2
    # The whole room stacks with both machines : its container
    # is saved, and the database rule keeps the bookings apart.
    whole = offer_on(
        made,
        Asset.objects.create(
            organization=made.org, name="Whole room", space=room, whole_space=True
        ),
    )
    with pytest.raises(OrderUnavailableError):
        checkout(whole, request(whole))
    free = request(whole, starts_at=at(hour=12), ends_at=at(hour=14))
    assert checkout(whole, free).bookings.count() == 1
    # And a machine over the booked room hour is refused the same way.
    with pytest.raises(OrderUnavailableError):
        checkout(made, request(made, starts_at=at(hour=12), ends_at=at(hour=14)))


def test_deadline_alone_does_not_release_but_explicit_state_does(made: Example) -> None:
    first = checkout(made)
    first.hold_expires_at = NOW - timedelta(minutes=1)
    first.save()
    with pytest.raises(OrderUnavailableError):
        checkout(made)
    taken = first.bookings.get().taken_time
    taken.state = "expired"
    taken.save()
    assert checkout(made).number == 2


@pytest.mark.parametrize("manual", [False, True])
def test_caller_transaction_is_refused_without_losing_pending_writes(
    made: Example, manual: bool
) -> None:
    if manual:
        connection.set_autocommit(False)
        try:
            Member.objects.of(made.org).filter(pk=made.member.pk).update(
                first_name="Pending"
            )
            with pytest.raises(RuntimeError, match="own transaction"):
                checkout(made)
            assert (
                Member.objects.of(made.org).get(pk=made.member.pk).first_name
                == "Pending"
            )
        finally:
            connection.rollback()
            connection.set_autocommit(True)
    else:
        with transaction.atomic(), pytest.raises(RuntimeError, match="own transaction"):
            checkout(made)
    assert_empty(made)


@pytest.mark.parametrize(("capacity", "winners"), [(1, 1), (2, 2), (4, 4)])
def test_four_simultaneous_application_checkouts_fit_capacity(
    made: Example, capacity: int, winners: int
) -> None:
    made.capacity(capacity)
    ready = Barrier(4)

    def run() -> int | None:
        try:
            ready.wait(timeout=15)
            return checkout(made).pk
        except OrderUnavailableError:
            return None
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run(), range(4)))
        assert sum(value is not None for value in results) == winners
        assert Order.objects.of(made.org).count() == winners
        assert TakenTime.objects.of(made.org).count() == winners
        assert CapacityAllocation.objects.of(made.org).count() == winners
        assert sorted(
            Order.objects.of(made.org).values_list("number", flat=True)
        ) == list(range(1, winners + 1))


def test_native_exclusion_retries_and_selects_another_spot(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    made.capacity(2)
    captured, edited = Event(), Event()
    original = ordering._take
    states: list[str | None] = []
    retryable = ordering._retryable
    calls = 0

    def take(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            captured.set()
            assert edited.wait(timeout=15)
        return result

    def record(error: Any) -> bool:
        states.append(error.__cause__.sqlstate)
        return retryable(error)

    def edit() -> None:
        try:
            assert captured.wait(timeout=15)
            made.claim(at(hour=10), at(hour=12), mode="shared", units=(1,))
        finally:
            edited.set()
            connection.close()

    monkeypatch.setattr(ordering, "_take", take)
    monkeypatch.setattr(ordering, "_retryable", record)
    with application_login(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(edit)
        order = checkout(made)
        future.result(timeout=15)
        assert numbers(order) == [2]
        assert "23P01" in states
        assert order.number == 1
        assert Order.objects.of(made.org).count() == 1


def test_five_attempts_that_meet_other_orders_are_contended_not_taken(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    made.capacity(2)
    tries = 0

    def contended(*args: Any, **kwargs: Any) -> Order:
        nonlocal tries
        tries += 1
        raise IntegrityError("another order at the same moment")

    monkeypatch.setattr(ordering, "_attempt", contended)
    monkeypatch.setattr(ordering, "_retryable", lambda error: True)
    with application_login(), pytest.raises(OrderContendedError):
        checkout(made)
    assert tries == ordering.MAX_ATTEMPTS
    assert_empty(made)


def race(
    made: Example,
    first: BookingRequest,
    second: BookingRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Order | OrderUnavailableError], list[str | None]]:
    """Two orders at once: the first has its spots and waits until the
    second waits for it, then both go on. What each ended in, and the
    database's answers that made an order try again."""
    inside = Event()
    original = ordering._take
    retryable = ordering._retryable
    states: list[str | None] = []

    def take(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if not inside.is_set():
            inside.set()
            deadline = time.monotonic() + 10
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)"
                    )
                    if cursor.fetchone()[0]:
                        break
                assert time.monotonic() < deadline
                time.sleep(0.01)
        return result

    def record(error: Any) -> bool:
        states.append(error.__cause__.sqlstate)
        return retryable(error)

    def order(wanted: BookingRequest, after: Event | None) -> Any:
        try:
            if after is not None:
                assert after.wait(timeout=15)
            return checkout(made, wanted)
        except OrderUnavailableError as error:
            return error
        finally:
            connection.close()

    monkeypatch.setattr(ordering, "_take", take)
    monkeypatch.setattr(ordering, "_retryable", record)
    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        ends = [pool.submit(order, first, None), pool.submit(order, second, inside)]
        return [end.result(timeout=30) for end in ends], states


def test_two_orders_for_the_same_lowest_spot_both_get_one_after_a_retry(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two orders never reach the rule against double booking (23P01): both
    take the organization row before they read a claim (the order's lock,
    and the order number on the same row), so the second waits there and
    fails to serialise (40001). An order meets the rule only beside a writer
    that does not take that row, as a block does (the test after these)."""
    made.capacity(2)
    ends, states = race(made, request(made), request(made), monkeypatch)
    assert all(isinstance(end, Order) for end in ends)
    assert [numbers(end) for end in ends if isinstance(end, Order)] == [[1], [2]]
    assert states == ["40001"]


def test_two_orders_for_one_picked_spot_end_in_one_order_and_one_refusal(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As above, the second order waits on the organization row and retries
    (40001), never at the rule (23P01); fresh, it finds the pick taken."""
    made.capacity(2)
    picked = request(made, spots=(1,))
    (first, second), states = race(made, picked, picked, monkeypatch)
    assert isinstance(first, Order) and numbers(first) == [1]
    assert isinstance(second, OrderUnavailableError)
    assert "picked spot is not free" in str(second)
    assert states == ["40001"]
    assert Order.objects.of(made.org).count() == 1


def test_an_order_and_a_block_at_once_meet_in_the_rule_and_the_order_retries(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block takes no organization row, so it can take the spot an order
    has chosen and not yet written: the order's write meets the rule
    (23P01) and the order tries again, on the next spot."""
    made.capacity(2)
    chosen, blocked = Event(), Event()
    original = ordering._take
    retryable = ordering._retryable
    states: list[str | None] = []

    def take(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if not chosen.is_set():
            chosen.set()
            assert blocked.wait(timeout=15)
        return result

    def record(error: Any) -> bool:
        states.append(error.__cause__.sqlstate)
        return retryable(error)

    def order() -> Any:
        try:
            return checkout(made)
        finally:
            connection.close()

    def block() -> None:
        try:
            assert chosen.wait(timeout=15)
            Block.objects.take(
                organization=made.org,
                asset=made.asset,
                booking_configuration=made.config,
                starts_at=at(hour=10),
                ends_at=at(hour=12),
                units=made_up.spots_of(made_up.floor_plan_of(made.asset), 1),
                made_by=made.member,
            )
        finally:
            blocked.set()
            connection.close()

    monkeypatch.setattr(ordering, "_take", take)
    monkeypatch.setattr(ordering, "_retryable", record)
    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        ordered, blocking = pool.submit(order), pool.submit(block)
        blocking.result(timeout=30)
        made_order = ordered.result(timeout=30)
    assert states == ["23P01"]
    assert numbers(made_order) == [2]


def test_unrelated_constraint_failure_is_not_hidden_and_rolls_everything_back(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = BookingLine.save

    def invalid(line: BookingLine, *args: Any, **kwargs: Any) -> None:
        line.quantity = 0
        save(line, *args, **kwargs)

    monkeypatch.setattr(BookingLine, "save", invalid)
    with pytest.raises(IntegrityError, match="quantity_above_zero"):
        checkout(made)
    assert_empty(made)


def test_multi_day_opening_is_required_on_every_occupied_day(made: Example) -> None:
    made.configure(
        period_type="fixed",
        period_start=1020,
        period_end=480,
        end_day=1,
        interval=900,
        minimum_length=900,
        maximum_length=900,
    )
    tiers(made, [(900, 900, 100)])
    wanted = request(
        made, starts_at=at(hour=17), ends_at=at(DAY + timedelta(days=1), hour=8)
    )
    made.available([(1020, 1440)], weekday=0)
    made.available([(0, 479)], weekday=1)
    with pytest.raises(OrderUnavailableError):
        checkout(made, wanted)
    assert_empty(made)
    made.available([(0, 480)], weekday=1)
    assert checkout(made, wanted).total == 100


@pytest.mark.parametrize("fault", ["naive", "reverse", "bool_id", "naive_now"])
def test_invalid_moments_and_ids_leave_nothing(made: Example, fault: str) -> None:
    wanted = request(made)
    now = NOW
    if fault == "naive":
        wanted = replace(wanted, starts_at=wanted.starts_at.replace(tzinfo=None))
    elif fault == "reverse":
        wanted = replace(wanted, ends_at=wanted.starts_at)
    elif fault == "bool_id":
        wanted = replace(wanted, configuration_id=True)
    else:
        now = now.replace(tzinfo=None)
    with pytest.raises(ValueError):
        create_order(
            member=made.member,
            requests=[wanted],
            now=now,
            accepted_version=made_up.accepted(made.member),
        )
    assert_empty(made)


def test_concurrent_price_and_capacity_edit_never_mixes_snapshots(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    made.capacity(8)
    made.price.billing_basis = "per_capacity_unit"
    made.price.save()
    captured, edited = Event(), Event()
    original = ordering._capture
    calls = 0

    def capture(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            captured.set()
            assert edited.wait(timeout=15)
        return result

    def edit() -> None:
        try:
            assert captured.wait(timeout=15)
            with transaction.atomic():
                made_up.fewer_spots(made_up.floor_plan_of(made.asset), 4)
                PriceTier.objects.of(made.org).filter(
                    price_book_entry=made.price
                ).update(amount=300)
        finally:
            edited.set()
            connection.close()

    monkeypatch.setattr(ordering, "_capture", capture)
    with application_login(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(edit)
        try:
            order: Order | None = checkout(made, request(made, quantity=6))
        except OrderUnavailableError:
            order = None
        future.result(timeout=15)
    # Six places at the old price, or none against the new four: never the
    # new price on the old places. The spots it would hold are gone, so it
    # tries again on the new ones.
    if order is None:
        assert calls == 2
        assert Order.objects.of(made.org).count() == 0
    else:
        line = order.bookings.get().lines.get()
        assert (line.amount, line.capacity_quantity, order.total) == (100, 6, 1200)


def test_native_conflicts_exhaust_retries_without_partial_order(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    made.capacity(6)
    original = ordering._take
    calls = 0

    def take(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1

        def steal() -> None:
            try:
                made.claim(at(hour=10), at(hour=12), mode="shared", units=(calls,))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(steal).result(timeout=15)
        return result

    monkeypatch.setattr(ordering, "_take", take)
    with (
        application_login(),
        pytest.raises(OrderUnavailableError, match="concurrent changes"),
    ):
        checkout(made)
    assert calls == 5
    assert Order.objects.of(made.org).count() == 0
    assert Booking.objects.of(made.org).count() == 0
    assert BookingLine.objects.of(made.org).count() == 0
    assert TakenTime.objects.of(made.org).count() == 5  # Only the competing writer.
    made.org.refresh_from_db()
    assert made.org.last_order_number == 0


def test_real_deadlock_is_retried_without_duplicate_order(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_locked, service_inserting = Event(), Event()
    original = ordering._take
    retryable = ordering._retryable
    states: list[str | None] = []
    service_pid = 0
    config = connection.settings_dict.copy()
    assert config["HOST"] in ("localhost", "127.0.0.1")
    assert config["NAME"].startswith("test_")

    def take(*args: Any, **kwargs: Any) -> Any:
        nonlocal service_pid
        result = original(*args, **kwargs)
        service_pid = connection.connection.info.backend_pid
        service_inserting.set()
        return result

    def record(error: Any) -> bool:
        states.append(error.__cause__.sqlstate)
        return retryable(error)

    def editor() -> None:
        try:
            # Give this competing test-only writer a longer detector timeout.
            # Otherwise either participant may be chosen as the deadlock victim,
            # and aborting the editor never exercises the checkout's retry.
            # Checkout still uses the real restricted application login.
            with psycopg.connect(
                host=config["HOST"],
                port=config["PORT"],
                dbname=config["NAME"],
                user=config["USER"],
                password=config["PASSWORD"],
            ) as editor_connection:
                editor_connection.execute("SET LOCAL deadlock_timeout = '10s'")
                editor_connection.execute(
                    "SELECT id FROM core_asset "
                    "WHERE organization_id=%s AND id=%s FOR UPDATE",
                    (made.org.pk, made.asset.pk),
                )
                asset_locked.set()
                assert service_inserting.wait(timeout=15)
                # Let the checkout wait on this asset first. The editor then
                # waits on its organization: a real two-connection lock cycle.
                waiting = False
                for _ in range(500):
                    editor_connection.execute("SELECT pg_stat_clear_snapshot()")
                    row = editor_connection.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (service_pid,),
                    ).fetchone()
                    if row and row[0] == "Lock":
                        waiting = True
                        break
                    Event().wait(0.01)
                assert waiting
                editor_connection.execute(
                    "UPDATE core_organization SET last_order_number = "
                    "last_order_number WHERE id=%s",
                    (made.org.pk,),
                )
        finally:
            asset_locked.set()

    monkeypatch.setattr(ordering, "_take", take)
    monkeypatch.setattr(ordering, "_retryable", record)
    with application_login(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(editor)
        assert asset_locked.wait(timeout=15)
        order = checkout(made)
        future.result(timeout=15)
        assert "40P01" in states
        assert Order.objects.of(made.org).count() == 1
        assert order.number == 1
        assert order.bookings.count() == 1


def test_a_shared_booking_holds_each_of_its_spots_for_the_whole_time(
    made: Example,
) -> None:
    made.capacity(4)
    made.claim(at(hour=10), at(hour=11), mode="shared", units=(1,))
    made.claim(at(hour=11), at(hour=12), mode="shared", units=(2,))
    booking = checkout(made, request(made, quantity=2)).bookings.get()
    rows = list(booking.taken_time.allocations.order_by("ordinal"))
    assert [(row.ordinal, row.spot and row.spot.number) for row in rows] == [
        (1, 3),
        (2, 4),
    ]
    assert all(
        (row.starts_at, row.ends_at) == (at(hour=10), at(hour=12)) for row in rows
    )
    assert booking.taken_time.quantity == 2


@pytest.mark.parametrize("field", ["starts_at", "ends_at", "now"])
@pytest.mark.parametrize(
    "moment",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))),
    ],
    ids=["before-utc-minimum", "after-utc-maximum"],
)
def test_utc_normalization_overflow_is_a_clean_refusal_before_sql(
    made: Example, field: str, moment: datetime
) -> None:
    wanted = request(made) if field == "now" else request(made, **{field: moment})
    version = made_up.accepted(made.member)
    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(OrderUnavailableError, match="supported range"),
    ):
        create_order(
            member=made.member,
            requests=[wanted],
            now=moment if field == "now" else NOW,
            accepted_version=version,
        )
    assert len(queries) == 0
    assert_empty(made)


@pytest.mark.parametrize("field", ["starts_at", "ends_at"])
@pytest.mark.parametrize("invalid", ["2027-03-01T10:00:00Z", DAY, None])
def test_non_datetime_booking_moments_are_validation_errors(
    made: Example, field: str, invalid: Any
) -> None:
    with pytest.raises(ValueError, match="datetimes"):
        checkout(made, request(made, **{field: invalid}))
    assert_empty(made)


@pytest.mark.parametrize("invalid", ["2027-03-01T10:00:00Z", DAY])
def test_non_datetime_explicit_server_clock_is_a_validation_error(
    made: Example, invalid: Any
) -> None:
    with pytest.raises(ValueError, match="datetimes"):
        create_order(
            member=made.member,
            requests=[request(made)],
            now=invalid,
            accepted_version=made_up.accepted(made.member),
        )
    assert_empty(made)


def test_non_utc_inputs_preserve_exact_instants_and_hold_deadline(
    made: Example,
) -> None:
    east = timezone(timedelta(hours=5, minutes=45))
    west = timezone(-timedelta(hours=3))
    order = create_order(
        member=made.member,
        requests=[
            request(
                made,
                starts_at=at(hour=10).astimezone(east),
                ends_at=at(hour=12).astimezone(west),
            )
        ],
        now=NOW.astimezone(east),
        accepted_version=made_up.accepted(made.member),
    )
    taken = order.bookings.get().taken_time
    assert (taken.starts_at, taken.ends_at) == (at(hour=10), at(hour=12))
    assert order.created_at == NOW
    assert order.hold_expires_at == NOW + ordering.HOLD_LENGTH
    assert order.total == 200
