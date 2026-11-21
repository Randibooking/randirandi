# SPDX-License-Identifier: AGPL-3.0-only
"""loaded acceptance setups through calendar, quote and checkout.

Expected prices and physical outcomes come from the approved examples. Every
journey runs through the real restricted application login, without mocks or
customer-specific product code. Members and databases are disposable.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from randirandi.core import setup
from randirandi.core.calendar import BookableInterval, bookable_intervals
from randirandi.core.models import (
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    CapacityAllocation,
    Member,
    Order,
    Organization,
    Payment,
    TakenTime,
)
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from randirandi.core.pricing import BookingPrice, PriceUnavailableError, price_booking
from randirandi.setup_file import parse
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, ZONE, at, scenario
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))


@dataclass
class Journey:
    org: Organization
    member: Member

    def request(
        self,
        number: int,
        start: datetime,
        end: datetime,
        *,
        category: str = "private",
        quantity: int = 1,
    ) -> BookingRequest:
        return BookingRequest(
            BookingConfiguration.objects.of(self.org).get(number=number).pk,
            BookingCategory.objects.of(self.org).get(name=category).pk,
            start,
            end,
            quantity,
        )

    def offers(self, wanted: BookingRequest) -> tuple[BookableInterval, ...]:
        config = BookingConfiguration.objects.of(self.org).get(
            pk=wanted.configuration_id
        )
        day = wanted.starts_at.astimezone(ZONE).date()
        return bookable_intervals(
            member=self.member,
            asset=config.asset,
            booking_type=config.booking_type,
            start_date=day,
            end_date=day + timedelta(days=1),
            quantity=wanted.quantity,
            now=NOW,
        )

    def available(self, wanted: BookingRequest) -> bool:
        return any(
            offer.configuration_id == wanted.configuration_id
            and offer.starts_at == wanted.starts_at
            and offer.ends_at == wanted.ends_at
            and wanted.booking_category_id in offer.category_ids
            and offer.quantity == wanted.quantity
            for offer in self.offers(wanted)
        )

    def quote(self, wanted: BookingRequest) -> BookingPrice:
        return price_booking(
            configuration=BookingConfiguration.objects.of(self.org).get(
                pk=wanted.configuration_id
            ),
            booking_category=BookingCategory.objects.of(self.org).get(
                pk=wanted.booking_category_id
            ),
            starts_at=wanted.starts_at,
            ends_at=wanted.ends_at,
            quantity=wanted.quantity,
        )

    def buy(self, *requests: BookingRequest, amount: int) -> Order:
        assert all(self.available(wanted) for wanted in requests)
        quotes = [self.quote(wanted) for wanted in requests]
        assert sum(quote.total for quote in quotes) == amount
        order = create_order(
            member=self.member,
            requests=requests,
            now=NOW,
            accepted_version=made_up.accepted(self.member),
        )
        assert order.total == amount
        assert order.member_id == self.member.pk
        assert order.hold_expires_at == NOW + timedelta(minutes=10)
        assert order.bookings.count() == len(requests)
        for quote in quotes:
            booking = order.bookings.select_related("taken_time").get(
                booking_type_id=quote.booking_type_id,
                booking_category_id=quote.booking_category_id,
                taken_time__asset_id=quote.asset_id,
                taken_time__starts_at=quote.starts_at,
                taken_time__ends_at=quote.ends_at,
            )
            assert booking.member_id == self.member.pk
            assert booking.booking_type_name == quote.booking_type_name
            assert booking.booking_category_name == quote.booking_category_name
            assert [line.total for line in booking.lines.order_by("pk")] == [
                line.total for line in quote.lines
            ]
            taken = booking.taken_time
            assert taken.quantity == quote.quantity
            assert taken.mode == quote.consumption_mode
            assert taken.state == "held"
            # One spot for each place, each for the whole time, also over
            # midnight; an exclusive booking holds the whole asset.
            held = list(taken.allocations.order_by("ordinal"))
            claims = quote.quantity if taken.mode == "shared" else 1
            assert [row.ordinal for row in held] == list(range(1, claims + 1))
            spots = {row.spot_id for row in held}
            assert (
                spots == {None} if taken.mode == "exclusive" else len(spots) == claims
            )
            assert all(
                row.starts_at == quote.starts_at
                and row.ends_at == quote.ends_at
                and row.state == "held"
                for row in held
            )
        assert Payment.objects.of(self.org).count() == 0
        return order

    def stored(self) -> tuple[int, ...]:
        self.org.refresh_from_db()
        return (
            self.org.last_order_number,
            *(
                model.objects.of(self.org).count()
                for model in (
                    Order,
                    Booking,
                    BookingLine,
                    TakenTime,
                    CapacityAllocation,
                )
            ),
        )

    def refuse(self, wanted: BookingRequest) -> None:
        assert not self.available(wanted)
        before = self.stored()
        with pytest.raises(OrderUnavailableError):
            create_order(
                member=self.member,
                requests=[wanted],
                now=NOW,
                accepted_version=made_up.accepted(self.member),
            )
        assert self.stored() == before


@contextmanager
def loaded(filename: str) -> Iterator[Journey]:
    org, member = scenario(filename)
    with application_login():
        yield Journey(org, member)


@pytest.fixture
def fjordside() -> Iterator[Journey]:
    with loaded("fjordside.toml") as journey:
        yield journey


@pytest.fixture
def sauna() -> Iterator[Journey]:
    with loaded("scenarios/sauna-palace.toml") as journey:
        yield journey


@pytest.fixture
def beds() -> Iterator[Journey]:
    with loaded("scenarios/four-beds.toml") as journey:
        yield journey


@pytest.fixture
def cinema() -> Iterator[Journey]:
    with loaded("scenarios/cinema.toml") as journey:
        yield journey


@pytest.fixture
def laundry() -> Iterator[Journey]:
    with loaded("scenarios/laundry.toml") as journey:
        yield journey


@pytest.fixture
def hall() -> Iterator[Journey]:
    with loaded("scenarios/riverside-hall.toml") as journey:
        yield journey


@pytest.mark.parametrize(
    ("days", "number", "category", "first", "amount", "lines"),
    [
        (0, 2, "Work", 10, 240000, [120000, 120000]),
        (0, 2, "Private", 10, 50000, [20000, 30000]),
        (5, 3, "Private", 14, 70000, [40000, 30000]),
        (6, 3, "Private", 14, 70000, [40000, 30000]),
    ],
)
def test_the_setups_tiered_prices_reach_the_order(
    fjordside: Journey,
    days: int,
    number: int,
    category: str,
    first: int,
    amount: int,
    lines: list[int],
) -> None:
    day = DAY + timedelta(days=days)
    wanted = fjordside.request(number, at(day, first), at(day, 22), category=category)
    order = fjordside.buy(wanted, amount=amount)
    assert [line.total for line in order.bookings.get().lines.order_by("pk")] == lines
    fjordside.refuse(wanted)


@pytest.mark.parametrize("days", [5, 6])
def test_weekend_opening_filters_the_whole_day_and_checkout(
    fjordside: Journey, days: int
) -> None:
    day = DAY + timedelta(days=days)
    wanted = fjordside.request(3, at(day, 14), at(day, 22), category="Private")
    # The whole day, 10:00 to 22:00, since the asset is available around the
    # clock; an open house is an event, not in the setup file.
    assert {(offer.starts_at, offer.ends_at) for offer in fjordside.offers(wanted)} == {
        (at(day, first), at(day, last))
        for first in range(10, 22)
        for last in range(first + 1, 23)
    }
    closed = replace(wanted, starts_at=at(day, 9), ends_at=at(day, 11))
    # Before the container's 10:00 start there is no hire, and no price.
    with pytest.raises(PriceUnavailableError):
        fjordside.quote(closed)
    fjordside.refuse(closed)
    job = replace(
        wanted,
        booking_category_id=BookingCategory.objects.of(fjordside.org)
        .get(name="Work")
        .pk,
    )
    fjordside.refuse(job)
    fjordside.buy(wanted, amount=70000)


@pytest.mark.parametrize("days", [4, 5])
def test_day_hire_and_full_overnight_share_one_asset_at_exact_boundary(
    fjordside: Journey, days: int
) -> None:
    day = DAY + timedelta(days=days)
    hire = fjordside.request(
        2 if days == 4 else 3, at(day, 14), at(day, 22), category="Private"
    )
    night = fjordside.request(
        4, at(day, 22), at(day + timedelta(days=1), 10), category="Private"
    )
    fjordside.buy(hire, night, amount=80000 if days == 4 else 115000)
    fjordside.refuse(night)
    following = fjordside.request(
        3,
        at(day + timedelta(days=1), 14),
        at(day + timedelta(days=1), 22),
        category="Private",
    )
    assert fjordside.available(following)


@pytest.mark.parametrize(
    ("hours", "quantity", "amount"), [(1, 1, 10000), (2, 1, 15000), (2, 3, 45000)]
)
def test_sauna_time_tiers_and_capacity_quantity_reach_checkout(
    sauna: Journey, hours: int, quantity: int, amount: int
) -> None:
    wanted = sauna.request(1, at(hour=12), at(hour=12 + hours), quantity=quantity)
    sauna.buy(wanted, amount=amount)
    assert sauna.available(replace(wanted, quantity=8 - quantity))
    sauna.refuse(replace(wanted, quantity=9 - quantity))
    assert sauna.available(
        replace(wanted, starts_at=wanted.ends_at, ends_at=at(hour=16), quantity=8)
    )


def test_sauna_capacity_at_each_instant_and_other_rooms_are_independent(
    sauna: Journey,
) -> None:
    wanted = sauna.request(1, at(hour=12), at(hour=14), quantity=3)
    sauna.buy(wanted, amount=45000)
    overlap = replace(wanted, starts_at=at(hour=13), ends_at=at(hour=15))
    assert sauna.available(replace(overlap, quantity=5))
    sauna.refuse(replace(overlap, quantity=6))
    sauna.buy(replace(wanted, quantity=5), amount=75000)
    sauna.refuse(replace(wanted, quantity=1))
    sauna.buy(
        sauna.request(3, at(hour=12), at(hour=14), quantity=8),
        sauna.request(5, at(hour=12), at(hour=14), quantity=8),
        sauna.request(7, at(hour=12), at(hour=14), quantity=20),
        amount=480000,
    )


def test_sauna_shared_to_private_boundary_keeps_eight_spots(
    sauna: Journey,
) -> None:
    shared = sauna.request(1, at(hour=17), at(hour=18), quantity=8)
    crossing = replace(shared, ends_at=at(hour=19))
    sauna.refuse(crossing)
    with pytest.raises(PriceUnavailableError):
        sauna.quote(crossing)
    private = sauna.request(2, at(hour=18), at(hour=22))
    order = sauna.buy(shared, private, amount=160000)
    claims = list(
        order.bookings.select_related("taken_time").order_by("taken_time__starts_at")
    )
    taken = claims[0].taken_time.allocations.values_list("spot__number", flat=True)
    assert set(taken) == set(range(1, 9))
    assert claims[1].taken_time.allocations.get().spot_id is None
    assert made_up.floor_plan_of(claims[1].taken_time.asset).capacity == 8
    sauna.refuse(private)


def test_sunshine_sells_four_beds_on_weeknights_and_refuses_a_fifth(
    beds: Journey,
) -> None:
    wanted = beds.request(1, at(hour=17), at(DAY + timedelta(days=1), 8), quantity=2)
    beds.buy(wanted, amount=20000)
    beds.buy(wanted, amount=20000)
    beds.refuse(replace(wanted, quantity=1))
    # The whole room is a container of its own, on Friday and Saturday only.
    beds.refuse(beds.request(2, wanted.starts_at, wanted.ends_at))
    assert beds.available(
        replace(
            wanted,
            starts_at=at(DAY + timedelta(days=1), 17),
            ends_at=at(DAY + timedelta(days=2), 8),
            quantity=4,
        )
    )


def test_sunshine_by_the_bed_and_whole_room_sit_back_to_back(beds: Journey) -> None:
    wednesday, thursday, friday, saturday, sunday = (
        DAY + timedelta(days=n) for n in (2, 3, 4, 5, 6)
    )
    bed = beds.request(1, at(thursday, 17), at(friday, 8), quantity=4)
    room = beds.request(2, at(friday, 17), at(saturday, 8))
    beds.buy(bed, room, amount=80000)
    beds.refuse(replace(bed, quantity=1))
    beds.refuse(room)
    # Neither container covers a night of the other's.
    beds.refuse(replace(bed, starts_at=at(saturday, 17), ends_at=at(sunday, 8)))
    beds.refuse(replace(room, starts_at=at(wednesday, 17), ends_at=at(thursday, 8)))
    assert beds.available(
        replace(
            bed, starts_at=at(sunday, 17), ends_at=at(sunday + timedelta(days=1), 8)
        )
    )


def test_cinema_sells_seats_per_screening_and_hire_on_other_evenings(
    cinema: Journey,
) -> None:
    friday = DAY + timedelta(days=4)
    seats = cinema.request(1, at(friday, 19), at(friday, 21), quantity=2)
    cinema.buy(seats, amount=24000)
    cinema.buy(replace(seats, quantity=198), amount=198 * 12000)
    cinema.refuse(replace(seats, quantity=1))
    # A screening's container is its one date.
    later = friday + timedelta(days=7)
    cinema.refuse(
        replace(seats, starts_at=at(later, 19), ends_at=at(later, 21), quantity=1)
    )
    saturday = friday + timedelta(days=1)
    cinema.buy(cinema.request(2, at(saturday, 19), at(saturday, 21)), amount=12000)
    # The whole cinema on the other evenings, never on a screening's.
    hire = cinema.request(3, at(DAY, 18), at(DAY, 23))
    cinema.buy(hire, amount=300000)
    cinema.refuse(hire)
    cinema.refuse(replace(hire, starts_at=at(friday, 18), ends_at=at(friday, 23)))


def test_laundry_machines_run_together_and_the_room_is_hired_on_sundays(
    laundry: Journey,
) -> None:
    machines = [laundry.request(n, at(hour=10), at(hour=12)) for n in range(1, 4)]
    laundry.buy(*machines, amount=12000)
    for wanted in machines:
        laundry.refuse(wanted)
        assert laundry.available(
            replace(wanted, starts_at=at(hour=12), ends_at=at(hour=14))
        )
    # The whole room is hired on Sundays, when no machine is offered.
    sunday = DAY + timedelta(days=6)
    room = laundry.request(4, at(sunday, 10), at(sunday, 12))
    laundry.buy(room, amount=20000)
    laundry.refuse(room)
    for wanted in machines:
        laundry.refuse(replace(wanted, starts_at=room.starts_at, ends_at=room.ends_at))
    laundry.refuse(replace(room, starts_at=at(hour=14), ends_at=at(hour=16)))


def test_laundry_takes_a_machine_container_on_the_room_hire_sunday() -> None:
    text = (SETUPS / "scenarios" / "laundry.toml").read_text()
    machines = 'period_start_weekdays = ["mon", "tue", "wed", "thu", "fri", "sat"]'
    assert text.count(machines) == 3
    every_day = (
        'period_start_weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]'
    )
    org = setup.load(parse(text.replace(machines, every_day, 1)), today=DAY)
    assert org.standard_role_id is not None
    member = Member.objects.create(
        organization=org,
        role_id=org.standard_role_id,
        email="scenario@example.com",
        status="active",
        origin="member import",
    )
    journey = Journey(org, member)
    sunday = DAY + timedelta(days=6)
    washer = journey.request(1, at(sunday, 10), at(sunday, 12))
    room = journey.request(4, at(sunday, 10), at(sunday, 12))
    # Both containers offer the Sunday hour; the first booking takes it.
    assert journey.available(washer) and journey.available(room)
    with application_login():
        journey.buy(washer, amount=4000)
        journey.refuse(room)
        journey.refuse(washer)
    later = journey.request(4, at(sunday, 14), at(sunday, 16))
    assert journey.available(later)


def test_laundry_cart_with_one_washer_twice_at_one_time_saves_nothing(
    laundry: Journey,
) -> None:
    washer = laundry.request(1, at(hour=10), at(hour=12))
    requests = [washer, washer, laundry.request(2, at(hour=10), at(hour=12))]
    assert all(laundry.available(wanted) for wanted in requests)
    assert [laundry.quote(wanted).total for wanted in requests] == [4000, 4000, 4000]
    before = laundry.stored()
    with pytest.raises(OrderUnavailableError):
        create_order(
            member=laundry.member,
            requests=requests,
            now=NOW,
            accepted_version=made_up.accepted(laundry.member),
        )
    assert laundry.stored() == before
    assert all(laundry.available(wanted) for wanted in requests)
    assert laundry.buy(*requests[1:], amount=8000).number == 1


def spots_of(order: Order) -> list[tuple[str, int]]:
    """The floor plan and number of each spot an order's one booking holds."""
    held = []
    for claim in (
        CapacityAllocation.objects.of(order.organization_id)
        .filter(taken_time__booking__order=order)
        .select_related("spot__floor_plan")
        .order_by("ordinal")
    ):
        assert claim.spot is not None
        held.append((claim.spot.floor_plan.name, claim.spot.number))
    return held


def test_riverside_hall_sells_seats_and_places_on_its_two_floor_plans(
    hall: Journey,
) -> None:
    friday, saturday = DAY + timedelta(days=4), DAY + timedelta(days=5)
    concert = hall.request(2, at(friday, 19), at(friday, 22), quantity=2)
    assert spots_of(hall.buy(concert, amount=60000)) == [("Seated", 1), ("Seated", 2)]
    rest = hall.buy(replace(concert, quantity=10), amount=300000)
    assert spots_of(rest) == [("Seated", n) for n in range(3, 13)]
    hall.refuse(replace(concert, quantity=1))
    # The club night stands, on the other floor plan, until midnight.
    club = hall.request(
        4, at(saturday, 20), at(saturday + timedelta(days=1)), quantity=40
    )
    assert spots_of(hall.buy(club, amount=600000)) == [
        ("Standing", n) for n in range(1, 41)
    ]
    hall.refuse(replace(club, quantity=1))


def test_riverside_hall_is_hired_whole_by_the_hour_and_on_saturdays(
    hall: Journey,
) -> None:
    saturday = DAY + timedelta(days=5)
    rehearsal = hall.request(1, at(hour=10), at(hour=12))
    hall.buy(rehearsal, amount=80000)
    hall.refuse(replace(rehearsal, starts_at=at(hour=11), ends_at=at(hour=13)))
    assert hall.available(
        replace(rehearsal, starts_at=at(hour=12), ends_at=at(hour=18))
    )
    # Rehearsals are Monday to Thursday; the concert has Friday evening.
    friday = DAY + timedelta(days=4)
    hall.refuse(replace(rehearsal, starts_at=at(friday, 10), ends_at=at(friday, 12)))
    day_hire = hall.request(3, at(saturday, 10), at(saturday, 18))
    hall.buy(day_hire, amount=500000)
    hall.refuse(day_hire)
    # The club night that evening has a container of its own.
    assert hall.available(
        hall.request(4, at(saturday, 20), at(saturday + timedelta(days=1)))
    )
