# SPDX-License-Identifier: AGPL-3.0-only
"""The Containers : the gate, CSRF and origin, malformed requests,
every refusal with the model's words (a container's own repeating periods and
week wrap, the availability cover, shared containers on other floor plans, the
fields that stay while a booking is still to come, a booking category with a
booking still to come), add / change / enable / disable / categories and their
own hours / remove, the audit events, concurrent edits, the restricted
application login, an export that reloads after edits, and Book's calendar after
an edit."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client

from randirandi import setup_file
from randirandi.core import admin_container_views as views
from randirandi.core import container_admin, setup_admin
from randirandi.core import setup as setups
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_calendar import member_calendar
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AuditEvent,
    AvailabilityCalendar,
    AvailableSpan,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    CategoryHoursSpan,
    ConfigurationCategory,
    Member,
    MemberStatus,
    Organization,
    Origin,
    PriceBookEntry,
    Role,
    RoleCategory,
    Weekday,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import answer as _answer
from tests.test_admin_blocks import request_to
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_booking_choices import cover_twelve_hours
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
)
from tests.test_second_factor import clocks, token  # noqa: F401
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = date(2027, 3, 1)
# After the made-up booking (10:00 to 12:00 on 1 March 2027) and block.
AFTER = datetime(2027, 3, 2, 12, 0, tzinfo=UTC)
MONDAY = "2027-03-08"
SUNDAY_AFTER = "2027-03-14"
MON, TUE, FRI, SAT, SUN = 0, 1, 4, 5, 6


@pytest.fixture
def made() -> made_up.MadeUp:
    """The made-up organization: one exclusive container with a booking and a
    block still to come (the real clock is before March 2027) and a price."""
    return made_up.everything()


def call(
    made: made_up.MadeUp,
    which: str,
    data: Any = None,
    *,
    pk: int = 0,
    who: Member | None = None,
    confirmed: bool = True,
    seen: list[dict[str, Any]] | None = None,
) -> HttpResponse:
    """A view called directly, by default as a confirmed administrator. A
    categories body without "seen" gets the container's categories as they are
    now (a sheet that is up to date); pass seen to say what a sheet saw."""
    if (
        which == "categories"
        and isinstance(data, dict)
        and "categories" in data
        and "seen" not in data
    ):
        data = {
            **data,
            "seen": seen
            if seen is not None
            else made_up.seen_categories(made.organization, pk),
        }
    request = request_to(
        who or made.administrator,
        "get" if which == "data" else "post",
        f"/admin/containers/{which}/",
        data,
        confirmed=confirmed,
    )
    match which:
        case "data":
            return views.containers_data(request)
        case "make":
            return views.container_make(request)
        case "change":
            return views.container_change(request, pk)
        case "categories":
            return views.container_categories(request, pk)
        case "remove":
            return views.container_remove(request, pk)
    raise AssertionError(which)


def answer(response: HttpResponse, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.content
    return _answer(response, status)


def refused(response: HttpResponse, code: str, status: int = 409) -> dict[str, Any]:
    found = answer(response, status)
    assert found["error"] == code
    return found


def reasons(found: dict[str, Any]) -> str:
    return " ".join(r["text"] for r in found["reasons"])


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.of(made.organization).filter(action=action).order_by("pk")
    )


def counted(made: made_up.MadeUp) -> int:
    return AuditEvent.objects.of(made.organization).count()


def row_of(found: dict[str, Any], pk: int) -> dict[str, Any]:
    return next(r for r in found["containers"] if r["id"] == pk)


def fresh(made: made_up.MadeUp, **more: Any) -> int:
    """A new container on the made-up asset, made as the screens make it."""
    body = {
        "asset": made.asset.pk,
        "booking_type": made.booking_type.pk,
        "period_type": "flexible",
        "uses": None,
        **more,
    }
    return container_admin.make_container(made.administrator, body)


def monday_only(made: made_up.MadeUp, pk: int, **more: Any) -> None:
    """Fit a new container into the asset's Monday hours (8:00 to 23:00)."""
    container_admin.change_container(
        made.administrator, pk, {"weekdays": [MON], **more}, now=AFTER
    )


def second_admin(org: Organization) -> Member:
    role = Role.objects.create(organization=org, name="Tester", admin_access=True)
    category = BookingCategory.objects.of(org).order_by("position").first()
    assert category is not None
    RoleCategory.objects.create(organization=org, role=role, booking_category=category)
    return Member.objects.create(
        organization=org,
        role=role,
        full_name="Test Admin",
        first_name="Test",
        last_name="Admin",
        email="tester@example.com",
        phone="+4790000099",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def open_all_week(asset: Asset, day: date = made_up.A_DAY) -> None:
    calendar = AvailabilityCalendar.objects.create(
        organization_id=asset.organization_id, asset=asset, start_date=day
    )
    for weekday in Weekday.values:
        AvailableSpan.objects.create(
            organization_id=asset.organization_id,
            availability_calendar=calendar,
            weekday=weekday,
            start_time=0,
            end_time=24 * 60,
        )


# --- The gate, CSRF and origin ---------------------------------------------------

EVERY_CALL = [
    ("data", {}),
    ("make", {"asset": 1, "booking_type": 1, "period_type": "fixed"}),
    ("change", {"status": "disabled"}),
    ("categories", {"categories": []}),
    ("remove", {}),
]


@pytest.mark.parametrize(("which", "data"), EVERY_CALL)
def test_only_a_confirmed_administrator_may(
    made: made_up.MadeUp, which: str, data: dict[str, Any]
) -> None:
    before = (
        BookingConfiguration.objects.of(made.organization).count(),
        counted(made),
    )
    for who, confirmed in ((made.administrator, False), (made.member, True)):
        with pytest.raises(PermissionDenied):
            call(
                made,
                which,
                data,
                pk=made.configuration.pk,
                who=who,
                confirmed=confirmed,
            )
    assert before == (
        BookingConfiguration.objects.of(made.organization).count(),
        counted(made),
    )


def test_another_organizations_administrator_is_a_stranger(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    with pytest.raises(PermissionDenied):
        call(
            made,
            "change",
            {"status": "disabled"},
            pk=made.configuration.pk,
            who=elsewhere.administrator,
        )
    assert (
        BookingConfiguration.objects.of(made.organization)
        .get(pk=made.configuration.pk)
        .status
        == "enabled"
    )


def test_ids_of_another_organization_are_a_404_or_refused(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    other = elsewhere.configuration.pk
    refused(call(made, "change", {"status": "disabled"}, pk=other), "not-found", 404)
    refused(call(made, "categories", {"categories": []}, pk=other), "not-found", 404)
    refused(call(made, "remove", pk=other), "not-found", 404)
    refused(call(made, "remove", pk=2**63), "not-found", 404)
    own = fresh(made)
    # Another organization's asset, booking type, price book and floor plan.
    for field, value in (
        ("asset", elsewhere.asset.pk),
        ("booking_type", elsewhere.booking_type.pk),
        ("price_book", elsewhere.price_book.pk),
        ("uses", made_up.floor_plan_of(elsewhere.asset).pk),
    ):
        found = refused(call(made, "change", {field: value}, pk=own), "unknown")
        assert found["reasons"][0]["field"] in {
            "asset",
            "booking_type",
            "price_book",
            "floor_plan",
        }
    refused(
        call(
            made,
            "make",
            {
                "asset": elsewhere.asset.pk,
                "booking_type": made.booking_type.pk,
                "period_type": "fixed",
            },
        ),
        "unknown",
    )
    assert BookingConfiguration.objects.of(elsewhere.organization).count() == 1


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    pk = made.configuration.pk
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, method in (
            ("/admin/containers/data/", "post"),
            ("/admin/containers/make/", "get"),
            (f"/admin/containers/{pk}/change/", "get"),
            (f"/admin/containers/{pk}/categories/", "get"),
            (f"/admin/containers/{pk}/remove/", "get"),
        ):
            response = getattr(client, method)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
    for path, method in (
        ("/admin/containers/data/", "get"),
        ("/admin/containers/make/", "post"),
        (f"/admin/containers/{pk}/change/", "post"),
    ):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    new = {
        "asset": made.asset.pk,
        "booking_type": made.booking_type.pk,
        "period_type": "fixed",
    }
    with application_login():
        sign_in_administrator(client, made.administrator)
        csrf = str(config(get(client, "/admin/containers/").content.decode())["csrf"])

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
        no_token = {"HTTP_ORIGIN": "https://" + HOST}
        pk = made.configuration.pk
        for path, body in (
            ("/admin/containers/make/", new),
            (f"/admin/containers/{pk}/change/", {"status": "disabled"}),
            (f"/admin/containers/{pk}/categories/", {"categories": []}),
            (f"/admin/containers/{pk}/remove/", {}),
        ):
            assert send(path, body, no_token).status_code == 403, path
            assert send(path, body, wrong).status_code == 403, path
        assert BookingConfiguration.objects.of(made.organization).count() == 1
        found = answer(send("/admin/containers/make/", new, right))
        assert found["made"] == (
            BookingConfiguration.objects.of(made.organization).latest("number").pk
        )
    assert BookingConfiguration.objects.of(made.organization).count() == 2


def test_the_place_opens_for_an_administrator(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        ui = config(get(client, "/admin/containers/").content.decode())
    assert ui["place"] == "containers"


@pytest.mark.parametrize(
    ("which", "body"),
    [
        ("make", b"not json"),
        ("make", b"[]"),
        ("make", {}),
        ("make", {"asset": 1, "booking_type": 1}),
        ("make", {"asset": "1", "booking_type": 1, "period_type": "fixed"}),
        ("make", {"asset": True, "booking_type": 1, "period_type": "fixed"}),
        ("make", {"asset": 1, "booking_type": 1, "period_type": 3}),
        (
            "make",
            {"asset": 1, "booking_type": 1, "period_type": "fixed", "status": "x"},
        ),
        ("make", {"asset": 1, "booking_type": 1, "period_type": "monthly"}),
        ("change", {}),
        ("change", {"nothing": 1}),
        ("change", {"period_start": "10:00"}),
        ("change", {"period_start": True}),
        ("change", {"period_start": 1.5}),
        ("change", {"status": 1}),
        ("change", {"weekdays": 1}),
        ("change", {"weekdays": ["mon"]}),
        ("change", {"weekdays": [True]}),
        ("change", {"first_week": 5}),
        ("change", {"first_week": "March"}),
        ("change", {"last_week": "9999-12-31"}),
        ("change", {"asset": None}),
        ("change", {"uses": "whole"}),
        ("change", {"price_book": "1"}),
        ("categories", {}),
        ("categories", {"categories": 1}),
        ("categories", {"categories": [1]}),
        ("categories", {"categories": [{"hours": []}]}),
        ("categories", {"categories": [{"category": "1"}]}),
        ("categories", {"categories": [{"category": 1}, {"category": 1}]}),
        ("categories", {"categories": [{"category": 1, "hours": 3}]}),
        ("categories", {"categories": [{"category": 1, "hours": [{"weekday": 0}]}]}),
        (
            "categories",
            {
                "categories": [
                    {
                        "category": 1,
                        "hours": [{"weekday": 0, "start": "8", "end": 9}],
                    }
                ]
            },
        ),
        ("categories", {"categories": [{"category": 1, "x": 1}]}),
        ("categories", {"categories": [], "extra": 1}),
    ],
)
def test_a_malformed_body_is_a_400_and_changes_nothing(
    made: made_up.MadeUp, which: str, body: Any
) -> None:
    pk = fresh(made)
    before = (counted(made), container_admin.containers_place(made.organization))
    refused(call(made, which, body, pk=pk), "bad-request", 400)
    assert before == (
        counted(made),
        container_admin.containers_place(made.organization),
    )


# --- What the place shows -----------------------------------------------------------


def test_the_place_shows_each_container_with_what_locks_it(
    made: made_up.MadeUp,
) -> None:
    found = answer(call(made, "data"))
    assert set(found) == {
        "containers",
        "assets",
        "booking_types",
        "booking_categories",
        "price_books",
    }
    row = row_of(found, made.configuration.pk)
    assert row["number"] == made.configuration.number
    assert row["asset"] == made.asset.pk
    assert (row["period_start"], row["period_end"], row["end_day"]) == (600, 1320, 0)
    assert row["weekdays"] == [MON]
    assert (row["interval"], row["minimum_length"], row["maximum_length"]) == (
        60,
        60,
        720,
    )
    assert (row["first_week"], row["last_week"], row["every_weeks"]) == (None, None, 1)
    assert row["status"] == "enabled"
    assert row["uses"] is None
    assert row["price_book"] == made.price_book.pk
    assert row["categories"] == [
        {"id": made.private.pk, "hours": [], "priced": True, "to_come": 1}
    ]
    assert row["to_come"] == 1
    assert row["locked"] is True
    assert row["removable"] is False
    assert row["last_to_come"] == {
        "order": made.order.number,
        "reference": made.order.reference,
        "date": "2027-03-01",
    }
    (asset,) = found["assets"]
    assert asset["floor_plans"][0]["name"] == "Floor plan 1"
    assert found["price_books"] == [{"id": made.price_book.pk, "name": "Prices 2027"}]
    # Once the booking is in the past, the container is not locked.
    later = container_admin.containers_place(made.organization, now=AFTER)
    assert row_of(later, made.configuration.pk)["locked"] is False
    assert row_of(later, made.configuration.pk)["last_to_come"] is None


def test_the_weeks_come_as_mondays(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    monday_only(made, pk, first_week=MONDAY, last_week="2027-03-22")
    row = row_of(container_admin.containers_place(made.organization), pk)
    assert (row["first_week"], row["last_week"]) == (MONDAY, "2027-03-22")
    stored = BookingConfiguration.objects.of(made.organization).get(pk=pk)
    assert stored.stop_date == date(2027, 3, 28)


# --- Add ----------------------------------------------------------------------------


def test_a_new_container_starts_disabled_with_every_category_and_one_event(
    made: made_up.MadeUp,
) -> None:
    guest = BookingCategory.objects.create(organization=made.organization, name="guest")
    before = counted(made)
    found = answer(
        call(
            made,
            "make",
            {
                "asset": made.asset.pk,
                "booking_type": made.booking_type.pk,
                "period_type": "fixed",
            },
        )
    )
    row = row_of(found, found["made"])
    assert row["number"] == made.configuration.number + 1
    assert row["status"] == "disabled"
    assert row["period_type"] == "fixed"
    assert (row["period_start"], row["period_end"]) == (600, 1320)
    assert row["weekdays"] == list(range(7))
    # A fixed container is booked whole: the system sets the lengths.
    assert (row["interval"], row["minimum_length"], row["maximum_length"]) == (
        720,
        720,
        720,
    )
    assert [c["id"] for c in row["categories"]] == [made.private.pk, guest.pk]
    assert row["price_book"] is None
    assert row["locked"] is False
    assert row["removable"] is True
    assert counted(made) == before + 1
    (event,) = events(made, AuditAction.MADE_BOOKING_CONFIGURATION)
    assert (event.object_kind, event.object_id) == (
        "booking_configuration",
        row["id"],
    )
    assert event.administrator == made.administrator


def test_a_flexible_one_is_by_the_hour_up_to_three(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    row = row_of(container_admin.containers_place(made.organization), pk)
    assert (row["interval"], row["minimum_length"], row["maximum_length"]) == (
        60,
        60,
        180,
    )


def test_a_shared_one_names_a_floor_plan_of_its_asset(made: made_up.MadeUp) -> None:
    plan = made_up.floor_plan_of(made.asset)
    pk = fresh(made, uses=plan.pk)
    row = row_of(container_admin.containers_place(made.organization), pk)
    assert row["uses"] == plan.pk
    stored = BookingConfiguration.objects.of(made.organization).get(pk=pk)
    assert stored.consumption_mode == "shared"


def test_a_floor_plan_of_another_asset_is_refused_with_the_validators_words(
    made: made_up.MadeUp,
) -> None:
    other = made.shared("Other room", 2)
    found = refused(
        call(
            made,
            "make",
            {
                "asset": made.asset.pk,
                "booking_type": made.booking_type.pk,
                "period_type": "fixed",
                "uses": other.floor_plan_id,
            },
        ),
        "refused",
    )
    assert "one of the asset's floor plans" in reasons(found)
    assert found["reasons"][0]["field"] == "floor_plan"
    assert BookingConfiguration.objects.of(made.organization).count() == 2


def test_no_booking_category_means_no_container() -> None:
    org = made_up.organization("bare")
    asset = Asset.objects.create(organization=org, name="Room")
    kind = BookingType.objects.create(organization=org, name="Hire")
    admin = second_admin_without_categories(org)
    with pytest.raises(setup_admin.SetupRefusedError) as refusal:
        container_admin.make_container(
            admin,
            {"asset": asset.pk, "booking_type": kind.pk, "period_type": "fixed"},
        )
    assert refusal.value.code == "no-category"
    assert not BookingConfiguration.objects.of(org).exists()


def second_admin_without_categories(org: Organization) -> Member:
    role = Role.objects.create(organization=org, name="Tester", admin_access=True)
    return Member.objects.create(
        organization=org,
        role=role,
        full_name="Test Admin",
        first_name="Test",
        last_name="Admin",
        email="tester@example.com",
        phone="+4790000099",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


# --- Change: the details, the time, the weeks, payment, status ---------------------


def test_a_change_writes_one_event_and_a_change_that_changes_nothing_none(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    before = counted(made)
    other = BookingType.objects.create(organization=made.organization, name="Party")
    found = answer(call(made, "change", {"booking_type": other.pk}, pk=pk))
    assert row_of(found, pk)["booking_type"] == other.pk
    assert counted(made) == before + 1
    assert [
        e.object_id for e in events(made, AuditAction.CHANGED_BOOKING_CONFIGURATION)
    ] == [pk]
    # The same again: nothing written, nothing checked.
    answer(call(made, "change", {"booking_type": other.pk}, pk=pk))
    answer(call(made, "change", {"weekdays": list(range(7))}, pk=pk))
    answer(call(made, "change", {"first_week": None, "uses": None}, pk=pk))
    assert counted(made) == before + 1


def test_the_time_sheet(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    found = answer(
        call(
            made,
            "change",
            {
                "period_type": "flexible",
                "period_start": 9 * 60,
                "period_end": 18 * 60,
                "weekdays": [MON, TUE],
                "interval": 30,
                "minimum_length": 60,
                "maximum_length": 180,
                "first_week": MONDAY,
                "last_week": SUNDAY_AFTER[:8] + "15",
                "every_weeks": 2,
            },
            pk=pk,
        )
    )
    row = row_of(found, pk)
    assert (row["period_start"], row["period_end"]) == (540, 1080)
    assert row["weekdays"] == [MON, TUE]
    assert (row["interval"], row["minimum_length"], row["maximum_length"]) == (
        30,
        60,
        180,
    )
    assert (row["first_week"], row["last_week"], row["every_weeks"]) == (
        MONDAY,
        "2027-03-15",
        2,
    )


def test_a_period_across_midnight_and_a_fixed_period(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    found = answer(
        call(
            made,
            "change",
            {
                "period_type": "fixed",
                "period_start": 22 * 60,
                "period_end": 10 * 60,
                "end_day": 1,
                "weekdays": [SUN, MON],
            },
            pk=pk,
        )
    )
    row = row_of(found, pk)
    assert (row["interval"], row["minimum_length"], row["maximum_length"]) == (
        720,
        720,
        720,
    )


@pytest.mark.parametrize(
    ("fields", "field", "words"),
    [
        ({"period_start": 12 * 60, "period_end": 12 * 60}, "period_end", "ends after"),
        (
            {"period_end": 23 * 60 + 59, "interval": 7},
            "interval",
            "whole number of intervals",
        ),
        ({"maximum_length": 13 * 60}, "maximum_length", "at most the period's length"),
        ({"minimum_length": 4 * 60}, "minimum_length", "at most the maximum"),
        ({"interval": 0}, "interval", ""),
        ({"weekdays": []}, "period_start_weekdays", ""),
        ({"weekdays": [7]}, "period_start_weekdays", ""),
        ({"every_weeks": 2}, "start_date", "needs one"),
        ({"every_weeks": 53, "first_week": MONDAY}, "every_weeks", "1 to 52"),
        ({"first_week": "2027-03-09"}, "start_date", "Monday"),
        (
            {"first_week": "2027-03-15", "last_week": MONDAY},
            "stop_date",
            "not before the first",
        ),
        ({"period_end": 24 * 60}, "", ""),
        ({"period_start": 24 * 60}, "", ""),
        ({"end_day": 40000}, "", ""),
        ({"status": "paused"}, "status", ""),
        ({"period_type": "monthly"}, "period_type", ""),
    ],
)
def test_the_models_own_checks_refuse_with_its_words(
    made: made_up.MadeUp, fields: dict[str, Any], field: str, words: str
) -> None:
    pk = fresh(made)
    before = (counted(made), container_admin.containers_place(made.organization))
    found = refused(call(made, "change", fields, pk=pk), "refused")
    assert found["reasons"], fields
    if field:
        assert field in {r["field"] for r in found["reasons"]}, found
    assert words in reasons(found)
    # Refused, nothing changed, and no event.
    assert before == (
        counted(made),
        container_admin.containers_place(made.organization),
    )


def test_a_containers_own_repeating_periods_may_not_overlap_also_across_the_week(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    # 36 hours every Friday and Saturday: Friday's runs into Saturday's.
    found = refused(
        call(
            made,
            "change",
            {
                "period_type": "fixed",
                "period_end": 22 * 60,
                "end_day": 1,
                "weekdays": [FRI, SAT],
            },
            pk=pk,
        ),
        "refused",
    )
    assert "longer than the time to the next weekday" in reasons(found)
    # Sunday's runs past Monday's start, round the end of the week.
    found = refused(
        call(
            made,
            "change",
            {
                "period_type": "fixed",
                "period_start": 22 * 60,
                "period_end": 23 * 60,
                "end_day": 1,
                "weekdays": [SUN, MON],
            },
            pk=pk,
        ),
        "refused",
    )
    assert "longer than the time to the next weekday" in reasons(found)
    # One Sunday evening to Monday morning every week is fine.
    answer(
        call(
            made,
            "change",
            {
                "period_type": "fixed",
                "period_start": 22 * 60,
                "period_end": 10 * 60,
                "end_day": 1,
                "weekdays": [SUN],
            },
            pk=pk,
        )
    )


def test_enabling_is_checked_against_the_assets_availability(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    # Every day 10:00 to 22:00, and the asset is open on Mondays only.
    found = refused(call(made, "change", {"status": "enabled"}, pk=pk), "refused")
    assert "is not covered by the asset's availability" in reasons(found)
    assert (
        BookingConfiguration.objects.of(made.organization).get(pk=pk).status
        == "disabled"
    )
    monday_only(made, pk)
    found = answer(call(made, "change", {"status": "enabled"}, pk=pk))
    assert row_of(found, pk)["status"] == "enabled"
    # Disabling is a save like any other and is allowed.
    found = answer(call(made, "change", {"status": "disabled"}, pk=pk))
    assert row_of(found, pk)["status"] == "disabled"
    assert [
        e.action
        for e in AuditEvent.objects.of(made.organization).order_by("pk")
        if e.object_id == pk
    ] == [
        AuditAction.MADE_BOOKING_CONFIGURATION,
        AuditAction.CHANGED_BOOKING_CONFIGURATION,
        AuditAction.CHANGED_BOOKING_CONFIGURATION,
        AuditAction.CHANGED_BOOKING_CONFIGURATION,
    ]


def test_a_period_the_asset_does_not_cover_is_refused_when_the_time_changes(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    monday_only(made, pk, status="enabled")
    found = refused(
        call(made, "change", {"period_end": 23 * 60 + 30, "interval": 30}, pk=pk),
        "refused",
    )
    assert "not covered by the asset's availability" in reasons(found)
    assert "from 23:00" in reasons(found)


def test_containers_stack_on_one_asset(made: made_up.MadeUp) -> None:
    """Exclusive containers over the same time are fine : the
    database keeps their bookings apart."""
    one, two = fresh(made), fresh(made, period_type="fixed")
    monday_only(made, one, status="enabled")
    monday_only(made, two, status="enabled")
    found = container_admin.containers_place(made.organization)
    assert [row_of(found, pk)["status"] for pk in (one, two)] == ["enabled"] * 2
    # Also over the made-up container, which is enabled with a booking.
    assert row_of(found, made.configuration.pk)["status"] == "enabled"


def two_plans(made: made_up.MadeUp) -> tuple[int, int, int]:
    """An asset open all week with two floor plans; two shared containers on
    them, disabled, over the same time."""
    asset = Asset.objects.create(organization=made.organization, name="Hall")
    open_all_week(asset)
    first = made_up.floor_plan_of(asset)
    second = AssetInventory.objects.create(
        organization=made.organization, asset=asset, capacity=2, unit="table"
    )
    body = {
        "asset": asset.pk,
        "booking_type": made.booking_type.pk,
        "period_type": "flexible",
    }
    one = container_admin.make_container(made.administrator, {**body, "uses": first.pk})
    two = container_admin.make_container(
        made.administrator, {**body, "uses": second.pk}
    )
    return asset.pk, one, two


def test_shared_containers_on_other_floor_plans_may_not_cover_the_same_time(
    made: made_up.MadeUp,
) -> None:
    _, one, two = two_plans(made)
    answer(call(made, "change", {"status": "enabled"}, pk=one))
    found = refused(call(made, "change", {"status": "enabled"}, pk=two), "refused")
    assert "covers the same time as container" in reasons(found)
    assert "different floor plans" in reasons(found)
    # Moving the other one's time clear of it is allowed, then both are.
    answer(call(made, "change", {"weekdays": [MON]}, pk=two))
    refused(call(made, "change", {"status": "enabled"}, pk=two), "refused")
    answer(call(made, "change", {"weekdays": [TUE, FRI]}, pk=two))
    one_days = answer(call(made, "change", {"weekdays": [MON, SAT]}, pk=one))
    assert row_of(one_days, one)["weekdays"] == [MON, SAT]
    found = answer(call(made, "change", {"status": "enabled"}, pk=two))
    assert row_of(found, two)["status"] == "enabled"


def test_two_enabled_at_once_end_in_one_valid_state(made: made_up.MadeUp) -> None:
    _, one, two = two_plans(made)
    other = second_admin(made.organization)
    barrier = Barrier(2)

    def enable(who: Member, pk: int) -> str:
        barrier.wait()
        try:
            container_admin.change_container(who, pk, {"status": "enabled"})
        except container_admin.ContainerRefusedError as error:
            return error.reasons[0]["text"]
        finally:
            connection.close()
        return "enabled"

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(enable, made.administrator, one),
            pool.submit(enable, other, two),
        ]
        results = [future.result() for future in futures]
        states = sorted(
            BookingConfiguration.objects.of(made.organization)
            .filter(pk__in=[one, two])
            .values_list("status", flat=True)
        )
    assert sorted(r == "enabled" for r in results) == [False, True]
    assert states == ["disabled", "enabled"]
    assert any("covers the same time as container" in r for r in results)


def test_edits_of_one_container_at_once_both_land(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    other = second_admin(made.organization)
    party = BookingType.objects.create(organization=made.organization, name="Party")
    barrier = Barrier(2)

    def run(who: Member, fields: dict[str, Any]) -> None:
        barrier.wait()
        try:
            container_admin.change_container(who, pk, fields)
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [
            pool.submit(run, made.administrator, {"booking_type": party.pk}),
            pool.submit(run, other, {"weekdays": [MON, TUE]}),
        ]:
            future.result()
        row = BookingConfiguration.objects.of(made.organization).get(pk=pk)
    assert row.booking_type_id == party.pk
    assert row.period_start_weekdays == [MON, TUE]


def test_payment_names_a_price_book_and_dropping_it_needs_the_prices_gone(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    found = answer(call(made, "change", {"price_book": made.price_book.pk}, pk=pk))
    assert row_of(found, pk)["price_book"] == made.price_book.pk
    # The made-up container has a price: it keeps its book.
    found = refused(
        call(made, "change", {"price_book": None}, pk=made.configuration.pk), "refused"
    )
    assert "remove its prices first" in reasons(found)
    found = answer(call(made, "change", {"price_book": None}, pk=pk))
    assert row_of(found, pk)["price_book"] is None


# --- While a booking is still to come ----------------------------------------------


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"period_start": 11 * 60, "maximum_length": 600}, "period_start"),
        ({"period_end": 21 * 60, "maximum_length": 600}, "period_end"),
        ({"weekdays": [MON, TUE]}, "period_start_weekdays"),
        ({"interval": 30}, "interval"),
        ({"period_type": "fixed"}, "period_type"),
        ({"every_weeks": 2, "first_week": MONDAY}, "every_weeks"),
        ({"first_week": MONDAY}, "start_date"),
        ({"uses": "plan"}, "floor_plan"),
    ],
)
def test_while_a_booking_is_to_come_the_time_and_the_use_stay(
    made: made_up.MadeUp, fields: dict[str, Any], field: str
) -> None:
    if fields.get("uses") == "plan":
        fields = {"uses": made_up.floor_plan_of(made.asset).pk}
    found = refused(call(made, "change", fields, pk=made.configuration.pk), "refused")
    assert "while the container has a booking still to come" in reasons(found)
    assert field in {r["field"] for r in found["reasons"]}


def test_while_a_booking_is_to_come_the_asset_stays_and_the_rest_may_change(
    made: made_up.MadeUp,
) -> None:
    other = made.shared("Other room", 2)
    found = refused(
        call(made, "change", {"asset": other.asset_id}, pk=made.configuration.pk),
        "refused",
    )
    assert "asset" in {r["field"] for r in found["reasons"]}
    party = BookingType.objects.create(organization=made.organization, name="Party")
    # The booking type, the lengths and the status may change (rule 9).
    found = answer(
        call(
            made,
            "change",
            {"booking_type": party.pk, "maximum_length": 6 * 60},
            pk=made.configuration.pk,
        )
    )
    row = row_of(found, made.configuration.pk)
    assert (row["booking_type"], row["maximum_length"]) == (party.pk, 360)
    # The last week may move, but not before the booking's week.
    found = answer(
        call(made, "change", {"last_week": "2027-03-08"}, pk=made.configuration.pk)
    )
    refused(
        call(made, "change", {"last_week": "2026-12-28"}, pk=made.configuration.pk),
        "refused",
    )
    found = answer(
        call(made, "change", {"status": "disabled"}, pk=made.configuration.pk)
    )
    assert row_of(found, made.configuration.pk)["status"] == "disabled"


def test_once_the_booking_is_past_the_time_may_change(made: made_up.MadeUp) -> None:
    container_admin.change_container(
        made.administrator,
        made.configuration.pk,
        {"period_start": 11 * 60, "maximum_length": 600},
        now=AFTER,
    )
    stored = BookingConfiguration.objects.of(made.organization).get(
        pk=made.configuration.pk
    )
    assert stored.period_start == 11 * 60


# --- Booking categories and their own hours -----------------------------------------


def guest_category(made: made_up.MadeUp, name: str = "guest") -> BookingCategory:
    return BookingCategory.objects.create(organization=made.organization, name=name)


def test_a_category_with_a_booking_still_to_come_stays(made: made_up.MadeUp) -> None:
    guest = guest_category(made)
    pk = made.configuration.pk
    found = refused(
        call(
            made,
            "categories",
            {"categories": [{"category": guest.pk, "hours": []}]},
            pk=pk,
        ),
        "refused",
    )
    assert "private stays while the container has a booking still to come" in reasons(
        found
    )
    assert "Cancel those bookings first" in reasons(found)
    assert found["reasons"][0]["field"] == "booking_categories"
    # Nothing was changed.
    assert list(
        ConfigurationCategory.objects.of(made.organization)
        .filter(booking_configuration=pk)
        .values_list("booking_category", flat=True)
    ) == [made.private.pk]
    # Adding one beside it is fine.
    found = answer(
        call(
            made,
            "categories",
            {
                "categories": [
                    {"category": made.private.pk},
                    {"category": guest.pk, "hours": []},
                ]
            },
            pk=pk,
        )
    )
    assert [c["id"] for c in row_of(found, pk)["categories"]] == [
        made.private.pk,
        guest.pk,
    ]


def test_a_stale_sheet_does_not_remove_a_category_another_administrator_added(
    made: made_up.MadeUp,
) -> None:
    guest = guest_category(made)
    pk = made.configuration.pk
    sheet = made_up.seen_categories(made.organization, pk)
    before = [c["category"] for c in sheet]
    # Another administrator adds the guest category after this sheet was read.
    made_up.change_categories_as_seen(
        made.administrator,
        pk,
        {"categories": [{"category": c} for c in [*before, guest.pk]]},
    )
    stale = refused(
        call(
            made,
            "categories",
            {"categories": [{"category": c, "hours": []} for c in before]},
            pk=pk,
            seen=sheet,
        ),
        "refused",
    )
    assert "changed while this sheet was open" in stale["reasons"][0]["text"]
    kept = set(
        ConfigurationCategory.objects.of(made.organization)
        .filter(booking_configuration=pk)
        .values_list("booking_category", flat=True)
    )
    assert kept == {*before, guest.pk}
    # The same sheet read again saves.
    answer(
        call(
            made,
            "categories",
            {"categories": [{"category": c, "hours": []} for c in before]},
            pk=pk,
        )
    )
    assert before != [] and ConfigurationCategory.objects.of(made.organization).filter(
        booking_configuration=pk
    ).count() == len(before)


def test_a_stale_sheet_does_not_overwrite_hours_another_administrator_set(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    monday_only(made, pk)
    sheet = made_up.seen_categories(made.organization, pk)
    kept = [{"category": c["category"], "hours": []} for c in sheet]
    # Another administrator gives the first category its own hours.
    own = [{**kept[0], "hours": hours((MON, 10 * 60, 12 * 60))}, *kept[1:]]
    made_up.change_categories_as_seen(made.administrator, pk, {"categories": own})
    stale = refused(
        call(made, "categories", {"categories": kept}, pk=pk, seen=sheet), "refused"
    )
    assert (
        "hours of this container changed while this sheet"
        in stale["reasons"][0]["text"]
    )
    assert made_up.seen_categories(made.organization, pk)[0]["hours"] == hours(
        (MON, 10 * 60, 12 * 60)
    )
    # The service enforces it too, not only the view.
    with pytest.raises(container_admin.ContainerRefusedError):
        container_admin.change_categories(
            made.administrator, pk, {"categories": kept, "seen": sheet}
        )
    with pytest.raises(ValueError):
        container_admin.change_categories(made.administrator, pk, {"categories": kept})


def test_a_categories_body_must_say_what_the_sheet_saw(made: made_up.MadeUp) -> None:
    pk = made.configuration.pk
    body = {"categories": [{"category": made.private.pk}]}
    found = answer(
        views.container_categories(
            request_to(
                made.administrator,
                "post",
                f"/admin/containers/{pk}/categories/",
                body,
                confirmed=True,
            ),
            pk,
        ),
        400,
    )
    assert found["error"]
    answer(call(made, "categories", {**body, "seen": "all"}, pk=pk), 400)
    answer(call(made, "categories", {**body, "seen": [True]}, pk=pk), 400)
    answer(call(made, "categories", {**body, "seen": [{"category": 1}]}, pk=pk), 400)


def test_a_category_with_a_price_stays_when_no_booking_is_to_come(
    made: made_up.MadeUp,
) -> None:
    guest = guest_category(made)
    pk = made.configuration.pk
    with pytest.raises(container_admin.ContainerRefusedError) as refusal:
        made_up.change_categories_as_seen(
            made.administrator,
            pk,
            {"categories": [{"category": guest.pk}]},
            now=AFTER,
        )
    assert "has a price on this container" in refusal.value.reasons[0]["text"]
    PriceBookEntry.objects.of(made.organization).delete()
    made_up.change_categories_as_seen(
        made.administrator, pk, {"categories": [{"category": guest.pk}]}, now=AFTER
    )
    assert list(
        ConfigurationCategory.objects.of(made.organization)
        .filter(booking_configuration=pk)
        .values_list("booking_category", flat=True)
    ) == [guest.pk]


def test_a_category_may_be_taken_off_a_container_never_booked(
    made: made_up.MadeUp,
) -> None:
    guest = guest_category(made)
    pk = fresh(made)
    found = answer(
        call(made, "categories", {"categories": [{"category": guest.pk}]}, pk=pk)
    )
    assert [c["id"] for c in row_of(found, pk)["categories"]] == [guest.pk]
    assert len(events(made, AuditAction.CHANGED_BOOKING_CONFIGURATION)) == 1


def test_a_container_keeps_a_category_or_is_refused(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    refused(call(made, "categories", {"categories": []}, pk=pk), "no-category")
    refused(
        call(made, "categories", {"categories": [{"category": 10**9}]}, pk=pk),
        "unknown-category",
    )
    elsewhere = made_up.everything("elsewhere")
    refused(
        call(
            made,
            "categories",
            {"categories": [{"category": elsewhere.private.pk}]},
            pk=pk,
        ),
        "unknown-category",
    )


def hours(*spans: tuple[int, int, int]) -> list[dict[str, int]]:
    return [{"weekday": w, "start": s, "end": e} for w, s, e in spans]


def test_a_categorys_own_hours_are_set_changed_and_cleared(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    monday_only(made, pk)
    guest = guest_category(made)
    both = [{"category": made.private.pk}, {"category": guest.pk}]
    before = counted(made)
    found = answer(
        call(
            made,
            "categories",
            {
                "categories": [
                    both[0],
                    {"category": guest.pk, "hours": hours((MON, 10 * 60, 12 * 60))},
                ]
            },
            pk=pk,
        )
    )
    row = row_of(found, pk)
    assert row["categories"][1]["hours"] == [{"weekday": MON, "start": 600, "end": 720}]
    assert row["categories"][0]["hours"] == []
    assert counted(made) == before + 1
    # The same again writes nothing.
    answer(
        call(
            made,
            "categories",
            {
                "categories": [
                    both[0],
                    {"category": guest.pk, "hours": hours((MON, 10 * 60, 12 * 60))},
                ]
            },
            pk=pk,
        )
    )
    assert counted(made) == before + 1
    # Replaced as a whole.
    found = answer(
        call(
            made,
            "categories",
            {
                "categories": [
                    both[0],
                    {
                        "category": guest.pk,
                        "hours": hours(
                            (MON, 10 * 60, 11 * 60), (MON, 14 * 60, 16 * 60)
                        ),
                    },
                ]
            },
            pk=pk,
        )
    )
    assert [h["start"] for h in row_of(found, pk)["categories"][1]["hours"]] == [
        600,
        840,
    ]
    # Cleared: the container's hours again.
    found = answer(call(made, "categories", {"categories": both}, pk=pk))
    assert row_of(found, pk)["categories"][1]["hours"] == []
    assert not CategoryHoursSpan.objects.of(made.organization).exists()


@pytest.mark.parametrize(
    ("spans", "words"),
    [
        (((MON, 9 * 60, 12 * 60),), "is not inside the hours of container"),
        (((TUE, 10 * 60, 12 * 60),), "is not inside the hours of container"),
        (((MON, 10 * 60, 12 * 60), (MON, 11 * 60, 13 * 60)), "share time"),
        (((MON, 12 * 60, 10 * 60),), "is not a stretch of one weekday"),
        (((9, 10 * 60, 12 * 60),), "is not a stretch of one weekday"),
    ],
)
def test_hours_the_container_does_not_have_are_refused(
    made: made_up.MadeUp, spans: tuple[tuple[int, int, int], ...], words: str
) -> None:
    pk = fresh(made)
    monday_only(made, pk)
    guest = guest_category(made)
    before = (counted(made), container_admin.containers_place(made.organization))
    found = refused(
        call(
            made,
            "categories",
            {
                "categories": [
                    {"category": made.private.pk},
                    {"category": guest.pk, "hours": hours(*spans)},
                ]
            },
            pk=pk,
        ),
        "refused",
    )
    assert words in reasons(found)
    # The category was not added either: one transaction.
    assert before == (
        counted(made),
        container_admin.containers_place(made.organization),
    )
    assert (
        not ConfigurationCategory.objects.of(made.organization)
        .filter(booking_configuration=pk, booking_category=guest)
        .exists()
    )


def test_hours_that_leave_a_booking_still_to_come_outside_are_refused(
    made: made_up.MadeUp,
) -> None:
    found = refused(
        call(
            made,
            "categories",
            {
                "categories": [
                    {
                        "category": made.private.pk,
                        "hours": hours((MON, 14 * 60, 18 * 60)),
                    }
                ]
            },
            pk=made.configuration.pk,
        ),
        "refused",
    )
    assert "stay as they are for a booking still to come" in reasons(found)
    assert made.order.reference in reasons(found)


def test_a_time_change_that_leaves_a_categorys_hours_outside_is_refused(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    monday_only(made, pk)
    answer(
        call(
            made,
            "categories",
            {
                "categories": [
                    {
                        "category": made.private.pk,
                        "hours": hours((MON, 10 * 60, 12 * 60)),
                    }
                ]
            },
            pk=pk,
        )
    )
    found = refused(call(made, "change", {"period_start": 13 * 60}, pk=pk), "refused")
    assert "Narrow the category's own hours first" in reasons(found)


# --- Remove -------------------------------------------------------------------------


def test_a_container_never_booked_is_removed_after_its_event(
    made: made_up.MadeUp,
) -> None:
    pk = fresh(made)
    answer(
        call(
            made,
            "categories",
            {"categories": [{"category": made.private.pk, "hours": []}]},
            pk=pk,
        )
    )
    number = BookingConfiguration.objects.of(made.organization).get(pk=pk).number
    found = answer(call(made, "remove", pk=pk))
    assert pk not in [r["id"] for r in found["containers"]]
    assert (
        not ConfigurationCategory.objects.of(made.organization)
        .filter(booking_configuration=pk)
        .exists()
    )
    (event,) = events(made, AuditAction.REMOVED_BOOKING_CONFIGURATION)
    assert event.object_id == pk
    # A number is never given twice.
    assert fresh(made) != pk
    assert (
        BookingConfiguration.objects.of(made.organization).latest("number").number
        == number + 1
    )


def test_a_container_that_was_booked_has_a_block_or_prices_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    found = refused(call(made, "remove", pk=made.configuration.pk), "in-use")
    assert found["used"] == {"bookings": 1, "blocks": 1, "prices": 1}
    # Past bookings count too: it was booked.
    found = refused(call(made, "remove", pk=made.configuration.pk), "in-use")
    assert found["used"]["bookings"] == 1
    priced = fresh(made, period_type="fixed")
    container_admin.change_container(
        made.administrator, priced, {"price_book": made.price_book.pk}
    )
    PriceBookEntry.objects.create(
        organization=made.organization,
        price_book=made.price_book,
        booking_configuration_id=priced,
        booking_category=made.private,
        tiers_go_by="length",
    )
    found = refused(call(made, "remove", pk=priced), "in-use")
    assert found["used"] == {"prices": 1}
    # Only a block holds one: it is not removed either.
    blocked = BookingConfiguration.objects.of(made.organization).get(
        pk=made.configuration.pk
    )
    assert blocked.pk == made.configuration.pk
    assert not events(made, AuditAction.REMOVED_BOOKING_CONFIGURATION)


# --- The application's own login -----------------------------------------------------


def test_the_restricted_application_login_can_do_all_of_it(
    made: made_up.MadeUp,
) -> None:
    guest = guest_category(made)
    with application_login():
        pk = fresh(made)
        monday_only(made, pk)
        container_admin.change_container(made.administrator, pk, {"status": "enabled"})
        made_up.change_categories_as_seen(
            made.administrator,
            pk,
            {
                "categories": [
                    {
                        "category": made.private.pk,
                        "hours": hours((MON, 10 * 60, 12 * 60)),
                    },
                    {"category": guest.pk},
                ]
            },
        )
        made_up.change_categories_as_seen(
            made.administrator,
            pk,
            {"categories": [{"category": made.private.pk}, {"category": guest.pk}]},
        )
        container_admin.change_container(made.administrator, pk, {"status": "disabled"})
        container_admin.remove_container(made.administrator, pk)
    assert len(events(made, AuditAction.MADE_BOOKING_CONFIGURATION)) == 1
    assert len(events(made, AuditAction.REMOVED_BOOKING_CONFIGURATION)) == 1


def test_a_service_needs_its_own_transaction(made: made_up.MadeUp) -> None:
    from django.db import transaction

    with transaction.atomic(), pytest.raises(RuntimeError):
        fresh(made)


def test_a_former_administrator_cannot_change_anything(made: made_up.MadeUp) -> None:
    pk = fresh(made)
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        status=MemberStatus.BLOCKED
    )
    with pytest.raises(PermissionDenied):
        call(made, "change", {"status": "disabled"}, pk=pk)
    with pytest.raises(PermissionDenied):
        container_admin.remove_container(made.administrator, pk)
    assert BookingConfiguration.objects.of(made.organization).filter(pk=pk).exists()


# --- The setup file after edits ------------------------------------------------------


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


def roundtrip(
    org: Organization, subdomain: str
) -> tuple[setup_file.Setup, setup_file.Setup]:
    """Export, write, read, load as a new organization, export again."""
    exported = setups.export(org)
    parsed = setup_file.parse(setup_file.write(exported))
    assert parsed.booking_configurations == exported.booking_configurations
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
    asset = Asset.objects.of(org).get()
    kind = BookingType.objects.of(org).get(name="Overnight stay")
    work = BookingCategory.objects.of(org).get(name="Work")
    internal = BookingCategory.objects.of(org).get(name="Internal")
    plan = made_up.floor_plan_of(asset)
    # A shared container, a fixed one; set the time, the weeks, the payment,
    # the categories with their own hours; enable; remove one.
    shared = container_admin.make_container(
        admin,
        {
            "asset": asset.pk,
            "booking_type": kind.pk,
            "period_type": "flexible",
            "uses": plan.pk,
        },
    )
    fixed = container_admin.make_container(
        admin,
        {"asset": asset.pk, "booking_type": kind.pk, "period_type": "fixed"},
    )
    gone = container_admin.make_container(
        admin,
        {"asset": asset.pk, "booking_type": kind.pk, "period_type": "fixed"},
    )
    container_admin.change_container(
        admin,
        shared,
        {
            "weekdays": [MON, TUE, FRI],
            "period_start": 9 * 60,
            "period_end": 17 * 60,
            "interval": 30,
            "minimum_length": 60,
            "maximum_length": 120,
            "first_week": "2027-03-08",
            "last_week": "2027-06-28",
            "every_weeks": 2,
            "status": "enabled",
        },
    )
    made_up.change_categories_as_seen(
        admin,
        shared,
        {
            "categories": [
                {"category": work.pk, "hours": hours((MON, 10 * 60, 12 * 60))},
                {"category": internal.pk},
            ]
        },
    )
    container_admin.change_container(
        admin,
        fixed,
        {
            "period_start": 22 * 60,
            "period_end": 10 * 60,
            "end_day": 1,
            "weekdays": [SAT],
            "price_book": None,
        },
    )
    container_admin.remove_container(admin, gone)
    exported, again = roundtrip(org, "second")
    assert again.booking_configurations == exported.booking_configurations
    numbers = [c.number for c in exported.booking_configurations]
    assert numbers == [1, 2, 3, 4, 5, 6]
    new = exported.booking_configurations[4]
    assert new.consumption_mode == "shared"
    assert new.status == "enabled"
    assert new.every_weeks == 2
    assert new.booking_categories == ("Work", "Internal")
    assert [h.booking_category for h in new.category_hours] == ["Work"]


def test_every_screen_refusal_is_a_refusal_of_the_file_too() -> None:
    """What the screens refuse, the file's loader refuses: a container with no
    category, a price for a category it no longer names."""
    org, admin = fjordside_with_admin()
    first = BookingConfiguration.objects.of(org).get(number=1)
    internal = BookingCategory.objects.of(org).get(name="Internal")
    private = BookingCategory.objects.of(org).get(name="Private")
    with pytest.raises(setup_admin.SetupRefusedError):
        made_up.change_categories_as_seen(admin, first.pk, {"categories": []})
    with pytest.raises(container_admin.ContainerRefusedError):
        made_up.change_categories_as_seen(
            admin, first.pk, {"categories": [{"category": private.pk}]}, now=AFTER
        )
    assert internal.pk in ConfigurationCategory.objects.of(org).filter(
        booking_configuration=first
    ).values_list("booking_category", flat=True)
    exported = setups.export(org)
    parsed = setup_file.parse(setup_file.write(exported))
    assert parsed.booking_configurations == exported.booking_configurations


# --- Book's calendar -----------------------------------------------------------------


def starts_on(made: made_up.MadeUp, day: date) -> list[int]:
    """The local hours Book offers an hour of the made-up category to start
    at on a day (the member calendar, what the members' pages are drawn from)."""
    found = member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        booking_type_id=made.booking_type.pk,
        booking_category_id=made.private.pk,
        length=60,
        start_date=day,
        end_date=date.fromordinal(day.toordinal() + 1),
        now=AFTER,
    )
    zone = made.asset.zone
    moments = [
        period.rows[start.row].astimezone(zone)
        for row in found.containers
        for period in row.periods
        for start in period.starts
    ]
    return sorted({moment.hour for moment in moments if moment.date() == day})


def test_book_shows_exactly_what_the_saved_containers_offer(
    made: made_up.MadeUp,
) -> None:
    monday = date(2027, 3, 8)
    pk = made.configuration.pk
    cover_twelve_hours(made)
    assert starts_on(made, monday) == list(range(10, 22))
    container_admin.change_container(
        made.administrator,
        pk,
        {"period_start": 12 * 60, "period_end": 20 * 60, "maximum_length": 8 * 60},
        now=AFTER,
    )
    assert starts_on(made, monday) == list(range(12, 20))
    # The category's own hours: 14:00 to 16:00 only.
    made_up.change_categories_as_seen(
        made.administrator,
        pk,
        {
            "categories": [
                {"category": made.private.pk, "hours": hours((MON, 14 * 60, 16 * 60))}
            ]
        },
        now=AFTER,
    )
    assert starts_on(made, monday) == [14, 15]
    made_up.change_categories_as_seen(
        made.administrator,
        pk,
        {"categories": [{"category": made.private.pk}]},
        now=AFTER,
    )
    assert starts_on(made, monday) == list(range(12, 20))
    # The weeks: every second week from the 15th; the 8th and 22nd are not offered.
    container_admin.change_container(
        made.administrator,
        pk,
        {"first_week": "2027-03-15", "every_weeks": 2},
        now=AFTER,
    )
    assert starts_on(made, monday) == []
    assert starts_on(made, date(2027, 3, 15)) == list(range(12, 20))
    assert starts_on(made, date(2027, 3, 22)) == []
    # Disabled: nothing is offered; enabled again, it is.
    container_admin.change_container(
        made.administrator, pk, {"status": "disabled"}, now=AFTER
    )
    assert starts_on(made, date(2027, 3, 15)) == []
    container_admin.change_container(
        made.administrator, pk, {"status": "enabled"}, now=AFTER
    )
    assert starts_on(made, date(2027, 3, 15)) == list(range(12, 20))
    # Another container over the same time stacks: Book offers what both do.
    other = fresh(made)
    monday_only(made, other, status="enabled")
    assert starts_on(made, date(2027, 3, 15)) == list(range(10, 22))
    container_admin.remove_container(made.administrator, other)
    assert starts_on(made, date(2027, 3, 15)) == list(range(12, 20))
    # Taken off its category (its price first), the member is offered nothing.
    guest = guest_category(made)
    PriceBookEntry.objects.of(made.organization).delete()
    made_up.change_categories_as_seen(
        made.administrator, pk, {"categories": [{"category": guest.pk}]}, now=AFTER
    )
    assert starts_on(made, date(2027, 3, 15)) == []
