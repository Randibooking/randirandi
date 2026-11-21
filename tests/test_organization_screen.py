# SPDX-License-Identifier: AGPL-3.0-only
"""The Organization place : the gate, CSRF and origin, malformed
bodies, every field and every refusal with its reason, one audit event a save,
a stale sheet, concurrent saves, the retention confirmation and its numbers
(the same the erasing job then erases), the booking limits applying to new
bookings only, and an export that reloads after edits."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import count
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client

from randirandi import setup_file
from randirandi.core import admin_setup_views as views
from randirandi.core import (
    audit,
    booking_conditions,
    organization_admin,
    retention,
    setup_admin,
)
from randirandi.core import setup as setups
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    AuditEvent,
    Booking,
    BookingCategory,
    BookingState,
    LoginAttempt,
    Member,
    MemberStatus,
    Order,
    Organization,
    Origin,
    Role,
    RoleCategory,
    TakenTime,
)
from randirandi.core.ordering import OrderOverDayLimitError, create_order
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_admin_blocks import answer, request_to
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_booking_limits import NOW, asked, limits, order
from tests.test_booking_limits import made as limits_made  # noqa: F401
from tests.test_calendar import Example
from tests.test_login_sessions import HOST, get, page_settings  # noqa: F401
from tests.test_login_sessions import NOW as CODE_NOW
from tests.test_second_factor import clocks, token  # noqa: F401
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = date(2027, 3, 1)
FORMER = count(1)


@pytest.fixture
def made() -> made_up.MadeUp:
    """The made-up organization with its Board role given a booking category,
    as the setup file requires of a role."""
    made = made_up.everything()
    RoleCategory.objects.create(
        organization=made.organization, role=made.board, booking_category=made.private
    )
    return made


def call(
    made: made_up.MadeUp,
    which: str,
    data: Any = None,
    *,
    who: Member | None = None,
    confirmed: bool = True,
) -> HttpResponse:
    request = request_to(
        who or made.administrator,
        "get" if which == "data" else "post",
        f"/admin/organization/{which}/",
        data,
        confirmed=confirmed,
    )
    if which == "data":
        return views.organization_data(request)
    return views.organization_change(request)


def current(made: made_up.MadeUp) -> Organization:
    return Organization.objects.get(pk=made.organization.pk)


def sheet(made: made_up.MadeUp) -> dict[str, Any]:
    """What a sheet is opened on."""
    return answer(call(made, "data"))


def save(made: made_up.MadeUp, **fields: Any) -> dict[str, Any]:
    """A save from a sheet opened just now. A confirmed lowering carries the
    administrator's current code unless the test gives one."""
    if "confirm" in fields and "code" not in fields:
        fields["code"] = fresh(made.administrator, now=CODE_NOW)["code"]
    version = organization_admin.version_of(current(made))
    r = call(made, "change", {"version": version, **fields})
    return answer(r)


def refused(
    made: made_up.MadeUp, code: str, status: int = 409, **fields: Any
) -> dict[str, Any]:
    if "confirm" in fields and "code" not in fields:
        fields["code"] = fresh(made.administrator, now=CODE_NOW)["code"]
    version = fields.pop("version", None) or organization_admin.version_of(
        current(made)
    )
    found = answer(call(made, "change", {"version": version, **fields}), status)
    assert found["error"] == code
    return found


def events(made: made_up.MadeUp) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.CHANGED_ORGANIZATION)
        .order_by("pk")
    )


def kept(made: made_up.MadeUp) -> tuple[Any, ...]:
    """The organization's row and the audit events: what a refusal leaves."""
    row = Organization.objects.filter(pk=made.organization.pk).values().get()
    return (row, AuditEvent.objects.of(made.organization).count())


def another_standard(made: made_up.MadeUp) -> Role:
    role = Role.objects.create(organization=made.organization, name="Guests")
    RoleCategory.objects.create(
        organization=made.organization, role=role, booking_category=made.private
    )
    return role


# --- The gate, CSRF and origin ---------------------------------------------------

EVERY_CALL = [("data", {}), ("change", {"version": "x", "name": "Hacked"})]


@pytest.mark.parametrize(("which", "data"), EVERY_CALL)
def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp, which: str, data: dict[str, Any]
) -> None:
    before = kept(made)
    for who, confirmed in ((made.administrator, False), (made.member, True)):
        with pytest.raises(PermissionDenied):
            call(made, which, data, who=who, confirmed=confirmed)
    assert kept(made) == before


def test_another_organizations_administrator_changes_neither(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    before, other_before = kept(made), kept(elsewhere)
    version = organization_admin.version_of(current(made))
    for which, data in (("data", None), ("change", {"version": version, "name": "X"})):
        with pytest.raises(PermissionDenied):
            call(made, which, data, who=elsewhere.administrator)
    assert (kept(made), kept(elsewhere)) == (before, other_before)


def test_a_role_of_another_organization_is_not_a_standard_role(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    before = kept(made)
    found = refused(made, "unknown-role", standard_role=elsewhere.standard.pk)
    assert "reason" not in found
    assert kept(made) == before
    assert current(elsewhere).standard_role_id == elsewhere.standard.pk


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, method in (
            ("/admin/organization/data/", "post"),
            ("/admin/organization/change/", "get"),
        ):
            response = getattr(client, method)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
        assert config(get(client, "/admin/organization/").content.decode())[
            "place"
        ] == ("organization")
    for path, method in (
        ("/admin/organization/data/", "get"),
        ("/admin/organization/change/", "post"),
    ):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        sign_in_administrator(client, made.administrator)
        csrf = str(config(get(client, "/admin/organization/").content.decode())["csrf"])
        version = organization_admin.version_of(current(made))

        def send(headers: dict[str, Any]) -> HttpResponse:
            response = client.post(
                "/admin/organization/change/",
                json.dumps({"version": version, "name": "Renamed co-op"}),
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        right = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://" + HOST}
        wrong = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://elsewhere.example"}
        no_token = {"HTTP_ORIGIN": "https://" + HOST}
        assert send(no_token).status_code == 403
        assert send(wrong).status_code == 403
        assert current(made).name != "Renamed co-op"
        assert answer(send(right))["name"] == "Renamed co-op"
    assert current(made).name == "Renamed co-op"


# --- What the place shows ---------------------------------------------------------


def test_the_place_shows_the_details_the_roles_and_what_may_be_chosen(
    made: made_up.MadeUp,
) -> None:
    before = AuditEvent.objects.of(made.organization).count()
    found = sheet(made)
    org = current(made)
    assert found["name"] == org.name and found["subdomain"] == "harbourview"
    assert found["contact_email"] == "board@harbourview.example"
    assert found["standard_role"] == made.standard.pk
    assert (found["max_bookings_a_day"], found["book_ahead_months"]) == (None, None)
    assert (found["tax_rate"], found["time_zone"], found["currency"]) == (
        0,
        "Europe/Oslo",
        "NOK",
    )
    assert (found["member_retention_months"], found["booking_retention_months"]) == (
        12,
        36,
    )
    assert found["version"] == organization_admin.version_of(org)
    assert {r["name"]: r["admin_access"] for r in found["roles"]} == {
        "Standard": False,
        "Board": True,
    }
    assert "Europe/Oslo" in found["time_zones"] and "UTC" in found["time_zones"]
    assert found["currencies"] == ["NOK", "DKK", "EUR"]
    assert found["language"] == "en"
    assert found["languages"] == [
        {"code": "en", "name": "English"},
        {"code": "nb", "name": "Norsk bokmål"},
    ]
    assert "labels" not in found
    # Reading writes no event.
    assert AuditEvent.objects.of(made.organization).count() == before


def test_another_organizations_roles_are_not_listed(made: made_up.MadeUp) -> None:
    made_up.everything("elsewhere")
    assert {r["name"] for r in sheet(made)["roles"]} == {"Standard", "Board"}


# --- Each field -------------------------------------------------------------------

VALID = [
    ("name", "  Renamed co-op  ", "Renamed co-op"),
    ("contact_email", " desk@example.org ", "desk@example.org"),
    ("max_bookings_a_day", 3, 3),
    ("max_bookings_a_day", None, None),
    ("book_ahead_months", 12, 12),
    ("book_ahead_months", None, None),
    ("tax_rate", 2500, 2500),
    ("tax_rate", 0, 0),
    ("tax_rate", 10000, 10000),
    ("member_retention_months", 24, 24),
    ("booking_retention_months", 60, 60),
    ("time_zone", "Europe/Stockholm", "Europe/Stockholm"),
    ("currency", "EUR", "EUR"),
    ("language", "nb", "nb"),
]


@pytest.mark.parametrize(("field", "sent", "stored"), VALID)
def test_each_field_is_saved_with_one_event_and_the_place_after(
    made: made_up.MadeUp, field: str, sent: Any, stored: Any
) -> None:
    Organization.objects.filter(pk=made.organization.pk).update(
        max_bookings_a_day=5, book_ahead_months=6, tax_rate=1000
    )
    before = current(made)
    found = save(made, **{field: sent})
    after = current(made)
    assert getattr(after, field) == stored and found[field] == stored
    assert found["version"] == organization_admin.version_of(after)
    for other in organization_admin.FIELDS:
        if other != field and other != "standard_role":
            assert getattr(after, other) == getattr(before, other)
    assert after.subdomain == before.subdomain and after.labels == before.labels
    [event] = events(made)
    assert (event.object_kind, event.object_id) == ("organization", after.pk)
    assert event.administrator_id == made.administrator.pk


def test_the_standard_role_is_one_of_its_own_roles_without_admin_access(
    made: made_up.MadeUp,
) -> None:
    guests = another_standard(made)
    found = save(made, standard_role=guests.pk)
    assert found["standard_role"] == guests.pk
    assert current(made).standard_role_id == guests.pk
    assert len(events(made)) == 1


def test_a_save_of_several_fields_is_one_event(made: made_up.MadeUp) -> None:
    save(made, name="Two", tax_rate=500, currency="DKK", time_zone="UTC")
    after = current(made)
    assert (after.name, after.tax_rate, after.currency, after.time_zone) == (
        "Two",
        500,
        "DKK",
        "UTC",
    )
    assert len(events(made)) == 1


def test_a_save_that_changes_nothing_writes_no_event(made: made_up.MadeUp) -> None:
    org = current(made)
    found = save(made, name=org.name, tax_rate=0, max_bookings_a_day=None)
    assert found["name"] == org.name
    assert events(made) == []


def test_the_subdomain_is_the_address_and_cannot_be_sent(made: made_up.MadeUp) -> None:
    before = kept(made)
    refused(made, "bad-request", 400, subdomain="other")
    assert kept(made) == before


def test_the_event_names_no_value(made: made_up.MadeUp) -> None:
    save(made, contact_email="secret.person@example.org", name="Private name")
    [event] = events(made)
    text = json.dumps(
        {k: str(v) for k, v in vars(event).items() if not k.startswith("_")}
    )
    assert "secret.person" not in text and "Private name" not in text


# --- Every refusal, with its reason, writes nothing ------------------------------

REFUSALS = [
    ({"name": ""}, "invalid-name"),
    ({"name": "   "}, "invalid-name"),
    ({"name": "x" * 201}, "invalid-name"),
    ({"contact_email": ""}, "invalid-contact-email"),
    ({"contact_email": "nobody"}, "invalid-contact-email"),
    ({"contact_email": "a@b"}, "invalid-contact-email"),
    ({"contact_email": "a b@example.org"}, "invalid-contact-email"),
    ({"contact_email": "a@@example.org"}, "invalid-contact-email"),
    # The setup file's rule lets it by; the model's own refuses it.
    ({"contact_email": "a@b.c"}, "invalid-contact-email"),
    ({"time_zone": ""}, "invalid-time-zone"),
    ({"time_zone": "Mars/Base"}, "invalid-time-zone"),
    ({"time_zone": "oslo"}, "invalid-time-zone"),
    ({"currency": ""}, "invalid-currency"),
    ({"currency": "USD"}, "invalid-currency"),
    ({"currency": "nok"}, "invalid-currency"),
    ({"language": ""}, "invalid-language"),
    ({"language": "de"}, "invalid-language"),
    ({"language": "NB"}, "invalid-language"),
    ({"max_bookings_a_day": 0}, "invalid-max-bookings-a-day"),
    ({"max_bookings_a_day": -1}, "invalid-max-bookings-a-day"),
    ({"book_ahead_months": 0}, "invalid-book-ahead-months"),
    ({"tax_rate": -1}, "invalid-tax-rate"),
    ({"tax_rate": 10001}, "invalid-tax-rate"),
    ({"member_retention_months": 0}, "invalid-member-retention-months"),
    ({"member_retention_months": 32768}, "invalid-member-retention-months"),
    ({"booking_retention_months": 0}, "invalid-booking-retention-months"),
    ({"booking_retention_months": -5}, "invalid-booking-retention-months"),
    ({"booking_retention_months": 10**9}, "invalid-booking-retention-months"),
]


@pytest.mark.parametrize(("fields", "code"), REFUSALS)
def test_a_value_the_model_or_the_setup_file_refuses_is_refused_with_its_reason(
    made: made_up.MadeUp, fields: dict[str, Any], code: str
) -> None:
    before = kept(made)
    found = refused(made, code, **fields)
    assert isinstance(found["reason"], str) and found["reason"]
    assert kept(made) == before


def test_the_standard_role_never_gives_admin_access(made: made_up.MadeUp) -> None:
    before = kept(made)
    refused(made, "standard-admin", standard_role=made.board.pk)
    refused(made, "unknown-role", standard_role=0)
    refused(made, "unknown-role", standard_role=2**40)
    assert kept(made) == before


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'"name"',
        {},
        {"version": 5, "name": "x"},
        {"name": "x"},
        {"version": "v"},
        {"version": "v", "labels": {}},
        {"version": "v", "name": 5},
        {"version": "v", "name": None},
        {"version": "v", "contact_email": 5},
        {"version": "v", "time_zone": 5},
        {"version": "v", "tax_rate": "5"},
        {"version": "v", "tax_rate": 5.5},
        {"version": "v", "tax_rate": True},
        {"version": "v", "tax_rate": None},
        {"version": "v", "member_retention_months": None},
        {"version": "v", "max_bookings_a_day": "1"},
        {"version": "v", "max_bookings_a_day": False},
        {"version": "v", "standard_role": "1"},
        {"version": "v", "standard_role": None},
        {"version": "v", "name": "x", "confirm": 5},
        {"version": "v", "name": "x", "confirm": {"members": "1"}},
        {"version": "v", "name": "x", "confirm": {"members": 1, "more": 1}},
        {"version": "v", "name": "x", "confirm": {"members": True}},
        {"version": "v", "name": "x", "subdomain": "y"},
    ],
)
def test_a_malformed_body_is_a_400_and_changes_nothing(
    made: made_up.MadeUp, body: Any
) -> None:
    before = kept(made)
    found = answer(call(made, "change", body), 400)
    assert found["error"] == "bad-request"
    assert kept(made) == before


# --- A stale sheet ----------------------------------------------------------------


def test_a_stale_sheet_is_refused_and_overwrites_nothing(made: made_up.MadeUp) -> None:
    opened = sheet(made)["version"]
    save(made, name="First editor's name")
    before = kept(made)
    refused(made, "stale", version=opened, name="Second editor's name", tax_rate=900)
    assert kept(made) == before
    assert current(made).name == "First editor's name" and current(made).tax_rate == 0
    # The same words from a sheet opened again are saved.
    save(made, name="Second editor's name")
    assert current(made).name == "Second editor's name"


def test_a_sheet_is_stale_after_a_change_on_another_screen(
    made: made_up.MadeUp,
) -> None:
    opened = sheet(made)["version"]
    guests = another_standard(made)
    setup_admin.change_role(made.administrator, guests.pk, {"standard": True})
    refused(made, "stale", version=opened, name="Late")
    assert current(made).name != "Late"


def test_a_sheet_is_not_stale_after_a_change_that_is_not_its_own(
    made: made_up.MadeUp,
) -> None:
    opened = sheet(made)["version"]
    Organization.objects.filter(pk=made.organization.pk).update(
        last_order_number=7, labels={"place.book": "Reserve"}
    )
    found = answer(call(made, "change", {"version": opened, "name": "Fine"}))
    assert found["name"] == "Fine"
    assert current(made).labels == {"place.book": "Reserve"}
    assert current(made).last_order_number == 7


def test_a_save_does_not_touch_what_it_does_not_change(made: made_up.MadeUp) -> None:
    Organization.objects.filter(pk=made.organization.pk).update(
        last_order_number=7, labels={"place.book": "Reserve"}
    )
    save(made, tax_rate=300)
    after = current(made)
    assert after.labels == {"place.book": "Reserve"} and after.last_order_number == 7


def second_administrator(made: made_up.MadeUp) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.board,
        full_name="Kim Lane",
        first_name="Kim",
        last_name="Lane",
        email="kim@harbourview.example",
        phone="+4790000003",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def test_two_saves_from_one_sheet_end_in_one_valid_state(made: made_up.MadeUp) -> None:
    second = second_administrator(made)
    version = organization_admin.version_of(current(made))
    barrier = Barrier(2)

    def run(who: Member, fields: dict[str, Any]) -> str:
        barrier.wait(timeout=15)
        try:
            organization_admin.change(who, {"version": version, **fields})
            return "saved"
        except setup_admin.SetupRefusedError as refusal:
            return refusal.code
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: run(*args),
                [
                    (made.administrator, {"name": "Alpha"}),
                    (second, {"tax_rate": 900}),
                ],
            )
        )
    assert sorted(results) == ["saved", "stale"]
    after = current(made)
    # Exactly one of the two landed, whole.
    assert (after.name, after.tax_rate) in {("Alpha", 0), (made.organization.name, 900)}
    assert len(events(made)) == 1


def test_saves_that_each_read_the_other_s_result_both_land(
    made: made_up.MadeUp,
) -> None:
    second = second_administrator(made)
    barrier = Barrier(2)

    def run(who: Member, field: str, value: Any) -> str:
        barrier.wait(timeout=15)
        for _ in range(5):
            version = organization_admin.version_of(
                Organization.objects.get(pk=made.organization.pk)
            )
            try:
                organization_admin.change(who, {"version": version, field: value})
                return "saved"
            except setup_admin.SetupRefusedError as refusal:
                assert refusal.code == "stale"
            finally:
                connection.close()
        return "gave up"

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, made.administrator, "name", "Alpha"),
            pool.submit(run, second, "tax_rate", 900),
        ]
        results = [f.result() for f in futures]
    assert results == ["saved", "saved"]
    after = current(made)
    assert (after.name, after.tax_rate) == ("Alpha", 900)
    assert len(events(made)) == 2


def test_the_real_application_login_may_save(made: made_up.MadeUp) -> None:
    with application_login():
        organization_admin.change(
            made.administrator,
            {"version": organization_admin.version_of(current(made)), "tax_rate": 700},
        )
    assert current(made).tax_rate == 700


def test_the_organization_row_is_locked_from_the_check_to_the_write(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a save is between its checks and its write, the row is held: an
    order's number taken meanwhile waits for the save and is not lost."""
    from randirandi.core.models.organization import take_number

    real = audit.record_event
    entered, release = Barrier(2), Barrier(2)

    def slow(**kwargs: Any) -> Any:
        entered.wait(timeout=15)
        release.wait(timeout=15)
        return real(**kwargs)

    monkeypatch.setattr(organization_admin, "record_event", slow)
    version = organization_admin.version_of(current(made))
    last = current(made).last_order_number

    def saver() -> None:
        try:
            organization_admin.change(
                made.administrator, {"version": version, "tax_rate": 800}
            )
        finally:
            connection.close()

    def number() -> int:
        try:
            return take_number(made.organization.pk, "last_order_number")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(saver)
        entered.wait(timeout=15)
        taker = pool.submit(number)
        with pytest.raises(TimeoutError):
            taker.result(timeout=1.5)
        release.wait(timeout=15)
        saving.result()
        assert taker.result(timeout=15) == last + 1
    assert current(made).tax_rate == 800
    assert current(made).last_order_number == last + 1


# --- The defaults are for new things only -----------------------------------------


def test_a_new_default_time_zone_and_currency_change_no_existing_asset_or_price_book(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core import asset_admin, price_admin
    from randirandi.core.models import Asset, PriceBook

    kept_asset = Asset.objects.of(made.organization).get()
    kept_book = PriceBook.objects.of(made.organization).get()
    assert (kept_asset.time_zone, kept_book.currency) == ("Europe/Oslo", "NOK")
    save(made, time_zone="Europe/Stockholm", currency="EUR")
    assert (
        Asset.objects.of(made.organization).get(pk=kept_asset.pk).time_zone
        == "Europe/Oslo"
    )
    assert (
        PriceBook.objects.of(made.organization).get(pk=kept_book.pk).currency == "NOK"
    )
    # The places offer the new defaults to their New sheets.
    organization = Organization.objects.get(pk=made.organization.pk)
    assert asset_admin.assets_place(organization)["time_zone"] == "Europe/Stockholm"
    assert price_admin.prices_place(organization)["currency"] == "EUR"
    # Made without their own, a new asset and a new price book take the new ones.
    asset_admin.make_asset(made.administrator, {"name": "New hall"})
    price_admin.make_book(made.administrator, {"name": "New prices"})
    new_asset = Asset.objects.of(made.organization).get(name="New hall")
    new_book = PriceBook.objects.of(made.organization).get(name="New prices")
    assert new_asset.time_zone == "Europe/Stockholm"
    assert new_book.currency == "EUR"
    # And the old ones are as they were.
    assert Asset.objects.of(made.organization).get(pk=kept_asset.pk).time_zone == (
        "Europe/Oslo"
    )


# --- Lowering a retention ---------------------------------------------------------


def keep(made: made_up.MadeUp, members: int, bookings: int) -> None:
    """Retention, in months, so that a test can lower it."""
    Organization.objects.filter(pk=made.organization.pk).update(
        member_retention_months=members, booking_retention_months=bookings
    )


def old_members(made: made_up.MadeUp, months_ago: list[int]) -> list[Member]:
    """Disabled members unlisted this many months ago, on the real clock."""
    now = datetime.now(UTC)
    found = []
    for months in months_ago:
        n = next(FORMER)
        found.append(
            Member.objects.create(
                organization=made.organization,
                role=made.standard,
                full_name=f"Former {n}",
                email=f"former{n}@harbourview.example",
                status=MemberStatus.DISABLED,
                listed=False,
                unlisted_at=now - timedelta(days=30 * months + 3),
                origin=Origin.MEMBER_IMPORT,
            )
        )
    return found


def old_booking(made: made_up.MadeUp, months_ago: int) -> Booking:
    """A confirmed booking that ended this many months ago, on the real clock."""
    day = (datetime.now(UTC) - timedelta(days=30 * months_ago + 3)).date()
    return replace(made, day=day).book(10, 12, order=made.spare_order)


def test_lowering_a_retention_is_refused_with_the_numbers_to_confirm(
    made: made_up.MadeUp,
) -> None:
    keep(made, 48, 48)
    old_members(made, [6, 14, 20, 40])
    old_booking(made, 30)
    old_booking(made, 40)
    before = kept(made)
    found = refused(made, "confirm-erasure", member_retention_months=18)
    # Unlisted 20 and 40 months ago are past 18; 14 and 6 are not.
    assert found["erase"] == {"members": 2, "bookings": 0}
    assert kept(made) == before
    found = refused(made, "confirm-erasure", booking_retention_months=24)
    assert found["erase"] == {"members": 0, "bookings": 2}
    found = refused(
        made,
        "confirm-erasure",
        member_retention_months=18,
        booking_retention_months=35,
    )
    assert found["erase"] == {"members": 2, "bookings": 1}
    assert kept(made) == before
    assert current(made).member_retention_months == 48


def test_the_numbers_are_under_the_new_values_of_both(made: made_up.MadeUp) -> None:
    keep(made, 48, 48)
    old_members(made, [6, 14, 20, 40])
    old_booking(made, 30)
    found = refused(
        made,
        "confirm-erasure",
        member_retention_months=36,
        booking_retention_months=24,
    )
    assert found["erase"] == {"members": 1, "bookings": 1}


def test_a_confirmation_of_other_numbers_is_refused_again_with_the_right_ones(
    made: made_up.MadeUp,
) -> None:
    keep(made, 48, 48)
    old_members(made, [20, 40])
    before = kept(made)
    for confirm in (
        {},
        {"members": 2},
        {"members": 1, "bookings": 0},
        {"members": 3, "bookings": 0},
    ):
        found = refused(
            made, "confirm-erasure", member_retention_months=18, confirm=confirm
        )
        assert found["erase"] == {"members": 2, "bookings": 0}
    assert kept(made) == before


def lowering(made: made_up.MadeUp, **more: Any) -> dict[str, Any]:
    """The confirmed lowering of the member retention, as a sheet sends it."""
    old_members(made, [20, 40])
    keep(made, 48, 48)
    return {
        "version": organization_admin.version_of(current(made)),
        "member_retention_months": 18,
        "confirm": {"members": 2, "bookings": 0},
        **more,
    }


def test_a_confirmed_lowering_without_a_code_or_with_a_wrong_one_erases_nothing(
    made: made_up.MadeUp,
) -> None:
    body = lowering(made)
    before = kept(made)
    for sent in ({}, {"code": ""}, {"code": "000000"}, {"code": "   "}):
        found = answer(call(made, "change", {**body, **sent}), 409)
        assert found["error"] == "code-refused"
    assert current(made).member_retention_months == 48
    assert events(made) == []
    assert kept(made)[0] == before[0]
    assert retention.purge_organization(current(made))["members"] == 0
    assert Member.objects.of(made.organization).filter(full_name="").count() == 0


def test_a_confirmed_lowering_with_a_right_code_is_saved_and_the_code_is_used_up(
    made: made_up.MadeUp,
) -> None:
    body = lowering(made, code=fresh(made.administrator, now=CODE_NOW)["code"])
    found = answer(call(made, "change", body))
    assert found["member_retention_months"] == 18
    assert len(events(made)) == 1
    # The same code again is refused: its counter is used.
    again = {**body, "version": found["version"], "member_retention_months": 12}
    assert answer(call(made, "change", again), 409)["error"] == "code-refused"
    assert current(made).member_retention_months == 18


def test_too_many_wrong_codes_limit_a_lowering(made: made_up.MadeUp) -> None:
    body = lowering(made)
    for _ in range(5):
        assert (
            answer(call(made, "change", {**body, "code": "000000"}), 409)["error"]
            == "code-refused"
        )
    right = {**body, "code": fresh(made.administrator, now=CODE_NOW)["code"]}
    assert answer(call(made, "change", right), 409)["error"] == "code-limited"
    assert current(made).member_retention_months == 48


def test_the_first_answer_to_a_lowering_asks_for_no_code(made: made_up.MadeUp) -> None:
    body = lowering(made)
    body.pop("confirm")
    found = answer(call(made, "change", body), 409)
    assert found["error"] == "confirm-erasure"
    assert found["erase"] == {"members": 2, "bookings": 0}
    assert LoginAttempt.objects.of(made.organization).count() == 0 or all(
        a.kind != "mfa" for a in LoginAttempt.objects.of(made.organization)
    )


def test_a_raise_and_other_saves_need_no_code(made: made_up.MadeUp) -> None:
    keep(made, 48, 48)
    save(made, member_retention_months=60, booking_retention_months=60)
    save(made, name="Other", tax_rate=100)
    assert current(made).member_retention_months == 60
    assert all(a.kind != "mfa" for a in LoginAttempt.objects.of(made.organization))


def test_a_code_is_text(made: made_up.MadeUp) -> None:
    body = lowering(made, code=123456)
    assert answer(call(made, "change", body), 400)["error"] == "bad-request"


def test_a_confirmed_lowering_is_saved_with_one_event(made: made_up.MadeUp) -> None:
    keep(made, 48, 48)
    old_members(made, [20, 40])
    found = save(
        made, member_retention_months=18, confirm={"members": 2, "bookings": 0}
    )
    assert found["member_retention_months"] == 18
    assert current(made).member_retention_months == 18
    assert len(events(made)) == 1


def test_a_lowering_with_nothing_to_erase_still_shows_zero_and_waits(
    made: made_up.MadeUp,
) -> None:
    found = refused(made, "confirm-erasure", booking_retention_months=6)
    assert found["erase"] == {"members": 0, "bookings": 0}
    assert current(made).booking_retention_months == 36
    save(made, booking_retention_months=6, confirm={"members": 0, "bookings": 0})
    assert current(made).booking_retention_months == 6


def test_the_confirmation_is_for_the_numbers_at_the_time_of_the_save(
    made: made_up.MadeUp,
) -> None:
    keep(made, 48, 48)
    old_members(made, [20])
    shown = refused(made, "confirm-erasure", member_retention_months=18)["erase"]
    assert shown == {"members": 1, "bookings": 0}
    old_members(made, [30])
    again = refused(made, "confirm-erasure", member_retention_months=18, confirm=shown)
    assert again["erase"] == {"members": 2, "bookings": 0}
    assert current(made).member_retention_months == 48


def test_raising_a_retention_needs_no_confirmation(made: made_up.MadeUp) -> None:
    old_members(made, [20, 40])
    save(made, member_retention_months=60, booking_retention_months=48)
    assert current(made).member_retention_months == 60
    assert current(made).booking_retention_months == 48
    # The same value is not a lowering either.
    save(made, member_retention_months=60)


def test_lowering_one_while_raising_the_other_asks_to_confirm(
    made: made_up.MadeUp,
) -> None:
    refused(
        made,
        "confirm-erasure",
        member_retention_months=6,
        booking_retention_months=60,
    )
    assert current(made).booking_retention_months == 36


def test_a_confirmation_alone_does_not_save_what_needs_none(
    made: made_up.MadeUp,
) -> None:
    found = save(made, tax_rate=100, confirm={"members": 9, "bookings": 9})
    assert found["tax_rate"] == 100


def test_the_numbers_are_what_the_next_run_then_erases(made: made_up.MadeUp) -> None:
    """The preview and the job read one selection: save as confirmed, run the
    job, and it erases exactly the members and bookings that were shown."""
    keep(made, 60, 60)
    old_members(made, [6, 14, 20, 40, 59])
    old_booking(made, 5)
    old_booking(made, 20)
    old_booking(made, 40)
    old_booking(made, 50)
    shown = refused(
        made,
        "confirm-erasure",
        member_retention_months=18,
        booking_retention_months=24,
    )["erase"]
    assert shown == {"members": 3, "bookings": 2}
    save(
        made,
        member_retention_months=18,
        booking_retention_months=24,
        confirm=shown,
    )
    done = retention.purge_organization(current(made))
    assert (done["members"], done["bookings"]) == (3, 2)
    assert retention.erasure_preview(current(made), 18, 24) == {
        "members": 0,
        "bookings": 0,
    }


def test_the_preview_writes_nothing_and_matches_the_job_at_any_moment(
    made: made_up.MadeUp,
) -> None:
    old_members(made, [6, 14, 20])
    old_booking(made, 30)
    org = current(made)
    for months, now in ((12, None), (6, None), (36, datetime(2040, 1, 1, tzinfo=UTC))):
        before = kept(made)
        shown = retention.erasure_preview(org, months, months, now=now)
        assert kept(made) == before
        assert shown["members"] == len(
            retention.members_due(org, months, now or datetime.now(UTC))
        )


# --- The booking limits apply to new bookings only --------------------------------


@pytest.fixture
def example(request: pytest.FixtureRequest) -> Example:
    found: Example = request.getfixturevalue("limits_made")
    return found


def administrator_of(example: Example) -> Member:
    role = Role.objects.create(
        organization=example.org, name="Tester", admin_access=True
    )
    RoleCategory.objects.create(
        organization=example.org, role=role, booking_category=example.category
    )
    return Member.objects.create(
        organization=example.org,
        role=role,
        full_name="Test Admin",
        first_name="Test",
        last_name="Admin",
        email="tester@example.com",
        phone="+4790000099",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def edit(example: Example, who: Member, **fields: Any) -> None:
    version = organization_admin.version_of(Organization.objects.get(pk=example.org.pk))
    organization_admin.change(who, {"version": version, **fields})


def rows_of(example: Example) -> list[Any]:
    return [
        list(
            model._base_manager.filter(organization=example.org).order_by("pk").values()
        )
        for model in (Order, Booking, TakenTime)
    ]


def test_new_limits_never_change_or_refuse_an_order_already_made(
    example: Example,
) -> None:
    made = example
    placed = order(made, 0, 1, 2, 3)
    before = rows_of(made)
    assert Booking.objects.of(made.org).count() == 4
    admin = administrator_of(made)
    # Both limits set tighter than what the member already has.
    edit(made, admin, max_bookings_a_day=1, book_ahead_months=1)
    organization = Organization.objects.get(pk=made.org.pk)
    assert (organization.max_bookings_a_day, organization.book_ahead_months) == (1, 1)
    # The order is as it was: its rows, its states, its hold.
    assert rows_of(made) == before
    placed.refresh_from_db()
    assert placed.bookings.count() == 4
    assert {b.taken_time.state for b in placed.bookings.all()} == {BookingState.HELD}
    # The limit counts it, so a new order is refused, and nothing is added.
    with pytest.raises(OrderOverDayLimitError) as refused_order:
        order(made, 5)
    assert refused_order.value.left == 0
    assert rows_of(made) == before


def test_removing_the_limits_lets_new_orders_in_and_leaves_old_ones(
    example: Example,
) -> None:
    made = example
    limits(made, day=1, ahead=1)
    order(made, 0)
    before = rows_of(made)
    with pytest.raises(OrderOverDayLimitError):
        order(made, 1)
    edit(made, administrator_of(made), max_bookings_a_day=None, book_ahead_months=None)
    assert rows_of(made) == before
    assert order(made, 1).bookings.count() == 1
    assert create_order(
        member=made.member,
        requests=asked(made, 2),
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )


def test_a_limit_raised_lets_the_next_order_in(example: Example) -> None:
    made = example
    limits(made, day=1)
    order(made, 0)
    with pytest.raises(OrderOverDayLimitError):
        order(made, 1)
    edit(made, administrator_of(made), max_bookings_a_day=2)
    assert order(made, 1).bookings.count() == 1
    with pytest.raises(OrderOverDayLimitError):
        order(made, 2)


# --- Export and reload ------------------------------------------------------------


def fjordside_with_admin() -> tuple[Organization, Member]:
    org = setups.load(setup_file.read(SETUPS / "fjordside.toml"), today=TODAY)
    admin = Member.objects.create(
        organization=org,
        role=Role.objects.of(org).get(name="Admin"),
        full_name="Sam Field",
        first_name="Sam",
        last_name="Field",
        email="sam@example.com",
        phone="+4790000002",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )
    return org, admin


def reloaded(
    org: Organization, subdomain: str
) -> tuple[setup_file.Setup, Organization]:
    exported = setups.export(org)
    parsed = setup_file.parse(setup_file.write(exported))
    assert parsed.organization == exported.organization
    second = setups.load(
        replace(parsed, organization=replace(parsed.organization, subdomain=subdomain)),
        today=TODAY,
    )
    return exported, second


def test_the_export_after_edits_reloads_to_the_same_details() -> None:
    org, admin = fjordside_with_admin()
    guests = BookingCategory.objects.of(org).get(name="Work")
    role = Role.objects.of(org).get(name="Standard")
    setup_admin.make_role(admin, {"name": "Gäst", "categories": [guests.pk]})
    gast = Role.objects.of(org).get(name="Gäst")
    assert role.pk != gast.pk

    def edit_org(**fields: Any) -> None:
        version = organization_admin.version_of(Organization.objects.get(pk=org.pk))
        organization_admin.change(admin, {"version": version, **fields})

    edit_org(
        name='Brf "Quote" \\ Étoile ☃',
        contact_email="post+bookings@example.org",
        standard_role=gast.pk,
        max_bookings_a_day=2,
        book_ahead_months=None,
        tax_rate=2500,
        member_retention_months=24,
        booking_retention_months=60,
        time_zone="Europe/Stockholm",
        currency="EUR",
    )
    org.refresh_from_db()
    exported, second = reloaded(org, "second")
    one, two = exported.organization, setups.export(second).organization
    assert replace(two, subdomain=one.subdomain) == one
    assert one.name == 'Brf "Quote" \\ Étoile ☃'
    assert one.standard_role == "Gäst" and one.tax_rate == 2500
    assert one.max_bookings_a_day == 2 and one.book_ahead_months is None
    assert (one.time_zone, one.currency) == ("Europe/Stockholm", "EUR")
    assert (one.member_retention_months, one.booking_retention_months) == (24, 60)
    for field in organization_admin.FIELDS:
        if field != "standard_role":
            assert getattr(second, field) == getattr(org, field), field
    assert second.standard_role is not None and second.standard_role.name == "Gäst"


def test_a_tax_rate_of_none_is_left_out_of_the_file_and_reads_back() -> None:
    org, _ = fjordside_with_admin()
    Organization.objects.filter(pk=org.pk).update(tax_rate=0)
    text = setup_file.write(setups.export(org))
    assert "tax_rate" not in text
    assert setup_file.parse(text).organization.tax_rate == 0
    Organization.objects.filter(pk=org.pk).update(tax_rate=1250)
    text = setup_file.write(setups.export(org))
    assert "tax_rate = 1250" in text
    assert setup_file.parse(text).organization.tax_rate == 1250


@pytest.mark.parametrize("bad", ["-1", "10001", '"25"', "2.5", "true"])
def test_the_setup_file_refuses_a_tax_rate_the_screen_refuses(bad: str) -> None:
    org, _ = fjordside_with_admin()
    text = setup_file.write(setups.export(org)).replace(
        "[organization]", f"[organization]\ntax_rate = {bad}", 1
    )
    with pytest.raises(setup_file.SetupError, match="tax_rate"):
        setup_file.parse(text)


def test_the_screens_values_and_the_setup_file_refuse_the_same_texts(
    made: made_up.MadeUp,
) -> None:
    """Whatever the screen accepts, the setup file accepts: a saved value is
    never one the export cannot read back."""
    for field, value in (
        ("contact_email", "desk@example.org"),
        ("name", "Ä Ö ø"),
        ("time_zone", "Europe/Oslo"),
        ("currency", "DKK"),
    ):
        save(made, **{field: value})
    exported = setups.export(current(made))
    assert setup_file.parse(setup_file.write(exported)).organization == (
        exported.organization
    )


# --- The contact address and the days of free cancellation  -------

PLAIN_ADDRESSES = [
    "desk@example.org",
    "post+bookings@sub.example.org",
    "a.b@c-d.example.no",
]


@pytest.mark.parametrize("address", PLAIN_ADDRESSES)
def test_a_plain_address_is_saved_audited_and_shown(
    made: made_up.MadeUp, address: str
) -> None:
    found = save(made, contact_email=address)
    assert found["contact_email"] == address
    assert current(made).contact_email == address
    assert len(events(made)) == 1


# Each shape, and the words it is refused in.
BAD_ADDRESSES = [
    ("Board <board@example.org>", "without a name"),
    ('"Board" <board@example.org>', "without a name"),
    ("<board@example.org>", "without a name"),
    ("a@example.org, b@example.org", "not a list"),
    ("a@example.org; b@example.org", "not a list"),
    ("a@example.org b@example.org", "not a list"),
    ("a@example.org,", "not a list"),
    ("a@example.org\n", "one line"),
    ("a@example.org\r\nBcc: x@example.org", "one line"),
    ("a@example.org\nb@example.org", "one line"),
    ("\na@example.org", "one line"),
    ("a@example.org\u2028", "one line"),
    ("a@example.org\x00", "one line"),
    ("(comment)a@example.org", "an email address"),
    ("a@example.org (Board)", "not a list"),
    ('"a b"@example.org', "without a name"),
    ("a@localhost", "an email address"),
    ("a..b@example.org", "an email address"),
    ("@example.org", "an email address"),
    ("a@", "an email address"),
    ("", "empty"),
    ("   ", "empty"),
    ("a@" + "b" * 250 + ".org", "an email address"),
]


@pytest.mark.parametrize(("address", "words"), BAD_ADDRESSES)
def test_a_contact_address_that_is_not_one_plain_address_is_refused_in_words(
    made: made_up.MadeUp, address: str, words: str
) -> None:
    before = kept(made)
    found = refused(made, "invalid-contact-email", contact_email=address)
    assert words in found["reason"]
    assert kept(made) == before
    assert events(made) == []


@pytest.mark.parametrize("days", [0, 1, 5, 365])
def test_days_from_0_to_365_are_saved_audited_and_shown(
    made: made_up.MadeUp, days: int
) -> None:
    Organization.objects.filter(pk=made.organization.pk).update(
        free_cancellation_days=9
    )
    found = save(made, free_cancellation_days=days)
    assert found["free_cancellation_days"] == days
    assert sheet(made)["free_cancellation_days"] == days
    assert current(made).free_cancellation_days == days
    assert len(events(made)) == 1


@pytest.mark.parametrize("days", [-1, 366, 1.5, 5.0, "5", "", "five", True, None, [5]])
def test_days_that_are_not_a_whole_number_from_0_to_365_are_refused_in_words(
    made: made_up.MadeUp, days: Any
) -> None:
    before = kept(made)
    found = refused(made, "invalid-free-cancellation-days", free_cancellation_days=days)
    assert isinstance(found["reason"], str) and found["reason"]
    assert kept(made) == before
    assert events(made) == []


def test_both_changes_are_audited_and_the_text_of_new_orders_shows_the_days(
    made: made_up.MadeUp,
) -> None:
    before = booking_conditions.current(current(made))
    assert before.days == 5
    save(made, contact_email="desk@example.org")
    save(made, free_cancellation_days=9)
    assert len(events(made)) == 2
    now = booking_conditions.current(current(made))
    assert (now.days, now.contact_email) == (9, "desk@example.org")
    words = " ".join(text for _, text in now.sections)
    assert "at least 9 days" in words and "at least 5 days" not in words
    # Both in one save are one event, as the page's other fields are.
    save(made, contact_email="board@example.org", free_cancellation_days=3)
    assert len(events(made)) == 3
    assert (current(made).contact_email, current(made).free_cancellation_days) == (
        "board@example.org",
        3,
    )


def test_the_export_after_the_new_fields_reads_back_the_same(
    made: made_up.MadeUp,
) -> None:
    for days in (0, 365):
        save(made, contact_email="desk@example.org", free_cancellation_days=days)
        exported = setups.export(current(made))
        assert exported.organization.free_cancellation_days == days
        assert setup_file.parse(setup_file.write(exported)).organization == (
            exported.organization
        )
