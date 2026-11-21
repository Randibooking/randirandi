# SPDX-License-Identifier: AGPL-3.0-only
"""The Price books : the gate, CSRF and origin, malformed
requests, every refusal with the model's reason, books and prices added,
changed and removed, the audit events, concurrent edits, the real application
login, the setup file after edits, and Book after a price edit (an order
already made keeps its amount)."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client

from randirandi import setup_file
from randirandi.core import admin_price_views as views
from randirandi.core import price_admin
from randirandi.core import setup as setups
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    AuditEvent,
    BookingConfiguration,
    BookingLine,
    ConfigurationCategory,
    Member,
    MemberStatus,
    Origin,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Role,
    RoleCategory,
    SpotGroup,
)
from randirandi.core.ordering import BookingRequest, create_order
from randirandi.core.setup_admin import SetupRefusedError
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import answer, request_to
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_calendar import DAY as CALENDAR_DAY
from tests.test_calendar import Example, at
from tests.test_calendar import made as calendar_made  # noqa: F401
from tests.test_login_sessions import HOST, get, page_settings  # noqa: F401
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = date(2027, 3, 1)
H = 60


@pytest.fixture
def example(calendar_made: Example) -> Example:  # noqa: F811
    return calendar_made


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def tier(up_to: int, paid_per: int, amount: int) -> dict[str, int]:
    return {"up_to": up_to, "paid_per": paid_per, "amount": amount}


def normal(
    *tiers: dict[str, Any], by: str = "length", basis: str = "per_booking"
) -> Any:
    return {
        "group": None,
        "tiers_go_by": by,
        "billing_basis": basis,
        "tiers": list(tiers),
    }


# The made-up container books up to 12 h between 10:00 and 22:00.
FULL = tier(12 * H, 12 * H, 25000)


def stamp_in(made: made_up.MadeUp, container: int, category: int) -> str:
    """The stamp a sheet would hold (empty when the container is not there)."""
    place: Any = price_admin.prices_place(made.organization)
    for each in place["containers"]:
        if each["id"] == container:
            return str(each["stamps"].get(str(category), ""))
    return ""


def call(
    made: made_up.MadeUp,
    which: str,
    data: Any = None,
    *,
    pk: int = 0,
    container: int | None = None,
    category: int | None = None,
    who: Member | None = None,
    confirmed: bool = True,
    stamped: bool = True,
) -> HttpResponse:
    c = made.configuration.pk if container is None else container
    k = made.private.pk if category is None else category
    if which in {"save", "remove"} and stamped and "stamp" not in (data or {}):
        data = {**(data or {}), "stamp": stamp_in(made, c, k)}
    request = request_to(
        who or made.administrator,
        "get" if which == "data" else "post",
        "/admin/prices/",
        data,
        confirmed=confirmed,
    )
    match which:
        case "data":
            return views.prices_data(request)
        case "book-make":
            return views.book_make(request)
        case "book-change":
            return views.book_change(request, pk)
        case "book-remove":
            return views.book_remove(request, pk)
        case "save":
            return views.prices_save(request, c, k)
        case "remove":
            return views.prices_remove(request, c, k)
    raise AssertionError(which)


def refused(response: HttpResponse, code: str, status: int = 409) -> dict[str, Any]:
    found = answer(response, status)
    assert found["error"] == code
    return found


def save(made: made_up.MadeUp, *entries: Any, **where: Any) -> dict[str, Any]:
    return answer(call(made, "save", {"entries": list(entries)}, **where))


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.of(made.organization).filter(action=action))


def stored(made: made_up.MadeUp) -> list[tuple[int, int, int]]:
    entry = PriceBookEntry.objects.of(made.organization).get(
        booking_configuration=made.configuration, booking_category=made.private
    )
    return list(
        PriceTier.objects.of(made.organization)
        .filter(price_book_entry=entry)
        .values_list("up_to", "paid_per", "amount")
    )


def shared_container(made: made_up.MadeUp) -> tuple[BookingConfiguration, SpotGroup]:
    """A shared container on the made-up asset (Tuesdays), with a group."""
    from randirandi.core.models import AvailableSpan, Status

    org = made.organization
    plan = made_up.floor_plan_of(made.asset)
    calendar = made.asset.availabilitycalendar_set.get()
    AvailableSpan.objects.create(
        organization=org,
        availability_calendar=calendar,
        weekday=1,
        start_time=8 * H,
        end_time=23 * H,
    )
    container = BookingConfiguration.objects.create(
        organization=org,
        asset=made.asset,
        booking_type=made.booking_type,
        consumption_mode="shared",
        floor_plan=plan,
        period_type="flexible",
        period_start=10 * H,
        period_end=14 * H,
        period_start_weekdays=[1],
        interval=H,
        minimum_length=H,
        maximum_length=4 * H,
        status=Status.ENABLED,
        price_book=made.price_book,
    )
    ConfigurationCategory.objects.create(
        organization=org, booking_configuration=container, booking_category=made.private
    )
    return container, SpotGroup.objects.of(org).get(floor_plan=plan)


# --- The gate, CSRF and origin ---

EVERY_CALL = [
    ("data", {}),
    ("book-make", {"name": "x"}),
    ("book-change", {"name": "x"}),
    ("book-remove", {}),
    ("save", {"entries": [normal(FULL)]}),
    ("remove", {}),
]


@pytest.mark.parametrize(("which", "data"), EVERY_CALL)
def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp, which: str, data: dict[str, Any]
) -> None:
    before = (PriceBook.objects.of(made.organization).count(), stored(made))
    events_before = AuditEvent.objects.of(made.organization).count()
    for who, confirmed in ((made.administrator, False), (made.member, True)):
        with pytest.raises(PermissionDenied):
            call(made, which, data, who=who, confirmed=confirmed, pk=made.price_book.pk)
    assert before == (PriceBook.objects.of(made.organization).count(), stored(made))
    assert AuditEvent.objects.of(made.organization).count() == events_before


def test_another_organizations_ids_are_a_404(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    refused(
        call(made, "book-change", {"name": "z"}, pk=elsewhere.price_book.pk),
        "not-found",
        404,
    )
    refused(call(made, "book-remove", pk=elsewhere.price_book.pk), "not-found", 404)
    refused(
        call(
            made,
            "save",
            {"entries": [normal(FULL)]},
            container=elsewhere.configuration.pk,
        ),
        "not-found",
        404,
    )
    refused(
        call(made, "save", {"entries": [normal(FULL)]}, category=elsewhere.private.pk),
        "not-found",
        404,
    )
    refused(
        call(made, "remove", container=elsewhere.configuration.pk), "not-found", 404
    )
    assert PriceBook.objects.of(elsewhere.organization).get().name == "Prices 2027"


def test_a_huge_id_and_a_missing_set_are_a_404(made: made_up.MadeUp) -> None:
    refused(call(made, "book-remove", pk=2**63), "not-found", 404)
    refused(
        call(made, "save", {"entries": [normal(FULL)]}, category=2**63),
        "not-found",
        404,
    )
    PriceBookEntry.objects.of(made.organization).delete()
    refused(call(made, "remove"), "not-found", 404)


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    c, k = made.configuration.pk, made.private.pk
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, method in (
            ("/admin/prices/data/", "post"),
            ("/admin/prices/books/make/", "get"),
            (f"/admin/prices/books/{made.price_book.pk}/change/", "get"),
            (f"/admin/prices/books/{made.price_book.pk}/remove/", "get"),
            (f"/admin/prices/containers/{c}/{k}/save/", "get"),
            (f"/admin/prices/containers/{c}/{k}/remove/", "get"),
        ):
            assert (
                getattr(client, method)(path, HTTP_HOST=HOST, secure=True).status_code
                == 405
            ), path
        assert (
            config(get(client, "/admin/prices/").content.decode())["place"] == "prices"
        )
    response = Client().get("/admin/prices/data/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    c, k = made.configuration.pk, made.private.pk
    with application_login():
        sign_in_administrator(client, made.administrator)
        csrf = str(config(get(client, "/admin/prices/").content.decode())["csrf"])

        def send(
            path: str, body: dict[str, Any], headers: dict[str, Any]
        ) -> HttpResponse:
            response = client.post(
                path,
                json.dumps(body),
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        right = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://" + HOST}
        wrong = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://elsewhere.example"}
        none = {"HTTP_ORIGIN": "https://" + HOST}
        calls: tuple[tuple[str, dict[str, Any]], ...] = (
            ("/admin/prices/books/make/", {"name": "Spring"}),
            (f"/admin/prices/books/{made.price_book.pk}/change/", {"name": "Renamed"}),
            (f"/admin/prices/containers/{c}/{k}/save/", {"entries": [normal(FULL)]}),
            (f"/admin/prices/containers/{c}/{k}/remove/", {}),
        )
        for path, body in calls:
            assert send(path, body, none).status_code == 403, path
            assert send(path, body, wrong).status_code == 403, path
        assert PriceBook.objects.of(made.organization).count() == 1
        assert len(stored(made)) == 1
        assert answer(send(calls[0][0], calls[0][1], right))["price_books"]
    assert PriceBook.objects.of(made.organization).count() == 2


@pytest.mark.parametrize(
    ("which", "body"),
    [
        ("book-make", b"not json"),
        ("book-make", b"[]"),
        ("book-make", {}),
        ("book-make", {"name": 5}),
        ("book-make", {"name": "x", "extra": 1}),
        ("book-make", {"name": "x", "currency": 5}),
        ("book-change", {}),
        ("save", {}),
        ("save", {"entries": []}),
        ("save", {"entries": "x"}),
        ("save", {"entries": [5]}),
        ("save", {"entries": [{"tiers": []}]}),
        ("save", {"entries": [{**normal(FULL), "tiers_go_by": 5}]}),
        ("save", {"entries": [{**normal(FULL), "group": "1"}]}),
        ("save", {"entries": [{**normal(FULL), "group": True}]}),
        ("save", {"entries": [normal({"up_to": "12 h", "paid_per": 60, "amount": 1})]}),
        ("save", {"entries": [normal({"up_to": 720, "paid_per": 60})]}),
        ("save", {"entries": [normal({**FULL, "amount": True})]}),
        ("save", {"entries": [normal({**FULL, "amount": 1.5})]}),
        ("save", {"entries": [normal(*[tier(i, 1, 1) for i in range(1, 60)])]}),
    ],
)
def test_malformed_requests_are_a_400(
    made: made_up.MadeUp, which: str, body: Any
) -> None:
    before = stored(made)
    response = call(made, which, body, pk=made.price_book.pk)
    assert response.status_code == 400
    assert stored(made) == before
    assert PriceBook.objects.of(made.organization).count() == 1


# --- Price books ---


def test_the_place_lists_books_containers_and_prices(made: made_up.MadeUp) -> None:
    found = answer(call(made, "data"))
    assert set(found) == {
        "price_books",
        "currencies",
        "currency",
        "booking_categories",
        "containers",
    }
    (book,) = found["price_books"]
    assert (book["name"], book["currency"], book["containers"], book["entries"]) == (
        "Prices 2027",
        "NOK",
        1,
        1,
    )
    assert book["removable"] is False
    assert found["currencies"] == ["NOK", "DKK", "EUR"]
    (container,) = found["containers"]
    assert container["number"] == 1 and container["book"] == book["id"]
    assert (container["start"], container["end"], container["maximum"]) == (
        600,
        1320,
        720,
    )
    assert container["entries"][0]["tiers"] == [tier(4 * H, 4 * H, 25000)]


def test_a_book_is_made_renamed_and_takes_the_organizations_currency(
    made: made_up.MadeUp,
) -> None:
    found = answer(call(made, "book-make", {"name": " Spring "}))
    spring = next(b for b in found["price_books"] if b["name"] == "Spring")
    assert spring["currency"] == "NOK" and spring["removable"] is True
    answer(call(made, "book-make", {"name": "Euro", "currency": "EUR"}))
    assert PriceBook.objects.of(made.organization).get(name="Euro").currency == "EUR"
    answer(call(made, "book-change", {"name": "Summer"}, pk=spring["id"]))
    assert PriceBook.objects.of(made.organization).filter(name="Summer").exists()
    assert [e.object_kind for e in events(made, AuditAction.MADE_PRICE_BOOK)] == [
        "price_book"
    ] * 2
    assert len(events(made, AuditAction.CHANGED_PRICE_BOOK)) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"name": ""}, "invalid-name"),
        ({"name": "x" * 201}, "invalid-name"),
        ({"name": "prices 2027"}, "name-taken"),
        ({"name": "x", "currency": "SEK"}, "unknown-currency"),
        ({"name": "x", "currency": "nok"}, "unknown-currency"),
    ],
)
def test_a_book_refused_leaves_nothing(
    made: made_up.MadeUp, body: dict[str, Any], code: str
) -> None:
    events_before = AuditEvent.objects.of(made.organization).count()
    refused(call(made, "book-make", body), code)
    assert PriceBook.objects.of(made.organization).count() == 1
    assert AuditEvent.objects.of(made.organization).count() == events_before


def test_a_rename_to_a_taken_name_is_refused_and_to_the_same_writes_nothing(
    made: made_up.MadeUp,
) -> None:
    answer(call(made, "book-make", {"name": "Spring"}))
    spring = PriceBook.objects.of(made.organization).get(name="Spring")
    refused(
        call(made, "book-change", {"name": "PRICES 2027"}, pk=spring.pk), "name-taken"
    )
    before = AuditEvent.objects.of(made.organization).count()
    answer(
        call(made, "book-change", {"name": "Spring", "currency": "NOK"}, pk=spring.pk)
    )
    assert AuditEvent.objects.of(made.organization).count() == before


def test_the_currency_changes_while_the_book_has_no_price_and_is_fixed_after(
    made: made_up.MadeUp,
) -> None:
    answer(call(made, "book-make", {"name": "Spring"}))
    spring = PriceBook.objects.of(made.organization).get(name="Spring")
    answer(call(made, "book-change", {"currency": "DKK"}, pk=spring.pk))
    assert PriceBook.objects.of(made.organization).get(pk=spring.pk).currency == "DKK"
    found = refused(
        call(made, "book-change", {"currency": "EUR"}, pk=made.price_book.pk),
        "currency-fixed",
    )
    assert "currency stays as it is" in found["reason"]
    assert (
        PriceBook.objects.of(made.organization).get(pk=made.price_book.pk).currency
        == "NOK"
    )
    # The same currency is not a change.
    answer(call(made, "book-change", {"currency": "NOK"}, pk=made.price_book.pk))


def test_a_book_no_container_names_is_removed_with_its_prices(
    made: made_up.MadeUp,
) -> None:
    answer(call(made, "book-make", {"name": "Spring"}))
    spring = PriceBook.objects.of(made.organization).get(name="Spring")
    # A price left in a book the container no longer names.
    entry = PriceBookEntry.objects.of(made.organization).get()
    other = PriceBookEntry.objects.create(
        organization=made.organization,
        price_book=spring,
        booking_configuration=made.configuration,
        booking_category=made.private,
        tiers_go_by="length",
    )
    PriceTier.objects.create(
        organization=made.organization,
        price_book_entry=other,
        up_to=720,
        paid_per=60,
        amount=1,
    )
    answer(call(made, "book-remove", pk=spring.pk))
    assert not PriceBook.objects.of(made.organization).filter(pk=spring.pk).exists()
    assert PriceBookEntry.objects.of(made.organization).get().pk == entry.pk
    assert len(events(made, AuditAction.REMOVED_PRICE_BOOK)) == 1
    assert len(events(made, AuditAction.REMOVED_PRICE)) == 1


def test_a_book_a_container_names_or_a_booking_line_keeps_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    found = refused(call(made, "book-remove", pk=made.price_book.pk), "in-use")
    assert found["used"] == {"containers": 1, "bookings": 1}
    # With no container left, the booking line still keeps its book.
    BookingConfiguration.objects.of(made.organization).update(price_book=None)
    PriceBookEntry.objects.of(made.organization).delete()
    assert BookingLine.objects.of(made.organization).count() == 1
    found = refused(call(made, "book-remove", pk=made.price_book.pk), "in-use")
    assert found["used"] == {"bookings": 1}


# --- Prices ---


def test_a_price_is_saved_and_the_place_answers_it(made: made_up.MadeUp) -> None:
    found = save(made, normal(tier(4 * H, 2 * H, 10000), tier(12 * H, 4 * H, 15000)))
    assert stored(made) == [(240, 120, 10000), (720, 240, 15000)]
    (container,) = found["containers"]
    assert [t["amount"] for t in container["entries"][0]["tiers"]] == [10000, 15000]
    assert len(events(made, AuditAction.CHANGED_PRICE)) == 1
    assert events(made, AuditAction.CHANGED_PRICE)[0].object_kind == "price_book_entry"


def test_a_price_as_it_was_writes_nothing(made: made_up.MadeUp) -> None:
    save(made, normal(tier(12 * H, 12 * H, 1)))
    before = AuditEvent.objects.of(made.organization).count()
    save(made, normal(tier(12 * H, 12 * H, 1)))
    assert AuditEvent.objects.of(made.organization).count() == before


def test_a_new_price_and_a_removed_price_have_their_events(
    made: made_up.MadeUp,
) -> None:
    second = made_up.everything("second")  # noqa: F841 - another organization is left alone
    other = BookingConfiguration.objects.of(made.organization).get()
    answer(call(made, "remove"))
    assert not PriceBookEntry.objects.of(made.organization).exists()
    assert len(events(made, AuditAction.REMOVED_PRICE)) == 1
    save(made, normal(FULL))
    assert len(events(made, AuditAction.MADE_PRICE)) == 1
    assert stored(made) == [(720, 720, 25000)]
    assert other.price_book_id == made.price_book.pk


def test_time_of_day_tiers_go_by_the_clock(made: made_up.MadeUp) -> None:
    save(
        made,
        normal(tier(17 * H, 4 * H, 500), tier(22 * H, 5 * H, 1000), by="time of day"),
    )
    entry = PriceBookEntry.objects.of(made.organization).get()
    assert entry.tiers_go_by == "time of day"


REFUSALS = [
    # (entries, code, part of the reason)
    ([normal(tier(4 * H, 4 * H, 1))], "tiers-incomplete", "can be booked for 12 h"),
    (
        [normal(tier(6 * H, 1, 1), tier(6 * H, 1, 1), tier(12 * H, 1, 1))],
        "tiers-overlap",
        "past the one before",
    ),
    (
        [normal(tier(8 * H, 1, 1), tier(4 * H, 1, 1), tier(12 * H, 1, 1))],
        "tiers-overlap",
        "past the one before",
    ),
    ([normal()], "no-tier", "no price tier"),
    (
        [normal(tier(17 * H, 4 * H, 1), by="time of day")],
        "tiers-incomplete",
        "period ends at 22:00",
    ),
    (
        [normal(tier(22 * H, 4 * H, 1), tier(25 * H, 4 * H, 1), by="time of day")],
        "invalid-tier",
        "time of day",
    ),
    ([normal(tier(0, 60, 1))], "invalid-tier", "length"),
    ([normal(tier(12 * H, 0, 1))], "invalid-tier", "Paid per"),
    ([normal(tier(12 * H, 60, -1))], "invalid-tier", "zero or more"),
    ([normal(tier(2**40, 60, 1))], "invalid-tier", ""),
    ([normal(tier(12 * H, 60, 2**40))], "invalid-tier", ""),
    ([normal(FULL, basis="per_capacity_unit")], "billing-basis", "exclusive"),
    ([{**normal(FULL), "tiers_go_by": "weekly"}], "invalid-tiers-go-by", ""),
    ([{**normal(FULL), "billing_basis": "per_head"}], "invalid-billing-basis", ""),
    ([normal(FULL), normal(FULL)], "group-twice", ""),
    ([{**normal(FULL), "group": 1}], "needs-normal-price", ""),
]


@pytest.mark.parametrize(("entries", "code", "reason"), REFUSALS)
def test_each_refusal_gives_the_models_reason_and_changes_nothing(
    made: made_up.MadeUp, entries: list[Any], code: str, reason: str
) -> None:
    before, count = stored(made), AuditEvent.objects.of(made.organization).count()
    found = refused(call(made, "save", {"entries": entries}), code)
    assert reason in found.get("reason", "")
    assert stored(made) == before
    assert AuditEvent.objects.of(made.organization).count() == count


def test_a_category_not_on_the_container_is_refused(made: made_up.MadeUp) -> None:
    from randirandi.core.models import BookingCategory

    work = BookingCategory.objects.create(organization=made.organization, name="work")
    found = refused(
        call(made, "save", {"entries": [normal(FULL)]}, category=work.pk),
        "category-not-on-container",
    )
    assert "does not apply" in found["reason"]
    assert (
        not PriceBookEntry.objects.of(made.organization)
        .filter(booking_category=work)
        .exists()
    )


def test_a_container_without_payment_has_no_price(made: made_up.MadeUp) -> None:
    PriceBookEntry.objects.of(made.organization).delete()
    BookingConfiguration.objects.of(made.organization).update(price_book=None)
    found = refused(
        call(made, "save", {"entries": [normal(FULL)]}), "container-without-payment"
    )
    assert "without payment" in found["reason"]
    assert answer(call(made, "data"))["containers"] == []


def test_a_shared_container_prices_per_spot_and_by_group(made: made_up.MadeUp) -> None:
    container, group = shared_container(made)
    where: dict[str, Any] = {"container": container.pk}
    per_spot = normal(tier(4 * H, H, 5000), basis="per_capacity_unit")
    own = {**normal(tier(4 * H, H, 8000), basis="per_capacity_unit"), "group": group.pk}
    # A group's own price needs the normal price beside it, per spot too.
    refused(call(made, "save", {"entries": [own]}, **where), "needs-normal-price")
    refused(
        call(made, "save", {"entries": [normal(tier(4 * H, H, 5000)), own]}, **where),
        "groups-per-spot",
    )
    found = save(made, per_spot, own, **where)
    (shared,) = (c for c in found["containers"] if c["id"] == container.pk)
    assert sorted(
        (e["group"], e["billing_basis"]) for e in shared["entries"] if e["group"]
    ) == [(group.pk, "per_capacity_unit")]
    assert len(shared["groups"]) == 1 and shared["unit"]
    assert len(events(made, AuditAction.MADE_PRICE)) == 2
    # Leaving the group's price out of the set removes it, with its event.
    save(made, per_spot, **where)
    assert (
        PriceBookEntry.objects.of(made.organization)
        .filter(booking_configuration=container)
        .count()
        == 1
    )
    assert len(events(made, AuditAction.REMOVED_PRICE)) == 1
    # Per booking is fine alone.
    save(made, normal(tier(4 * H, 4 * H, 5000)), **where)


def test_a_group_not_on_the_containers_floor_plan_is_refused(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core.models import Asset

    container, _ = shared_container(made)
    other_asset = Asset.objects.create(organization=made.organization, name="Hall 2")
    plan = made_up.floor_plan_of(other_asset)
    elsewhere = SpotGroup.objects.create(
        organization=made.organization, floor_plan=plan, name="Far", word="end"
    )
    per_spot = normal(tier(4 * H, H, 5000), basis="per_capacity_unit")
    own = {
        **normal(tier(4 * H, H, 8000), basis="per_capacity_unit"),
        "group": elsewhere.pk,
    }
    found = refused(
        call(made, "save", {"entries": [per_spot, own]}, container=container.pk),
        "group-not-on-floor-plan",
    )
    assert "floor plan" in found["reason"]
    refused(
        call(
            made,
            "save",
            {"entries": [per_spot, {**own, "group": 2**40}]},
            container=container.pk,
        ),
        "group-not-on-floor-plan",
    )
    assert (
        not PriceBookEntry.objects.of(made.organization)
        .filter(booking_configuration=container)
        .exists()
    )
    # A group on an exclusive container.
    own_here = {
        **own,
        "group": SpotGroup.objects.of(made.organization).get(name="Stage end").pk,
    }
    refused(
        call(made, "save", {"entries": [normal(tier(12 * H, 12 * H, 1)), own_here]}),
        "groups-per-spot",
    )


def test_a_failure_half_way_leaves_the_set_as_it_was(made: made_up.MadeUp) -> None:
    container, group = shared_container(made)
    per_spot = normal(tier(4 * H, H, 5000), basis="per_capacity_unit")
    save(made, per_spot, container=container.pk)
    bad = {**normal(tier(1 * H, H, 1), basis="per_capacity_unit"), "group": group.pk}
    refused(
        call(
            made,
            "save",
            {"entries": [normal(tier(4 * H, H, 9999), basis="per_capacity_unit"), bad]},
            container=container.pk,
        ),
        "tiers-incomplete",
    )
    entry = PriceBookEntry.objects.of(made.organization).get(
        booking_configuration=container
    )
    assert list(
        PriceTier.objects.of(made.organization)
        .filter(price_book_entry=entry)
        .values_list("amount", flat=True)
    ) == [5000]


# --- Concurrent edits, privileges ---


def admin_in(made: made_up.MadeUp) -> Member:
    role = Role.objects.create(
        organization=made.organization, name="Tester", admin_access=True
    )
    RoleCategory.objects.create(
        organization=made.organization, role=role, booking_category=made.private
    )
    return Member.objects.create(
        organization=made.organization,
        role=role,
        full_name="Test Admin",
        first_name="Test",
        last_name="Admin",
        email="tester@example.com",
        phone="+4790000099",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def test_two_administrators_saving_one_price_at_once_end_in_one_valid_state(
    made: made_up.MadeUp,
) -> None:
    second = admin_in(made)
    barrier = Barrier(4)
    mine = [
        {"entries": [normal(tier(12 * H, 12 * H, 1000 * (n + 1)))]} for n in range(4)
    ]

    def go(who: Member, n: int) -> None:
        barrier.wait()
        try:
            price_admin.save_prices(
                who, made.configuration.pk, made.private.pk, mine[n]
            )
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=4) as pool:
        for future in [
            pool.submit(go, (made.administrator, second)[n % 2], n) for n in range(4)
        ]:
            future.result()
        rows = stored(made)
    assert len(rows) == 1 and rows[0][2] in {1000, 2000, 3000, 4000}
    assert PriceBookEntry.objects.of(made.organization).count() == 1


def test_a_currency_change_and_a_first_price_at_once_never_both_land(
    made: made_up.MadeUp,
) -> None:
    answer(call(made, "book-make", {"name": "Spring"}))
    spring = PriceBook.objects.of(made.organization).get(name="Spring")
    BookingConfiguration.objects.of(made.organization).update(price_book=spring)
    PriceBookEntry.objects.of(made.organization).update(price_book=made.price_book)
    PriceBookEntry.objects.of(made.organization).delete()
    barrier = Barrier(2)
    outcomes: list[str] = []

    def currency() -> None:
        barrier.wait()
        try:
            price_admin.change_book(made.administrator, spring.pk, {"currency": "EUR"})
            outcomes.append("currency")
        except SetupRefusedError:
            outcomes.append("refused")
        finally:
            connection.close()

    def price() -> None:
        barrier.wait()
        try:
            price_admin.save_prices(
                made.administrator,
                made.configuration.pk,
                made.private.pk,
                {"entries": [normal(FULL)]},
            )
            outcomes.append("price")
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(currency), pool.submit(price)]:
            future.result()
        book = PriceBook.objects.of(made.organization).get(pk=spring.pk)
        entry = PriceBookEntry.objects.of(made.organization).get()
    assert entry.currency == book.currency


def test_the_restricted_application_login_can_do_all_of_it(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        price_admin.make_book(made.administrator, {"name": "Spring"})
        spring = PriceBook.objects.of(made.organization).get(name="Spring")
        price_admin.change_book(
            made.administrator, spring.pk, {"currency": "DKK", "name": "Vår"}
        )
        price_admin.save_prices(
            made.administrator,
            made.configuration.pk,
            made.private.pk,
            {"entries": [normal(tier(6 * H, H, 10), tier(12 * H, 2 * H, 5))]},
        )
        price_admin.remove_prices(
            made.administrator, made.configuration.pk, made.private.pk
        )
        price_admin.save_prices(
            made.administrator,
            made.configuration.pk,
            made.private.pk,
            {"entries": [normal(FULL)]},
        )
        price_admin.remove_book(made.administrator, spring.pk)
    assert (
        AuditEvent.objects.of(made.organization)
        .filter(
            action__in=[
                AuditAction.MADE_PRICE_BOOK,
                AuditAction.CHANGED_PRICE_BOOK,
                AuditAction.REMOVED_PRICE_BOOK,
                AuditAction.MADE_PRICE,
                AuditAction.CHANGED_PRICE,
                AuditAction.REMOVED_PRICE,
            ]
        )
        .count()
        == 6
    )


def test_a_service_needs_its_own_transaction(made: made_up.MadeUp) -> None:
    from django.db import transaction

    with transaction.atomic(), pytest.raises(RuntimeError):
        price_admin.make_book(made.administrator, {"name": "Spring"})


def test_a_former_administrator_cannot_change_anything(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        status=MemberStatus.BLOCKED
    )
    with pytest.raises(PermissionDenied):
        call(made, "book-make", {"name": "Spring"})
    with pytest.raises(PermissionDenied):
        price_admin.save_prices(
            made.administrator,
            made.configuration.pk,
            made.private.pk,
            {"entries": [normal(FULL)]},
        )
    assert len(stored(made)) == 1


# --- The setup file ---


def fjordside_with_admin() -> tuple[Any, Member]:
    org = setups.load(setup_file.read(Path("setups/fjordside.toml")), today=TODAY)
    role = Role.objects.of(org).get(name="Admin")
    admin = Member.objects.create(
        organization=org,
        role=role,
        full_name="Sam Field",
        first_name="Sam",
        last_name="Field",
        email="sam@example.com",
        phone="+4790000002",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )
    return org, admin


def roundtrip(org: Any, subdomain: str) -> tuple[setup_file.Setup, setup_file.Setup]:
    exported = setups.export(org)
    parsed = setup_file.parse(setup_file.write(exported))
    assert parsed.price_books == exported.price_books
    second = setups.load(
        replace(parsed, organization=replace(parsed.organization, subdomain=subdomain)),
        today=TODAY,
    )
    again = setups.export(second)
    return exported, replace(
        again,
        organization=replace(
            again.organization, subdomain=exported.organization.subdomain
        ),
    )


def test_the_export_after_edits_on_the_screens_reloads_to_the_same_setup() -> None:
    org, admin = fjordside_with_admin()
    from randirandi.core.models import BookingCategory

    hire = BookingConfiguration.objects.of(org).get(number=2)
    work = BookingCategory.objects.of(org).get(name="Work")
    private = BookingCategory.objects.of(org).get(name="Private")
    night = BookingConfiguration.objects.of(org).get(number=1)
    # Work's tiers by time of day, edited; Private's by length, odd amount.
    price_admin.save_prices(
        admin,
        hire.pk,
        work.pk,
        {
            "entries": [
                normal(
                    tier(12 * H, 4 * H, 49950),
                    tier(22 * H, 5 * H, 100000),
                    by="time of day",
                )
            ]
        },
    )
    price_admin.save_prices(
        admin,
        hire.pk,
        private.pk,
        {"entries": [normal(tier(4 * H, 4 * H, 25000), tier(12 * H, 90, 12345))]},
    )
    price_admin.remove_prices(admin, night.pk, private.pk)
    # A second book with its own currency, and the first renamed.
    price_admin.make_book(admin, {"name": 'Spring "26" ☃', "currency": "EUR"})
    book = PriceBook.objects.of(org).get(name="Standard")
    price_admin.change_book(admin, book.pk, {"name": "Standard 2"})
    exported, again = roundtrip(org, "second")
    assert exported.price_books == again.price_books
    assert [b.name for b in exported.price_books] == ["Standard 2", 'Spring "26" ☃']
    assert exported.price_books[1].currency == "EUR"
    work_entry = next(
        e
        for e in exported.price_books[0].entries
        if (e.booking_configuration, e.booking_category) == (2, "Work")
    )
    assert work_entry.tiers[0].amount == 49950


def test_a_price_for_a_group_exports_and_reloads_with_its_group() -> None:
    from randirandi.core.models import AssetInventory, BookingCategory, Spot

    org = setups.load(
        setup_file.read(Path("setups/scenarios/cinema.toml")), today=TODAY
    )
    admin = admin_in_org(org)
    plan = AssetInventory.objects.of(org).get(name="Seats")
    group = SpotGroup.objects.create(
        organization=org, floor_plan=plan, name="Front rows", word="row"
    )
    for spot in Spot.objects.of(org).filter(floor_plan=plan, number__lte=20):
        spot.group = group
        spot.save()
    screening = BookingConfiguration.objects.of(org).get(number=1)
    category = BookingCategory.objects.of(org).get(name="private")
    price_admin.save_prices(
        admin,
        screening.pk,
        category.pk,
        {
            "entries": [
                normal(tier(2 * H, 2 * H, 12000), basis="per_capacity_unit"),
                {
                    **normal(tier(2 * H, 2 * H, 20000), basis="per_capacity_unit"),
                    "group": group.pk,
                },
            ]
        },
    )
    exported, again = roundtrip(org, "cinema2")
    entries = exported.price_books[0].entries
    assert {e.group for e in entries if e.booking_configuration == 1} == {
        "",
        "Front rows",
    }
    assert exported.price_books == again.price_books
    price_admin.save_prices(
        admin,
        screening.pk,
        category.pk,
        {"entries": [normal(tier(2 * H, 2 * H, 12000), basis="per_capacity_unit")]},
    )
    exported, again = roundtrip(org, "cinema3")
    assert exported.price_books == again.price_books


def admin_in_org(org: Any) -> Member:
    role = Role.objects.of(org).get(name="Member")
    role.admin_access = True
    role.save()
    return Member.objects.create(
        organization=org,
        role=role,
        full_name="Sam Field",
        first_name="Sam",
        last_name="Field",
        email="sam@cinema.example",
        phone="+4790000002",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def test_every_book_removed_still_exports_and_reloads() -> None:
    org, admin = fjordside_with_admin()
    BookingConfiguration.objects.of(org).update(price_book=None)
    PriceBookEntry.objects.of(org).delete()
    for book in PriceBook.objects.of(org):
        price_admin.remove_book(admin, book.pk)
    exported = setups.export(org)
    assert exported.price_books == ()
    assert setup_file.parse(setup_file.write(exported)).price_books == ()


def test_the_screens_and_the_setup_file_take_a_tier_up_to_24_00() -> None:
    """A time-of-day tier runs to 24:00, the end of the day, and no later."""
    from randirandi.core.models import BookingCategory

    org, admin = fjordside_with_admin()
    hire = BookingConfiguration.objects.of(org).get(number=2)
    work = BookingCategory.objects.of(org).get(name="Work")
    with pytest.raises(SetupRefusedError):
        price_admin.save_prices(
            admin,
            hire.pk,
            work.pk,
            {"entries": [normal(tier(25 * H, 4 * H, 1), by="time of day")]},
        )
    price_admin.save_prices(
        admin,
        hire.pk,
        work.pk,
        {"entries": [normal(tier(24 * H, 4 * H, 1), by="time of day")]},
    )
    exported = setups.export(org)
    assert "24:00" in setup_file.write(exported)
    assert (
        setup_file.parse(setup_file.write(exported)).price_books == exported.price_books
    )


def stamp_of(made: made_up.MadeUp) -> str:
    place: Any = price_admin.prices_place(made.organization)
    container = next(c for c in place["containers"] if c["id"] == made.configuration.pk)
    stamp: str = container["stamps"][str(made.private.pk)]
    return stamp


def test_a_save_on_stale_prices_is_refused_with_the_reason_and_changes_nothing(
    made: made_up.MadeUp,
) -> None:
    seen = stamp_of(made)
    # Another administrator saves first.
    save(made, normal(tier(12 * H, 12 * H, 111)))
    assert stamp_of(made) != seen
    before = stored(made)
    count = AuditEvent.objects.of(made.organization).count()
    found = refused(
        call(
            made,
            "save",
            {"entries": [normal(tier(12 * H, 12 * H, 222))], "stamp": seen},
        ),
        "changed-meanwhile",
    )
    assert "changed by someone else" in found["reason"]
    assert stored(made) == before
    assert AuditEvent.objects.of(made.organization).count() == count
    # The stamp of what is there now is taken, and moves on.
    fresh = stamp_of(made)
    save_with = {"entries": [normal(tier(12 * H, 12 * H, 222))], "stamp": fresh}
    answer(call(made, "save", save_with))
    assert stored(made) == [(12 * H, 12 * H, 222)]
    assert stamp_of(made) != fresh


def test_a_remove_on_stale_prices_is_refused(made: made_up.MadeUp) -> None:
    seen = stamp_of(made)
    save(made, normal(tier(12 * H, 12 * H, 111)))
    refused(call(made, "remove", {"stamp": seen}), "changed-meanwhile")
    assert stored(made) == [(12 * H, 12 * H, 111)]
    answer(call(made, "remove", {"stamp": stamp_of(made)}))
    assert not PriceBookEntry.objects.of(made.organization).exists()


def test_the_views_require_the_stamp_and_the_service_does_not(
    made: made_up.MadeUp,
) -> None:
    body = {"entries": [normal(FULL)]}
    before = stored(made)
    assert call(made, "save", body, stamped=False).status_code == 400
    assert call(made, "remove", {}, stamped=False).status_code == 400
    assert stored(made) == before
    # The service (the setup file's and the tests' way in) takes none.
    price_admin.save_prices(
        made.administrator, made.configuration.pk, made.private.pk, body
    )
    assert stored(made) == [(12 * H, 12 * H, 25000)]


def test_a_bad_stamp_is_a_malformed_request(made: made_up.MadeUp) -> None:
    for stamp in (5, ["x"], "x" * 65):
        assert (
            call(made, "save", {"entries": [normal(FULL)], "stamp": stamp}).status_code
            == 400
        )


def test_the_setup_file_and_the_screens_share_the_entries_rule() -> None:
    from randirandi import price_entries_rule

    name = "what_the_entries_of_a_category_get_wrong"
    assert (
        getattr(price_admin, name)
        is getattr(price_entries_rule, name)
        is vars(setup_file)[name]
    )


# --- Book after an edit ---


def administrator_of(example: Example) -> Member:
    role = Role.objects.create(
        organization=example.org, name="Admins", admin_access=True
    )
    RoleCategory.objects.create(
        organization=example.org, role=role, booking_category=example.category
    )
    return Member.objects.create(
        organization=example.org,
        role=role,
        full_name="Sam Field",
        first_name="Sam",
        last_name="Field",
        email="sam@example.com",
        phone="+4790000002",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def test_book_shows_the_new_price_and_an_earlier_order_keeps_its_amount(
    example: Example,
) -> None:
    from randirandi.core.pricing import price_booking

    admin = administrator_of(example)
    ask: dict[str, Any] = {
        "configuration": example.config,
        "booking_category": example.category,
        "starts_at": at(hour=10),
        "ends_at": at(hour=12),
    }
    before = price_booking(**ask).total
    assert before > 0 and before != 99900
    order = create_order(
        member=example.member,
        requests=[
            BookingRequest(
                example.config.pk, example.category.pk, at(hour=10), at(hour=12)
            )
        ],
        now=at(CALENDAR_DAY - timedelta(days=1)),
        accepted_version=made_up.accepted(example.member),
    )
    line = BookingLine.objects.of(example.org).get(booking__order=order)
    assert order.total == before and line.total == before
    price_admin.save_prices(
        admin,
        example.config.pk,
        example.category.pk,
        {"entries": [normal(tier(4 * H, 4 * H, 99900))]},
    )
    # The members' calendar and the cart price from the new tiers ...
    assert price_booking(**ask).total == 99900
    assert example.offers()
    # ... and the order already made keeps what it was.
    order.refresh_from_db()
    line.refresh_from_db()
    assert (order.total, line.total) == (before, before)
    price_admin.remove_prices(admin, example.config.pk, example.category.pk)
    order.refresh_from_db()
    line.refresh_from_db()
    assert (order.total, line.total) == (before, before)
    assert BookingLine.objects.of(example.org).count() == 1


def test_a_category_without_a_complete_price_is_not_offered(example: Example) -> None:
    admin = administrator_of(example)
    assert example.offers()
    price_admin.save_prices(
        admin,
        example.config.pk,
        example.category.pk,
        {"entries": [normal(tier(4 * H, H, 700))]},
    )
    assert example.offers()
    # The tiers come to stop short of the longest booking (a write past the
    # screens): the category is not offered.
    PriceTier.objects.of(example.org).update(up_to=3 * H)
    assert not example.offers()
    price_admin.save_prices(
        admin,
        example.config.pk,
        example.category.pk,
        {"entries": [normal(tier(4 * H, H, 700))]},
    )
    assert example.offers()
    price_admin.remove_prices(admin, example.config.pk, example.category.pk)
    assert not example.offers()
