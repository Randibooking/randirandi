# SPDX-License-Identifier: AGPL-3.0-only
"""the member's cart, its bookings checked together (T4).

Made-up setups only: a sauna club (seats by the hour) and a guest bedroom
whose named beds share stock with its overnight places.
"""

import json
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from randirandi.core import booking_cart, booking_conditions, ordering
from randirandi.core import calendar as calendar_module
from randirandi.core.booking_cart import CartRow, Refusal
from randirandi.core.booking_quote import Choice, Quote, cart_quotes, quote
from randirandi.core.member_calendar import MemberCalendar
from randirandi.core.models import (
    AvailabilityCalendar,
    AvailableSpan,
    Member,
    Organization,
    PriceTier,
)
from tests import made_up
from tests.test_calendar import DAY, ZONE, Example, at, scenario
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_member_calendar import ask, ids, starts, take

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))
BESIDE = "does-not-fit-beside-cart"
EVENING, MORNING = at(hour=17), at(DAY + timedelta(days=1), 8)


def choice(
    org: Organization,
    asset: str,
    kind: str,
    start: datetime,
    end: datetime,
    places: int = 1,
    spots: tuple[int, ...] = (),
) -> Choice:
    found = ids(org, asset, kind, "private")
    return Choice(
        found["configuration_id"],
        found["booking_category_id"],
        places,
        start,
        end,
        spots,
    )


def priced(member: Member, wanted: Choice, cart: list[Choice]) -> Any:
    return quote(
        member=member,
        configuration_id=wanted.configuration_id,
        booking_category_id=wanted.booking_category_id,
        quantity=wanted.quantity,
        spots=wanted.spots,
        starts_at=wanted.starts_at,
        ends_at=wanted.ends_at,
        now=NOW,
        cart=cart,
    )


def test_bookings_that_fit_alone_but_not_together_do_not_fit_beside_the_cart() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=10), at(hour=11), units=(1, 2, 3, 4))
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 3)
    more = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 2)
    assert priced(member, more, []).reason is None
    # Four stored and three in the cart leave one place (T6).
    assert priced(member, more, [seats]) == Quote(BESIDE, places_left=1)
    # After the stored seats leave at 11:00, both fit.
    later = choice(org, "Sauna 1", "Sauna visit", at(hour=11), at(hour=12), 2)
    assert priced(member, later, [seats]).reason is None
    # Another sauna is another space: the cart there is no obstacle.
    elsewhere = choice(org, "Sauna 2", "Sauna visit", at(hour=10), at(hour=11), 8)
    assert priced(member, elsewhere, [seats]).reason is None
    session = SessionStore()
    assert isinstance(booking_cart.add(session, member, seats, 45000, NOW), CartRow)
    assert booking_cart.add(session, member, more, 20000, NOW) == Refusal(BESIDE)
    assert len(booking_cart.rows(session, member)) == 1


def test_a_time_that_would_push_a_cart_booking_out_does_not_fit_beside_it() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=12), at(hour=14), units=(2, 3, 4, 5, 6, 7, 8))
    later = choice(org, "Sauna 1", "Sauna visit", at(hour=11), at(hour=13))
    earlier = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12))
    assert priced(member, earlier, []).reason is None
    # An order takes the earlier one first, on the lowest free spot, the one
    # the later one needs: it would push that one out.
    assert priced(member, later, []).reason is None
    assert priced(member, earlier, [later]) == Quote(BESIDE)


def test_the_calendar_agrees_with_the_price_for_every_cart() -> None:
    # The room Sunshine: a bed a night from Sunday to Thursday, the whole room
    # on Friday and Saturday (setups/scenarios/four-beds.toml).
    org, member = scenario("scenarios/four-beds.toml")
    friday = DAY + timedelta(days=4)

    def night(kind: str, day: date, places: int = 1) -> Choice:
        start, end = at(day, 17), at(day + timedelta(days=1), 8)
        return choice(org, "Sunshine", kind, start, end, places)

    def free(wanted: Choice, kind: str, cart: list[Choice]) -> bool:
        found = ask(
            org,
            member,
            asset="Sunshine",
            kind=kind,
            quantity=wanted.quantity,
            day=wanted.starts_at.astimezone(ZONE).date(),
            cart=cart,
        )
        return any(
            (start, end) == (wanted.starts_at, wanted.ends_at)
            for row in found.containers
            for start, end, _ in starts(row)
        )

    room = night("Private bedroom", friday)
    carts = [
        [],
        [night("Overnight bed", DAY)],
        [night("Overnight bed", DAY, 3)],
        [night("Overnight bed", DAY, 4)],
        [night("Overnight bed", DAY, 2), night("Overnight bed", DAY, 2)],
        [room],
        [night("Overnight bed", DAY), room],
    ]
    seen = set()
    for cart in carts:
        for places in range(1, 5):
            beds = night("Overnight bed", DAY, places)
            fits = priced(member, beds, cart).reason is None
            assert free(beds, "Overnight bed", cart) == fits, (cart, places)
            seen.add(fits)
        fits = priced(member, room, cart).reason is None
        assert free(room, "Private bedroom", cart) == fits, cart
        seen.add(fits)
    assert seen == {True, False}


def test_the_calendar_leaves_out_what_the_cart_needs() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 6)
    plain = ask(org, member, asset="Sauna 1", kind="Sauna visit", quantity=3, length=60)
    found = member_calendar_with(org, member, [seats], quantity=3)
    (visit,) = plain.containers
    assert [start for start, _, _ in starts(visit)][:2] == [at(hour=10), at(hour=11)]
    assert starts(found.containers[0])[0][0] == at(hour=12)
    # The cart is not shown as taken: the page draws its own.
    assert all(
        period.taken == ()
        for row in (*plain.containers, *found.containers)
        for period in row.periods
    )
    # A booking being changed is left out of its own way.
    assert member_calendar_with(org, member, [], quantity=3) == plain


def member_calendar_with(
    org: Organization, member: Member, cart: list[Choice], *, quantity: int
) -> MemberCalendar:
    return ask(
        org,
        member,
        asset="Sauna 1",
        kind="Sauna visit",
        quantity=quantity,
        length=60,
        cart=cart,
    )


def test_the_cart_s_beds_are_taken_in_the_starts_with_the_spots_left() -> None:
    org, member = scenario("scenarios/four-beds.toml")
    take(org, "Sunshine", EVENING, MORNING, units=(1,))
    beds = choice(org, "Sunshine", "Overnight bed", EVENING, MORNING, 2)

    def left(cart: list[Choice], quantity: int = 1) -> list[tuple[int, ...]]:
        found = ask(
            org,
            member,
            asset="Sunshine",
            kind="Overnight bed",
            quantity=quantity,
            cart=cart,
        )
        return [spots for _, _, spots in starts(found.containers[0])]

    # Bed 1 is taken; the cart's two beds are the next two (2 and 3).
    assert left([]) == [(2,)]
    assert left([beds]) == [(4,)]
    assert left([beds], 2) == []


def test_the_cart_holds_fifty_bookings() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11))
    # Stored straight in, in another sauna, so only the limit is at stake.
    elsewhere = choice(org, "Sauna 2", "Sauna visit", at(hour=10), at(hour=11))
    booking_cart._save(
        session,
        member,
        [CartRow(f"{index:08x}", elsewhere, 10000) for index in range(49)],
    )
    assert isinstance(booking_cart.add(session, member, seats, 10000, NOW), CartRow)
    assert booking_cart.add(session, member, seats, 10000, NOW) == Refusal("cart-full")
    assert len(booking_cart.rows(session, member)) == booking_cart.MAX_ROWS == 50


def test_a_moved_price_or_a_started_time_stops_the_order() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 2)
    assert booking_cart.add(session, member, seats, 1, NOW) == Refusal(
        "price-changed", 30000
    )
    row = booking_cart.add(session, member, seats, 30000, NOW)
    assert isinstance(row, CartRow)
    cart = booking_cart.look(session, member, NOW)
    assert (cart.total, cart.ready, cart.items[0].state) == (30000, True, "ok")
    PriceTier.objects.of(org).filter(up_to=60).update(amount=12000)
    cart = booking_cart.look(session, member, NOW)
    assert (cart.total, cart.ready, cart.items[0].state) == (
        34000,
        False,
        "price-changed",
    )
    assert cart.items[0].row.seen == 30000
    started = booking_cart.look(session, member, at(hour=10, minute=1))
    assert (started.total, started.ready, started.items[0].state) == (
        0,
        False,
        "already-started",
    )


def test_a_change_keeps_its_place_or_leaves_the_booking_as_it_was() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = SessionStore()
    first = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 4)
    second = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 4)
    one = booking_cart.add(session, member, first, 40000, NOW)
    two = booking_cart.add(session, member, second, 40000, NOW)
    assert isinstance(one, CartRow) and isinstance(two, CartRow)
    # Five places would not fit beside the other four.
    bigger = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 5)
    assert booking_cart.change(session, member, one.id, bigger, 50000, NOW) == Refusal(
        BESIDE
    )
    assert booking_cart.rows(session, member) == [one, two]
    # Three fit, in the first booking's place and with its id.
    smaller = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 3)
    changed = booking_cart.change(session, member, one.id, smaller, 45000, NOW)
    assert changed == CartRow(one.id, smaller, 45000)
    assert booking_cart.rows(session, member) == [changed, two]
    assert booking_cart.change(session, member, "00000000", smaller, 45000, NOW) is None
    assert booking_cart.remove(session, member, one.id)
    assert not booking_cart.remove(session, member, one.id)
    assert booking_cart.rows(session, member) == [two]
    booking_cart.empty(session, member)
    assert booking_cart.rows(session, member) == []


def test_the_cart_is_the_members_own() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    session = SessionStore()
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11))
    assert isinstance(booking_cart.add(session, member, seats, 10000, NOW), CartRow)
    other = Member.objects.create(
        organization=org,
        role_id=member.role_id,
        email="other@example.com",
        status="active",
        origin="member import",
    )
    assert booking_cart.rows(session, other) == []
    assert booking_cart.look(session, other, NOW).items == ()
    # Choices from another organization are never offered.
    riverside = made_up.everything("riverside")
    foreign = Choice(
        riverside.configuration.pk,
        riverside.private.pk,
        1,
        at(hour=10),
        at(hour=11),
    )
    assert booking_cart.add(session, member, foreign, 0, NOW) == Refusal("not-offered")
    # A row saved before the container was named  reads as empty.
    stored = session[booking_cart.SESSION_KEY]
    assert len(booking_cart.rows(session, member)) == 1
    stored["rows"] = [
        {key: value for key, value in row.items() if key != "container"}
        for row in stored["rows"]
    ]
    session[booking_cart.SESSION_KEY] = stored
    assert booking_cart.rows(session, member) == []
    # A stored cart that does not read is empty.
    session[booking_cart.SESSION_KEY] = {"member": member.pk, "rows": [{"id": 1}]}
    assert booking_cart.rows(session, member) == []


def send(client: Client, path: str, body: Any = None) -> Any:
    return client.post(
        path,
        json.dumps(body) if body is not None else "",
        content_type="application/json",
        HTTP_HOST=HOST,
        HTTP_ORIGIN="https://" + HOST,
        HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
        secure=True,
    )


def test_the_page_keeps_the_cart_in_the_members_session(made: Example) -> None:
    client = Client(enforce_csrf_checks=True)
    assert get(client, "/booking/cart/").status_code == 302
    sign_in(client, made.member)
    start = at(DAY, 10).astimezone(ZONE)
    booking = {
        "container": made.config.pk,
        "category": made.category.pk,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
        "total": 200,
    }
    assert json.loads(get(client, "/booking/cart/").content) == {
        "count": 0,
        "limit": 50,
        "currency": made.org.currency,
        "total": 0,
        "ready": False,
        "locked": False,
        "order": None,
        "bookings": [],
        "conditions": {
            "version": booking_conditions.current(
                Organization.objects.get(pk=made.org.pk)
            ).digest
        },
    }
    response = send(client, "/booking/cart/add/", booking)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    body = json.loads(response.content)
    row_id = body["bookings"][0]["id"]
    assert body == {
        "count": 1,
        "limit": 50,
        "currency": made.org.currency,
        "total": 200,
        "ready": True,
        "locked": False,
        "order": None,
        "conditions": {
            "version": booking_conditions.current(
                Organization.objects.get(pk=made.org.pk)
            ).digest
        },
        "bookings": [
            {
                "id": row_id,
                "container": {"id": made.config.pk, "number": made.config.number},
                "asset": {"id": made.asset.pk, "name": "Pool"},
                "type": {"id": made.kind.pk, "name": "Visit"},
                "category": {"id": made.category.pk, "name": "Private"},
                "quantity": 1,
                "spots": [],
                "spot_word": None,
                "group": None,
                "start": "2027-03-01T10:00:00+01:00",
                "end": "2027-03-01T12:00:00+01:00",
                "state": "ok",
                "text": "",
                "total": 200,
                "seen": 200,
                "places_left": None,
            }
        ],
    }
    assert json.loads(get(client, "/booking/cart/").content) == body
    # The pool is the member's from 10:00 to 12:00 in the cart: 11:00 is
    # taken there, except for the booking being changed.
    later = {**booking, "start": (start + timedelta(hours=1)).isoformat()}
    query = {key: later[key] for key in ("container", "category", "start", "end")}
    assert json.loads(price_page(client, query).content) == {
        "available": False,
        "reason": BESIDE,
        "text": "does not fit beside your cart",
    }
    assert json.loads(price_page(client, {**query, "editing": row_id}).content)[
        "available"
    ]
    assert price_page(client, {**query, "editing": "0" * 16}).status_code == 404
    assert price_page(client, {**query, "editing": "x"}).status_code == 400
    days: dict[str, Any] = {"asset": made.asset.pk, "category": made.category.pk}
    days |= {"start": "2027-03-01", "days": 1, "length": 60}

    def offered(found: Any) -> list[str]:
        (row,) = found["containers"]
        (period,) = row["periods"]
        assert period["taken"] == []
        return [period["rows"][start["row"]] for start in period["starts"]]

    free = json.loads(get(client, "/booking/calendar/?" + urlencode(days)).content)
    assert offered(free) == [
        "2027-03-01T12:00:00+01:00",
        "2027-03-01T13:00:00+01:00",
    ]
    editing = json.loads(
        get(
            client, "/booking/calendar/?" + urlencode({**days, "editing": row_id})
        ).content
    )
    assert len(offered(editing)) == 4
    # Refusals: taken, a moved price, a bad body, no token.
    assert send(client, "/booking/cart/add/", later).status_code == 409
    assert json.loads(send(client, "/booking/cart/add/", later).content) == {
        "reason": BESIDE,
        "text": "does not fit beside your cart",
    }
    moved = {
        **booking,
        "start": (start + timedelta(hours=2)).isoformat(),
        "end": (start + timedelta(hours=3)).isoformat(),
    }
    assert json.loads(send(client, "/booking/cart/add/", moved).content) == {
        "reason": "price-changed",
        "text": "the price has changed",
        "total": 100,
    }
    bad_bodies = (
        {**booking, "container": "1"},
        {**booking, "total": -1},
        {**booking, "quantity": 0},
        [booking],
        None,
    )
    for bad in bad_bodies:
        assert send(client, "/booking/cart/add/", bad).status_code == 400, bad
    assert (
        send(
            client, "/booking/cart/add/", {**booking, "end": booking["start"]}
        ).status_code
        == 400
    )
    for raw in ("[" * 100_000, " " * (settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1)):
        response = client.post(
            "/booking/cart/add/",
            raw,
            content_type="application/json",
            HTTP_HOST=HOST,
            HTTP_ORIGIN="https://" + HOST,
            HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
            secure=True,
        )
        assert response.status_code == 400
    refused = client.post(
        "/booking/cart/add/",
        json.dumps(booking),
        content_type="application/json",
        HTTP_HOST=HOST,
        HTTP_ORIGIN="https://" + HOST,
        secure=True,
    )
    assert refused.status_code == 403
    assert get(client, "/booking/cart/add/").status_code == 405
    # Change keeps the id; remove and empty.
    changed = send(client, f"/booking/cart/{row_id}/change/", {**later, "total": 100})
    assert changed.status_code == 200, changed.content
    assert json.loads(changed.content)["bookings"][0]["id"] == row_id
    assert (
        json.loads(changed.content)["bookings"][0]["start"]
        == "2027-03-01T11:00:00+01:00"
    )
    unknown = "/booking/cart/000000000000000a/change/"
    assert send(client, unknown, later).status_code == 404
    assert send(client, "/booking/cart/nope/remove/").status_code == 404
    assert send(client, f"/booking/cart/{row_id}/remove/").status_code == 200
    assert send(client, f"/booking/cart/{row_id}/remove/").status_code == 404
    assert send(client, "/booking/cart/add/", booking).status_code == 200
    emptied = json.loads(send(client, "/booking/cart/empty/").content)
    assert (emptied["count"], emptied["bookings"]) == (0, [])
    # Another browser of the same member starts with its own cart.
    assert send(client, "/booking/cart/add/", booking).status_code == 200
    other = Client()
    sign_in(other, made.member)
    assert json.loads(get(other, "/booking/cart/").content)["count"] == 0


def price_page(client: Client, query: dict[str, Any]) -> Any:
    return get(client, "/booking/price/?" + urlencode(query))


def test_a_full_cart_reads_each_offer_once() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    cart = [
        choice(
            org,
            "Sauna 1",
            "Sauna visit",
            at(DAY + timedelta(days=7 * week), hour),
            at(DAY + timedelta(days=7 * week), hour + 1),
        )
        for week in range(10)
        for hour in range(10, 15)
    ]
    wanted = choice(org, "Sauna 1", "Sauna visit", at(hour=16), at(hour=17))
    for call in (
        lambda: cart_quotes(member=member, cart=cart, now=NOW),
        lambda: priced(member, wanted, cart),
        lambda: member_calendar_with(org, member, cart, quantity=1),
    ):
        with CaptureQueriesContext(connection) as queries:
            call()
        # One claims read per booking, the order's own; about 3,400 reads
        # when each booking read its whole snapshot .
        assert len(queries.captured_queries) < 100


def old_opening(data: Any, start: datetime, end: datetime) -> bool:
    """The order's opening-hours check minute by minute."""
    spans = []
    first = start.date() - timedelta(days=1)
    last = end.date() + timedelta(days=1)
    for ordinal in range(first.toordinal(), last.toordinal() + 1):
        day = date.fromordinal(ordinal)
        applicable = [item for item in data.calendars if item.start_date <= day]
        if not applicable:
            continue
        current = applicable[-1]
        midnight = datetime.combine(day, time())
        for begin, finish in data.spans[current.pk][day.weekday()]:
            spans.extend(
                calendar_module._local_span(
                    midnight + begin * calendar_module.MINUTE,
                    midnight + finish * calendar_module.MINUTE,
                    lambda wall: calendar_module._instants(wall, data.zone),
                )
            )
    return calendar_module._covered(start, end, calendar_module._merge(spans))


def test_the_opening_hours_check_is_unchanged(made: Example) -> None:
    # Overlapping early windows that cross the skipped hour in March, then a
    # calendar from 29 March open only on Sundays from 01:30, across the
    # repeated hour in October.
    made.available([(60, 200), (150, 300), (600, 840)])
    later = AvailabilityCalendar.objects.create(
        organization=made.org, asset=made.asset, start_date=date(2027, 3, 29)
    )
    AvailableSpan.objects.create(
        organization=made.org,
        availability_calendar=later,
        weekday=6,
        start_time=90,
        end_time=1440,
    )
    data = calendar_module._read(
        made.member, made.asset, made.kind, date(2027, 1, 1), date(2028, 1, 1), NOW
    )
    assert data is not None
    grid = calendar_module._Grid(data)
    checked = set()
    for day in (date(2027, 3, 27), date(2027, 10, 30)):
        for hour in range(0, 72, 5):
            start = at(day) + timedelta(hours=hour, minutes=30 * (hour % 2))
            for length in (30, 60, 150, 600, 1440, 2 * 1440):
                end = start + timedelta(minutes=length)
                expected = old_opening(data, start, end)
                assert ordering._opening(data, start, end) == expected
                assert ordering._opening(data, start, end, grid) == expected
                checked.add(expected)
    assert checked == {True, False}


def test_the_cart_in_the_way_has_its_own_words_and_others_in_the_way_say_taken() -> (
    None
):
    org, member = scenario("scenarios/sauna-palace.toml")
    take(org, "Sauna 1", at(hour=10), at(hour=11), units=(1, 2, 3, 4))
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 3)
    two = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 2)
    five = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=11), 5)
    # Two fit beside the stored four, but not beside the cart's three.
    assert priced(member, two, []).reason is None
    assert priced(member, two, [seats]) == Quote(BESIDE, places_left=1)
    # Five fit nowhere, with or without the cart: others' bookings are in the way.
    assert priced(member, five, []) == Quote("taken", places_left=4)
    assert priced(member, five, [seats]) == Quote("taken", places_left=1)
    # In the cart itself, the second of two is the one the cart pushes out.
    assert [
        row.reason for row in cart_quotes(member=member, cart=[seats, two], now=NOW)
    ] == [
        None,
        BESIDE,
    ]
    # The whole sauna twice: the cart's own is in the way; once a block holds
    # the evening, others are in the way, cart or no cart.
    evening = choice(org, "Sauna 1", "Private sauna", at(hour=18), at(hour=22))
    assert priced(member, evening, []).reason is None
    assert priced(member, evening, [evening]).reason == BESIDE
    take(org, "Sauna 1", at(hour=18), at(hour=22), state="blocked")
    assert priced(member, evening, []).reason == "taken"
    assert priced(member, evening, [evening]).reason == "taken"


def test_a_start_that_has_passed_says_so_in_the_price_and_the_cart() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    seats = choice(org, "Sauna 1", "Sauna visit", at(hour=10), at(hour=12), 2)
    after = at(hour=10, minute=1)
    found = quote(
        member=member,
        configuration_id=seats.configuration_id,
        booking_category_id=seats.booking_category_id,
        quantity=2,
        starts_at=seats.starts_at,
        ends_at=seats.ends_at,
        now=after,
    )
    assert found == Quote("already-started")
    assert [
        row.reason for row in cart_quotes(member=member, cart=[seats], now=after)
    ] == ["already-started"]
    session = SessionStore()
    assert booking_cart.add(session, member, seats, 30000, after) == Refusal(
        "already-started"
    )
