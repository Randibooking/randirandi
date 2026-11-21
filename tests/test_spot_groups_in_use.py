# SPDX-License-Identifier: AGPL-3.0-only
"""groups of spots in use (the drawing of 2026-10-17, section 2).

Lantern Supper Club, made up: one dining room of 20 seats at three tables, the
Window table (seats 1-8), the Long table (9-16) and the Bar (17-20), loaded
through the setup loader. Dinner on Friday has a price per table; Sunday lunch
books the same seats from a price book with none.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from randirandi import setup_file
from randirandi.core import booking_cart, booking_checkout, setup
from randirandi.core.booking_quote import Choice, Quote, cart_quotes, quote
from randirandi.core.member_calendar import (
    ContainerCalendar,
    GroupFree,
    member_calendar,
)
from randirandi.core.models import (
    Asset,
    AssetInventory,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    Member,
    Order,
    Organization,
    PriceBookEntry,
    PriceTier,
    SpotGroup,
)
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, Example, at, scenario
from tests.test_login_sessions import get, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_member_calendar import at_home, calendar_page
from tests.test_ordering import race
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))
FRIDAY = DAY + timedelta(days=4)
SUNDAY = DAY + timedelta(days=6)
SCENARIO = "scenarios/supper-club.toml"
# Kroner a seat for three hours, as the file has them, in the smallest unit.
WINDOW, LONG, NORMAL, LUNCH = 50000, 40000, 35000, 20000


@dataclass
class Club:
    org: Organization
    member: Member

    @property
    def dining_room(self) -> Asset:
        return Asset.objects.of(self.org).get(name="Dining room")

    def group(self, name: str) -> SpotGroup:
        return SpotGroup.objects.of(self.org).get(name=name)

    def request(
        self,
        quantity: int,
        *,
        spots: tuple[int, ...] = (),
        group: str | None = None,
        lunch: bool = False,
    ) -> BookingRequest:
        day, first, last = (SUNDAY, 12, 15) if lunch else (FRIDAY, 18, 21)
        return BookingRequest(
            BookingConfiguration.objects.of(self.org).get(number=2 if lunch else 1).pk,
            BookingCategory.objects.of(self.org).get(name="guest").pk,
            at(day, first),
            at(day, last),
            quantity,
            spots=spots,
            group_id=None if group is None else self.group(group).pk,
        )

    def book(self, wanted: BookingRequest) -> Order:
        return create_order(
            member=self.member,
            requests=[wanted],
            now=NOW,
            accepted_version=made_up.accepted(self.member),
        )

    def party(self, quantity: int, **asking: Any) -> Order:
        return self.book(self.request(quantity, **asking))

    def calendar(
        self, *, group: str | None = None, quantity: int = 1, lunch: bool = False
    ) -> tuple[ContainerCalendar, ...]:
        day = SUNDAY if lunch else FRIDAY
        return member_calendar(
            member=self.member,
            asset_id=self.dining_room.pk,
            quantity=quantity,
            start_date=day,
            end_date=day + timedelta(days=1),
            now=NOW,
            group_id=None if group is None else self.group(group).pk,
        ).containers

    def quote(self, wanted: BookingRequest) -> Quote:
        return quote(
            member=self.member,
            configuration_id=wanted.configuration_id,
            booking_category_id=wanted.booking_category_id,
            quantity=wanted.quantity,
            starts_at=wanted.starts_at,
            ends_at=wanted.ends_at,
            now=NOW,
            spots=wanted.spots,
            group_id=wanted.group_id,
        )


@pytest.fixture
def club() -> Iterator[Club]:
    org, member = scenario(SCENARIO)
    with application_login():
        yield Club(org, member)


@pytest.fixture
def club_apart() -> Club:
    """For a race, which logs in as the application itself."""
    return Club(*scenario(SCENARIO))


def seats(order: Order) -> list[int]:
    held = order.bookings.get().taken_time.allocations.order_by("spot__number")
    return list(held.values_list("spot__number", flat=True))


def lines(order: Order) -> list[tuple[str | None, int, int]]:
    """Each line's group name, spots and amount, in the order they were made."""
    return [
        (
            None if line.spot_group is None else line.spot_group.name,
            line.capacity_quantity,
            line.total,
        )
        for line in BookingLine.objects.of(order.organization_id)
        .filter(booking__order=order)
        .select_related("spot_group")
        .order_by("pk")
    ]


def test_the_five_parties_of_the_drawing_land_as_drawn(club: Club) -> None:
    # Parties of 4, 6, 3, 4 and 2 come one after the other. Table 1 has 4
    # left for the party of 6, who go to Table 2 whole; the party of 3 goes
    # back to Table 1; the next 4 only fit at Table 3; the 2 fill Table 2.
    landed = [seats(club.party(size)) for size in (4, 6, 3, 4, 2)]
    assert landed == [
        [1, 2, 3, 4],
        [9, 10, 11, 12, 13, 14],
        [5, 6, 7],
        [17, 18, 19, 20],
        [15, 16],
    ]
    # Seat 8 is the one seat left, and the calendar says whose table it is.
    (dinner,) = club.calendar()
    (start,) = dinner.periods[0].starts
    window, long, bar = (club.group(name).pk for name in ("Window", "Long", "Bar"))
    assert start.spots == (8,)
    assert start.free == (
        GroupFree(window, (8,)),
        GroupFree(long, ()),
        GroupFree(bar, ()),
    )
    with pytest.raises(OrderUnavailableError):
        club.party(2)


def test_a_party_of_ten_takes_the_lowest_free_spots_across_tables(
    club: Club,
) -> None:
    # No table seats ten: the lowest seats anywhere, as before groups.
    lunch = club.party(10, lunch=True)
    assert seats(lunch) == list(range(1, 11))
    assert lunch.total == 10 * LUNCH
    # With the seats spread out, the same: Window 5-8 and Long 15-16 are the
    # lowest free ones and the Bar is the last. The lines follow the tables.
    club.party(4)  # Window 1-4.
    club.party(6)  # Long 9-14, the Window has 4 left.
    dinner = club.party(10)
    assert seats(dinner) == [5, 6, 7, 8, 15, 16, 17, 18, 19, 20]
    assert lines(dinner) == [
        ("Window", 4, 4 * WINDOW),
        ("Long", 2, 2 * LONG),
        ("Bar", 4, 4 * NORMAL),
    ]
    assert dinner.total == 4 * WINDOW + 2 * LONG + 4 * NORMAL


def test_three_picked_seats_over_two_groups_cost_the_sum(club: Club) -> None:
    # Seat 8 is at the Window, seats 9 and 10 at the Long table.
    wanted = club.request(3, spots=(8, 9, 10))
    shown = club.quote(wanted)
    assert shown.reason is None
    assert shown.total == WINDOW + 2 * LONG
    assert [(line.group_id, line.times, line.total) for line in shown.lines] == [
        (club.group("Window").pk, 1, WINDOW),
        (club.group("Long").pk, 2, 2 * LONG),
    ]
    order = club.book(wanted)
    assert order.total == WINDOW + 2 * LONG
    # The lines of the booking record each group, and add up to the order.
    assert lines(order) == [("Window", 1, WINDOW), ("Long", 2, 2 * LONG)]
    assert seats(order) == [8, 9, 10]


def test_a_group_without_a_price_of_its_own_takes_the_normal_price(
    club: Club,
) -> None:
    # Seat 7 at the Window, 12 at the Long table, 18 at the Bar: the Bar has
    # no price of its own.
    order = club.book(club.request(3, spots=(7, 12, 18)))
    assert order.total == WINDOW + LONG + NORMAL
    assert lines(order) == [
        ("Window", 1, WINDOW),
        ("Long", 1, LONG),
        ("Bar", 1, NORMAL),
    ]


def test_a_container_whose_price_book_has_no_group_prices_charges_every_spot_alike(
    club: Club,
) -> None:
    across = club.request(3, spots=(1, 9, 17), lunch=True)
    shown = club.quote(across)
    assert shown.total == 3 * LUNCH
    assert [(line.group_id, line.times) for line in shown.lines] == [(None, 3)]
    order = club.book(across)
    assert order.total == 3 * LUNCH
    assert lines(order) == [(None, 3, 3 * LUNCH)]
    assert seats(order) == [1, 9, 17]
    # Nor does the party that stays together cost more at the Window table.
    assert club.book(club.request(2, lunch=True)).total == 2 * LUNCH


def test_asking_for_a_group_answers_only_its_spots(club: Club) -> None:
    club.party(6, group="Long")  # Long 9-14, though the Window has more room.
    (dinner,) = club.calendar(group="Long")
    assert dinner.floor_plan is not None
    assert [spot.number for spot in dinner.floor_plan.spots] == list(range(9, 17))
    assert [group.name for group in dinner.floor_plan.groups] == ["Long"]
    assert dinner.max_quantity == 8
    period = dinner.periods[0]
    # Only the group's seats are taken cells, and its two seats left are the
    # only room: a party of 3 has no start here although the room is half empty.
    taken = {span.spot for span in period.taken if span.spot is not None}
    assert sorted(taken) == [9, 10, 11, 12, 13, 14]
    assert [start.spots for start in period.starts] == [(15,)]
    (three,) = club.calendar(group="Long", quantity=3)
    assert three.periods[0].starts == ()
    assert club.calendar(quantity=3)[0].periods[0].starts[0].spots == (1, 2, 3)
    # A party of 9 does not fit the group of 8 although the room seats 20.
    assert club.calendar(group="Long", quantity=9) == ()
    assert club.calendar(quantity=9) != ()
    # The quote and the order take only the group's spots.
    asked = club.request(2, group="Long")
    assert club.quote(asked).total == 2 * LONG
    assert seats(club.book(asked)) == [15, 16]
    with pytest.raises(OrderUnavailableError):
        club.book(club.request(1, group="Long"))
    assert club.quote(club.request(1, group="Long")).reason == "taken"
    # A pick outside the group is refused, and so is a group of no floor plan.
    with pytest.raises(OrderUnavailableError):
        club.book(club.request(1, spots=(1,), group="Long"))
    assert club.quote(club.request(1, spots=(1,), group="Long")).reason == "taken"
    foreign = replace(club.request(1), group_id=a_group_elsewhere().pk)
    with pytest.raises(OrderUnavailableError):
        club.book(foreign)
    assert club.quote(foreign).reason == "not-offered"


def a_group_elsewhere() -> SpotGroup:
    """A group of another organization."""
    other = made_up.everything("other")
    group = SpotGroup(
        organization=other.organization,
        floor_plan=made_up.floor_plan_of(other.asset),
        name="Elsewhere",
    )
    group.save()
    return group


def test_a_group_of_another_organization_answers_nothing(club: Club) -> None:
    # The calendar answers no container for a group it cannot see.
    group = a_group_elsewhere()
    assert (
        member_calendar(
            member=club.member,
            asset_id=club.dining_room.pk,
            start_date=FRIDAY,
            end_date=FRIDAY + timedelta(days=1),
            now=NOW,
            group_id=group.pk,
        ).containers
        == ()
    )


def test_the_calendar_names_each_spots_group_and_the_free_spots_per_group(
    club: Club,
) -> None:
    (dinner,) = club.calendar()
    assert dinner.floor_plan is not None
    named = {
        (spot.number, spot.group.number, spot.group.name, spot.group.word)
        for spot in dinner.floor_plan.spots
        if spot.group is not None
    }
    assert len(named) == 20
    assert {(1, 1, "Window", "table"), (9, 2, "Long", "table")} <= named
    assert (20, 3, "Bar", "table") in named
    assert [(g.number, g.name) for g in dinner.floor_plan.groups] == [
        (1, "Window"),
        (2, "Long"),
        (3, "Bar"),
    ]
    # Free spots per group, for each bookable start, after one party.
    club.party(5)  # Window 1-5.
    (dinner,) = club.calendar()
    (start,) = dinner.periods[0].starts
    assert [(free.group, len(free.spots)) for free in start.free] == [
        (club.group("Window").pk, 3),
        (club.group("Long").pk, 8),
        (club.group("Bar").pk, 4),
    ]
    assert start.free[0].spots == (6, 7, 8)


def test_two_orders_at_once_for_the_last_room_at_one_table_end_in_one_order_there(
    club_apart: Club, monkeypatch: pytest.MonkeyPatch
) -> None:
    club = club_apart
    club.party(4)  # Window 1-4: room for four more, and Long and Bar free.
    four = club.request(4)
    stand_in = cast(Example, SimpleNamespace(member=club.member))
    (first, second), states = race(stand_in, four, four, monkeypatch)
    assert isinstance(first, Order) and isinstance(second, Order)
    # The first has the last room at the Window; the second finds it taken,
    # and goes to the next table with room, the Long table.
    assert seats(first) == [5, 6, 7, 8]
    assert seats(second) == [9, 10, 11, 12]
    assert states == ["40001"]
    assert Order.objects.of(club.org).count() == 3
    assert connection.in_atomic_block is False


def test_the_cart_keeps_the_group_and_the_order_takes_it(club: Club) -> None:
    wanted = club.request(2, group="Long")
    choice = Choice(
        wanted.configuration_id,
        wanted.booking_category_id,
        2,
        wanted.starts_at,
        wanted.ends_at,
        group_id=wanted.group_id,
    )
    session = SessionStore()
    row = booking_cart.add(session, club.member, choice, 2 * LONG, NOW)
    assert isinstance(row, booking_cart.CartRow)
    assert row.choice.group_id == wanted.group_id
    assert [booking_cart.rows(session, club.member)[0].choice.group_id] == [
        wanted.group_id
    ]
    assert [q.total for q in cart_quotes(member=club.member, cart=[choice])] == [
        2 * LONG
    ]
    placed = booking_checkout.order(
        session,
        club.member,
        2 * LONG,
        NOW,
        accepted_version=made_up.accepted(club.member),
    )
    assert isinstance(placed, booking_checkout.Placed)
    order = Order.objects.of(club.org).get(public_id=placed.public_id)
    assert seats(order) == [9, 10]


def test_the_pages_take_the_group_and_name_it(page_settings: None, club: Club) -> None:
    del club
    org, member = at_home(SCENARIO)
    client = Client()
    sign_in(client, member)
    dining = Asset.objects.of(org).get(name="Dining room").pk
    long = SpotGroup.objects.of(org).get(name="Long").pk
    query = {"asset": dining, "start": FRIDAY.isoformat(), "days": 1}
    body = json.loads(calendar_page(client, **query).content)
    (dinner,) = body["containers"]
    plan = dinner["floor_plan"]
    assert [group["name"] for group in plan["groups"]] == ["Window", "Long", "Bar"]
    assert plan["groups"][1] == {
        "id": long,
        "number": 2,
        "name": "Long",
        "word": "table",
    }
    assert plan["spots"][8]["group"] == long and plan["spots"][8]["number"] == 9
    start = dinner["periods"][0]["starts"][0]
    assert [(part["group"], len(part["spots"])) for part in start["free"]] == [
        (plan["groups"][0]["id"], 8),
        (long, 8),
        (plan["groups"][2]["id"], 4),
    ]
    asked = json.loads(calendar_page(client, **query, group=long).content)
    (only,) = asked["containers"]
    assert [spot["number"] for spot in only["floor_plan"]["spots"]] == list(
        range(9, 17)
    )
    assert only["max_quantity"] == 8
    # The price of two seats at the Long table, by the page.
    price = get(
        client,
        "/booking/price/?"
        + urlencode(
            {
                "container": dinner["id"],
                "category": BookingCategory.objects.of(org).get(name="guest").pk,
                "quantity": 2,
                "group": long,
                "start": _local(org, at(FRIDAY, 18)),
                "end": _local(org, at(FRIDAY, 21)),
            }
        ),
    )
    shown = json.loads(price.content)
    assert shown["available"] is True and shown["total"] == 2 * LONG
    assert [(line["group"], line["capacity_quantity"]) for line in shown["lines"]] == [
        (long, 2)
    ]


def _local(org: Organization, moment: datetime) -> str:
    return moment.astimezone(ZoneInfo(org.time_zone)).isoformat()


def test_a_party_keeps_the_table_of_the_date_period_before(club: Club) -> None:
    # Keeping the spots of the week before (W18) comes before the lowest
    # table: two seats picked at the Bar one Friday are asked for again the
    # next Friday without a pick, and the Bar keeps them.
    first = club.request(2, spots=(17, 18))
    again = club.request(2)
    later = timedelta(days=7)
    again = replace(
        again, starts_at=again.starts_at + later, ends_at=again.ends_at + later
    )
    order = create_order(
        member=club.member,
        requests=[first, again],
        now=NOW,
        accepted_version=made_up.accepted(club.member),
    )
    assert sorted(
        spot
        for booking in order.bookings.all()
        for spot in booking.taken_time.allocations.values_list(
            "spot__number", flat=True
        )
    ) == [17, 17, 18, 18]
    assert lines(order)[0][0] == "Bar"


def entry_for(club: Club, group: str) -> PriceBookEntry:
    return PriceBookEntry.objects.of(club.org).get(spot_group=club.group(group))


def test_a_price_for_a_group_must_be_for_a_spot_of_the_container_floor_plan(
    club: Club,
) -> None:
    window = entry_for(club, "Window")
    booth = AssetInventory(
        organization=club.org, asset=club.dining_room, name="Booths", unit="booth"
    )
    booth.save()
    elsewhere = SpotGroup(organization=club.org, floor_plan=booth, name="Booth row")
    elsewhere.save()
    window.spot_group = elsewhere
    with pytest.raises(ValidationError, match="not on the floor plan"):
        window.full_clean()
    window.spot_group = a_group_elsewhere()
    with pytest.raises(ValidationError, match="not on the floor plan"):
        window.full_clean()
    window.spot_group = club.group("Window")
    window.billing_basis = "per_booking"
    with pytest.raises(ValidationError, match="priced per capacity unit"):
        window.full_clean()


def test_the_database_holds_one_price_for_each_group_and_one_for_none(
    club: Club,
) -> None:
    normal = PriceBookEntry.objects.of(club.org).get(
        spot_group__isnull=True, booking_configuration__number=1
    )
    for group in (None, club.group("Long")):
        with pytest.raises(IntegrityError), transaction.atomic():
            PriceBookEntry.objects.create(
                organization=club.org,
                price_book=normal.price_book,
                booking_configuration=normal.booking_configuration,
                booking_category=normal.booking_category,
                spot_group=group,
                tiers_go_by=normal.tiers_go_by,
                billing_basis=normal.billing_basis,
            )
    # The Bar has none yet: its own price may be made.
    PriceBookEntry.objects.create(
        organization=club.org,
        price_book=normal.price_book,
        booking_configuration=normal.booking_configuration,
        booking_category=normal.booking_category,
        spot_group=club.group("Bar"),
        tiers_go_by=normal.tiers_go_by,
        billing_basis=normal.billing_basis,
    )


def test_a_group_price_without_tiers_takes_the_category_out_of_the_offer(
    club: Club,
) -> None:
    # A booking of those spots could not be priced, so no spot is offered.
    PriceTier.objects.of(club.org).filter(
        price_book_entry=entry_for(club, "Long")
    ).delete()
    assert club.calendar() == ()
    with pytest.raises(OrderUnavailableError):
        club.party(2)


def test_a_group_price_per_booking_takes_the_category_out_of_the_offer(
    club: Club,
) -> None:
    PriceBookEntry.objects.of(club.org).filter(spot_group=club.group("Long")).update(
        billing_basis="per_booking"
    )
    assert club.calendar() == ()


def club_text() -> str:
    return (SETUPS / SCENARIO).read_text()


@pytest.mark.parametrize(
    ("old", "new", "fault"),
    [
        (
            'group = "Window"',
            'group = "Balcony"',
            "the floor plan of configuration 1 has no group named 'Balcony'",
        ),
        (
            'group = "Window"',
            'group = "Long"',
            "this configuration and booking category already have a price for "
            "the group 'Long'.",
        ),
        (
            'billing_basis = "per_capacity_unit"\ngroup = "Window"',
            'billing_basis = "per_booking"\ngroup = "Window"',
            "is priced per spot, per_capacity_unit, in every entry of it.",
        ),
        (
            'group = "Window"',
            'group = ""',
            "",
        ),
    ],
    ids=["unknown-group", "twice", "per-booking", "no-group-is-the-normal-price"],
)
def test_the_file_refuses_a_group_price_the_floor_plan_cannot_take(
    old: str, new: str, fault: str
) -> None:
    text = club_text().replace(old, new, 1)
    if not fault:
        # An empty name is the container's normal price, given twice.
        with pytest.raises(setup_file.SetupError) as error:
            setup_file.parse(text)
        assert any("already have a price" in line for line in error.value.faults)
        return
    with pytest.raises(setup_file.SetupError) as error:
        setup_file.parse(text)
    assert any(fault in line for line in error.value.faults), error.value.faults


def test_the_file_refuses_a_group_price_without_the_normal_price() -> None:
    normal = (
        "[[price_books.entries]]\nbooking_configuration = 1\n"
        'booking_category = "guest"\ntiers_go_by = "length"\n'
        'billing_basis = "per_capacity_unit"\ntiers = [\n'
        '  { up_to = "3 h", paid_per = "3 h", amount = 350 },\n]\n\n'
    )
    text = club_text()
    assert normal in text
    with pytest.raises(setup_file.SetupError) as error:
        setup_file.parse(text.replace(normal, "", 1))
    assert any("needs the container's normal price" in x for x in error.value.faults)


def test_a_format_one_file_has_no_price_for_a_group() -> None:
    text = club_text()
    with pytest.raises(setup_file.SetupError):
        setup_file.parse(text.replace("format = 3", "format = 1", 1))
    parsed = setup_file.parse(text)
    price = parsed.price_books[0].entries[1]
    assert price.group == "Window"
    with pytest.raises(setup_file.SetupError, match="lose floor plan data"):
        setup_file.validate(replace(parsed, format=1))


def test_the_group_prices_go_out_and_come_back_in_an_export(club: Club) -> None:
    exported = setup.export(club.org)
    groups = [entry.group for book in exported.price_books for entry in book.entries]
    assert groups == ["", "Window", "Long", ""]
    assert setup_file.parse(setup_file.write(exported)) == exported
    # Whatever the file said of prices and floor plans, the export says again.
    written = setup_file.parse(club_text())
    assert exported.price_books == written.price_books
    assert exported.assets == written.assets


def test_undoing_the_migration_is_refused_while_a_price_names_a_group(
    club_apart: Club,
) -> None:
    before = "0018_payment_yes_or_no"
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    # Undoing 0027 first asks for the organization's own currency and zone.
    club_apart.org.currency = "NOK"
    club_apart.org.save(update_fields=["currency"])
    try:
        with pytest.raises(RuntimeError, match="names a group"):
            MigrationExecutor(connection).migrate([("core", before)])
        # The migrations after 0018 are undone by then: read the rows again
        # from the current schema.
        MigrationExecutor(connection).migrate(latest)
        assert PriceBookEntry.objects.of(club_apart.org).count() == 4
        # Without them, it is undone, with the other prices as they were.
        PriceBookEntry.objects.of(club_apart.org).filter(
            spot_group__isnull=False
        ).delete()
        MigrationExecutor(connection).migrate([("core", before)])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "column_name = 'spot_group_id' AND table_name IN "
                "('core_pricebookentry', 'core_bookingline')"
            )
            assert cursor.fetchone() == (0,)
    finally:
        MigrationExecutor(connection).migrate(latest)
    assert PriceBookEntry.objects.of(club_apart.org).count() == 2


def test_undoing_the_migration_is_refused_while_a_booking_line_names_a_group(
    club_apart: Club,
) -> None:
    with application_login():
        order = club_apart.book(club_apart.request(1, spots=(8,)))
    assert lines(order) == [("Window", 1, WINDOW)]
    # With no price left that names a group, the line alone refuses it.
    PriceBookEntry.objects.of(club_apart.org).filter(spot_group__isnull=False).delete()
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="names a group"):
            MigrationExecutor(connection).migrate([("core", "0018_payment_yes_or_no")])
        assert lines(order) == [("Window", 1, WINDOW)]
    finally:
        MigrationExecutor(connection).migrate(latest)
