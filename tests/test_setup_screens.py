# SPDX-License-Identifier: AGPL-3.0-only
"""The Booking types and categories and the Roles : the gate,
CSRF and origin, malformed requests, every refusal, add / rename / reorder /
remove, a role's categories and admin access, the audit events, concurrent
edits, the real application login, and an export that reloads after edits."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client

from randirandi import setup_file
from randirandi.core import admin_setup_views as views
from randirandi.core import setup as setups
from randirandi.core import setup_admin
from randirandi.core.audit_types import AuditAction
from randirandi.core.booking_choices import booking_choices
from randirandi.core.models import (
    AuditEvent,
    BookingCategory,
    BookingType,
    ConfigurationCategory,
    Member,
    MemberStatus,
    Organization,
    Origin,
    Role,
    RoleCategory,
    SecurityNotice,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import answer, request_to
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
TYPES = "booking-types"
CATEGORIES = "booking-categories"


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
    kind: str = TYPES,
    pk: int = 0,
    who: Member | None = None,
    confirmed: bool = True,
) -> HttpResponse:
    """A view called directly, by default as a confirmed administrator."""
    reads = which in {"types", "roles"}
    path = f"/admin/{which}/"
    request = request_to(
        who or made.administrator,
        "get" if reads else "post",
        path,
        data,
        confirmed=confirmed,
    )
    match which:
        case "types":
            return views.types_data(request)
        case "roles":
            return views.roles_data(request)
        case "make":
            return views.item_make(request, kind)
        case "change":
            return views.item_change(request, kind, pk)
        case "move":
            return views.item_move(request, kind, pk)
        case "remove":
            return views.item_remove(request, kind, pk)
        case "role-make":
            return views.role_make(request)
        case "role-change":
            return views.role_change(request, pk)
        case "role-remove":
            return views.role_remove(request, pk)
    raise AssertionError(which)


def refused(response: HttpResponse, code: str, status: int = 409) -> dict[str, Any]:
    found = answer(response, status)
    assert found["error"] == code
    return found


def counted(made: made_up.MadeUp) -> int:
    """The audit events there are; the made-up organization has some of its own."""
    return AuditEvent.objects.of(made.organization).count()


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.of(made.organization).filter(action=action).order_by("pk")
    )


def names(found: dict[str, Any], key: str) -> list[str]:
    return [row["name"] for row in found[key]]


def add_type(made: made_up.MadeUp, name: str) -> int:
    found = answer(call(made, "make", {"name": name}, kind=TYPES))
    return int(next(r["id"] for r in found["booking_types"] if r["name"] == name))


def add_category(made: made_up.MadeUp, name: str, **more: Any) -> int:
    found = answer(call(made, "make", {"name": name, **more}, kind=CATEGORIES))
    return int(next(r["id"] for r in found["booking_categories"] if r["name"] == name))


def order(
    made: made_up.MadeUp, model: type[BookingType | BookingCategory]
) -> list[str]:
    return [row.name for row in model.objects.of(made.organization)]


# --- The gate, CSRF and origin ---------------------------------------------------

EVERY_CALL = [
    ("types", {}, {}),
    ("roles", {}, {}),
    ("make", {"name": "x"}, {"kind": TYPES}),
    ("make", {"name": "x"}, {"kind": CATEGORIES}),
    ("change", {"name": "x"}, {"kind": TYPES, "pk": 1}),
    ("change", {"name": "x"}, {"kind": CATEGORIES, "pk": 1}),
    ("move", {"direction": "up"}, {"kind": TYPES, "pk": 1}),
    ("remove", {}, {"kind": CATEGORIES, "pk": 1}),
    ("role-make", {"name": "x", "categories": [1]}, {}),
    ("role-change", {"name": "x"}, {"pk": 1}),
    ("role-remove", {}, {"pk": 1}),
]


@pytest.mark.parametrize(("which", "data", "extra"), EVERY_CALL)
def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp, which: str, data: dict[str, Any], extra: dict[str, Any]
) -> None:
    elsewhere = made_up.everything("elsewhere")
    before = (
        order(made, BookingType),
        order(made, BookingCategory),
        Role.objects.of(made.organization).count(),
        AuditEvent.objects.of(made.organization).count(),
    )
    for who, confirmed in (
        (made.administrator, False),
        (made.member, True),
        (elsewhere.administrator, True),
    ):
        if who is elsewhere.administrator:
            # Another organization's administrator is a stranger at this
            # address: the gate looks at the address's organization.
            continue
        with pytest.raises(PermissionDenied):
            call(made, which, data, who=who, confirmed=confirmed, **extra)
    after = (
        order(made, BookingType),
        order(made, BookingCategory),
        Role.objects.of(made.organization).count(),
        AuditEvent.objects.of(made.organization).count(),
    )
    assert before == after


def test_another_organizations_administrator_cannot_reach_this_one(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    with pytest.raises(PermissionDenied):
        call(
            made,
            "role-change",
            {"name": "Hacked"},
            who=elsewhere.administrator,
            pk=made.standard.pk,
        )
    assert (
        Role.objects.of(made.organization).get(pk=made.standard.pk).name == "Standard"
    )


def test_ids_of_another_organization_are_a_404(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    for kind, other in (
        (TYPES, elsewhere.booking_type.pk),
        (CATEGORIES, elsewhere.private.pk),
    ):
        refused(
            call(made, "change", {"name": "z"}, kind=kind, pk=other), "not-found", 404
        )
        refused(
            call(made, "move", {"direction": "up"}, kind=kind, pk=other),
            "not-found",
            404,
        )
        refused(call(made, "remove", kind=kind, pk=other), "not-found", 404)
    refused(
        call(made, "role-change", {"name": "z"}, pk=elsewhere.standard.pk),
        "not-found",
        404,
    )
    refused(call(made, "role-remove", pk=elsewhere.standard.pk), "not-found", 404)
    refused(
        call(made, "role-make", {"name": "z", "categories": [elsewhere.private.pk]}),
        "unknown-category",
    )
    assert BookingType.objects.of(elsewhere.organization).count() == 1
    assert elsewhere.booking_type.name == "Room hire"


def test_an_unknown_list_and_a_huge_id_are_a_404(made: made_up.MadeUp) -> None:
    refused(call(made, "make", {"name": "x"}, kind="nothing"), "not-found", 404)
    refused(call(made, "remove", kind=TYPES, pk=2**63), "not-found", 404)
    refused(call(made, "role-remove", pk=0), "not-found", 404)


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, method in (
            ("/admin/types/data/", "post"),
            ("/admin/roles/data/", "post"),
            ("/admin/types/booking-types/make/", "get"),
            (f"/admin/types/booking-categories/{made.private.pk}/change/", "get"),
            (f"/admin/types/booking-types/{made.booking_type.pk}/move/", "get"),
            (f"/admin/types/booking-types/{made.booking_type.pk}/remove/", "get"),
            ("/admin/roles/make/", "get"),
            (f"/admin/roles/{made.standard.pk}/change/", "get"),
            (f"/admin/roles/{made.standard.pk}/remove/", "get"),
        ):
            response = getattr(client, method)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
    for path, method in (
        ("/admin/types/data/", "get"),
        ("/admin/roles/data/", "get"),
        ("/admin/roles/make/", "post"),
        ("/admin/types/booking-types/make/", "post"),
    ):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        sign_in_administrator(client, made.administrator)
        csrf = str(config(get(client, "/admin/types/").content.decode())["csrf"])

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
        for path, body in (
            ("/admin/types/booking-types/make/", {"name": "Sauna"}),
            ("/admin/types/booking-categories/make/", {"name": "Guest"}),
            (
                "/admin/roles/make/",
                {"name": "Guests", "categories": [made.private.pk]},
            ),
            (
                f"/admin/types/booking-types/{made.booking_type.pk}/move/",
                {"direction": "down"},
            ),
            (f"/admin/roles/{made.standard.pk}/change/", {"name": "Renamed"}),
            (f"/admin/roles/{made.board.pk}/remove/", {}),
        ):
            assert send(path, body, no_token).status_code == 403, path
            assert send(path, body, wrong).status_code == 403, path
        assert order(made, BookingType) == ["Room hire"]
        assert order(made, BookingCategory) == ["private"]
        assert not Role.objects.of(made.organization).filter(name="Guests").exists()
        assert answer(
            send("/admin/types/booking-types/make/", {"name": "Sauna"}, right)
        )["booking_types"]
        assert answer(
            send(
                "/admin/roles/make/",
                {"name": "Guests", "categories": [made.private.pk]},
                right,
            )
        )["roles"]
    assert order(made, BookingType) == ["Room hire", "Sauna"]


def test_the_places_open_for_an_administrator(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for place in ("types", "roles"):
            ui = config(get(client, f"/admin/{place}/").content.decode())
            assert ui["place"] == place


@pytest.mark.parametrize(
    ("which", "extra", "body"),
    [
        ("make", {"kind": TYPES}, b"not json"),
        ("make", {"kind": TYPES}, b"[]"),
        ("make", {"kind": TYPES}, b'"name"'),
        ("make", {"kind": TYPES}, {}),
        ("make", {"kind": TYPES}, {"name": 5}),
        ("make", {"kind": TYPES}, {"name": "x", "icon": "work"}),
        ("make", {"kind": TYPES}, {"name": "x", "description": 5}),
        ("make", {"kind": TYPES}, {"name": "x", "extra": 1}),
        ("make", {"kind": CATEGORIES}, {"name": "x", "description": "d"}),
        ("make", {"kind": CATEGORIES}, {"name": "x", "icon": 3}),
        ("change", {"kind": TYPES, "pk": 1}, {}),
        ("move", {"kind": TYPES, "pk": 1}, {}),
        ("move", {"kind": TYPES, "pk": 1}, {"direction": "left"}),
        ("move", {"kind": TYPES, "pk": 1}, {"direction": 1}),
        ("move", {"kind": TYPES, "pk": 1}, {"direction": "up", "by": 2}),
        ("role-make", {}, {"name": "x"}),
        ("role-make", {}, {"categories": [1]}),
        ("role-make", {}, {"name": "x", "categories": "1"}),
        ("role-make", {}, {"name": "x", "categories": [True]}),
        ("role-make", {}, {"name": "x", "categories": ["1"]}),
        ("role-make", {}, {"name": "x", "categories": [1], "admin_access": "yes"}),
        ("role-make", {}, {"name": "x", "categories": [1], "standard": 1}),
        ("role-make", {}, {"name": "x", "categories": [1], "members": 1}),
        ("role-change", {"pk": 1}, {}),
    ],
)
def test_a_malformed_body_is_a_400_and_changes_nothing(
    made: made_up.MadeUp, which: str, extra: dict[str, Any], body: Any
) -> None:
    before = (
        order(made, BookingType),
        order(made, BookingCategory),
        Role.objects.of(made.organization).count(),
        AuditEvent.objects.of(made.organization).count(),
    )
    refused(call(made, which, body, **extra), "bad-request", 400)
    assert before == (
        order(made, BookingType),
        order(made, BookingCategory),
        Role.objects.of(made.organization).count(),
        AuditEvent.objects.of(made.organization).count(),
    )


# --- Booking types ----------------------------------------------------------------


def test_the_lists_in_the_order_members_see_them(made: made_up.MadeUp) -> None:
    add_type(made, "Sauna")
    add_category(made, "Guest", icon="group")
    found = answer(call(made, "types"))
    assert names(found, "booking_types") == ["Room hire", "Sauna"]
    assert names(found, "booking_categories") == ["private", "Guest"]
    assert [row["icon"] for row in found["booking_categories"]] == ["", "group"]
    assert found["icons"] == ["person", "work", "building", "home", "group"]
    room, sauna = found["booking_types"]
    assert (room["containers"], room["bookings"], room["removable"]) == (1, 1, False)
    assert (sauna["containers"], sauna["removable"]) == (0, True)
    private = found["booking_categories"][0]
    # In the Roles list's order (oldest first), not alphabetical.
    assert private["roles"] == ["Standard", "Board"]
    assert [r["name"] for r in answer(call(made, "roles"))["roles"]][:2] == [
        "Standard", "Board",
    ]  # fmt: skip
    assert (private["containers"], private["prices"], private["bookings"]) == (1, 1, 1)
    assert private["removable"] is False


def test_a_new_type_goes_last_with_its_description_and_one_event(
    made: made_up.MadeUp,
) -> None:
    answer(call(made, "make", {"name": "  Sauna  "}, kind=TYPES))
    one = BookingType.objects.of(made.organization).get(name="Sauna").pk
    two = answer(
        call(
            made,
            "make",
            {"name": "Boat", "description": "  By the hour \n"},
            kind=TYPES,
        )
    )["booking_types"]
    assert [(r["name"], r["description"]) for r in two] == [
        ("Room hire", ""),
        ("Sauna", ""),
        ("Boat", "By the hour"),
    ]
    assert [row.position for row in BookingType.objects.of(made.organization)] == [
        1,
        2,
        3,
    ]
    made_events = events(made, AuditAction.MADE_BOOKING_TYPE)
    assert len(made_events) == 2
    assert made_events[0].object_kind == "booking_type"
    assert made_events[0].object_id == one
    assert made_events[0].administrator_id == made.administrator.pk


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_a_name_is_text_of_one_to_two_hundred_characters(
    made: made_up.MadeUp, name: str
) -> None:
    baseline = counted(made)
    for kind in (TYPES, CATEGORIES):
        refused(call(made, "make", {"name": name}, kind=kind), "invalid-name")
    refused(
        call(made, "role-make", {"name": name, "categories": [made.private.pk]}),
        "invalid-name",
    )
    refused(
        call(made, "change", {"name": name}, kind=TYPES, pk=made.booking_type.pk),
        "invalid-name",
    )
    assert counted(made) == baseline


def test_two_hundred_characters_are_a_name(made: made_up.MadeUp) -> None:
    long = "é" * 200
    assert add_type(made, long)
    assert order(made, BookingType)[-1] == long


def test_a_name_is_taken_whatever_its_case_and_a_rename_may_change_case(
    made: made_up.MadeUp,
) -> None:
    refused(call(made, "make", {"name": "room hire"}, kind=TYPES), "name-taken")
    refused(call(made, "make", {"name": "PRIVATE"}, kind=CATEGORIES), "name-taken")
    refused(
        call(made, "role-make", {"name": "standard", "categories": [made.private.pk]}),
        "name-taken",
    )
    sauna = add_type(made, "Sauna")
    refused(
        call(made, "change", {"name": "ROOM HIRE"}, kind=TYPES, pk=sauna), "name-taken"
    )
    answer(
        call(made, "change", {"name": "ROOM HIRE"}, kind=TYPES, pk=made.booking_type.pk)
    )
    assert (
        BookingType.objects.of(made.organization).get(pk=made.booking_type.pk).name
        == "ROOM HIRE"
    )
    # The same name in another list, and in another organization, is fine.
    add_category(made, "Sauna")
    other = made_up.everything("elsewhere")
    assert other.booking_type.name == "Room hire"


def test_rename_and_describe_a_type_and_change_a_categorys_icon(
    made: made_up.MadeUp,
) -> None:
    found = answer(
        call(
            made,
            "change",
            {"name": "Hire a room", "description": "By the hour"},
            kind=TYPES,
            pk=made.booking_type.pk,
        )
    )
    assert found["booking_types"][0]["name"] == "Hire a room"
    assert found["booking_types"][0]["description"] == "By the hour"
    answer(call(made, "change", {"icon": "work"}, kind=CATEGORIES, pk=made.private.pk))
    category = BookingCategory.objects.of(made.organization).get(pk=made.private.pk)
    assert (category.name, category.icon) == ("private", "work")
    answer(call(made, "change", {"icon": ""}, kind=CATEGORIES, pk=made.private.pk))
    assert (
        BookingCategory.objects.of(made.organization).get(pk=made.private.pk).icon == ""
    )
    changed = events(made, AuditAction.CHANGED_BOOKING_TYPE)
    assert [e.object_id for e in changed] == [made.booking_type.pk]
    assert len(events(made, AuditAction.CHANGED_BOOKING_CATEGORY)) == 2
    # The copy a booking keeps of the name is not touched by a rename.
    assert made.booking.booking_type_name == "Room hire"


def test_a_change_that_changes_nothing_writes_no_event(made: made_up.MadeUp) -> None:
    baseline = counted(made)
    answer(
        call(made, "change", {"name": "Room hire"}, kind=TYPES, pk=made.booking_type.pk)
    )
    answer(call(made, "change", {"icon": ""}, kind=CATEGORIES, pk=made.private.pk))
    answer(call(made, "role-change", {"name": "Standard"}, pk=made.standard.pk))
    assert counted(made) == baseline


@pytest.mark.parametrize("icon", ["star", "Person", " person", "personal", "x" * 9])
def test_an_unknown_icon_is_refused_on_make_and_change(
    made: made_up.MadeUp, icon: str
) -> None:
    refused(
        call(made, "make", {"name": "Guest", "icon": icon}, kind=CATEGORIES),
        "unknown-icon",
    )
    refused(
        call(made, "change", {"icon": icon}, kind=CATEGORIES, pk=made.private.pk),
        "unknown-icon",
    )
    assert order(made, BookingCategory) == ["private"]


@pytest.mark.parametrize("icon", ["person", "work", "building", "home", "group"])
def test_each_icon_of_the_list_is_taken(made: made_up.MadeUp, icon: str) -> None:
    add_category(made, "Guest", icon=icon)
    assert BookingCategory.objects.of(made.organization).get(name="Guest").icon == icon


# --- Reordering ---------------------------------------------------------------------


def three_types(made: made_up.MadeUp) -> tuple[int, int, int]:
    return made.booking_type.pk, add_type(made, "Sauna"), add_type(made, "Boat")


def test_the_arrows_move_a_row_one_place_and_the_ends_stay(
    made: made_up.MadeUp,
) -> None:
    room, sauna, boat = three_types(made)
    found = answer(call(made, "move", {"direction": "down"}, kind=TYPES, pk=room))
    assert names(found, "booking_types") == ["Sauna", "Room hire", "Boat"]
    found = answer(call(made, "move", {"direction": "up"}, kind=TYPES, pk=boat))
    assert names(found, "booking_types") == ["Sauna", "Boat", "Room hire"]
    before = AuditEvent.objects.of(made.organization).count()
    found = answer(call(made, "move", {"direction": "up"}, kind=TYPES, pk=sauna))
    assert names(found, "booking_types") == ["Sauna", "Boat", "Room hire"]
    found = answer(call(made, "move", {"direction": "down"}, kind=TYPES, pk=room))
    assert names(found, "booking_types") == ["Sauna", "Boat", "Room hire"]
    assert AuditEvent.objects.of(made.organization).count() == before
    assert [row.position for row in BookingType.objects.of(made.organization)] == [
        1,
        2,
        3,
    ]


def test_categories_move_too_and_the_member_sees_the_order_and_icons(
    made: made_up.MadeUp,
) -> None:
    cover_twelve_hours(made)
    work = add_category(made, "Work", icon="work")
    RoleCategory.objects.create(
        organization=made.organization,
        role=made.standard,
        booking_category=BookingCategory.objects.of(made.organization).get(pk=work),
    )
    sauna = add_type(made, "Sauna")
    first = booking_choices(made.member).categories
    assert [c.name for c in first] == ["private", "Work"]
    answer(call(made, "move", {"direction": "up"}, kind=CATEGORIES, pk=work))
    answer(call(made, "move", {"direction": "up"}, kind=TYPES, pk=sauna))
    after = booking_choices(made.member)
    assert [(c.name, c.icon) for c in after.categories] == [
        ("Work", "work"),
        ("private", ""),
    ]
    assert [row.name for row in BookingType.objects.of(made.organization)] == [
        "Sauna",
        "Room hire",
    ]
    assert len(events(made, AuditAction.CHANGED_BOOKING_CATEGORY)) == 1


def test_the_first_type_the_member_is_offered_follows_the_order() -> None:
    from tests.test_calendar import scenario

    org, member = scenario("scenarios/sauna-palace.toml")
    manager = admin_in(org)
    first, second = (
        BookingType.objects.of(org).filter(name=name).get().pk
        for name in ("Sauna visit", "Private sauna")
    )

    def offered() -> list[str]:
        found = booking_choices(member)
        return [row.name for row in found.assets[0].types]

    before = offered()
    setup_admin.move_item(manager, BookingType, second, "up")
    setup_admin.move_item(manager, BookingType, first, "down")
    assert sorted(offered()) == sorted(before)
    assert offered()[0] == (
        BookingType.objects.of(org).order_by("position", "pk").first().name  # type: ignore[union-attr]
    )


def admin_in(org: Organization) -> Member:
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


def test_positions_that_were_never_tidy_become_one_two_three(
    made: made_up.MadeUp,
) -> None:
    room, sauna, boat = three_types(made)
    BookingType.objects.of(made.organization).filter(pk=room).update(position=40)
    BookingType.objects.of(made.organization).filter(pk=sauna).update(position=40)
    BookingType.objects.of(made.organization).filter(pk=boat).update(position=0)
    found = answer(call(made, "move", {"direction": "down"}, kind=TYPES, pk=boat))
    assert names(found, "booking_types") == ["Room hire", "Boat", "Sauna"]
    assert [r.position for r in BookingType.objects.of(made.organization)] == [1, 2, 3]


def test_two_administrators_reordering_at_once_end_in_one_valid_order(
    made: made_up.MadeUp,
) -> None:
    second_admin = admin_in(made.organization)
    ids = [made.booking_type.pk] + [add_type(made, f"Type {n}") for n in range(5)]
    barrier = Barrier(2)

    def move(who: Member, pk: int, direction: str) -> None:
        barrier.wait()
        try:
            setup_admin.move_item(who, BookingType, pk, direction)
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(move, made.administrator, ids[0], "down"),
            pool.submit(move, second_admin, ids[1], "up"),
        ]
        for future in futures:
            future.result()
        rows = list(BookingType.objects.of(made.organization))
    assert sorted(r.position for r in rows) == [1, 2, 3, 4, 5, 6]
    assert sorted(r.pk for r in rows) == sorted(ids)


def test_many_moves_at_once_keep_the_positions_one_to_n(made: made_up.MadeUp) -> None:
    second_admin = admin_in(made.organization)
    ids = [made.booking_type.pk] + [add_type(made, f"Type {n}") for n in range(7)]
    barrier = Barrier(4)

    def shuffle(who: Member, seed: int) -> None:
        barrier.wait()
        try:
            for step in range(6):
                pk = ids[(seed * 3 + step * 5) % len(ids)]
                setup_admin.move_item(
                    who, BookingType, pk, "up" if (seed + step) % 2 else "down"
                )
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(shuffle, who, seed)
            for seed, who in enumerate((made.administrator, second_admin) * 2)
        ]
        for future in futures:
            future.result()
        rows = list(BookingType.objects.of(made.organization))
    assert [r.position for r in rows] == list(range(1, 9))
    assert sorted(r.pk for r in rows) == sorted(ids)


def test_a_rename_and_a_reorder_at_once_both_land(made: made_up.MadeUp) -> None:
    sauna = add_type(made, "Sauna")
    barrier = Barrier(2)

    def rename() -> None:
        barrier.wait()
        try:
            setup_admin.change_item(
                made.administrator, BookingType, sauna, {"name": "Bath"}
            )
        finally:
            connection.close()

    def move() -> None:
        barrier.wait()
        try:
            setup_admin.move_item(made.administrator, BookingType, sauna, "up")
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(rename), pool.submit(move)]:
            future.result()
        rows = list(BookingType.objects.of(made.organization))
    assert [r.name for r in rows] == ["Bath", "Room hire"]


# --- Removing ------------------------------------------------------------------------


def test_a_type_a_container_or_a_booking_names_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    found = refused(call(made, "remove", kind=TYPES, pk=made.booking_type.pk), "in-use")
    assert found["used"] == {"containers": 1, "bookings": 1}
    assert BookingType.objects.of(made.organization).count() == 1
    assert events(made, AuditAction.REMOVED_BOOKING_TYPE) == []


def test_a_type_only_a_booking_names_is_not_removed(made: made_up.MadeUp) -> None:
    from randirandi.core.models import BookingConfiguration

    BookingConfiguration.objects.of(made.organization).filter(
        pk=made.configuration.pk
    ).update(status="disabled")
    found = refused(call(made, "remove", kind=TYPES, pk=made.booking_type.pk), "in-use")
    assert found["used"] == {"containers": 1, "bookings": 1}


@pytest.mark.parametrize("holder", ["role", "container"])
def test_a_category_a_role_or_a_container_names_is_not_removed(
    made: made_up.MadeUp, holder: str
) -> None:
    extra = BookingCategory.objects.create(
        organization=made.organization, name="Guest", position=2
    )
    if holder == "role":
        RoleCategory.objects.create(
            organization=made.organization, role=made.board, booking_category=extra
        )
    else:
        ConfigurationCategory.objects.create(
            organization=made.organization,
            booking_configuration=made.configuration,
            booking_category=extra,
        )
    found = refused(call(made, "remove", kind=CATEGORIES, pk=extra.pk), "in-use")
    assert found["used"] == {("roles" if holder == "role" else "containers"): 1}
    assert BookingCategory.objects.of(made.organization).filter(pk=extra.pk).exists()


def test_a_category_bookings_and_prices_and_roles_name_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    found = refused(call(made, "remove", kind=CATEGORIES, pk=made.private.pk), "in-use")
    assert found["used"] == {"roles": 2, "containers": 1, "prices": 1, "bookings": 1}


def test_a_row_nothing_names_is_removed_after_its_event_and_the_rest_renumber(
    made: made_up.MadeUp,
) -> None:
    _room, sauna, _boat = three_types(made)
    found = answer(call(made, "remove", kind=TYPES, pk=sauna))
    assert names(found, "booking_types") == ["Room hire", "Boat"]
    assert [r.position for r in BookingType.objects.of(made.organization)] == [1, 2]
    (removed,) = events(made, AuditAction.REMOVED_BOOKING_TYPE)
    assert (removed.object_kind, removed.object_id) == ("booking_type", sauna)
    guest = add_category(made, "Guest")
    answer(call(made, "remove", kind=CATEGORIES, pk=guest))
    assert order(made, BookingCategory) == ["private"]
    assert len(events(made, AuditAction.REMOVED_BOOKING_CATEGORY)) == 1
    # A second press finds nothing.
    refused(call(made, "remove", kind=CATEGORIES, pk=guest), "not-found", 404)
    assert len(events(made, AuditAction.REMOVED_BOOKING_CATEGORY)) == 1


def test_a_member_whose_last_category_was_removed_falls_back(
    made: made_up.MadeUp,
) -> None:
    guest = add_category(made, "Guest")
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        last_booking_category=guest
    )
    answer(call(made, "remove", kind=CATEGORIES, pk=guest))
    stored = Member.objects.of(made.organization).get(pk=made.member.pk)
    assert stored.last_booking_category_id is None


# --- Roles ------------------------------------------------------------------------


def role_of(made: made_up.MadeUp, name: str) -> Role:
    return Role.objects.of(made.organization).get(name=name)


def test_the_roles_with_their_categories_access_standard_and_members(
    made: made_up.MadeUp,
) -> None:
    guest = add_category(made, "Guest")
    found = answer(call(made, "roles"))
    by_name = {r["name"]: r for r in found["roles"]}
    assert by_name["Standard"] == {
        "id": made.standard.pk,
        "name": "Standard",
        "categories": [made.private.pk],
        "admin_access": False,
        "standard": True,
        "members": 1,
    }
    assert by_name["Board"]["admin_access"] is True
    assert by_name["Board"]["standard"] is False
    assert [c["id"] for c in found["booking_categories"]] == [made.private.pk, guest]


def test_a_new_role_has_categories_optional_admin_access_and_one_event(
    made: made_up.MadeUp,
) -> None:
    guest = add_category(made, "Guest")
    notices = SecurityNotice.objects.of(made.organization).count()
    found = answer(
        call(
            made,
            "role-make",
            {"name": " Guests ", "categories": [guest, made.private.pk]},
        )
    )
    role = role_of(made, "Guests")
    row = next(r for r in found["roles"] if r["id"] == role.pk)
    assert row["categories"] == [made.private.pk, guest]
    assert (row["admin_access"], row["standard"], row["members"]) == (False, False, 0)
    answer(
        call(
            made,
            "role-make",
            {"name": "Staff", "categories": [guest], "admin_access": True},
        )
    )
    assert role_of(made, "Staff").admin_access is True
    assert len(events(made, AuditAction.MADE_ROLE)) == 2
    # A role with no members gives nobody admin access.
    assert events(made, AuditAction.GAVE_ADMIN_ACCESS) == []
    assert SecurityNotice.objects.of(made.organization).count() == notices


def test_a_role_needs_a_booking_category_of_this_organization(
    made: made_up.MadeUp,
) -> None:
    refused(
        call(made, "role-make", {"name": "Guests", "categories": []}), "no-category"
    )
    refused(
        call(made, "role-make", {"name": "Guests", "categories": [999999]}),
        "unknown-category",
    )
    refused(
        call(
            made,
            "role-make",
            {"name": "Guests", "categories": [made.private.pk, 999999]},
        ),
        "unknown-category",
    )
    refused(
        call(made, "role-change", {"categories": []}, pk=made.standard.pk),
        "no-category",
    )
    refused(
        call(made, "role-change", {"categories": [999999]}, pk=made.standard.pk),
        "unknown-category",
    )
    assert not Role.objects.of(made.organization).filter(name="Guests").exists()
    assert (
        RoleCategory.objects.of(made.organization).filter(role=made.standard).count()
        == 1
    )


def test_a_role_with_no_category_cannot_be_changed_without_one() -> None:
    made = made_up.everything()
    # made_up's Board has none, which the setup file would refuse.
    refused(
        call(made, "role-change", {"name": "Committee"}, pk=made.board.pk),
        "no-category",
    )
    answer(
        call(
            made,
            "role-change",
            {"name": "Committee", "categories": [made.private.pk]},
            pk=made.board.pk,
        )
    )
    assert role_of(made, "Committee").admin_access is True


def test_the_standard_role_never_gives_admin_access(made: made_up.MadeUp) -> None:
    refused(
        call(
            made,
            "role-make",
            {
                "name": "Std",
                "categories": [made.private.pk],
                "admin_access": True,
                "standard": True,
            },
        ),
        "standard-admin",
    )
    refused(
        call(made, "role-change", {"admin_access": True}, pk=made.standard.pk),
        "standard-admin",
    )
    refused(
        call(made, "role-change", {"standard": True}, pk=made.board.pk),
        "standard-admin",
    )
    refused(
        call(
            made,
            "role-change",
            {"standard": True, "admin_access": True},
            pk=made.standard.pk,
        ),
        "standard-admin",
    )
    assert role_of(made, "Standard").admin_access is False
    org = Organization.objects.get(pk=made.organization.pk)
    assert org.standard_role_id == made.standard.pk


def test_the_standard_role_moves_to_another_and_never_to_none(
    made: made_up.MadeUp,
) -> None:
    refused(
        call(made, "role-change", {"standard": False}, pk=made.standard.pk),
        "standard-needed",
    )
    guest = answer(
        call(made, "role-make", {"name": "Guests", "categories": [made.private.pk]})
    )
    guests = next(r["id"] for r in guest["roles"] if r["name"] == "Guests")
    found = answer(call(made, "role-change", {"standard": True}, pk=guests))
    standard = [r["name"] for r in found["roles"] if r["standard"]]
    assert standard == ["Guests"]
    assert Organization.objects.get(pk=made.organization.pk).standard_role_id == guests
    refused(
        call(made, "role-change", {"standard": False}, pk=guests), "standard-needed"
    )
    # The old standard role can now take admin access and be removed.
    answer(
        call(
            made,
            "role-make",
            {"name": "New standard", "categories": [made.private.pk], "standard": True},
        )
    )
    now_standard = Organization.objects.get(pk=made.organization.pk).standard_role
    assert now_standard is not None
    assert now_standard.name == "New standard"


def test_admin_access_taken_from_the_last_administrators_role_is_refused(
    made: made_up.MadeUp,
) -> None:
    refused(
        call(made, "role-change", {"admin_access": False}, pk=made.board.pk),
        "last-administrator",
    )
    assert role_of(made, "Board").admin_access is True
    # A second administrator elsewhere lets the first role lose it.
    guests = answer(
        call(
            made,
            "role-make",
            {"name": "Guests", "categories": [made.private.pk], "admin_access": True},
        )
    )
    guests_id = next(r["id"] for r in guests["roles"] if r["name"] == "Guests")
    helper = Member.objects.create(
        organization=made.organization,
        role=role_of(made, "Guests"),
        full_name="Pat Admin",
        first_name="Pat",
        last_name="Admin",
        email="pat@example.com",
        phone="+4790000077",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )
    answer(call(made, "role-change", {"admin_access": False}, pk=made.board.pk))
    assert role_of(made, "Board").admin_access is False
    # Now the helper's role is the last one.
    refused(
        call(made, "role-change", {"admin_access": False}, pk=guests_id, who=helper),
        "last-administrator",
    )
    # A blocked administrator is not one the organization can count on.
    Member.objects.of(made.organization).filter(pk=helper.pk).update(
        status=MemberStatus.BLOCKED
    )


def test_a_role_with_no_active_member_may_lose_admin_access(
    made: made_up.MadeUp,
) -> None:
    extra = answer(
        call(
            made,
            "role-make",
            {"name": "Staff", "categories": [made.private.pk], "admin_access": True},
        )
    )
    staff = next(r["id"] for r in extra["roles"] if r["name"] == "Staff")
    answer(call(made, "role-change", {"admin_access": False}, pk=staff))
    assert role_of(made, "Staff").admin_access is False


def test_giving_a_role_admin_access_gives_each_member_a_gave_admin_access_event(
    made: made_up.MadeUp,
) -> None:
    second = Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Kim Blocked",
        first_name="Kim",
        last_name="Blocked",
        email="kim@example.com",
        phone="+4790000055",
        status=MemberStatus.BLOCKED,
        origin=Origin.MEMBER_IMPORT,
    )
    staff = answer(
        call(made, "role-make", {"name": "Staff", "categories": [made.private.pk]})
    )
    staff_id = next(r["id"] for r in staff["roles"] if r["name"] == "Staff")
    Member.objects.of(made.organization).filter(
        pk__in=[made.member.pk, second.pk]
    ).update(role=staff_id)
    answer(call(made, "role-change", {"admin_access": True}, pk=staff_id))
    gave = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert sorted(e.object_id for e in gave) == sorted([made.member.pk, second.pk])
    assert {e.administrator_id for e in gave} == {made.administrator.pk}
    # Each event is announced to every active administrator (Sam and Alex).
    for event in gave:
        recipients = SecurityNotice.objects.of(made.organization).filter(event=event)
        assert sorted(n.recipient_id for n in recipients) == sorted(
            [made.administrator.pk, made.member.pk]
        )
    # Taking it back and giving it again is a new gain.
    answer(call(made, "role-change", {"admin_access": False}, pk=staff_id))
    assert len(events(made, AuditAction.GAVE_ADMIN_ACCESS)) == 2
    answer(call(made, "role-change", {"admin_access": True}, pk=staff_id))
    assert len(events(made, AuditAction.GAVE_ADMIN_ACCESS)) == 4
    assert len(events(made, AuditAction.CHANGED_ROLE)) == 3


def test_a_roles_categories_change_with_one_event_and_the_member_page_follows(
    made: made_up.MadeUp,
) -> None:
    cover_twelve_hours(made)
    guest = add_category(made, "Guest", icon="home")
    answer(
        call(
            made,
            "role-change",
            {"categories": [made.private.pk, guest]},
            pk=made.standard.pk,
        )
    )
    assert [c.name for c in booking_choices(made.member).categories] == [
        "private",
        "Guest",
    ]
    answer(call(made, "role-change", {"categories": [guest]}, pk=made.standard.pk))
    assert [c.name for c in booking_choices(made.member).categories] == ["Guest"]
    assert list(
        RoleCategory.objects.of(made.organization)
        .filter(role=made.standard)
        .values_list("booking_category", flat=True)
    ) == [guest]
    assert len(events(made, AuditAction.CHANGED_ROLE)) == 2


def test_a_role_a_member_has_or_the_standard_role_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    found = refused(call(made, "role-remove", pk=made.board.pk), "has-members")
    assert found["used"] == {"members": 1}
    refused(call(made, "role-remove", pk=made.standard.pk), "is-standard")
    assert Role.objects.of(made.organization).count() == 2
    assert events(made, AuditAction.REMOVED_ROLE) == []


def test_a_role_nobody_has_is_removed_with_its_categories(
    made: made_up.MadeUp,
) -> None:
    staff = answer(
        call(made, "role-make", {"name": "Staff", "categories": [made.private.pk]})
    )
    staff_id = next(r["id"] for r in staff["roles"] if r["name"] == "Staff")
    found = answer(call(made, "role-remove", pk=staff_id))
    assert "Staff" not in names(found, "roles")
    assert (
        not RoleCategory.objects.of(made.organization).filter(role_id=staff_id).exists()
    )
    (removed,) = events(made, AuditAction.REMOVED_ROLE)
    assert (removed.object_kind, removed.object_id) == ("role", staff_id)
    refused(call(made, "role-remove", pk=staff_id), "not-found", 404)


def test_two_administrators_changing_one_role_at_once_take_turns(
    made: made_up.MadeUp,
) -> None:
    second_admin = admin_in(made.organization)
    guest = add_category(made, "Guest")
    barrier = Barrier(2)

    def run(who: Member, fields: dict[str, Any]) -> None:
        barrier.wait()
        try:
            setup_admin.change_role(who, made.standard.pk, fields)
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [
            pool.submit(run, made.administrator, {"categories": [guest]}),
            pool.submit(run, second_admin, {"name": "Everyone"}),
        ]:
            future.result()
        role = Role.objects.of(made.organization).get(pk=made.standard.pk)
        assert role.name == "Everyone"
        assert list(
            RoleCategory.objects.of(made.organization)
            .filter(role=role)
            .values_list("booking_category", flat=True)
        ) == [guest]


# --- Privileges and the setup file --------------------------------------------------


def test_the_restricted_application_login_can_do_all_of_it(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        setup_admin.make_item(made.administrator, BookingType, {"name": "Sauna"})
        setup_admin.make_item(
            made.administrator, BookingCategory, {"name": "Guest", "icon": "home"}
        )
        category = BookingCategory.objects.of(made.organization).get(name="Guest")
        sauna = BookingType.objects.of(made.organization).get(name="Sauna")
        setup_admin.change_item(
            made.administrator, BookingCategory, category.pk, {"icon": "work"}
        )
        setup_admin.move_item(made.administrator, BookingType, sauna.pk, "up")
        setup_admin.make_role(
            made.administrator,
            {"name": "Guests", "categories": [category.pk], "standard": True},
        )
        guests = Role.objects.of(made.organization).get(name="Guests")
        setup_admin.change_role(
            made.administrator,
            guests.pk,
            {"categories": [category.pk, made.private.pk]},
        )
        setup_admin.change_role(
            made.administrator, made.standard.pk, {"admin_access": False}
        )
        setup_admin.remove_item(made.administrator, BookingType, sauna.pk)
        assert (
            Organization.objects.get(pk=made.organization.pk).standard_role_id
            == guests.pk
        )
    assert AuditEvent.objects.of(made.organization).count() >= 8


def test_a_service_needs_its_own_transaction(made: made_up.MadeUp) -> None:
    from django.db import transaction

    with transaction.atomic(), pytest.raises(RuntimeError):
        setup_admin.make_item(made.administrator, BookingType, {"name": "Sauna"})


def test_a_former_administrator_cannot_change_anything(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        status=MemberStatus.BLOCKED
    )
    with pytest.raises(PermissionDenied):
        call(made, "make", {"name": "Sauna"}, kind=TYPES)
    with pytest.raises(PermissionDenied):
        setup_admin.make_item(made.administrator, BookingType, {"name": "Sauna"})
    assert order(made, BookingType) == ["Room hire"]


def same_lists(one: setup_file.Setup, two: setup_file.Setup) -> None:
    """What the screens edit: the lists, the roles and the standard role."""
    assert one.booking_types == two.booking_types
    assert one.booking_categories == two.booking_categories
    assert one.roles == two.roles
    assert one.organization.standard_role == two.organization.standard_role


def roundtrip(
    org: Organization, subdomain: str
) -> tuple[setup_file.Setup, setup_file.Setup]:
    """Export, write, read, load as a new organization, export again."""
    exported = setups.export(org)
    parsed = setup_file.parse(setup_file.write(exported))
    same_lists(exported, parsed)
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


def fjordside_with_admin() -> tuple[Organization, Member]:
    org = setups.load(setup_file.read(SETUPS / "fjordside.toml"), today=TODAY)
    admin_role = Role.objects.of(org).get(name="Admin")
    admin = Member.objects.create(
        organization=org,
        role=admin_role,
        full_name="Sam Field",
        first_name="Sam",
        last_name="Field",
        email="sam@example.com",
        phone="+4790000002",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )
    return org, admin


def test_the_export_after_edits_on_the_screens_reloads_to_the_same_setup() -> None:
    org, admin = fjordside_with_admin()
    # Add, rename (with characters a file must quote), reorder, re-icon.
    setup_admin.make_item(
        admin,
        BookingType,
        {"name": 'Boat "A" \\ trip', "description": "Line one\nline two"},
    )
    setup_admin.make_item(admin, BookingType, {"name": "Étoile ☃", "description": ""})
    setup_admin.make_item(admin, BookingCategory, {"name": "Guest", "icon": "group"})
    work = BookingCategory.objects.of(org).get(name="Work")
    setup_admin.change_item(
        admin, BookingCategory, work.pk, {"name": "Job", "icon": "home"}
    )
    setup_admin.move_item(admin, BookingCategory, work.pk, "down")
    boat = BookingType.objects.of(org).get(name='Boat "A" \\ trip')
    setup_admin.move_item(admin, BookingType, boat.pk, "up")
    setup_admin.move_item(admin, BookingType, boat.pk, "up")
    # Roles: add, give categories, make standard, rename, drop one.
    guest = BookingCategory.objects.of(org).get(name="Guest")
    setup_admin.make_role(admin, {"name": "Gäst", "categories": [guest.pk, work.pk]})
    setup_admin.make_role(
        admin, {"name": "Temp", "categories": [guest.pk], "admin_access": True}
    )
    temp = Role.objects.of(org).get(name="Temp")
    setup_admin.change_role(
        admin, temp.pk, {"admin_access": False, "name": "Temporary"}
    )
    setup_admin.remove_role(admin, temp.pk)
    board = Role.objects.of(org).get(name="Board")
    setup_admin.change_role(
        admin, board.pk, {"categories": [guest.pk, work.pk], "standard": True}
    )
    exported, again = roundtrip(org, "second")
    same_lists(exported, again)
    assert exported.organization.standard_role == "Board"
    assert [c.name for c in exported.booking_categories] == [
        "Private",
        "Internal",
        "Job",
        "Guest",
    ]
    assert [c.icon for c in exported.booking_categories] == [
        "person",
        "building",
        "home",
        "group",
    ]
    names = [t.name for t in exported.booking_types]
    # Added last, then the boat moved up two places.
    assert names[-1] == "Étoile ☃"
    assert names[-4] == 'Boat "A" \\ trip'
    assert exported.booking_types[-4].description == "Line one\nline two"
    assert "Gäst" in [r.name for r in exported.roles]
    assert "Temp" not in [r.name for r in exported.roles]


def test_every_item_removed_still_exports_and_reloads() -> None:
    # An organization with no container, role, booking type or category left
    # but one standard role is the smallest the screens leave.
    org2 = made_up.organization("bare")
    only = BookingCategory.objects.create(organization=org2, name="only")
    role = Role.objects.create(organization=org2, name="All")
    RoleCategory.objects.create(organization=org2, role=role, booking_category=only)
    org2.standard_role = role
    org2.save()
    manager = admin_in(org2)
    setup_admin.make_item(manager, BookingType, {"name": "temp"})
    (temp,) = BookingType.objects.of(org2).all()
    setup_admin.remove_item(manager, BookingType, temp.pk)
    assert not BookingType.objects.of(org2).exists()
    exported = setups.export(org2)
    assert exported.booking_types == ()
    same_lists(exported, setup_file.parse(setup_file.write(exported)))


def test_the_screens_refuse_what_the_setup_file_would() -> None:
    """The screens' rules are the file's: whatever they let through reads."""
    org, admin = fjordside_with_admin()
    for fields in (
        {"name": "x", "categories": []},
        {"name": " x", "categories": [1]},
    ):
        with pytest.raises((setup_admin.SetupRefusedError, ValueError)):
            setup_admin.make_role(admin, fields)
    same_lists(
        setups.export(org), setup_file.parse(setup_file.write(setups.export(org)))
    )


def test_the_made_up_organization_exports_after_edits_too(made: made_up.MadeUp) -> None:
    add_type(made, "Sauna")
    add_category(made, "Guest", icon="group")
    answer(
        call(
            made,
            "move",
            {"direction": "up"},
            kind=CATEGORIES,
            pk=BookingCategory.objects.of(made.organization).get(name="Guest").pk,
        )
    )
    exported = setups.export(made.organization)
    assert [c.name for c in exported.booking_categories] == ["Guest", "private"]
    same_lists(exported, setup_file.parse(setup_file.write(exported)))


def test_what_the_places_answer_is_fixed(made: made_up.MadeUp) -> None:
    assert set(answer(call(made, "types"))) == {
        "booking_types",
        "booking_categories",
        "icons",
    }
    assert set(answer(call(made, "roles"))) == {"roles", "booking_categories"}


def test_the_file_reader_shares_the_name_rule_with_the_screens() -> None:
    from tests.test_setup_file import SMALLEST

    def faults(text: str) -> list[str]:
        try:
            setup_file.validate(setup_file.parse(text))
        except setup_file.SetupError as error:
            return error.faults
        return []

    renamed = SMALLEST.replace('"private"', f'"{"x" * 201}"')
    assert any("at most 200" in line for line in faults(renamed))
    clash = SMALLEST + '\n[[booking_categories]]\nname = "Private"\n'
    assert any("whatever their case" in line for line in faults(clash))
    # What the screens accept, the file reads.
    assert faults(SMALLEST.replace('"private"', f'"{"x" * 200}"')) == []
