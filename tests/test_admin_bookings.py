# SPDX-License-Identifier: AGPL-3.0-only
"""The Bookings, the Schedule and a booking's page : the gate, the
filters, both orders, the pages, the search, the states, the asset's clock,
the currency, the inspected event and the number of queries."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from randirandi.core import admin_views, audit_log, booking_admin
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import erase_identity, export_member
from randirandi.core.models import (
    Asset,
    AuditEvent,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    BookingState,
    ConfigurationCategory,
    Member,
    Order,
    Payment,
    PriceBook,
    Refund,
    SpotGroup,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_login_sessions import (
    NOW as CODE_TIME,
)
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
OSLO = ZoneInfo("Europe/Oslo")
NEW_YORK = ZoneInfo("America/New_York")
FIRST = datetime(2027, 1, 10, 8, 0, tzinfo=UTC)
NOW = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)
LISTS = {"bookings": "/admin/bookings/list/", "schedule": "/admin/schedule/list/"}


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


@pytest.fixture(autouse=True)
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booking_admin, "_now", lambda: NOW)


def request_for(made: made_up.MadeUp, path: str, query: dict[str, Any]) -> Any:
    request = RequestFactory().get(path, query, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = made.administrator
    return request


def answer(response: HttpResponse) -> dict[str, Any]:
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    found = json.loads(response.content)
    assert isinstance(found, dict)
    return found


def bookings(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    request = request_for(made, LISTS["bookings"], query)
    return answer(admin_views.bookings(request))


def schedule(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    request = request_for(made, LISTS["schedule"], query)
    return answer(admin_views.schedule(request))


def one(made: made_up.MadeUp, number: int) -> dict[str, Any]:
    request = request_for(made, f"/admin/bookings/{number}/data/", {})
    return answer(admin_views.booking_data(request, number))


def refused(made: made_up.MadeUp, which: str, **query: Any) -> int:
    request = request_for(made, LISTS[which], query)
    view = admin_views.bookings if which == "bookings" else admin_views.schedule
    return int(view(request).status_code)


def numbers(found: dict[str, Any]) -> list[int]:
    return [row["number"] for row in found["bookings"]]


def starts(found: dict[str, Any]) -> list[str]:
    return [f"{row['start_date']} {row['start_time']}" for row in found["lines"]]


def another_member(made: made_up.MadeUp, name: str = "Robin Lake") -> Member:
    first, last = name.split()
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name=name,
        first_name=first,
        last_name=last,
        email=f"{first.lower()}@harbourview.example",
        phone=f"+47901{sum(map(ord, name)) % 100000:05d}",
        status=made.member.status,
        origin=made.member.origin,
    )


def category(made: made_up.MadeUp, name: str) -> BookingCategory:
    found = BookingCategory.objects.of(made.organization).filter(name=name).first()
    if found is not None:
        return found
    return BookingCategory.objects.create(organization=made.organization, name=name)


def order_of(
    made: made_up.MadeUp,
    *,
    member: Member | None = None,
    created: datetime = FIRST,
    total: int = 0,
) -> Order:
    return Order.objects.create(
        organization=made.organization,
        member=member or made.member,
        total=total,
        created_at=created,
    )


def book(
    made: made_up.MadeUp,
    order: Order,
    *,
    day: int = 10,
    hour: int = 10,
    hours: int = 1,
    state: BookingState = BookingState.CONFIRMED,
    price: int = 0,
    kind: str = "private",
    configuration: BookingConfiguration | None = None,
    starts: datetime | None = None,
) -> Booking:
    """A booking of the order on the made-up asset, `day` days after the made-up
    day, in UTC; with a price line when `price` is more than 0."""
    configuration = configuration or made.configuration
    begin = starts or made_up.at(made.day + timedelta(days=day), hour)
    made_booking = Booking.objects.take(
        organization=made.organization,
        asset=configuration.asset,
        booking_configuration=configuration,
        starts_at=begin,
        ends_at=begin + timedelta(hours=hours),
        state=state,
        order=order,
        member_id=order.member_id,
        booking_type=made.booking_type,
        booking_category=category(made, kind),
        booking_type_name=made.booking_type.name,
        booking_category_name=kind,
    )
    price_book = configuration.price_book
    assert price_book is not None
    if price:
        BookingLine.objects.create(
            organization=made.organization,
            booking=made_booking,
            booking_configuration=configuration,
            price_book=price_book,
            up_to=240,
            quantity=1,
            amount=price,
            configuration_number=configuration.number,
            price_book_number=price_book.number,
        )
    return made_booking


def many(made: made_up.MadeUp, count: int, first: int = 0) -> list[Order]:
    """`count` orders a day apart, each with one booking on its own day."""
    return [
        book(
            made,
            order_of(made, created=FIRST + timedelta(days=index)),
            day=200 + index,
        ).order
        for index in range(first, first + count)
    ]


def deck(made: made_up.MadeUp, currency: str = "EUR") -> BookingConfiguration:
    """An asset on New York's clock, with a container priced in `currency`."""
    asset = Asset.objects.create(
        organization=made.organization, name="Harbor Deck", time_zone=str(NEW_YORK)
    )
    book_ = PriceBook.objects.create(
        organization=made.organization, name="Deck prices", currency=currency
    )
    container = BookingConfiguration.objects.create(
        organization=made.organization,
        asset=asset,
        booking_type=made.booking_type,
        period_type="flexible",
        period_start=600,
        period_end=840,
        end_day=0,
        period_start_weekdays=list(range(7)),
        interval=60,
        minimum_length=60,
        maximum_length=240,
        status="enabled",
        price_book=book_,
    )
    ConfigurationCategory.objects.create(
        organization=made.organization,
        booking_configuration=container,
        booking_category=made.private,
    )
    return container


# --- The gate -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/admin/bookings/list/", "/admin/schedule/list/", "/admin/bookings/1/data/"],
)
def test_the_gate_is_the_shells(made: made_up.MadeUp, path: str) -> None:
    signed_out = get(Client(), path)
    assert signed_out.status_code == 302
    assert signed_out.headers["Location"].startswith("/login/")
    member, admin = Client(), Client()
    with application_login():
        assert post(member, "/login/code/", code=request_code(member, made.member))
        assert get(member, path).status_code == 403
        sign_in_administrator(admin, made.administrator)
        assert get(admin, path).status_code == 200
        assert post(admin, path).status_code == 405
        assert admin.head(path, HTTP_HOST=HOST, secure=True).status_code == 405


@pytest.mark.parametrize(
    "path",
    ["/admin/bookings/list/", "/admin/schedule/list/", "/admin/bookings/1/data/"],
)
def test_without_the_second_step_or_from_another_organization(
    made: made_up.MadeUp, path: str
) -> None:
    views = {
        "/admin/bookings/list/": lambda r: admin_views.bookings(r),
        "/admin/schedule/list/": lambda r: admin_views.schedule(r),
        "/admin/bookings/1/data/": lambda r: admin_views.booking_data(r, 1),
    }
    request = RequestFactory().get(path, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made.administrator
    with pytest.raises(PermissionDenied):
        views[path](request)
    elsewhere = made_up.everything("elsewhere")
    request.session["admin_confirmed"] = True
    request.user = elsewhere.administrator
    # The other organization's administrator is on this address's organization
    # no more than a stranger is.
    with pytest.raises(PermissionDenied):
        views[path](request)
    assert (
        not AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.INSPECT)
        .exists()
    )


# --- Bookings --------------------------------------------------------------


def test_a_booking_as_the_list_shows_it(made: made_up.MadeUp) -> None:
    order = order_of(
        made, created=datetime(2027, 1, 10, 9, 5, 7, tzinfo=UTC), total=2500
    )
    book(made, order, day=10, price=2500, kind="private")
    book(made, order, day=11, price=0, kind="internal")
    book(made, order, day=12, price=0, kind="private")
    found = bookings(made, q="alex")
    assert found["count"] == 3
    row = next(row for row in found["bookings"] if row["number"] == order.number)
    assert row == {
        "number": order.number,
        "reference": order.reference,
        # Oslo is UTC+1 in January.
        "created": "2027-01-10, 10:05:07",
        "categories": "private, internal",
        "member": "Alex Rivers",
        "state": "paid",
        "state_label": "Paid",
        "amount": 2500,
        "currency": "NOK",
    }
    assert [s["value"] for s in found["states"]] == [
        "paid", "held", "cancelled", "expired",
    ]  # fmt: skip


def test_newest_first_by_number_and_by_created_both_ways(
    made: made_up.MadeUp,
) -> None:
    later = order_of(made, created=FIRST + timedelta(days=1))
    earlier = order_of(made, created=FIRST - timedelta(days=1))
    middle = order_of(made, created=FIRST)
    base = [made.order.number, made.spare_order.number]
    # The made-up orders are made now; ordered by number, newest first.
    assert numbers(bookings(made, q="alex")) == [
        middle.number, earlier.number, later.number, *base[::-1],
    ]  # fmt: skip
    oldest = bookings(made, order="oldest")
    assert numbers(oldest) == numbers(bookings(made))[::-1]
    by_time = numbers(bookings(made, sort="created", order="oldest"))
    assert by_time.index(earlier.number) < by_time.index(middle.number)
    assert by_time.index(middle.number) < by_time.index(later.number)
    newest = numbers(bookings(made, sort="created"))
    assert newest == by_time[::-1]
    assert refused(made, "bookings", sort="state") == 400
    assert refused(made, "bookings", order="sideways") == 400


def test_pages_of_25_and_the_last_page(made: made_up.MadeUp) -> None:
    many(made, 58)
    first = bookings(made)
    assert (first["count"], first["pages"], first["per_page"]) == (60, 3, 25)
    assert len(first["bookings"]) == 25 and first["page"] == 1
    second, last = bookings(made, page=2), bookings(made, page=3)
    assert len(second["bookings"]) == 25 and len(last["bookings"]) == 10
    every = numbers(first) + numbers(second) + numbers(last)
    assert every == sorted(every, reverse=True) and len(set(every)) == 60
    assert bookings(made, page=4)["bookings"] == []
    for page in ("0", "-1", "x", "1.5"):
        assert refused(made, "bookings", page=page) == 400


def test_nothing_matches(made: made_up.MadeUp) -> None:
    none = bookings(made, q="zzz")
    assert (none["count"], none["pages"], none["bookings"]) == (0, 0, [])


def test_created_in_the_organizations_time_zone(made: made_up.MadeUp) -> None:
    # Oslo is UTC+1 in January: 23:30 UTC on the 15th is 00:30 on the 16th.
    edges = [
        datetime(2027, 1, 15, 22, 59, 59, tzinfo=UTC),
        datetime(2027, 1, 15, 23, 0, tzinfo=UTC),
        datetime(2027, 1, 16, 22, 59, 59, tzinfo=UTC),
        datetime(2027, 1, 16, 23, 0, tzinfo=UTC),
    ]
    orders = [order_of(made, created=moment) for moment in edges]
    ours = {order.number for order in orders}

    def found(**query: str) -> set[int]:
        return set(numbers(bookings(made, **query))) & ours

    day = {"from": "2027-01-16", "to": "2027-01-16"}
    assert found(**day) == {orders[1].number, orders[2].number}
    assert found(**{"from": "2027-01-15", "to": "2027-01-16"}) == {
        order.number for order in orders[:3]
    }
    assert found(**{"from": "2027-01-17"}) == {orders[3].number}
    assert found(to="2027-01-15") == {orders[0].number}
    assert found(**{"from": "2027-01-17", "to": "2027-01-16"}) == set()
    for bad in ("2027-1-1", "2027-13-01", "today", ""):
        assert refused(made, "bookings", **{"from": bad}) == 400


def test_each_state_and_how_it_maps(made: made_up.MadeUp) -> None:
    states = BookingState
    held = book(made, order_of(made), day=30, state=states.HELD, price=100).order
    paid = book(made, order_of(made, total=100), day=31, price=100).order
    free = book(made, order_of(made), day=32).order
    cancelled = book(made, order_of(made), day=33, state=states.CANCELLED).order
    expired = book(made, order_of(made), day=34, state=states.EXPIRED).order
    # An order follows its bookings: waiting while any is held, else paid
    # while any is confirmed, else cancelled while any is, else not completed.
    held_and_paid = book(made, order_of(made), day=35, state=states.HELD).order
    book(made, held_and_paid, day=36)
    paid_and_cancelled = book(made, order_of(made), day=37).order
    book(made, paid_and_cancelled, day=38, state=states.CANCELLED)
    cancelled_and_expired = book(
        made, order_of(made), day=39, state=states.CANCELLED
    ).order
    book(made, cancelled_and_expired, day=40, state=states.EXPIRED)
    empty = order_of(made)
    expected = {
        held: ("held", "Being paid"),
        paid: ("paid", "Paid"),
        free: ("paid", "Confirmed"),
        cancelled: ("cancelled", "Cancelled"),
        expired: ("expired", "Not completed"),
        held_and_paid: ("held", "Being paid"),
        paid_and_cancelled: ("paid", "Confirmed"),
        cancelled_and_expired: ("cancelled", "Cancelled"),
        empty: ("expired", "Not completed"),
    }
    shown = {row["number"]: row for row in bookings(made)["bookings"]}
    for order, (state, label) in expected.items():
        assert (shown[order.number]["state"], shown[order.number]["state_label"]) == (
            state,
            label,
        ), order.number
    for state in booking_admin.STATE_WORDS:
        asked = set(numbers(bookings(made, state=state)))
        assert asked >= {o.number for o, (s, _) in expected.items() if s == state}
        assert all(shown[n]["state"] == state for n in asked if n in shown)
    # The page of one booking says the same as the list.
    for order, (state, label) in expected.items():
        page = one(made, order.number)
        assert (page["state"], page["state_label"]) == (state, label)
    assert refused(made, "bookings", state="paid-ish") == 400


def test_the_member_search_and_an_erased_member(made: made_up.MadeUp) -> None:
    robin = another_member(made)
    ours = book(made, order_of(made), day=30).order
    theirs = book(made, order_of(made, member=robin), day=31).order
    gone = another_member(made, "Pat Quill")
    erased = book(made, order_of(made, member=gone), day=32).order
    erase_identity(gone)
    assert set(numbers(bookings(made, q="robin"))) == {theirs.number}
    assert set(numbers(bookings(made, q="  LAKE "))) == {theirs.number}
    assert ours.number in numbers(bookings(made, q="alex"))
    assert theirs.number not in numbers(bookings(made, q="alex"))
    # An erased member is named by kind and number, as the Audit log does.
    shown = {r["number"]: r for r in bookings(made, q=f"member {gone.pk}")["bookings"]}
    assert shown[erased.number]["member"] == f"Member {gone.pk}"
    assert set(shown) == {erased.number}
    assert bookings(made, q="100%")["count"] == 0
    assert refused(made, "bookings", q="x" * 101) == 400


def test_the_orders_own_currency(made: made_up.MadeUp) -> None:
    euro = deck(made)
    paid = order_of(made, total=1250)
    book(
        made,
        paid,
        configuration=euro,
        starts=datetime(2027, 4, 1, 15, tzinfo=UTC),
        price=1250,
    )
    Payment.objects.create(
        organization=made.organization,
        order=paid,
        payment_provider="vipps",
        provider_reference="deck-1",
        amount=1250,
        state="captured",
        captured_amount=1250,
        currency="DKK",
    )
    unpaid = order_of(made, total=1250)
    book(
        made,
        unpaid,
        configuration=euro,
        starts=datetime(2027, 4, 2, 15, tzinfo=UTC),
        price=1250,
    )
    no_line = book(made, order_of(made), day=50).order
    shown = {r["number"]: r["currency"] for r in bookings(made)["bookings"]}
    # The payment's; else the first priced booking's price book; else the
    # organization's.
    assert shown[paid.number] == "DKK"
    assert shown[unpaid.number] == "EUR"
    assert shown[no_line.number] == "NOK"
    assert one(made, unpaid.number)["currency"] == "EUR"


# --- The Schedule ----------------------------------------------------------


def test_the_schedule_holds_only_time_that_is_held(made: made_up.MadeUp) -> None:
    states = BookingState
    for index, state in enumerate(
        (states.CONFIRMED, states.HELD, states.CANCELLED, states.EXPIRED)
    ):
        book(made, order_of(made), day=30 + index, state=state, price=100)
    # Oslo is on summer time from 28 March: 10:00 UTC is 12:00.
    found = schedule(made, when="coming")
    assert starts(found) == ["2027-03-31 12:00", "2027-04-01 12:00"]
    assert [row["state"] for row in found["lines"]] == ["paid", "held"]
    assert [row["state_label"] for row in found["lines"]] == ["Paid", "Being paid"]
    # The made-up booking is on the made-up day (1 March), and has ended.
    assert schedule(made, when="past")["lines"] == [
        {
            "id": made.booking.pk,
            "start_date": "2027-03-01",
            "start_time": "11:00",
            "end_date": "2027-03-01",
            "end_time": "13:00",
            "member": "Alex Rivers",
            "category": "private",
            "state": "paid",
            "state_label": "Paid",
            "booking": made.order.number,
            "reference": made.order.reference,
        }
    ]


def test_the_periods_of_booked_for(made: made_up.MadeUp) -> None:
    # Now is 1 March 2027, 13:00 in Oslo. The made-up booking is 11:00 to 13:00
    # there that day, so it has just ended.
    for day, hour in ((-3, 10), (0, 15), (1, 10), (6, 10), (7, 10), (30, 10)):
        book(made, order_of(made), day=day, hour=hour)
    # Evening in Oslo is still the same date in UTC.
    when = {
        "today": ["2027-03-01 11:00", "2027-03-01 16:00"],
        "week": [
            "2027-03-01 11:00", "2027-03-01 16:00", "2027-03-02 11:00",
            "2027-03-07 11:00",
        ],
        "coming": [
            "2027-03-01 16:00", "2027-03-02 11:00", "2027-03-07 11:00",
            "2027-03-08 11:00", "2027-03-31 12:00",
        ],
        "past": ["2027-03-01 11:00", "2027-02-26 11:00"],
    }  # fmt: skip
    for key, expected in when.items():
        assert starts(schedule(made, when=key)) == expected, key
    assert starts(schedule(made)) == when["week"]
    # Past turns to newest first; it can be turned back, and the others turn.
    assert starts(schedule(made, when="past", order="earliest")) == when["past"][::-1]
    assert starts(schedule(made, when="coming", order="latest")) == when["coming"][::-1]
    assert starts(schedule(made, when="day", **{"from": "2027-03-02"})) == [
        "2027-03-02 11:00"
    ]
    period = {"from": "2027-03-02", "to": "2027-03-08"}
    both = ["2027-03-02 11:00", "2027-03-07 11:00", "2027-03-08 11:00"]
    assert starts(schedule(made, when="period", **period)) == both
    backwards = {"from": "2027-03-08", "to": "2027-03-02"}
    assert starts(schedule(made, when="period", **backwards)) == both
    assert refused(made, "schedule", when="sometime") == 400
    assert refused(made, "schedule", when="day") == 400
    assert refused(made, "schedule", when="period", **{"from": "2027-03-02"}) == 400
    assert refused(made, "schedule", when="day", **{"from": "2027-3-2"}) == 400
    assert refused(made, "schedule", order="sideways") == 400


def test_today_is_on_the_organizations_clock(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 23:30 UTC on 1 March is 00:30 on 2 March in Oslo.
    monkeypatch.setattr(
        booking_admin, "_now", lambda: datetime(2027, 3, 1, 23, 30, tzinfo=UTC)
    )
    book(made, order_of(made), day=1, hour=10)
    assert starts(schedule(made, when="today")) == ["2027-03-02 11:00"]
    # The week begins on that day; the made-up booking is the day before.
    assert starts(schedule(made, when="week")) == ["2027-03-02 11:00"]


def test_times_are_on_the_assets_clock_and_midnight_ends_the_next_day(
    made: made_up.MadeUp,
) -> None:
    euro = deck(made)
    # 23:00 to 00:00 on the deck's New York clock (UTC-5): 04:00 to 05:00 UTC
    # on 2 March, which is 05:00 Oslo time that day.
    late = book(
        made,
        order_of(made),
        configuration=euro,
        starts=datetime(2027, 3, 1, 23, 0, tzinfo=NEW_YORK).astimezone(UTC),
    )
    row = next(r for r in schedule(made, when="coming")["lines"] if r["id"] == late.pk)
    assert (row["start_date"], row["start_time"]) == ("2027-03-01", "23:00")
    assert (row["end_date"], row["end_time"]) == ("2027-03-02", "00:00")

    # A day takes a line by its start date on its own asset's clock.
    def on(day: str) -> list[int]:
        found = schedule(made, when="day", **{"from": day})
        return [row["id"] for row in found["lines"]]

    assert late.pk in on("2027-03-01") and late.pk not in on("2027-03-02")
    # The booking's page says the same.
    page = one(made, late.order.number)["lines"][0]
    assert (page["start_date"], page["start_time"]) == ("2027-03-01", "23:00")
    assert (page["end_date"], page["end_time"]) == ("2027-03-02", "00:00")
    # Today, on the organization's clock, is still 1 March at 13:00 in Oslo.
    assert late.pk in [r["id"] for r in schedule(made, when="today")["lines"]]


def test_the_schedule_pages_and_search(made: made_up.MadeUp) -> None:
    robin = another_member(made)
    for index in range(60):
        member = robin if index % 3 == 0 else made.member
        book(made, order_of(made, member=member), day=2 + index)
    first = schedule(made, when="coming")
    assert (first["count"], first["pages"], first["per_page"]) == (60, 3, 25)
    assert len(first["lines"]) == 25
    assert len(schedule(made, when="coming", page=3)["lines"]) == 10
    assert schedule(made, when="coming", page=4)["lines"] == []
    every = [
        row["id"]
        for page in (1, 2, 3)
        for row in schedule(made, when="coming", page=page)["lines"]
    ]
    assert len(set(every)) == 60
    lake = schedule(made, when="coming", q="lake")
    assert lake["count"] == 20 and {r["member"] for r in lake["lines"]} == {
        "Robin Lake"
    }
    # No reference can hold an O (REFERENCE_ALPHABET), nor a name here.
    assert schedule(made, when="coming", q="ooo")["lines"] == []
    for page in ("0", "x"):
        assert refused(made, "schedule", page=page) == 400


# --- A booking's page ------------------------------------------------------


def test_a_bookings_page(made: made_up.MadeUp) -> None:
    org = made.organization
    Order.objects.of(org).filter(pk=made.order.pk).update(
        created_at=datetime(2027, 2, 20, 9, 30, 15, tzinfo=UTC),
        confirmation_sent_at=datetime(2027, 2, 20, 9, 32, 0, tzinfo=UTC),
    )
    Payment.objects.of(org).filter(pk=made.payment.pk).update(
        provider_reference="rr-1042-0001",
        captured_at=datetime(2027, 2, 20, 9, 31, 0, tzinfo=UTC),
    )
    refund = Refund.objects.of(org).get(payment=made.payment)
    Refund.objects.of(org).filter(pk=refund.pk).update(
        made_at=datetime(2027, 2, 21, 8, 0, tzinfo=UTC),
        completed_at=datetime(2027, 2, 21, 8, 0, 30, tzinfo=UTC),
        provider_reference="rr-1042-0001-r1",
        booking=made.booking,
    )
    page = one(made, made.order.number)
    assert page["number"] == made.order.number
    assert page["created"] == "2027-02-20, 10:30:15"
    assert (page["state"], page["state_label"]) == ("paid", "Paid")
    assert (page["amount"], page["currency"]) == (25000, "NOK")
    assert page["member"] == {
        "id": made.member.pk,
        "name": "Alex Rivers",
        "email": "alex@harbourview.example",
        "booked_as": "private",
    }
    assert page["payment"] == {
        "state": "captured",
        "state_label": "Captured",
        "amount": 25000,
        "captured": 25000,
        "captured_at": "2027-02-20, 10:31:00",
        "refunded": 10000,
        "reference": "rr-1042-0001",
        "trouble": None,
        "missing_refund": False,
        "attention": False,
        "fee": None,
        "net": None,
        "payout": None,
    }
    assert page["lines"] == [
        {
            "id": made.booking.pk,
            # An order from before the booking conditions has no moment.
            "free_until": None,
            # Over at NOW (13:00 on the asset's clock): nothing to cancel.
            "coming": False,
            "start_date": "2027-03-01",
            "start_time": "11:00",
            "end_date": "2027-03-01",
            "end_time": "13:00",
            "booking_type": "Room hire",
            "category": "private",
            "state": "paid",
            "state_label": "Paid",
            "amount": 25000,
            "prices": [
                {
                    "times": 1,
                    "amount": 25000,
                    "places": 1,
                    "unit": "booking",
                    "group": None,
                    "total": 25000,
                }
            ],
        }
    ]
    assert page["refunds"] == [
        {
            "id": refund.pk,
            "requested": "2027-02-21, 09:00:00",
            "completed": "2027-02-21, 09:00:30",
            "amount": 10000,
            "line": {
                "start_date": "2027-03-01",
                "start_time": "11:00",
                "end_date": "2027-03-01",
                "end_time": "13:00",
            },
            "by": "Sam Field",
            "automatic": False,
            "reference": "rr-1042-0001-r1",
        }
    ]
    assert page["history"] == [
        {"at": "2027-02-20, 10:30:15", "text": "Booked by Alex Rivers: 1 line"},
        {"at": "2027-02-20, 10:31:00", "text": "Paid with Vipps, 250 kr"},
        {
            "at": "2027-02-20, 10:32:00",
            "text": "Confirmation email sent to Alex Rivers",
        },
        {
            "at": "2027-02-21, 09:00:00",
            "text": "Refund of 100 kr requested by Sam Field",
        },
        {"at": "2027-02-21, 09:00:30", "text": "Refunded 100 kr through Vipps"},
    ]


def test_a_cancelled_booking_and_an_automatic_refund(made: made_up.MadeUp) -> None:
    order = order_of(made, created=datetime(2027, 2, 1, 9, 0, tzinfo=UTC), total=300)
    gone = book(made, order, day=5, state=BookingState.CANCELLED, price=100)
    book(made, order, day=6, price=200)
    Booking.objects.of(made.organization).filter(pk=gone.pk).update(
        cancelled_at=datetime(2027, 2, 2, 8, 0, tzinfo=UTC),
        cancelled_by=made.administrator,
    )
    payment = Payment.objects.create(
        captured_at=datetime(2027, 2, 1, 9, 1, tzinfo=UTC),
        organization=made.organization,
        order=order,
        payment_provider="vipps",
        provider_reference="x-2",
        amount=300,
        state="captured",
        captured_amount=300,
        refunded_amount=100,
        currency="NOK",
    )
    Refund.objects.create(
        organization=made.organization,
        payment=payment,
        amount=100,
        automatic=True,
        refund_key=uuid.uuid4(),
        booking=gone,
        made_at=datetime(2027, 2, 2, 8, 1, tzinfo=UTC),
    )
    page = one(made, order.number)
    assert [r["state"] for r in page["lines"]] == ["cancelled", "paid"]
    assert page["state"] == "paid"
    assert page["refunds"][0]["by"] is None and page["refunds"][0]["automatic"]
    assert page["refunds"][0]["completed"] is None
    texts = [event["text"] for event in page["history"]]
    assert texts == [
        "Booked by Alex Rivers: 2 lines",
        "Paid with Vipps, 3 kr",
        "2027-03-06 11:00 to 2027-03-06 12:00 cancelled by Sam Field",
        "Refund of 1 kr requested automatically",
    ]
    assert "Confirmation email" not in " ".join(texts)


def test_a_booking_not_paid_in_time_and_one_being_paid(made: made_up.MadeUp) -> None:
    waiting = book(
        made, order_of(made), day=5, state=BookingState.HELD, price=100
    ).order
    waiting_page = one(made, waiting.number)
    assert [e["text"] for e in waiting_page["history"]] == [
        "Booked by Alex Rivers: 1 line",
        "Waiting for payment",
    ]
    assert waiting_page["payment"] is None
    late = book(made, order_of(made), day=6, state=BookingState.EXPIRED).order
    Order.objects.of(made.organization).filter(pk=late.pk).update(
        hold_expires_at=datetime(2027, 1, 10, 8, 15, tzinfo=UTC)
    )
    assert [e["text"] for e in one(made, late.number)["history"]][-1] == (
        "Not paid in time: the time was let go"
    )
    Payment.objects.create(
        organization=made.organization,
        order=late,
        payment_provider="vipps",
        provider_reference="x-3",
        amount=100,
        state="cancelled",
        currency="NOK",
    )
    assert [e["text"] for e in one(made, late.number)["history"]][-1] == (
        "Payment stopped in Vipps"
    )


def test_price_lines_with_places_and_a_group(made: made_up.MadeUp) -> None:
    org = made.organization
    line = book(made, order_of(made, total=600), day=5, price=100).lines.get()
    group = SpotGroup.objects.of(org).get()
    BookingLine.objects.of(org).filter(pk=line.pk).update(
        quantity=2,
        billing_basis="per_capacity_unit",
        capacity_quantity=3,
        capacity_unit="place",
        spot_group=group,
    )
    price = one(made, line.booking.order.number)["lines"][0]
    assert price["amount"] == 600
    assert price["prices"] == [
        {
            "times": 2,
            "amount": 100,
            "places": 3,
            "unit": "place",
            "group": "Stage end",
            "total": 600,
        }
    ]


def test_an_erased_member_and_a_missing_or_foreign_booking(
    made: made_up.MadeUp,
) -> None:
    gone = another_member(made, "Pat Quill")
    order = book(made, order_of(made, member=gone), day=5).order
    erase_identity(gone)
    page = one(made, order.number)
    assert (
        page["member"]["name"] == f"Member {gone.pk}" and page["member"]["email"] == ""
    )
    assert page["history"][0]["text"] == f"Booked by Member {gone.pk}: 1 line"

    def status(number: int) -> int:
        request = request_for(made, "/admin/x/", {})
        return int(admin_views.booking_data(request, number).status_code)

    for number in (0, 1_000_000_000, 2**40):
        assert status(number) == 404
    # Another organization's order has a number of its own; none of its
    # numbers opens here, and nothing is written there.
    elsewhere = made_up.everything("elsewhere")
    events = AuditEvent.objects.of(elsewhere.organization).count()
    for _ in range(5):
        order_of(elsewhere)
    top = Order.objects.of(elsewhere.organization).order_by("-number")[0].number
    assert not Order.objects.of(made.organization).filter(number=top).exists()
    assert status(top) == 404
    assert AuditEvent.objects.of(elsewhere.organization).count() == events


def test_the_page_opens_at_its_own_address(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        shown = get(client, f"/admin/bookings/{made.order.number}/")
        ui = config(shown.content.decode())
        assert shown.status_code == 200 and "no-store" in shown.headers["Cache-Control"]
        assert ui["place"] == "bookings" and ui["booking"] == made.order.number
        assert (
            config(get(client, "/admin/bookings/").content.decode())["booking"] is None
        )
        assert get(client, "/admin/schedule/").status_code == 200
        # The page of a booking writes no event; its data does.
        assert (
            not AuditEvent.objects.of(made.organization)
            .filter(action=AuditAction.INSPECT)
            .exists()
        )
    signed_out = get(Client(), "/admin/bookings/1/")
    assert signed_out.status_code == 302
    member = Client()
    with application_login():
        assert post(member, "/login/code/", code=request_code(member, made.member))
        assert get(member, "/admin/bookings/1/").status_code == 403


# --- The inspected event ---------------------------------------------------


def inspected(made: made_up.MadeUp) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.INSPECT)
        .order_by("pk")
    )


def test_opening_a_page_writes_one_event_and_the_lists_none(
    made: made_up.MadeUp,
) -> None:
    many(made, 3)
    bookings(made)
    bookings(made, q="alex", state="paid", sort="created")
    schedule(made)
    schedule(made, when="past")
    assert inspected(made) == []
    one(made, made.order.number)
    events = inspected(made)
    assert len(events) == 1
    event = events[0]
    assert (event.administrator_id, event.object_kind, event.object_id) == (
        made.administrator.pk,
        "order",
        made.order.pk,
    )
    one(made, made.order.number)
    one(made, made.spare_order.number)
    assert len(inspected(made)) == 3
    # A booking that is not found writes none.
    admin_views.booking_data(request_for(made, "/admin/x/", {}), 987654)
    assert len(inspected(made)) == 3


def test_the_audit_log_shows_it_and_opens_the_booking(made: made_up.MadeUp) -> None:
    one(made, made.order.number)
    org = made.organization
    for kind, pk in (
        ("payment", made.payment.pk),
        ("refund", Refund.objects.of(org).get().pk),
    ):
        AuditEvent.objects.create(
            organization=org,
            administrator=made.administrator,
            action=AuditAction.REQUEST_REFUND,
            object_kind=kind,
            object_id=pk,
        )
    AuditEvent.objects.create(
        organization=org,
        administrator=made.administrator,
        action=AuditAction.INSPECT,
        object_kind="booking",
        object_id=made.booking.pk,
    )
    AuditEvent.objects.create(
        organization=org,
        administrator=made.administrator,
        action=AuditAction.INSPECT,
        object_kind="member",
        object_id=made.member.pk,
    )
    shown = audit_log.answer(org, audit_log.Query())["events"]
    assert isinstance(shown, list)
    # The order's event and a line's both open the order's page.
    opened = [
        row
        for row in shown
        if row["what"]["kind"] == "Reference"
        and row["what"]["booking"] == made.order.number
    ]
    assert len(opened) == 2
    by_kind = {(row["action_label"], row["what"]["kind"]): row for row in shown}
    order_event = by_kind["Looked up", "Reference"]
    assert order_event["what"] == {
        "kind": "Reference",
        "name": made.order.reference,
        "booking": made.order.number,
        "target": None,
    }
    assert (
        by_kind["Requested refund", "Payment"]["what"]["booking"] == made.order.number
    )
    assert by_kind["Requested refund", "Refund"]["what"]["booking"] == made.order.number
    assert by_kind["Looked up", "Member"]["what"]["booking"] is None


def test_the_members_export_holds_the_inspected_events_on_their_own_orders(
    made: made_up.MadeUp,
) -> None:
    robin = another_member(made)
    mine = book(made, order_of(made), day=5).order
    theirs = book(made, order_of(made, member=robin), day=6).order
    one(made, mine.number)
    one(made, theirs.number)
    with application_login():
        export = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator, now=CODE_TIME),
        )
    seen = {
        (row["object_kind"], row["object_id"])
        for row in export["audit_events"]
        if row["action"] == "inspected"
    }
    assert ("order", mine.pk) in seen
    assert ("order", theirs.pk) not in seen
    encoded = json.dumps(export)
    assert robin.email not in encoded and "Robin Lake" not in encoded


# --- The number of queries -------------------------------------------------


def queries(view: Any, *args: Any, **query: Any) -> int:
    with CaptureQueriesContext(connection) as seen:
        assert view(*args).status_code == 200
    return len(seen)


def test_the_number_of_queries_does_not_grow_with_the_rows(
    made: made_up.MadeUp,
) -> None:
    def list_cost() -> tuple[int, int]:
        return (
            queries(admin_views.bookings, request_for(made, LISTS["bookings"], {})),
            queries(
                admin_views.schedule,
                request_for(made, LISTS["schedule"], {"when": "coming"}),
            ),
        )

    many(made, 2)
    few = list_cost()
    many(made, 40, first=2)
    full = list_cost()
    assert full == few
    # Bookings: the gate (the administrator), the count, the page, the
    # categories, the payments, the price lines' currencies. The Schedule: the
    # gate, the count, the page, the totals.
    # Each also writes its look-up: seven more queries, however many rows.
    assert few == (15, 13)

    def page_cost(order: Order) -> int:
        request = request_for(made, "/admin/x/", {})
        return queries(admin_views.booking_data, request, order.number)

    small = book(made, order_of(made, total=100), day=100, price=100).order
    cost = page_cost(small)
    large = order_of(made, total=100)
    for index in range(8):
        book(made, large, day=110 + index, price=100)
    assert page_cost(large) == cost
    assert cost <= 18


def test_a_payment_without_a_capture_moment_shows_none(made: made_up.MadeUp) -> None:
    Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
        captured_at=None
    )
    Order.objects.of(made.organization).filter(pk=made.order.pk).update(
        confirmation_due_at=datetime(2027, 2, 20, 9, 31, tzinfo=UTC)
    )
    page = one(made, made.order.number)
    assert page["payment"]["captured"] == 25000
    assert page["payment"]["captured_at"] is None
    assert not [e for e in page["history"] if e["text"].startswith("Paid with")]


def test_a_line_across_a_clock_change_on_its_assets_clock(
    made: made_up.MadeUp,
) -> None:
    # New York changes its clocks on 14 March 2027, 02:00 to 03:00: 01:00 to
    # 04:00 on the wall is two hours.
    euro = deck(made)
    line = book(
        made,
        order_of(made),
        configuration=euro,
        hours=2,
        starts=datetime(2027, 3, 14, 1, 0, tzinfo=NEW_YORK).astimezone(UTC),
    )
    page = one(made, line.order.number)["lines"][0]
    assert (page["start_date"], page["start_time"]) == ("2027-03-14", "01:00")
    assert (page["end_date"], page["end_time"]) == ("2027-03-14", "04:00")
    row = next(
        r
        for r in schedule(made, when="day", **{"from": "2027-03-14"})["lines"]
        if r["id"] == line.pk
    )
    assert (row["start_time"], row["end_time"]) == ("01:00", "04:00")


def test_malformed_parameters_answer_400_on_each_endpoint(
    made: made_up.MadeUp,
) -> None:
    for query in (
        {"to": "2027-02-30"}, {"to": "2101-01-01"}, {"from": "1999-12-31"},
        {"page": "1e3"}, {"page": "1000000000"}, {"state": ""}, {"q": "x" * 101},
        {"sort": ""}, {"order": "oldest-first"},
    ):  # fmt: skip
        assert refused(made, "bookings", **query) == 400, query
    for query in (
        {"when": ""}, {"when": "period", "from": "2027-03-01"},
        {"when": "period", "from": "2027-03-01", "to": "nope"},
        {"when": "day", "from": "2101-01-01"}, {"page": "1e3"},
        {"q": "x" * 101}, {"order": "oldest"}, {"order": ""},
    ):  # fmt: skip
        assert refused(made, "schedule", **query) == 400, query
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path in (
            "/admin/bookings/abc/data/",
            "/admin/bookings/-1/data/",
            "/admin/bookings/1.5/data/",
        ):
            assert get(client, path).status_code == 404
        assert get(client, "/admin/bookings/list/?page=x").status_code == 400
        assert get(client, "/admin/schedule/list/?when=x").status_code == 400
    assert (
        not AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.INSPECT)
        .exists()
    )


def test_search_finds_an_order_by_its_reference_in_any_case(
    made: made_up.MadeUp,
) -> None:
    ours = book(made, order_of(made), day=30).order
    theirs = book(made, order_of(made, member=another_member(made)), day=31).order
    for query in (ours.reference, ours.reference.lower(), f" {ours.reference[:4]} "):
        assert ours.number in numbers(bookings(made, q=query))
    assert theirs.number not in numbers(bookings(made, q=ours.reference))
    row = next(r for r in bookings(made, q=ours.reference.lower())["bookings"])
    assert row["reference"] == ours.reference
    lines = schedule(made, when="coming", q=ours.reference.lower())["lines"]
    assert lines and {line["booking"] for line in lines} == {ours.number}
    assert {line["reference"] for line in lines} == {ours.reference}
    assert one(made, ours.number)["reference"] == ours.reference
