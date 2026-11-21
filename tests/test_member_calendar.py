# SPDX-License-Identifier: AGPL-3.0-only
"""the booking calendar, answered per container (W4, W8, W15).

Made-up setups stand in for real organizations: the sauna palace (seats by
the hour and the whole sauna in the evening, in one space), the room
Sunshine (four named beds, the whole room at the weekend), the laundry
(machines Monday to Saturday, the whole laundry room on Sundays) and the
cinema (200 seats a screening).
"""

import gc
import json
import re
import time
import tracemalloc
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from randirandi import setup_file
from randirandi.core import (
    booking_cart,
    booking_checkout,
    member_data,
    ordering,
    setup,
)
from randirandi.core import calendar as calendar_module
from randirandi.core.booking_quote import Choice, Quote, QuoteInputError, quote
from randirandi.core.calendar import CalendarLimitError, bookable_intervals, closed_days
from randirandi.core.member_calendar import (
    ContainerCalendar,
    DayCategories,
    MemberCalendar,
    TakenSpan,
    member_calendar,
)
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityClosure,
    AvailableSpan,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    CapacityAllocation,
    ConfigurationCategory,
    Member,
    Order,
    Organization,
    Role,
    RoleCategory,
    Space,
    TakenTime,
    Weekday,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, Example, at, offer_on, scenario
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))
MINUTE = timedelta(minutes=1)


def ask(
    org: Organization,
    member: Member,
    *,
    asset: str | None = None,
    space: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    quantity: int = 1,
    length: int | None = None,
    day: date = DAY,
    days: int = 1,
    now: datetime = NOW,
    cart: Sequence[Choice] = (),
) -> MemberCalendar:
    def named(model: Any, name: str | None) -> int | None:
        return None if name is None else model.objects.of(org).get(name=name).pk

    return member_calendar(
        member=member,
        asset_id=named(Asset, asset),
        space_id=named(Space, space),
        booking_type_id=named(BookingType, kind),
        booking_category_id=named(BookingCategory, category),
        quantity=quantity,
        length=length,
        start_date=day,
        end_date=day + timedelta(days=days),
        now=now,
        cart=cart,
    )


def numbered(found: MemberCalendar) -> dict[int, ContainerCalendar]:
    return {row.number: row for row in found.containers}


def container(org: Organization, number: int) -> int:
    return BookingConfiguration.objects.of(org).get(number=number).pk


def private(org: Organization) -> int:
    return BookingCategory.objects.of(org).get(name="private").pk


class Ids(TypedDict):
    configuration_id: int
    booking_category_id: int


def ids(
    org: Organization,
    asset: str,
    kind: str,
    category: str = "private",
    number: int | None = None,
) -> Ids:
    """The container of an asset and a booking type (the one numbered, when
    the asset has several) and the category: what the quote is sent."""
    found = BookingConfiguration.objects.of(org).filter(
        asset__name=asset, booking_type__name=kind
    )
    return {
        "configuration_id": (
            found.get(number=number) if number else found.order_by("number")[0]
        ).pk,
        "booking_category_id": BookingCategory.objects.of(org).get(name=category).pk,
    }


def starts(row: ContainerCalendar) -> list[tuple[datetime, datetime, tuple[int, ...]]]:
    """Every bookable start: its moment, its end and the spots it takes."""
    return [
        (period.rows[start.row], start.ends_at, start.spots)
        for period in row.periods
        for start in period.starts
    ]


def take(
    org: Organization,
    asset: str,
    start: datetime,
    end: datetime,
    *,
    units: tuple[int, ...] = (),
    state: str = "confirmed",
) -> TakenTime:
    on = Asset.objects.of(org).get(name=asset)
    return TakenTime.objects.take(
        organization=org,
        asset=on,
        starts_at=start,
        ends_at=end,
        mode="shared" if units else "exclusive",
        quantity=len(units) or 1,
        state=state,
        units=made_up.spots_of(made_up.floor_plan_of(on), *units) if units else None,
    )


def spots_of(order: Order) -> list[int]:
    return sorted(
        CapacityAllocation.objects.of(order.organization_id)
        .filter(taken_time__in=[row.taken_time_id for row in order.bookings.all()])
        .values_list("spot__number", flat=True)
    )


def at_home(filename: str) -> tuple[Organization, Member]:
    """A made-up setup at the address the page tests sign in at."""
    text = re.sub(
        r'(?m)^subdomain = ".*"$',
        'subdomain = "harbourview"',
        (SETUPS / filename).read_text(),
    )
    org = setup.load(setup_file.parse(text), today=DAY)
    assert org.standard_role_id is not None
    member = Member.objects.create(
        organization=org,
        role_id=org.standard_role_id,
        email="scenario@example.com",
        status="active",
        origin="member import",
    )
    return org, member


def test_an_asset_answers_each_container_with_its_columns_rows_and_payment() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    found = ask(org, member, asset="Sauna 1", length=60)
    assert found.worked_out_at == NOW
    assert [row.number for row in found.containers] == [1, 2]
    visit, evening = found.containers
    assert (
        visit.id,
        visit.asset_name,
        visit.booking_type_name,
        visit.category_ids,
        visit.period_type,
        visit.consumption_mode,
        visit.with_payment,
        (visit.interval, visit.minimum_length, visit.maximum_length),
    ) == (
        container(org, 1),
        "Sauna 1",
        "Sauna visit",
        (private(org),),
        "flexible",
        "shared",
        True,
        (60, 60, 480),
    )
    assert visit.floor_plan is not None
    assert [spot.number for spot in visit.floor_plan.spots] == list(range(1, 9))
    assert visit.max_quantity == 8
    (period,) = visit.periods
    assert period.day == DAY
    assert period.rows == tuple(at(hour=hour) for hour in range(10, 18))
    assert period.ends_at == at(hour=18)
    assert period.taken == ()
    assert starts(visit) == [
        (at(hour=hour), at(hour=hour + 1), (1,)) for hour in range(10, 18)
    ]
    # The whole sauna: one column, one row.
    assert (evening.floor_plan, evening.max_quantity) == (None, 1)
    assert evening.periods[0].rows == (at(hour=18),)
    assert starts(evening) == [(at(hour=18), at(hour=22), ())]
    # Booking type and category filter the containers; a fixed one needs no
    # length.
    alone = ask(org, member, asset="Sauna 1", kind="Private sauna")
    assert [row.number for row in alone.containers] == [2]
    assert list(
        numbered(ask(org, member, asset="Sauna 1", category="private", length=60))
    ) == [1, 2]
    board = Role.objects.create(organization=org, name="Board")
    inside = BookingCategory.objects.create(organization=org, name="internal")
    RoleCategory.objects.create(organization=org, role=board, booking_category=inside)
    ConfigurationCategory.objects.create(
        organization=org,
        booking_configuration_id=container(org, 1),
        booking_category=inside,
    )
    assert ask(org, member, asset="Sauna 1", category="internal").containers == ()


def test_a_space_answers_the_machines_and_the_whole_room_side_by_side() -> None:
    org, member = scenario("scenarios/laundry.toml")
    week = ask(org, member, space="Laundry room", length=60, days=7)
    assert [(row.asset_name, row.number) for row in week.containers] == [
        ("Washer A", 1),
        ("Washer B", 2),
        ("Dryer", 3),
        ("Whole laundry room", 4),
    ]
    sides: dict[date, set[str]] = {}
    for row in week.containers:
        side = "room" if row.asset_name == "Whole laundry room" else "machines"
        for period in row.periods:
            sides.setdefault(period.day, set()).add(side)
    sunday = DAY + timedelta(days=6)
    assert sides == {
        **{DAY + timedelta(days=days): {"machines"} for days in range(6)},
        sunday: {"room"},
    }
    # Asked for one day, a space answers the side that day has.
    monday = ask(org, member, space="Laundry room", length=60)
    assert [row.number for row in monday.containers] == [1, 2, 3]
    assert [
        row.number
        for row in ask(
            org, member, space="Laundry room", length=60, day=sunday
        ).containers
    ] == [4]
    # A machine's booking takes its own column, not the others'.
    take(org, "Washer A", at(hour=10), at(hour=12))
    machines = numbered(ask(org, member, space="Laundry room", length=120))
    assert machines[1].periods[0].taken == (TakenSpan(None, 0, 1, "confirmed"),)
    assert machines[2].periods[0].taken == ()
    assert starts(machines[1])[0][0] == at(hour=12)
    assert starts(machines[2])[0][0] == at(hour=10)


def test_taken_cells_come_by_spot_as_spans_with_their_state() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=11), at(hour=13), units=(1, 2, 3))
    take(org, "Sauna 1", at(hour=12), at(hour=14), units=(4, 5), state="held")
    take(org, "Sauna 1", at(hour=10), at(hour=18), units=(8,), state="blocked")
    take(org, "Sauna 1", at(hour=18), at(hour=22), state="blocked")
    gone = take(org, "Sauna 1", at(hour=10), at(hour=11), units=(6,))
    gone.state = "cancelled"
    gone.save(update_fields=["state"])
    # Another sauna of the space is another asset: nothing of it shows.
    take(org, "Sauna 2", at(hour=10), at(hour=18), units=(7,))
    visit, evening = ask(org, member, asset="Sauna 1", length=60).containers
    assert visit.periods[0].taken == (
        TakenSpan(1, 1, 2, "confirmed"),
        TakenSpan(2, 1, 2, "confirmed"),
        TakenSpan(3, 1, 2, "confirmed"),
        TakenSpan(4, 2, 3, "held"),
        TakenSpan(5, 2, 3, "held"),
        # A spot taken all day is one span.
        TakenSpan(8, 0, 7, "block"),
    )
    # The whole sauna's one column, blocked.
    assert evening.periods[0].taken == (TakenSpan(None, 0, 0, "block"),)
    assert starts(evening) == []
    # Taken cells are the same whatever the quantity and length asked.
    assert (
        ask(org, member, asset="Sauna 1", quantity=3, length=120)
        .containers[0]
        .periods[0]
        .taken
        == visit.periods[0].taken
    )


def test_a_hold_past_its_deadline_stays_taken_until_it_is_freed() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="olga@example.com",
        status="active",
        origin="member import",
    )
    held = ordering.create_order(
        member=other,
        requests=[
            ordering.BookingRequest(
                container(org, 1), private(org), at(hour=10), at(hour=12), 2
            )
        ],
        now=NOW,
        accepted_version=made_up.accepted(other),
    )
    later = NOW + timedelta(hours=1)
    assert held.hold_expires_at is not None and held.hold_expires_at < later
    visit = ask(org, member, asset="Sauna 1", length=60, now=later).containers[0]
    assert visit.periods[0].taken == (
        TakenSpan(1, 0, 1, "held"),
        TakenSpan(2, 0, 1, "held"),
    )
    assert starts(visit)[0] == (at(hour=10), at(hour=11), (3,))
    assert ask(org, member, asset="Sauna 1", length=60, now=later).worked_out_at == (
        later
    )


def test_bookable_starts_take_the_lowest_free_spots_as_the_order_does() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=10), at(hour=12), units=(1, 2))
    take(org, "Sauna 1", at(hour=11), at(hour=13), units=(4,))
    visit = ask(org, member, asset="Sauna 1", quantity=2, length=60).containers[0]
    assert starts(visit)[:4] == [
        (at(hour=10), at(hour=11), (3, 4)),
        (at(hour=11), at(hour=12), (3, 5)),
        (at(hour=12), at(hour=13), (1, 2)),
        (at(hour=13), at(hour=14), (1, 2)),
    ]
    # The starts are the calendar's own full expansion, for every quantity
    # and length.
    kind = BookingType.objects.of(org).get(name="Sauna visit")
    asset = Asset.objects.of(org).get(name="Sauna 1")
    for quantity in (1, 2, 6, 7, 8):
        every = bookable_intervals(
            member=member,
            asset=asset,
            booking_type=kind,
            start_date=DAY,
            end_date=DAY + timedelta(days=1),
            quantity=quantity,
            now=NOW,
        )
        for length in (60, 120, 480):
            found = ask(org, member, asset="Sauna 1", quantity=quantity, length=length)
            assert {(start, end) for start, end, _ in starts(found.containers[0])} == {
                (row.starts_at, row.ends_at)
                for row in every
                if row.ends_at - row.starts_at == length * MINUTE
            }, (quantity, length)
    # An order without a pick takes the spots the start showed.
    placed = ordering.create_order(
        member=member,
        requests=[
            ordering.BookingRequest(visit.id, private(org), at(hour=11), at(hour=12), 2)
        ],
        now=NOW,
        accepted_version=made_up.accepted(member),
    )
    assert spots_of(placed) == [3, 5]


def test_the_cart_counts_as_taken_in_the_starts_and_is_no_taken_cell() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    cart = [Choice(container(org, 1), private(org), 2, at(hour=10), at(hour=12))]
    visit = ask(
        org, member, asset="Sauna 1", quantity=2, length=60, cart=cart
    ).containers[0]
    assert visit.periods[0].taken == ()
    assert starts(visit)[:3] == [
        (at(hour=10), at(hour=11), (3, 4)),
        (at(hour=11), at(hour=12), (3, 4)),
        (at(hour=12), at(hour=13), (1, 2)),
    ]
    # Seven asked: the cart's two leave six until noon.
    seven = ask(org, member, asset="Sauna 1", quantity=7, length=60, cart=cart)
    assert starts(seven.containers[0])[0][0] == at(hour=12)
    # A cart booking on another sauna of the space takes nothing here.
    other = ask(org, member, asset="Sauna 2", quantity=8, length=60, cart=cart)
    assert starts(other.containers[0])[0][0] == at(hour=10)


def test_the_next_night_of_a_stay_offers_the_cart_s_bed() -> None:
    org, member = scenario("scenarios/four-beds.toml")
    take(org, "Sunshine", at(hour=17), at(DAY + timedelta(days=1), 8), units=(1,))
    night = Choice(
        container(org, 1),
        private(org),
        1,
        at(hour=17),
        at(DAY + timedelta(days=1), 8),
        (3,),
    )
    tuesday = DAY + timedelta(days=1)
    beds = numbered(
        ask(
            org,
            member,
            kind="Overnight bed",
            asset="Sunshine",
            day=tuesday,
            cart=[night],
        )
    )[1]
    # Monday's night runs into Tuesday and comes whole, from its own day.
    assert [period.day for period in beds.periods] == [DAY, tuesday]
    monday, following = beds.periods
    assert monday.rows == (at(hour=17),) and monday.ends_at == at(tuesday, 8)
    assert monday.taken == (TakenSpan(1, 0, 0, "confirmed"),)
    assert [start.spots for start in monday.starts] == [(2,)]
    # The cart's bed 3 is kept for the next night (W18).
    assert [start.spots for start in following.starts] == [(3,)]


def test_the_quote_checks_the_pick_and_the_order_keeps_it() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=10), at(hour=12), units=(1,))

    def priced(
        spots: tuple[int, ...],
        quantity: int | None = None,
        number: int = 1,
        hours: tuple[int, int] = (10, 11),
    ) -> Quote:
        return quote(
            member=member,
            configuration_id=container(org, number),
            booking_category_id=private(org),
            quantity=quantity or len(spots) or 1,
            starts_at=at(hour=hours[0]),
            ends_at=at(hour=hours[1]),
            now=NOW,
            spots=spots,
        )

    assert priced((2, 3)).reason is None
    assert priced(()).configuration_id == container(org, 1)
    assert priced((1, 2)) == Quote("taken", places_left=7)
    # A spot the floor plan does not have is never free.
    assert priced((9,)).reason == "taken"
    with pytest.raises(QuoteInputError):
        priced((2,), quantity=2)
    with pytest.raises(QuoteInputError):
        priced((2, 2))
    # The whole sauna takes no spot.
    assert priced((), number=2, hours=(18, 22)).reason is None
    assert priced((1,), number=2, hours=(18, 22)) == Quote("not-offered")
    # The container is named: a time of another container is not offered.
    assert priced((), number=2).reason == "not-offered"
    foreign = made_up.everything("riverside")
    with pytest.raises(QuoteInputError):
        quote(
            member=member,
            configuration_id=0,
            booking_category_id=private(org),
            quantity=1,
            starts_at=at(hour=10),
            ends_at=at(hour=11),
        )
    assert (
        quote(
            member=member,
            configuration_id=foreign.configuration.pk,
            booking_category_id=private(org),
            quantity=1,
            starts_at=at(hour=10),
            ends_at=at(hour=11),
            now=NOW,
        ).reason
        == "not-offered"
    )
    # The cart and the order keep the spots picked.
    session = SessionStore()
    wanted = Choice(
        container(org, 1), private(org), 2, at(hour=10), at(hour=11), (5, 7)
    )
    seen = priced((5, 7)).total
    assert isinstance(
        booking_cart.add(session, member, wanted, seen, NOW), booking_cart.CartRow
    )
    taken = Choice(container(org, 1), private(org), 1, at(hour=10), at(hour=11), (1,))
    assert booking_cart.add(session, member, taken, 0, NOW) == booking_cart.Refusal(
        "taken"
    )
    placed = booking_checkout.order(
        session, member, seen, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, booking_checkout.Placed)
    assert spots_of(Order.objects.of(org).get(public_id=placed.public_id)) == [5, 7]


def test_an_exclusive_container_answers_a_party_of_any_size() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    party = ask(org, member, asset="Sauna 1", quantity=9, length=60)
    # Eight seats are too few for nine; the whole sauna takes any party.
    assert [row.number for row in party.containers] == [2]
    assert starts(party.containers[0]) == [(at(hour=18), at(hour=22), ())]
    found = quote(
        member=member,
        configuration_id=container(org, 2),
        booking_category_id=private(org),
        quantity=9,
        starts_at=at(hour=18),
        ends_at=at(hour=22),
        now=NOW,
    )
    assert (found.reason, found.quantity, found.consumption_mode) == (
        None,
        1,
        "exclusive",
    )
    session = SessionStore()
    wanted = Choice(container(org, 2), private(org), 9, at(hour=18), at(hour=22))
    assert isinstance(
        booking_cart.add(session, member, wanted, found.total, NOW),
        booking_cart.CartRow,
    )
    placed = booking_checkout.order(
        session, member, found.total, NOW, accepted_version=made_up.accepted(member)
    )
    assert isinstance(placed, booking_checkout.Placed)
    (booked,) = TakenTime.objects.of(org).filter(state="held")
    assert (booked.mode, booked.quantity) == ("exclusive", 1)


def calendar_page(client: Client, **query: Any) -> Any:
    text = "&".join(f"{key}={value}" for key, value in query.items())
    return get(client, "/booking/calendar/?" + text)


def another(org: Organization, member: Member) -> Member:
    return Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="olga.other@example.com",
        full_name="Olga Otherby",
        status="active",
        origin="member import",
    )


def order(
    org: Organization,
    member: Member,
    start: datetime,
    end: datetime,
    quantity: int = 1,
    *,
    confirmed: bool = False,
) -> Order:
    placed = ordering.create_order(
        member=member,
        requests=[
            ordering.BookingRequest(
                container(org, 1), private(org), start, end, quantity
            )
        ],
        now=NOW,
        accepted_version=made_up.accepted(member),
    )
    if confirmed:
        for row in placed.bookings.all():
            row.taken_time.state = "confirmed"
            row.taken_time.save(update_fields=["state"])
    return placed


def test_the_members_own_bookings_are_mine_and_no_one_else_s() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = another(org, member)
    order(org, member, at(hour=10), at(hour=11), confirmed=True)
    order(org, member, at(hour=11), at(hour=12))
    order(org, other, at(hour=13), at(hour=14), 2)
    take(org, "Sauna 1", at(hour=15), at(hour=16), units=(5,))
    take(org, "Sauna 1", at(hour=10), at(hour=18), units=(8,), state="blocked")
    spans = ask(org, member, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert spans == (
        TakenSpan(1, 0, 0, "confirmed", True),
        TakenSpan(1, 1, 1, "held", True),
        TakenSpan(1, 3, 3, "held", False, None, "Olga Otherby"),
        TakenSpan(2, 3, 3, "held", False, None, "Olga Otherby"),
        TakenSpan(5, 5, 5, "confirmed", False),
        TakenSpan(8, 0, 7, "block", False),
    )
    # The other member sees the same cells the other way round.
    seen = ask(org, other, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert [(span.spot, span.first, span.mine) for span in seen] == [
        (1, 0, False),
        (1, 1, False),
        (1, 3, True),
        (2, 3, True),
        (5, 5, False),
        (8, 0, False),
    ]


def test_a_shared_cell_follows_the_member_s_booking_when_the_state_is_the_same() -> (
    None
):
    org, member = scenario("scenarios/sauna-palace.toml")
    mine = order(org, member, at(hour=10), at(hour=11), confirmed=True)
    row = mine.bookings.get().taken_time
    row.ends_at = at(hour=10, minute=30)
    row.save(update_fields=["ends_at"])
    take(org, "Sauna 1", at(hour=10, minute=30), at(hour=12), units=(1,))
    spans = ask(org, member, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert spans == (
        TakenSpan(1, 0, 0, "confirmed", True),
        TakenSpan(1, 1, 1, "confirmed", False),
    )


def test_the_own_bookings_are_found_in_one_booking_read() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    order(org, member, at(hour=10), at(hour=11))
    order(org, member, at(hour=12), at(hour=13))
    order(
        org,
        member,
        at(DAY + timedelta(days=1), hour=10),
        at(DAY + timedelta(days=1), hour=11),
    )
    with CaptureQueriesContext(connection) as queries:
        found = ask(org, member, asset="Sauna 1", length=60, days=2)
    periods = found.containers[0].periods
    assert len(periods) >= 2
    assert all(span.mine for period in periods for span in period.taken)
    assert sum(len(period.taken) for period in periods) == 3
    reads = [
        row
        for row in queries.captured_queries
        if row["sql"].startswith('SELECT "core_booking"."')
    ]
    assert len(reads) == 1


def test_a_shared_container_marks_only_the_member_s_spots() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = another(org, member)
    order(org, member, at(hour=10), at(hour=11), 2)
    order(org, other, at(hour=10), at(hour=11))
    spans = ask(org, member, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert spans == (
        TakenSpan(1, 0, 0, "held", True),
        TakenSpan(2, 0, 0, "held", True),
        TakenSpan(3, 0, 0, "held", False, None, "Olga Otherby"),
    )


def test_the_page_sends_mine_for_the_signed_in_members_booking() -> None:
    org, member = at_home("scenarios/sauna-palace.toml")
    order(org, member, at(hour=10), at(hour=12), 2)
    client = Client()
    sign_in(client, member)
    sauna = Asset.objects.of(org).get(name="Sauna 1").pk
    response = calendar_page(
        client, asset=sauna, start=DAY.isoformat(), days=1, length=60
    )
    assert response.status_code == 200
    taken = json.loads(response.content)["containers"][0]["periods"][0]["taken"]
    assert taken == [
        {"spot": number, "first": 0, "last": 1, "state": "held", "mine": True}
        for number in (1, 2)
    ]


def test_the_page_names_another_member_and_nothing_else_about_them() -> None:
    org, member = at_home("scenarios/sauna-palace.toml")
    # An id no sequence reaches in a test, so finding it in the answer means
    # the member's id leaked, never another row's id with the same digits
    # (2328 held 328 on 2026-10-26).
    other = Member.objects.create(
        pk=987_654_321,
        organization=org,
        role_id=member.role_id,
        email="olga.other@example.com",
        full_name="Olga Otherby",
        phone="+4799988877",
        status="active",
        origin="member import",
    )
    placed = ordering.create_order(
        member=other,
        requests=[
            ordering.BookingRequest(
                container(org, 1), private(org), at(hour=10), at(hour=12), 3
            )
        ],
        now=NOW,
        accepted_version=made_up.accepted(other),
    )
    # And one paid: confirmed, as after Vipps .
    paid = ordering.create_order(
        member=other,
        requests=[
            ordering.BookingRequest(
                container(org, 1), private(org), at(hour=12), at(hour=13), 2
            )
        ],
        now=NOW,
        accepted_version=made_up.accepted(other),
    )
    TakenTime.objects.of(org).filter(booking__order=paid).update(state="confirmed")
    Order.objects.of(org).filter(pk=paid.pk).update(hold_expires_at=None)
    client = Client()
    sign_in(client, member)
    sauna = Asset.objects.of(org).get(name="Sauna 1").pk
    query = {"asset": sauna, "start": DAY.isoformat(), "days": 1, "length": 60}
    response = calendar_page(client, **query)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert "private" in response["Cache-Control"]
    body = json.loads(response.content)
    visit = body["containers"][0]
    taken = visit["periods"][0]["taken"]
    name = "Olga Otherby"
    assert [cell for cell in taken if cell["state"] == "held"] == [
        {
            "spot": number,
            "first": 0,
            "last": 1,
            "state": "held",
            "mine": False,
            "name": name,
        }
        for number in (1, 2, 3)
    ]
    confirmed = [cell for cell in taken if cell["state"] == "confirmed"]
    assert len(confirmed) == 2 and len(taken) == 5
    # The name and nothing else about the member .
    assert all(
        set(cell) == {"spot", "first", "last", "state", "mine", "name"}
        and cell["name"] == name
        for cell in taken
    )
    assert not any(cell["mine"] for cell in taken)
    text = response.content.decode()
    assert name in text
    for value in (
        other.email,
        other.phone,
        str(other.pk),
        "member_id",
        str(placed.public_id),
        str(paid.public_id),
        '"order"',
        '"member"',
    ):
        assert value not in text, value
    assert set(visit) == {
        "id",
        "number",
        "asset",
        "type",
        "categories",
        "period_type",
        "consumption_mode",
        "with_payment",
        "prices",
        "interval",
        "minimum_length",
        "maximum_length",
        "floor_plan",
        "max_quantity",
        "periods",
    }
    assert set(visit["periods"][0]) == {
        "day",
        "rows",
        "end",
        "row_ends",
        "closed_rows",
        "taken",
        "starts",
        "category_hours",
    }


def test_the_page_answers_a_space_with_local_offsets_and_its_moment() -> None:
    org, member = at_home("scenarios/laundry.toml")
    client = Client()
    sign_in(client, member)
    room = Space.objects.of(org).get(name="Laundry room").pk
    query = {"space": room, "start": DAY.isoformat(), "days": 1, "length": 120}
    body = json.loads(calendar_page(client, **query).content)
    assert re.fullmatch(
        r"[0-9]{4}-..-..T..:..:..(\.[0-9]+)?\+0[12]:00", body["worked_out_at"]
    )
    washer = body["containers"][0]
    assert washer["asset"] == {
        "id": Asset.objects.of(org).get(name="Washer A").pk,
        "name": "Washer A",
        "maximum_occupancy": None,
    }
    assert washer["type"]["name"] == "Machine use"
    assert washer["floor_plan"] is None and washer["max_quantity"] == 1
    (period,) = washer["periods"]
    assert period["day"] == "2027-03-01"
    assert period["rows"][:2] == [
        "2027-03-01T10:00:00+01:00",
        "2027-03-01T11:00:00+01:00",
    ]
    assert period["end"] == "2027-03-01T18:00:00+01:00"
    assert period["starts"][0] == {
        "row": 0,
        "end": "2027-03-01T12:00:00+01:00",
        "spots": [],
        "free": [],
    }
    assert len(period["starts"]) == 7


def test_the_page_names_each_categorys_own_hours_in_local_time() -> None:
    # Fjordside's work hire (container 2) ends at 17:00 on Friday; private hire
    # has the container's hours and is not named (2.99).
    org, member = at_home("fjordside.toml")
    client = Client()
    sign_in(client, member)
    friday = DAY + timedelta(days=4)
    query = {
        "asset": Asset.objects.of(org).get(name="Hall").pk,
        "start": friday.isoformat(),
        "days": 1,
        "length": 60,
    }
    body = json.loads(calendar_page(client, **query).content)
    hours = {
        row["number"]: row["periods"][0]["category_hours"] for row in body["containers"]
    }
    assert hours.pop(2) == [
        {
            "category": BookingCategory.objects.of(org).get(name="Work").pk,
            "spans": [["2027-03-05T10:00:00+01:00", "2027-03-05T17:00:00+01:00"]],
        }
    ]
    assert hours and all(value == [] for value in hours.values())


def test_the_page_refuses_bad_and_large_queries(made: Example) -> None:
    client = Client()
    sign_in(client, made.member)
    good = {"asset": made.asset.pk, "start": "2027-03-01", "days": 7, "length": 60}
    assert calendar_page(client, **good).status_code == 200
    for change in (
        {"asset": "+1"},
        {"asset": "01"},
        # An Arabic-Indic digit one, which int() would accept.
        {"type": chr(0x661)},
        {"category": ""},
        {"start": "20270301"},
        {"start": "2027-02-30"},
        {"days": "0"},
        {"quantity": "0"},
        {"length": "-60"},
        {"start": "9999-12-31"},
        {"space": made.asset.pk},
    ):
        response = calendar_page(client, **{**good, **change})
        assert response.status_code == 400, change
    no_place = {key: value for key, value in good.items() if key != "asset"}
    assert calendar_page(client, **no_place).status_code == 400
    # No length is no refusal: each container is answered at its own minimum.
    no_length = {key: value for key, value in good.items() if key != "length"}
    assert calendar_page(client, **no_length).status_code == 200
    response = calendar_page(client, **{**good, "days": 32})
    assert response.status_code == 422
    assert json.loads(response.content) == {"error": "fewer-days"}
    # However far past the limit: no date past year 9999 is worked out.
    response = calendar_page(client, **{**good, "days": 3_000_000})
    assert response.status_code == 422
    assert json.loads(response.content) == {"error": "fewer-days"}
    response = client.post("/booking/calendar/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 405
    # Not offered is blank: no container, whatever the reason (W4).
    foreign = made_up.everything("riverside")
    body = json.loads(
        calendar_page(client, **{**good, "asset": foreign.asset.pk}).content
    )
    assert body["containers"] == []
    assert body["assets"] == [] and body["closed_days"] == []
    made.available([])
    assert json.loads(calendar_page(client, **good).content)["containers"] == []


def test_limits_and_own_transaction(made: Example) -> None:
    def asked(**changes: Any) -> MemberCalendar:
        parameters: dict[str, Any] = {
            "member": made.member,
            "asset_id": made.asset.pk,
            "length": 60,
            "start_date": DAY,
            "end_date": DAY + timedelta(days=1),
            "now": NOW,
        }
        return member_calendar(**{**parameters, **changes})

    with pytest.raises(ValueError):
        asked(quantity=0)
    with pytest.raises(ValueError):
        asked(space_id=made.asset.pk)
    assert asked(length=None).containers
    with pytest.raises(CalendarLimitError):
        asked(end_date=DAY + timedelta(days=32))
    with transaction.atomic(), pytest.raises(RuntimeError):
        asked()
    with application_login():
        assert asked().containers
    made.member.status = "disabled"
    made.member.save(update_fields=["status"])
    assert asked().containers == ()


def test_too_many_taken_spans_ask_for_fewer_days(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    made.capacity(3)
    made.claim(at(hour=10), at(hour=12), mode="shared", units=(1, 2, 3))
    monkeypatch.setattr(calendar_module, "MAX_RESULTS", 2)
    with pytest.raises(CalendarLimitError):
        member_calendar(
            member=made.member,
            asset_id=made.asset.pk,
            length=60,
            start_date=DAY,
            end_date=DAY + timedelta(days=1),
            now=NOW,
        )


@pytest.mark.parametrize(
    "day,offsets",
    [
        # The clocks go forward: 02:00-03:00 does not happen.
        (
            date(2027, 3, 28),
            "00:00+01 00:30+01 01:00+01 01:30+01 03:00+02 03:30+02 04:00+02 04:30+02",
        ),
        # The clocks go back: 02:00-03:00 happens twice.
        (
            date(2027, 10, 31),
            "00:00+02 00:30+02 01:00+02 01:30+02 02:00+02 02:30+02 "
            "02:00+01 02:30+01 03:00+01 03:30+01 04:00+01 04:30+01",
        ),
    ],
)
def test_a_flexible_container_on_both_clock_change_days(
    made: Example, day: date, offsets: str
) -> None:
    made.configure(
        period_start=0,
        period_end=300,
        interval=30,
        minimum_length=30,
        maximum_length=60,
    )
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": day.isoformat(), "days": 1, "length": 60}
    body = json.loads(calendar_page(client, **query).content)
    (period,) = body["containers"][0]["periods"]
    assert period["rows"] == [
        f"{day.isoformat()}T{moment[:5]}:00{moment[5:]}:00"
        for moment in offsets.split()
    ]
    # Every start but the last runs a whole hour, counted in elapsed time.
    found = period["starts"]
    assert [start["row"] for start in found] == list(range(len(offsets.split()) - 1))
    for start in found:
        begin = datetime.fromisoformat(period["rows"][start["row"]])
        assert datetime.fromisoformat(start["end"]) - begin == timedelta(hours=1)
    # Each row ends where the next begins, the last at the period's end.
    assert period["row_ends"] == [*period["rows"][1:], period["end"]]
    assert period["closed_rows"] == []


def test_a_row_ends_before_the_next_where_the_hours_break_off(made: Example) -> None:
    # 02:15-02:45 happens twice on the night the clocks go back.
    day = date(2027, 10, 31)
    made.configure(
        period_start=2 * 60 + 15,
        period_end=2 * 60 + 45,
        interval=15,
        minimum_length=15,
        maximum_length=30,
    )
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": day.isoformat(), "days": 1, "length": 15}
    body = json.loads(calendar_page(client, **query).content)
    (period,) = body["containers"][0]["periods"]
    d = day.isoformat()
    assert period["rows"] == [
        f"{d}T02:15:00+02:00",
        f"{d}T02:30:00+02:00",
        f"{d}T02:15:00+01:00",
        f"{d}T02:30:00+01:00",
    ]
    assert period["row_ends"] == [
        f"{d}T02:30:00+02:00",
        f"{d}T02:45:00+02:00",
        f"{d}T02:30:00+01:00",
        f"{d}T02:45:00+01:00",
    ]
    assert period["end"] == f"{d}T02:45:00+01:00"


def test_rows_outside_the_assets_availability_are_closed(made: Example) -> None:
    made.available([(0, 12 * 60), (13 * 60, 24 * 60)])
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": DAY.isoformat(), "days": 1, "length": 60}
    body = json.loads(calendar_page(client, **query).content)
    (period,) = body["containers"][0]["periods"]
    assert len(period["rows"]) == len(period["row_ends"]) == 4
    assert period["closed_rows"] == [2]
    assert [start["row"] for start in period["starts"]] == [0, 1, 3]
    # Nothing closed, nothing listed.
    made.available([(0, 24 * 60)])
    body = json.loads(calendar_page(client, **query).content)
    assert body["containers"][0]["periods"][0]["closed_rows"] == []


def test_a_categorys_own_hours_close_no_row() -> None:
    # Work is open 10:00-17:00 on Friday on container 2; the rows
    # outside stay open, the browser blanks them from category_hours.
    org, member = at_home("fjordside.toml")
    client = Client()
    sign_in(client, member)
    query = {
        "asset": Asset.objects.of(org).get(name="Hall").pk,
        "start": (DAY + timedelta(days=4)).isoformat(),
        "days": 1,
        "length": 60,
    }
    body = json.loads(calendar_page(client, **query).content)
    (work,) = [row for row in body["containers"] if row["number"] == 2]
    (period,) = work["periods"]
    assert period["category_hours"] != []
    assert period["closed_rows"] == []


def test_an_empty_day_learns_the_members_other_categories_with_time() -> None:
    # Work hire has no hours at the weekend: asked for it on a
    # Saturday, the answer names private hire, which the weekend containers
    # offer alone . The staff's own category is
    # not the member's role's and is never named.
    org, member = at_home("fjordside.toml")
    client = Client()
    sign_in(client, member)
    ids = {row.name: row.pk for row in BookingCategory.objects.of(org)}
    query = {
        "asset": Asset.objects.of(org).get(name="Hall").pk,
        "type": BookingType.objects.of(org).get(name="Hall hire").pk,
        "start": DAY.isoformat(),
        "days": 7,
    }
    work = json.loads(calendar_page(client, **query, category=ids["Work"]).content)
    # Monday to Friday private hire has the container's hours too.
    assert work["other_categories"] == [
        {"day": (DAY + timedelta(days=n)).isoformat(), "categories": [ids["Private"]]}
        for n in range(7)
    ]
    # The weekend container offers no work hire, so it is not answered.
    assert 3 not in {row["number"] for row in work["containers"]}
    private = json.loads(
        calendar_page(client, **query, category=ids["Private"]).content
    )
    assert private["other_categories"] == [
        {"day": (DAY + timedelta(days=n)).isoformat(), "categories": [ids["Work"]]}
        for n in range(5)
    ]
    # Without a category asked for, none.
    assert json.loads(calendar_page(client, **query).content)["other_categories"] == []


def test_the_other_categories_leave_out_time_already_passed() -> None:
    org, member = scenario("fjordside.toml")
    saturday = DAY + timedelta(days=5)
    found = ask(
        org, member, asset="Hall", kind="Hall hire", category="Work", day=saturday
    )
    private = BookingCategory.objects.of(org).get(name="Private").pk
    assert found.other_categories == (DayCategories(saturday, (private,)),)
    late = ask(
        org,
        member,
        asset="Hall",
        kind="Hall hire",
        category="Work",
        day=saturday,
        now=at(saturday, 22),
    )
    assert late.other_categories == ()


def test_a_row_taken_by_a_clashing_container_on_the_same_asset_is_closed(
    made: Example,
) -> None:
    standing = AssetInventory.objects.create(
        organization=made.org, asset=made.asset, name="Standing"
    )
    BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
        consumption_mode="shared", floor_plan=made_up.floor_plan_of(made.asset)
    )
    made.config.refresh_from_db()
    clone = offer_on(
        made,
        Asset.objects.create(organization=made.org, name="Spare"),
        period_start=720,
        maximum_length=120,
    )
    # Shared on two floor plans of one asset: the validator would refuse it,
    # and the run-time guard leaves the shared time closed.
    BookingConfiguration.objects.of(made.org).filter(pk=clone.config.pk).update(
        asset=made.asset, floor_plan=standing
    )
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": DAY.isoformat(), "days": 1, "length": 60}
    body = json.loads(calendar_page(client, **query).content)
    (own,) = [row for row in body["containers"] if row["id"] == made.config.pk]
    (period,) = own["periods"]
    assert len(period["rows"]) == 4
    assert period["closed_rows"] == [2, 3]


def test_no_start_offered_for_a_length_overlaps_what_is_taken(made: Example) -> None:
    made.configure(
        period_start=6 * 60,
        period_end=23 * 60,
        minimum_length=60,
        maximum_length=8 * 60,
    )
    made.claim(at(hour=15), at(hour=18))
    found = ask(made.org, made.member, asset=made.asset.name, length=8 * 60)
    (row,) = found.containers
    offered = starts(row)
    assert [begin for begin, _, _ in offered] == [at(hour=6), at(hour=7)]
    assert all(end <= at(hour=15) or at(hour=18) <= begin for begin, end, _ in offered)


def test_with_no_length_a_flexible_container_is_answered_at_its_own_minimum(
    made: Example,
) -> None:
    (row,) = ask(made.org, made.member, asset=made.asset.name).containers
    offered = starts(row)
    assert offered
    assert {end - begin for begin, end, _ in offered} == {timedelta(minutes=60)}


def test_two_flexible_containers_with_different_minimums_are_both_answered(
    made: Example,
) -> None:
    longer = offer_on(
        made,
        Asset.objects.create(organization=made.org, name="Spare"),
        booking_type=BookingType.objects.create(organization=made.org, name="Long"),
        minimum_length=120,
    )
    # Two containers on one asset; the second's own asset only carried its hours.
    BookingConfiguration.objects.of(made.org).filter(pk=longer.config.pk).update(
        asset=made.asset
    )
    found = ask(made.org, made.member, asset=made.asset.name)
    short, long = (
        numbered(found)[made.config.number],
        numbered(found)[longer.config.number],
    )
    assert {end - begin for begin, end, _ in starts(short)} == {timedelta(minutes=60)}
    assert {end - begin for begin, end, _ in starts(long)} == {timedelta(minutes=120)}


def test_the_page_answers_a_flexible_container_with_no_length_at_its_minimum(
    made: Example,
) -> None:
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": DAY.isoformat(), "days": 1}
    response = calendar_page(client, **query)
    assert response.status_code == 200
    (row,) = json.loads(response.content)["containers"]
    (day,) = row["periods"]
    assert day["starts"]
    for begin in day["starts"]:
        started = datetime.fromisoformat(day["rows"][begin["row"]])
        assert datetime.fromisoformat(begin["end"]) - started == timedelta(minutes=60)
    # A length of nothing is no length: refused, not read as "left out".
    assert calendar_page(client, **query, length=0).status_code == 400


def test_a_given_length_still_applies(made: Example) -> None:
    (row,) = ask(made.org, made.member, asset=made.asset.name, length=120).containers
    assert {end - begin for begin, end, _ in starts(row)} == {timedelta(minutes=120)}
    (row,) = ask(made.org, made.member, asset=made.asset.name, length=90).containers
    assert starts(row) == []


@pytest.mark.parametrize(
    "day,start,end",
    [
        (date(2027, 3, 27), "2027-03-27T17:00:00+01:00", "2027-03-28T08:00:00+02:00"),
        (date(2027, 10, 30), "2027-10-30T17:00:00+02:00", "2027-10-31T08:00:00+01:00"),
    ],
)
def test_a_fixed_container_on_both_clock_change_days(
    made: Example, day: date, start: str, end: str
) -> None:
    made.configure(
        period_type="fixed", period_start=17 * 60, period_end=8 * 60, end_day=1
    )
    client = Client()
    sign_in(client, made.member)

    def days(asked: date) -> list[str]:
        query = {"asset": made.asset.pk, "start": asked.isoformat(), "days": 1}
        found = json.loads(calendar_page(client, **query).content)["containers"]
        return [period["day"] for row in found for period in row["periods"]]

    query = {"asset": made.asset.pk, "start": day.isoformat(), "days": 1}
    body = json.loads(calendar_page(client, **query).content)
    # The night before runs into the day and comes too, from its own day.
    before, night = body["containers"][0]["periods"]
    assert before["day"] == (day - timedelta(days=1)).isoformat()
    # The night keeps its local ends and its one row, 14 or 16 hours long.
    assert (night["day"], night["rows"], night["end"]) == (
        day.isoformat(),
        [start],
        end,
    )
    assert night["row_ends"] == [end] and night["closed_rows"] == []
    assert night["starts"] == [{"row": 0, "end": end, "spots": [], "free": []}]
    # A fixed period whose end does not exist, or comes twice, is not offered.
    made.configure(period_start=60, period_end=150, end_day=0)
    change = day + timedelta(days=1)
    assert days(change) == []
    assert days(day) == [day.isoformat()]
    assert days(change + timedelta(days=1)) == [
        (change + timedelta(days=1)).isoformat()
    ]


def test_a_date_period_of_several_days_comes_whole_with_all_its_taken_cells(
    made: Example,
) -> None:
    # A week's stay, Monday evening to Sunday morning, once a week.
    made.configure(
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=6,
        period_start_weekdays=[0],
    )
    saturday = DAY + timedelta(days=5)
    made.claim(at(saturday, 12), at(saturday, 13))
    found = ask(made.org, made.member, asset=made.asset.name, day=saturday)
    (stay,) = found.containers
    (period,) = stay.periods
    # Asked for the Saturday only, the stay that started on Monday comes.
    assert period.day == DAY
    assert period.rows == (at(DAY, 17),)
    assert period.ends_at == at(DAY + timedelta(days=6), 8)
    assert period.taken == (TakenSpan(None, 0, 0, "confirmed"),)
    assert period.starts == ()


def test_a_month_of_five_minute_starts_answers_in_under_a_second(
    made: Example, request: pytest.FixtureRequest
) -> None:
    made.capacity(3)
    made.configure(
        period_start=6 * 60,
        period_end=22 * 60,
        interval=5,
        minimum_length=5,
        maximum_length=240,
    )
    made.claim(at(DAY, 9), at(DAY, 10), mode="shared", units=(1, 2))
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": DAY.isoformat(), "length": 60}
    assert calendar_page(client, **query, days=1).status_code == 200
    for quantity in (1, 2):
        begun = time.perf_counter()
        response = calendar_page(client, **query, days=31, quantity=quantity)
        took = time.perf_counter() - begun
        assert response.status_code == 200
        periods = json.loads(response.content)["containers"][0]["periods"]
        # 181 starts a day run an hour before 22:00; for two, those that
        # meet 09:00-10:00 on day one do not.
        assert sum(len(period["starts"]) for period in periods) == (
            181 * 31 if quantity == 1 else 181 * 31 - 23
        )
        request.node.user_properties.append((f"seconds_for_{quantity}", took))
        assert took < 1.0, took


def test_the_cinema_full_for_a_month_answers_in_under_a_second_and_500_kb(
    request: pytest.FixtureRequest,
) -> None:
    org, member = at_home("scenarios/cinema.toml")
    # A screening every evening for the month; the hire evenings go.
    BookingConfiguration.objects.of(org).filter(number__in=[2, 3]).update(
        status="disabled"
    )
    BookingConfiguration.objects.of(org).filter(number=1).update(
        period_start_weekdays=list(range(7)), start_date=None, stop_date=None
    )
    cinema = Asset.objects.of(org).get(name="Cinema")
    seats = made_up.spots_of(made_up.floor_plan_of(cinema), *range(1, 201))
    states = ("confirmed", "held", "blocked", "confirmed")
    for days in range(31):
        evening = DAY + timedelta(days=days)
        for part, state in enumerate(states):
            TakenTime.objects.take(
                organization=org,
                asset=cinema,
                starts_at=at(evening, 19),
                ends_at=at(evening, 21),
                mode="shared",
                quantity=50,
                state=state,
                units=seats[part * 50 : part * 50 + 50],
            )
    client = Client()
    sign_in(client, member)
    query = {"asset": cinema.pk, "start": DAY.isoformat()}
    assert calendar_page(client, **query, days=1).status_code == 200
    begun = time.perf_counter()
    response = calendar_page(client, **query, days=31)
    took = time.perf_counter() - begun
    assert response.status_code == 200
    (screenings,) = json.loads(response.content)["containers"]
    assert len(screenings["periods"]) == 31
    assert all(
        len(period["taken"]) == 200 and period["starts"] == []
        for period in screenings["periods"]
    )
    size = len(response.content)
    # Recorded in the test report: docs/booking-model.md gives the size measured.
    request.node.user_properties += [("json_bytes", size), ("seconds", took)]
    assert took < 1.0, took
    assert size < 500_000, size


def test_the_page_answers_only_the_signed_in_member_at_their_address(
    made: Example,
) -> None:
    client = Client()
    sign_in(client, made.member)
    query = {"asset": made.asset.pk, "start": "2027-03-01", "days": 1, "length": 60}
    assert calendar_page(client, **query).status_code == 200
    made_up.everything("riverside")
    elsewhere = client.get(
        "/booking/calendar/?" + "&".join(f"{k}={v}" for k, v in query.items()),
        HTTP_HOST="riverside.sandbox.example.org",
        secure=True,
    )
    assert elsewhere.status_code != 200
    # Presenting the session at another organization ends it everywhere.
    assert calendar_page(client, **query).status_code == 302
    sign_in(client, made.member)
    assert calendar_page(client, **query).status_code == 200
    made.member.status = "disabled"
    made.member.save(update_fields=["status"])
    assert calendar_page(client, **query).status_code == 302


def test_a_calendar_is_freed_when_it_returns_not_by_the_cycle_collector() -> None:
    # the grid's caches held the grid itself, so a month's
    # calendar (about 8 MB here) waited for the cycle
    # collector, and the sandbox ran out of memory between collections.
    # Measured in this thread while no other thread allocates.
    org, member = scenario("scenarios/sauna-palace.toml")
    ask(org, member, asset="Sauna 1", length=60, days=31)
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        ask(org, member, asset="Sauna 1", length=60, days=31)
        held = tracemalloc.get_traced_memory()[0]
        gc.collect()
        waited = held - tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
        gc.enable()
    # What is left is Django's own (about 250 KiB).
    assert waited < 1024 * 1024


def narrow_member(org: Organization, member: Member) -> tuple[Member, Role]:
    """A member whose role holds none of the standard role's categories."""
    standard = Role.objects.of(org).get(pk=member.role_id)
    org.standard_role = standard
    org.save()
    narrow = Role.objects.create(organization=org, name="Narrow")
    olga = Member.objects.create(
        organization=org,
        role=narrow,
        email="olga.narrow@example.com",
        status="active",
        origin="member import",
    )
    return olga, standard


def test_the_price_list_is_the_standard_roles_for_a_member_whose_role_lacks_it() -> (
    None
):
    org, member = scenario("scenarios/sauna-palace.toml")
    olga, standard = narrow_member(org, member)
    # The narrow member's own calendar has nothing in the standard category.
    assert ask(org, olga, asset="Sauna 1", category="private").containers == ()
    own = ask(org, member, asset="Sauna 1", category="private")
    listed = member_calendar(
        member=olga,
        asset_id=Asset.objects.of(org).get(name="Sauna 1").pk,
        booking_category_id=private(org),
        start_date=DAY,
        end_date=DAY + timedelta(days=1),
        now=NOW,
        role_id=standard.pk,
    )
    assert [row.number for row in listed.containers] == [
        row.number for row in own.containers
    ]
    assert listed.containers
    assert all(private(org) in row.category_ids for row in listed.containers)


def test_the_price_list_over_http_carries_the_standard_roles_prices() -> None:
    org, member = at_home("scenarios/sauna-palace.toml")
    olga, _ = narrow_member(org, member)
    client = Client()
    sign_in(client, olga)
    query = {
        "asset": Asset.objects.of(org).get(name="Sauna 1").pk,
        "category": private(org),
        "start": DAY.isoformat(),
        "days": 1,
    }
    own = calendar_page(client, **query)
    assert own.status_code == 200
    assert own.json()["containers"] == []
    listed = calendar_page(client, **query, prices=1)
    assert listed.status_code == 200
    containers = listed.json()["containers"]
    assert containers
    assert all(private(org) in row["categories"] for row in containers)
    assert any(row["prices"] for row in containers)


def test_the_price_list_without_a_standard_role_is_empty() -> None:
    org, member = at_home("scenarios/sauna-palace.toml")
    org.standard_role = None
    org.save()
    client = Client()
    sign_in(client, member)
    found = calendar_page(
        client,
        asset=Asset.objects.of(org).get(name="Sauna 1").pk,
        start=DAY.isoformat(),
        days=1,
        prices=1,
    )
    assert found.status_code == 200
    assert found.json()["containers"] == []


def test_only_another_known_members_booking_is_named() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = another(org, member)
    order(org, member, at(hour=10), at(hour=11))
    order(org, other, at(hour=12), at(hour=13))
    take(org, "Sauna 1", at(hour=14), at(hour=15), units=(5,), state="blocked")
    # Only first and last name: the member list's name is made of them.
    split = Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="sven.split@example.com",
        first_name="Sven",
        last_name="Split",
        status="active",
        origin="member import",
    )
    order(org, split, at(hour=16), at(hour=17), confirmed=True)
    gone = Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="gone@example.com",
        full_name="Gina Gone",
        status="active",
        origin="member import",
    )
    order(org, gone, at(hour=17), at(hour=18))
    member_data.erase_identity(gone)
    spans = ask(org, member, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert [(span.first, span.state, span.mine, span.name) for span in spans] == [
        (0, "held", True, ""),
        (2, "held", False, "Olga Otherby"),
        (6, "confirmed", False, "Sven Split"),
        (7, "held", False, ""),
        # The block is on spot 5, so it comes after spot 1's spans.
        (4, "block", False, ""),
    ]


def test_bookings_one_after_the_other_are_two_spans_each_with_its_name() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = another(org, member)
    third = Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="tor.third@example.com",
        full_name="Tor Third",
        status="active",
        origin="member import",
    )
    order(org, other, at(hour=10), at(hour=11))
    order(org, third, at(hour=11), at(hour=12))
    # The same member twice in a row: still two.
    order(org, other, at(hour=12), at(hour=13), confirmed=True)
    order(org, other, at(hour=13), at(hour=14), confirmed=True)
    spans = ask(org, member, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert [(span.first, span.last, span.name) for span in spans] == [
        (0, 0, "Olga Otherby"),
        (1, 1, "Tor Third"),
        (2, 2, "Olga Otherby"),
        (3, 3, "Olga Otherby"),
    ]
    # One booking of two hours is one span.
    order(org, third, at(hour=15), at(hour=17))
    spans = ask(org, member, asset="Sauna 1", length=60).containers[0].periods[0].taken
    assert [(s.first, s.last) for s in spans if s.name == "Tor Third"] == [
        (1, 1),
        (5, 6),
    ]


def test_another_organizations_booking_is_never_named() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    text = (SETUPS / "scenarios/sauna-palace.toml").read_text()
    elsewhere = setup.load(
        setup_file.parse(
            re.sub(r'(?m)^subdomain = ".*"$', 'subdomain = "elsewhere"', text)
        ),
        today=DAY,
    )
    assert elsewhere.standard_role_id is not None
    stranger = Member.objects.create(
        organization=elsewhere,
        role_id=elsewhere.standard_role_id,
        email="stranger@example.com",
        full_name="Sture Stranger",
        status="active",
        origin="member import",
    )
    order(elsewhere, stranger, at(hour=10), at(hour=11))
    found = ask(org, member, asset="Sauna 1", length=60)
    assert found.containers[0].periods[0].taken == ()


def test_naming_another_members_booking_costs_one_query() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    other = another(org, member)
    order(org, member, at(hour=10), at(hour=11))

    def count() -> tuple[int, int]:
        with CaptureQueriesContext(connection) as queries:
            ask(org, member, asset="Sauna 1", length=60)
        names = [
            row
            for row in queries.captured_queries
            if '"core_booking"."taken_time_id"' in row["sql"]
            and '"core_member"."full_name"' in row["sql"]
        ]
        return len(queries), len(names)

    without = count()
    order(org, other, at(hour=12), at(hour=13))
    with_name = count()
    assert without[1] == 0
    assert with_name[1] == 1
    assert with_name[0] == without[0] + 1


def test_the_page_says_when_an_asset_is_closed_and_why() -> None:
    org, member = at_home("scenarios/laundry.toml")
    washer = Asset.objects.of(org).get(name="Washer A")
    AvailabilityClosure.objects.create(
        first_date=DAY, last_date=DAY, reason="holiday", organization=org, asset=washer
    )
    AvailabilityClosure.objects.create(
        first_date=DAY + timedelta(days=1),
        last_date=DAY + timedelta(days=1),
        organization=org,
        asset=washer,
    )
    # The weekly availability does not open on the Sunday (the 7th).
    AvailableSpan.objects.of(org).filter(
        availability_calendar__asset=washer, weekday=Weekday.SUNDAY
    ).delete()
    client = Client()
    sign_in(client, member)
    body = json.loads(
        calendar_page(client, asset=washer.pk, start=DAY.isoformat(), days=7).content
    )
    row = {"asset": washer.pk}
    assert body["assets"] == [washer.pk]
    # Only the day, the kind and the reason's key: no who, no when made.
    assert body["closed_days"] == [
        {**row, "day": "2027-03-01", "by": "closure", "reason": "holiday"},
        {**row, "day": "2027-03-02", "by": "closure", "reason": ""},
        {**row, "day": "2027-03-07", "by": "weekly", "reason": ""},
    ]
    # Closed whatever is asked of the asset: a type it has no container of.
    other = BookingType.objects.of(org).exclude(
        pk__in=BookingConfiguration.objects.of(org)
        .filter(asset=washer)
        .values("booking_type_id")
    )
    assert other.exists()
    typed = json.loads(
        calendar_page(
            client,
            asset=washer.pk,
            start=DAY.isoformat(),
            days=7,
            type=other.first().pk,  # type: ignore[union-attr]
        ).content
    )
    assert typed["containers"] == []
    assert [item["day"] for item in typed["closed_days"]] == [
        "2027-03-01",
        "2027-03-02",
        "2027-03-07",
    ]
    # An open day is in no list; a day outside the query is not asked for.
    assert "2027-03-03" not in str(body["closed_days"])
    window = json.loads(
        calendar_page(
            client, asset=washer.pk, start="2027-03-02", days=1, length=60
        ).content
    )
    assert [item["day"] for item in window["closed_days"]] == ["2027-03-02"]
    # A closure that covers several days names each; the first reason decides.
    AvailabilityClosure.objects.create(
        first_date=DAY + timedelta(days=2),
        last_date=DAY + timedelta(days=3),
        reason="repair",
        organization=org,
        asset=washer,
    )
    AvailabilityClosure.objects.create(
        first_date=DAY + timedelta(days=3),
        last_date=DAY + timedelta(days=3),
        reason="holiday",
        organization=org,
        asset=washer,
    )
    many = json.loads(
        calendar_page(client, asset=washer.pk, start=DAY.isoformat(), days=7).content
    )
    assert {item["day"]: item["reason"] for item in many["closed_days"]} == {
        "2027-03-01": "holiday",
        "2027-03-02": "",
        "2027-03-03": "repair",
        "2027-03-04": "repair",
        "2027-03-07": "",
    }


def test_the_closed_days_are_told_to_members_of_the_organization_only() -> None:
    org, member = at_home("scenarios/laundry.toml")
    washer = Asset.objects.of(org).get(name="Washer A")
    AvailabilityClosure.objects.create(
        organization=org, asset=washer, first_date=DAY, last_date=DAY
    )
    foreign_member = made_up.everything("riverside").member
    # A member of another organization asking for this asset gets nothing.
    found = member_calendar(
        member=foreign_member,
        asset_id=washer.pk,
        start_date=DAY,
        end_date=DAY + timedelta(days=2),
    )
    assert found.closed_days == () and found.asset_ids == ()
    # So does a member who is no longer active.
    member.status = "disabled"
    member.save(update_fields=["status"])
    found = member_calendar(
        member=member,
        asset_id=washer.pk,
        start_date=DAY,
        end_date=DAY + timedelta(days=2),
    )
    assert found.closed_days == ()


def test_closed_days_cost_the_same_queries_for_one_asset_or_many() -> None:
    org, member = at_home("scenarios/laundry.toml")
    assets = list(Asset.objects.of(org).order_by("pk"))
    assert len(assets) >= 3
    for asset in assets:
        AvailabilityClosure.objects.create(
            organization=org, asset=asset, first_date=DAY, last_date=DAY
        )

    def queries(chosen: list[Asset]) -> int:
        with CaptureQueriesContext(connection) as seen:
            found = closed_days(member, chosen, DAY, DAY + timedelta(days=3))
        assert {row.asset_id for row in found} == {asset.pk for asset in chosen}
        return len(seen)

    assert queries(assets[:1]) == queries(assets) == 5


def test_a_closed_day_offers_nothing_to_book() -> None:
    org, member = at_home("scenarios/laundry.toml")
    washer = Asset.objects.of(org).get(name="Washer A")
    room = Asset.objects.of(org).get(name="Whole laundry room")
    AvailabilityClosure.objects.create(
        organization=org, asset=washer, first_date=DAY, last_date=DAY, reason="repair"
    )
    # The whole laundry room (a fixed container hired on Sundays) closed that Sunday.
    sunday = DAY + timedelta(days=6)
    AvailabilityClosure.objects.create(
        organization=org, asset=room, first_date=sunday, last_date=sunday
    )
    category = BookingCategory.objects.of(org).order_by("pk").first()
    assert category is not None
    # No container, so no start, no night and no dialog to open: the day is empty.
    assert ask(org, member, asset="Washer A", length=120).containers == ()
    assert ask(org, member, asset="Whole laundry room", day=sunday).containers == ()
    next_day = ask(org, member, asset="Washer A", length=120, day=DAY + timedelta(1))
    assert next_day.containers

    def priced(on: date) -> Quote:
        return quote(
            member=member,
            configuration_id=BookingConfiguration.objects.of(org).get(asset=washer).pk,
            booking_category_id=category.pk,
            quantity=1,
            starts_at=at(on, 10),
            ends_at=at(on, 12),
            now=NOW,
        )

    # Not priced, so never in the cart; the day after is.
    assert priced(DAY).reason == "not-offered"
    assert priced(DAY + timedelta(1)).reason is None
    session = SessionStore()
    config = BookingConfiguration.objects.of(org).get(asset=washer).pk
    row = booking_cart.add(
        session,
        member,
        Choice(config, category.pk, 1, at(DAY, 10), at(DAY, 12)),
        0,
        NOW,
    )
    assert row == booking_cart.Refusal("not-offered")
