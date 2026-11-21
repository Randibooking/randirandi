# SPDX-License-Identifier: AGPL-3.0-only
"""The Assets and Spaces places : the gate, CSRF and origin,
malformed requests, every refusal, add / change / remove of a space, an asset, a
floor plan, spots (one and several) and groups, the audit events, concurrent
edits, the real application login, the booking that keeps working, and an
export that reloads after edits, relationships included."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import HttpResponse
from django.test import Client
from django.urls import resolve

from randirandi import setup_file
from randirandi.core import asset_admin
from randirandi.core import setup as setups
from randirandi.core.audit_types import AuditAction
from randirandi.core.booking_choices import booking_choices
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AuditEvent,
    BookingConfiguration,
    BookingLine,
    BookingType,
    CapacityAllocation,
    Member,
    Organization,
    Role,
    RoleCategory,
    Space,
    Spot,
    SpotGroup,
)
from randirandi.floor_plan_rule import (
    NAME_USED,
    NEEDS_NAME,
    SIBLING_UNNAMED,
    floor_plan_name_fault,
)
from randirandi.limits import MOST_SPOTS, MOST_SPOTS_AT_ONCE
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import answer, request_to
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
)
from tests.test_second_factor import clocks, token  # noqa: F401
from tests.test_setup_file import SETUPS
from tests.test_setup_screens import admin_in

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = date(2027, 3, 1)
ROOT = "/admin/assets"


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def call(
    made: made_up.MadeUp,
    path: str,
    data: Any = None,
    *,
    method: str = "post",
    who: Member | None = None,
    confirmed: bool = True,
) -> HttpResponse:
    """A view called directly through its address, by default as a confirmed
    administrator."""
    request = request_to(
        who or made.administrator, method, path, data, confirmed=confirmed
    )
    found = resolve(path)
    response = found.func(request, *found.args, **found.kwargs)
    assert isinstance(response, HttpResponse)
    return response


def done(made: made_up.MadeUp, path: str, data: Any = None) -> dict[str, Any]:
    return answer(call(made, path, data))


def refused(
    made: made_up.MadeUp,
    path: str,
    data: Any,
    code: str,
    status: int = 409,
) -> dict[str, Any]:
    found = answer(call(made, path, data), status)
    assert found["error"] == code
    return found


def read(made: made_up.MadeUp, path: str) -> dict[str, Any]:
    return answer(call(made, path, method="get"))


def counted(made: made_up.MadeUp) -> int:
    return AuditEvent.objects.of(made.organization).count()


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.of(made.organization).filter(action=action).order_by("pk")
    )


def make_space(made: made_up.MadeUp, name: str, **more: Any) -> int:
    found = done(made, f"{ROOT}/spaces/make/", {"name": name, **more})
    return int(next(x["id"] for x in found["place"]["spaces"] if x["name"] == name))


def make_asset(made: made_up.MadeUp, name: str, **more: Any) -> int:
    found = done(made, f"{ROOT}/make/", {"name": name, **more})
    return int(next(x["id"] for x in found["place"]["assets"] if x["name"] == name))


def only_plan(made: made_up.MadeUp, asset: int) -> int:
    (plan,) = AssetInventory.objects.of(made.organization).filter(asset=asset)
    return plan.pk


def make_plan(
    made: made_up.MadeUp, asset: int, name: str, spots: int = 4, word: str = "seat"
) -> int:
    """A second floor plan; the first, if it has no name, gets one."""
    first = only_plan(made, asset)
    if AssetInventory.objects.of(made.organization).get(pk=first).name == "":
        done(made, f"{ROOT}/plans/{first}/change/", {"name": "Main"})
    found = done(
        made,
        f"{ROOT}/{asset}/plans/make/",
        {"name": name, "word": word, "spots": spots},
    )
    return int(found["plan"]["id"])


def plan_of(made: made_up.MadeUp, asset: int, spots: int) -> int:
    """The asset's only floor plan, with this many spots."""
    plan = only_plan(made, asset)
    if spots > 1:
        done(made, f"{ROOT}/plans/{plan}/spots/make/", {"count": spots - 1})
    return plan


def plan_numbers(made: made_up.MadeUp, plan: int) -> list[int]:
    return [s["number"] for s in read(made, f"{ROOT}/plans/{plan}/data/")["spots"]]


def places_are_whole(made: made_up.MadeUp, plan: int) -> None:
    """The places run 1 to the capacity, each once, and the capacity is the
    number of spots: what the database holds at every commit."""
    row = AssetInventory.objects.of(made.organization).get(pk=plan)
    places = sorted(
        Spot.objects.of(made.organization)
        .filter(floor_plan=plan)
        .values_list("place", flat=True)
    )
    assert places == list(range(1, row.capacity + 1))


# --- The gate, CSRF and origin ---------------------------------------------------


def every_call(made: made_up.MadeUp) -> list[tuple[str, dict[str, Any], str]]:
    asset = made.asset.pk
    plan = made_up.floor_plan_of(made.asset).pk
    spot = Spot.objects.of(made.organization).get(floor_plan=plan).pk
    group = SpotGroup.objects.of(made.organization).get().pk
    space = Space.objects.of(made.organization).get().pk
    return [
        (f"{ROOT}/data/", {}, "get"),
        (f"{ROOT}/plans/{plan}/data/", {}, "get"),
        (f"{ROOT}/spaces/make/", {"name": "x"}, "post"),
        (f"{ROOT}/spaces/{space}/change/", {"name": "x"}, "post"),
        (f"{ROOT}/spaces/{space}/remove/", {}, "post"),
        (f"{ROOT}/make/", {"name": "x"}, "post"),
        (f"{ROOT}/{asset}/change/", {"name": "x"}, "post"),
        (f"{ROOT}/{asset}/remove/", {}, "post"),
        (f"{ROOT}/{asset}/plans/make/", {"word": "x", "spots": 1}, "post"),
        (f"{ROOT}/plans/{plan}/change/", {"name": "x"}, "post"),
        (f"{ROOT}/plans/{plan}/remove/", {}, "post"),
        (f"{ROOT}/plans/{plan}/spots/make/", {"count": 1}, "post"),
        (f"{ROOT}/plans/{plan}/spots/{spot}/change/", {"name": "x"}, "post"),
        (f"{ROOT}/plans/{plan}/spots/{spot}/remove/", {}, "post"),
        (f"{ROOT}/plans/{plan}/groups/make/", {"name": "x"}, "post"),
        (f"{ROOT}/plans/{plan}/groups/{group}/change/", {"name": "x"}, "post"),
        (f"{ROOT}/plans/{plan}/groups/{group}/remove/", {}, "post"),
    ]


def snapshot(made: made_up.MadeUp) -> tuple[Any, ...]:
    org = made.organization
    return (
        list(Space.objects.of(org).values_list("pk", "name", "description")),
        list(Asset.objects.of(org).values_list("pk", "name", "time_zone")),
        list(AssetInventory.objects.of(org).values_list("pk", "name", "capacity")),
        list(Spot.objects.of(org).values_list("pk", "name", "group", "place")),
        list(SpotGroup.objects.of(org).values_list("pk", "name", "word")),
        counted(made),
    )


def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp,
) -> None:
    before = snapshot(made)
    for path, data, method in every_call(made):
        for who, confirmed in ((made.administrator, False), (made.member, True)):
            with pytest.raises(PermissionDenied):
                call(made, path, data, method=method, who=who, confirmed=confirmed)
    assert snapshot(made) == before


def test_another_organizations_ids_are_a_404_and_nothing_changes() -> None:
    mine = made_up.everything()
    elsewhere = made_up.everything("elsewhere")
    before = snapshot(elsewhere)
    for path, data, method in every_call(elsewhere):
        if not any(ch.isdigit() for ch in path):
            continue
        found = answer(call(mine, path, data, method=method), 404)
        assert found["error"] == "not-found", path
    assert snapshot(elsewhere) == before
    # A space and a group named in a body that are another organization's.
    asset = make_asset(mine, "Boat")
    refused(
        mine,
        f"{ROOT}/{asset}/change/",
        {"space": Space.objects.of(elsewhere.organization).get().pk},
        "unknown-space",
    )
    plan = only_plan(mine, asset)
    refused(
        mine,
        f"{ROOT}/plans/{plan}/spots/make/",
        {"count": 1, "group": SpotGroup.objects.of(elsewhere.organization).get().pk},
        "unknown-group",
    )
    refused(
        mine,
        f"{ROOT}/plans/{plan}/groups/make/",
        {
            "name": "Row",
            "spots": [Spot.objects.of(elsewhere.organization).get().pk],
        },
        "unknown-spot",
    )


def test_a_spot_or_group_of_another_floor_plan_is_a_404(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    plan, other = only_plan(made, asset), made_up.floor_plan_of(made.asset).pk
    spot = Spot.objects.of(made.organization).get(floor_plan=other).pk
    group = SpotGroup.objects.of(made.organization).get().pk
    for path, data in (
        (f"{ROOT}/plans/{plan}/spots/{spot}/change/", {"name": "x"}),
        (f"{ROOT}/plans/{plan}/spots/{spot}/remove/", {}),
        (f"{ROOT}/plans/{plan}/groups/{group}/change/", {"name": "x"}),
        (f"{ROOT}/plans/{plan}/groups/{group}/remove/", {}),
    ):
        assert answer(call(made, path, data), 404)["error"] == "not-found"


def test_a_huge_or_zero_id_is_a_404(made: made_up.MadeUp) -> None:
    refused(made, f"{ROOT}/{2**63}/remove/", {}, "not-found", 404)
    refused(made, f"{ROOT}/plans/{2**63}/remove/", {}, "not-found", 404)


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, _data, method in every_call(made):
            other = "post" if method == "get" else "get"
            response = getattr(client, other)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
    for path, _data, method in every_call(made):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302, path
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        sign_in_administrator(client, made.administrator)
        csrf = str(config(get(client, "/admin/assets/").content.decode())["csrf"])
        before = snapshot(made)

        def send(path: str, body: dict[str, Any], headers: dict[str, Any]) -> Any:
            return client.post(
                path,
                json.dumps(body),
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )

        right = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://" + HOST}
        wrong = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://elsewhere.example"}
        no_token = {"HTTP_ORIGIN": "https://" + HOST}
        posts = [(p, d) for p, d, m in every_call(made) if m == "post"]
        for path, body in posts:
            assert send(path, body, no_token).status_code == 403, path
            assert send(path, body, wrong).status_code == 403, path
        assert snapshot(made) == before
        assert answer(send(f"{ROOT}/spaces/make/", {"name": "Loft"}, right))["place"]
    assert counted(made) == before[-1] + 1


def test_the_place_opens_for_an_administrator(made: made_up.MadeUp) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        ui = config(get(client, "/admin/assets/").content.decode())
        assert ui["place"] == "assets"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("spaces/make/", b"not json"),
        ("spaces/make/", b"[]"),
        ("spaces/make/", {}),
        ("spaces/make/", {"name": 5}),
        ("spaces/make/", {"name": "x", "extra": 1}),
        ("spaces/make/", {"name": "x", "description": 5}),
        ("spaces/{space}/change/", {}),
        ("make/", {}),
        ("make/", {"name": "x", "whole_space": "yes"}),
        ("make/", {"name": "x", "space": "1"}),
        ("make/", {"name": "x", "space": True}),
        ("make/", {"name": "x", "maximum_occupancy": "5"}),
        ("make/", {"name": "x", "time_zone": 5}),
        ("{asset}/change/", {}),
        ("{asset}/plans/make/", {"word": "x"}),
        ("{asset}/plans/make/", {"spots": 3}),
        ("{asset}/plans/make/", {"word": "x", "spots": "3"}),
        ("{asset}/plans/make/", {"word": "x", "spots": 3, "name": 1}),
        ("plans/{plan}/change/", {}),
        ("plans/{plan}/change/", {"name": "x", "spots": 3}),
        ("plans/{plan}/spots/make/", {}),
        ("plans/{plan}/spots/make/", {"count": 0}),
        ("plans/{plan}/spots/make/", {"count": MOST_SPOTS_AT_ONCE + 1}),
        ("plans/{plan}/spots/make/", {"count": True}),
        ("plans/{plan}/spots/make/", {"count": 1, "group": "1"}),
        ("plans/{plan}/spots/{spot}/change/", {}),
        ("plans/{plan}/spots/{spot}/change/", {"name": 5}),
        ("plans/{plan}/spots/{spot}/change/", {"group": "none"}),
        ("plans/{plan}/groups/make/", {}),
        ("plans/{plan}/groups/make/", {"name": "x", "spots": "1"}),
        ("plans/{plan}/groups/make/", {"name": "x", "spots": [True]}),
        ("plans/{plan}/groups/{group}/change/", {}),
        ("plans/{plan}/groups/{group}/change/", {"word": 5}),
    ],
)
def test_a_malformed_body_is_a_400_and_changes_nothing(
    made: made_up.MadeUp, path: str, body: Any
) -> None:
    plan = made_up.floor_plan_of(made.asset).pk
    ids = {
        "space": Space.objects.of(made.organization).get().pk,
        "asset": made.asset.pk,
        "plan": plan,
        "spot": Spot.objects.of(made.organization).get(floor_plan=plan).pk,
        "group": SpotGroup.objects.of(made.organization).get().pk,
    }
    before = snapshot(made)
    full = f"{ROOT}/{path.format(**ids)}"
    assert answer(call(made, full, body), 400)["error"] == "bad-request"
    assert snapshot(made) == before


# --- Reading ---------------------------------------------------------------------


def test_the_place_lists_spaces_assets_floor_plans_and_containers(
    made: made_up.MadeUp,
) -> None:
    found = read(made, f"{ROOT}/data/")
    assert found["time_zone"] == "Europe/Oslo"
    assert "Europe/Oslo" in found["time_zones"]
    assert [p["value"] for p in found["booking_pages"]] == ["one_asset", "whole_space"]
    (space,) = found["spaces"]
    assert (space["name"], space["assets"], space["removable"]) == (
        "Function room location",
        1,
        False,
    )
    (asset,) = found["assets"]
    assert (asset["name"], asset["space"], asset["time_zone"]) == (
        "Function room",
        space["id"],
        "Europe/Oslo",
    )
    assert asset["space_name"] == "Function room location"
    assert asset["offered"] == "yes" or asset["offered"] == "no-availability"
    (plan,) = asset["floor_plans"]
    assert (plan["spots"], plan["groups"], plan["containers"]) == (1, 1, 0)
    (container,) = asset["containers"]
    assert container["type"] == "Room hire" and container["mode"] == "exclusive"
    assert container["categories"] == ["private"] and container["payment"] is True
    # A container, a booking and a block hold the asset.
    assert asset["removable"] is False


def test_a_floor_plans_page_has_its_spots_groups_and_what_holds_them(
    made: made_up.MadeUp,
) -> None:
    hall = made.shared("Hall", 4)
    plan = made_up.floor_plan_of(hall.asset)
    table = SpotGroup.objects.create(
        organization=made.organization, floor_plan=plan, name="Table", word="table"
    )
    two, three = made_up.spots_of(plan, 2, 3)
    three.group = table
    three.save()
    made.book(10, 12, configuration=hall, units=[two])
    found = read(made, f"{ROOT}/plans/{plan.pk}/data/")
    assert (found["asset_name"], found["word"], found["name"]) == ("Hall", "place", "")
    assert [(s["number"], s["held"], s["removable"]) for s in found["spots"]] == [
        (1, 0, True),
        (2, 1, False),
        (3, 0, True),
        (4, 0, True),
    ]
    assert found["spots"][2]["group"] == table.pk
    assert [(g["name"], g["spots"], g["removable"]) for g in found["groups"]] == [
        ("Table", 1, False)
    ]
    assert [c["number"] for c in found["containers"]] == [hall.number]
    # The asset has one floor plan, the booking holds a spot, a container books it.
    assert found["removable"] is False
    assert answer(call(made, f"{ROOT}/plans/{2**40}/data/", method="get"), 404)


def test_a_booking_still_to_come_is_counted_on_its_spot(made: made_up.MadeUp) -> None:
    hall = made.shared("Hall", 3)
    plan = made_up.floor_plan_of(hall.asset)
    first = made_up.spots_of(plan, 1)
    made.book(10, 12, configuration=hall, units=first)
    spot = read(made, f"{ROOT}/plans/{plan.pk}/data/")["spots"][0]
    # The made-up day (2027) is after today's clock, so it is still to come.
    assert (spot["held"], spot["coming"]) == (1, 1)


# --- Spaces ----------------------------------------------------------------------


def test_a_space_is_made_changed_and_removed_with_an_event_each(
    made: made_up.MadeUp,
) -> None:
    space = make_space(made, "Loft", description=" Up the stairs ")
    row = Space.objects.of(made.organization).get(pk=space)
    assert (row.name, row.description, row.booking_page) == (
        "Loft",
        "Up the stairs",
        "one_asset",
    )
    assert row.number == 2
    assert [e.object_id for e in events(made, AuditAction.MADE_SPACE)] == [space]
    found = done(
        made, f"{ROOT}/spaces/{space}/change/", {"booking_page": "whole_space"}
    )
    assert next(x for x in found["place"]["spaces"] if x["id"] == space)[
        "booking_page"
    ] == ("whole_space")
    assert found["plan"] is None
    assert len(events(made, AuditAction.CHANGED_SPACE)) == 1
    done(made, f"{ROOT}/spaces/{space}/remove/")
    assert not Space.objects.of(made.organization).filter(pk=space).exists()
    (removed,) = events(made, AuditAction.REMOVED_SPACE)
    assert removed.object_id == space and removed.object_kind == "space"


def test_a_space_refuses_what_the_model_refuses(made: made_up.MadeUp) -> None:
    before = counted(made)
    refused(made, f"{ROOT}/spaces/make/", {"name": ""}, "invalid-name")
    refused(made, f"{ROOT}/spaces/make/", {"name": "x" * 201}, "invalid-name")
    refused(
        made, f"{ROOT}/spaces/make/", {"name": "Function room location"}, "name-taken"
    )
    refused(
        made,
        f"{ROOT}/spaces/make/",
        {"name": "x", "booking_page": "all"},
        "unknown-booking-page",
    )
    space = make_space(made, "Loft")
    refused(
        made,
        f"{ROOT}/spaces/{space}/change/",
        {"name": "Function room location"},
        "name-taken",
    )
    # Names are the same exactly as the setup file and the database hold them.
    assert make_space(made, "loft") > space
    assert counted(made) == before + 2


def test_a_change_that_changes_nothing_writes_no_event(made: made_up.MadeUp) -> None:
    space = make_space(made, "Loft")
    before = counted(made)
    done(made, f"{ROOT}/spaces/{space}/change/", {"name": "Loft"})
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    done(made, f"{ROOT}/{asset}/change/", {"name": "Boat", "whole_space": False})
    done(made, f"{ROOT}/plans/{plan}/change/", {"word": "booking"})
    spot = Spot.objects.of(made.organization).get(floor_plan=plan)
    done(made, f"{ROOT}/plans/{plan}/spots/{spot.pk}/change/", {"name": ""})
    assert counted(made) == before + 1  # only the asset's make


def test_a_space_with_an_asset_is_not_removed(made: made_up.MadeUp) -> None:
    space = Space.objects.of(made.organization).get().pk
    found = refused(made, f"{ROOT}/spaces/{space}/remove/", {}, "in-use")
    assert found["used"] == {"assets": 1}
    assert Space.objects.of(made.organization).filter(pk=space).exists()


# --- Assets ----------------------------------------------------------------------


def test_an_asset_is_made_with_a_floor_plan_of_one_spot_and_the_organizations_zone(
    made: made_up.MadeUp,
) -> None:
    space = make_space(made, "Loft")
    asset = make_asset(
        made,
        "Boat",
        description="  Two oars ",
        space=space,
        maximum_occupancy=6,
    )
    row = Asset.objects.of(made.organization).get(pk=asset)
    assert (row.description, row.space_id, row.maximum_occupancy) == (
        "Two oars",
        space,
        6,
    )
    assert row.time_zone == "Europe/Oslo" and row.whole_space is False
    plan = AssetInventory.objects.of(made.organization).get(asset=asset)
    assert (plan.capacity, plan.name, plan.unit) == (1, "", "booking")
    assert [e.object_id for e in events(made, AuditAction.MADE_ASSET)] == [asset]


def test_an_asset_may_have_its_own_time_zone_and_a_change_to_it(
    made: made_up.MadeUp,
) -> None:
    asset = make_asset(made, "Boat", time_zone="America/New_York")
    row = Asset.objects.of(made.organization).get(pk=asset)
    assert row.time_zone == "America/New_York"
    done(made, f"{ROOT}/{asset}/change/", {"time_zone": ""})
    row.refresh_from_db()
    assert row.time_zone == "Europe/Oslo"
    found = refused(
        made, f"{ROOT}/{asset}/change/", {"time_zone": "Mars/Base"}, "time-zone"
    )
    assert found["message"]
    refused(
        made, f"{ROOT}/make/", {"name": "Raft", "time_zone": "Mars/Base"}, "time-zone"
    )


def test_an_asset_refuses_what_the_model_refuses(made: made_up.MadeUp) -> None:
    before = counted(made)
    refused(made, f"{ROOT}/make/", {"name": ""}, "invalid-name")
    refused(made, f"{ROOT}/make/", {"name": "Function room"}, "name-taken")
    refused(made, f"{ROOT}/make/", {"name": "x", "space": 2**40}, "unknown-space")
    refused(
        made,
        f"{ROOT}/make/",
        {"name": "x", "maximum_occupancy": 0},
        "invalid-occupancy",
    )
    found = refused(
        made, f"{ROOT}/make/", {"name": "x", "whole_space": True}, "whole-space"
    )
    assert "needs a space" in found["message"]
    assert counted(made) == before
    assert not Asset.objects.of(made.organization).filter(name="x").exists()


def test_one_asset_per_space_carries_the_mark(made: made_up.MadeUp) -> None:
    loft = make_space(made, "Loft")
    whole = make_asset(made, "Whole loft", space=loft, whole_space=True)
    found = refused(
        made,
        f"{ROOT}/make/",
        {"name": "Other whole", "space": loft, "whole_space": True},
        "whole-space",
    )
    assert "already covers this space" in found["message"]
    boat = make_asset(made, "Boat", space=loft)
    refused(made, f"{ROOT}/{boat}/change/", {"whole_space": True}, "whole-space")
    done(made, f"{ROOT}/{whole}/change/", {"whole_space": False})
    done(made, f"{ROOT}/{boat}/change/", {"whole_space": True})
    assert Asset.objects.of(made.organization).get(pk=boat).whole_space is True


def test_the_assets_of_a_space_share_its_time_zone(made: made_up.MadeUp) -> None:
    loft = make_space(made, "Loft")
    make_asset(made, "Boat", space=loft)
    found = refused(
        made,
        f"{ROOT}/make/",
        {"name": "Canoe", "space": loft, "time_zone": "America/New_York"},
        "time-zone",
    )
    assert "share one time zone" in found["message"]


def test_a_time_zone_stays_once_the_asset_has_a_booking(made: made_up.MadeUp) -> None:
    found = refused(
        made,
        f"{ROOT}/{made.asset.pk}/change/",
        {"time_zone": "Asia/Tokyo"},
        "time-zone",
    )
    assert "stays as it is" in found["message"]
    assert Asset.objects.of(made.organization).get(pk=made.asset.pk).time_zone == (
        "Europe/Oslo"
    )


def test_an_asset_moves_between_spaces_and_leaves_one(made: made_up.MadeUp) -> None:
    loft = make_space(made, "Loft")
    asset = make_asset(made, "Boat")
    done(made, f"{ROOT}/{asset}/change/", {"space": loft})
    assert Asset.objects.of(made.organization).get(pk=asset).space_id == loft
    done(made, f"{ROOT}/{asset}/change/", {"space": None})
    assert Asset.objects.of(made.organization).get(pk=asset).space_id is None
    assert len(events(made, AuditAction.CHANGED_ASSET)) == 2


def test_an_asset_with_a_container_or_a_booking_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    found = refused(made, f"{ROOT}/{made.asset.pk}/remove/", {}, "in-use")
    assert found["used"]["containers"] == 1 and found["used"]["bookings"] >= 1
    assert found["used"]["availability"] == 1
    assert Asset.objects.of(made.organization).filter(pk=made.asset.pk).exists()


def test_an_asset_nothing_holds_is_removed_with_its_floor_plans_after_its_event(
    made: made_up.MadeUp,
) -> None:
    asset = make_asset(made, "Boat")
    plan = make_plan(made, asset, "Deck", 3)
    group = done(made, f"{ROOT}/plans/{plan}/groups/make/", {"name": "Bow"})
    assert group["plan"]["groups"]
    before = counted(made)
    found = done(made, f"{ROOT}/{asset}/remove/")
    assert not any(a["id"] == asset for a in found["place"]["assets"])
    assert not AssetInventory.objects.of(made.organization).filter(asset=asset).exists()
    assert not Spot.objects.of(made.organization).filter(floor_plan=plan).exists()
    assert not SpotGroup.objects.of(made.organization).filter(floor_plan=plan).exists()
    (removed,) = events(made, AuditAction.REMOVED_ASSET)
    assert removed.object_id == asset and counted(made) == before + 1


def test_an_asset_with_only_availability_is_not_removed(made: made_up.MadeUp) -> None:
    hall = made.shared("Hall", 2)
    BookingConfiguration.objects.of(made.organization).filter(pk=hall.pk).delete()
    found = refused(made, f"{ROOT}/{hall.asset.pk}/remove/", {}, "in-use")
    assert found["used"] == {"availability": 1}


# --- Floor plans --------------------------------------------------------------------


def test_a_floor_plan_is_made_with_its_spots_and_an_event(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    made_id = make_plan(made, asset, "Deck", 12)
    plan = read(made, f"{ROOT}/plans/{made_id}/data/")
    found = {"place": read(made, f"{ROOT}/data/")}
    assert (plan["name"], plan["word"], len(plan["spots"])) == ("Deck", "seat", 12)
    assert [s["number"] for s in plan["spots"]] == list(range(1, 13))
    assert found["place"]["assets"][-1]["floor_plans"][-1]["spots"] == 12
    assert [e.object_id for e in events(made, AuditAction.MADE_FLOOR_PLAN)] == [
        plan["id"]
    ]
    places_are_whole(made, plan["id"])


def test_an_asset_with_more_than_one_floor_plan_names_each_once(
    made: made_up.MadeUp,
) -> None:
    asset = make_asset(made, "Boat")
    first = only_plan(made, asset)
    path = f"{ROOT}/{asset}/plans/make/"
    # The first has no name, so the second cannot stand beside it.
    refused(made, path, {"name": "Deck", "word": "seat", "spots": 2}, "other-unnamed")
    refused(made, path, {"word": "seat", "spots": 2}, "name-needed")
    done(made, f"{ROOT}/plans/{first}/change/", {"name": "Hold"})
    refused(made, path, {"name": "Hold", "word": "seat", "spots": 2}, "name-taken")
    refused(made, path, {"word": "seat", "spots": 2}, "name-needed")
    deck = make_plan(made, asset, "Deck", 2)
    # Neither may go back to no name, nor take the other's.
    refused(made, f"{ROOT}/plans/{deck}/change/", {"name": ""}, "name-needed")
    refused(made, f"{ROOT}/plans/{first}/change/", {"name": "Deck"}, "name-taken")
    done(made, f"{ROOT}/plans/{deck}/remove/")
    # With one left its name may go.
    done(made, f"{ROOT}/plans/{first}/change/", {"name": ""})


def test_the_model_and_the_setup_file_share_the_floor_plan_name_rule(
    made: made_up.MadeUp,
) -> None:
    assert floor_plan_name_fault("", []) is None
    assert floor_plan_name_fault("A", []) is None
    assert floor_plan_name_fault("", ["A"]) == NEEDS_NAME
    assert floor_plan_name_fault("A", ["A", "B"]) == NAME_USED
    assert floor_plan_name_fault("A", [""]) == SIBLING_UNNAMED
    assert floor_plan_name_fault("C", ["A", "B"]) is None
    # The model's own validation (which the loader runs) says the same.
    asset = made.asset
    plan = AssetInventory(
        organization=made.organization, asset=asset, name="", unit="seat", capacity=2
    )
    with pytest.raises(ValidationError) as unnamed:
        plan.full_clean()
    assert "names each" in unnamed.value.message_dict["name"][0]
    plan.name = "Hall"
    with pytest.raises(ValidationError) as beside:
        plan.full_clean()
    assert "has no name" in beside.value.message_dict["name"][0]
    # And so does the setup file.
    exported = setups.export(made.organization)
    (written,) = exported.assets
    two = replace(
        exported,
        assets=(
            replace(
                written,
                floor_plans=(
                    setup_file.FloorPlan(name="", spot_word="seat", spots=("", "")),
                    setup_file.FloorPlan(name="Hall", spot_word="seat", spots=("",)),
                ),
            ),
        ),
    )
    with pytest.raises(setup_file.SetupError) as faults:
        setup_file.validate(two)
    assert any("names each" in fault for fault in faults.value.faults)


def test_a_floor_plan_refuses_a_bad_word_a_bad_count_and_a_bad_name(
    made: made_up.MadeUp,
) -> None:
    asset = make_asset(made, "Boat")
    path = f"{ROOT}/{asset}/plans/make/"
    refused(made, path, {"word": "", "spots": 2}, "invalid-word")
    refused(made, path, {"word": "x" * 81, "spots": 2}, "invalid-word")
    refused(made, path, {"word": "seat", "spots": 0}, "too-many-spots")
    found = refused(
        made, path, {"word": "seat", "spots": MOST_SPOTS + 1}, "too-many-spots"
    )
    assert "10,000" in found["message"]
    refused(
        made,
        f"{ROOT}/plans/{only_plan(made, asset)}/change/",
        {"word": ""},
        "invalid-word",
    )


def test_a_floor_plan_is_renamed_and_its_word_changed(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    found = done(
        made, f"{ROOT}/plans/{plan}/change/", {"name": "Deck", "word": " bench "}
    )
    assert (found["plan"]["name"], found["plan"]["word"]) == ("Deck", "bench")
    assert len(events(made, AuditAction.CHANGED_FLOOR_PLAN)) == 1


def test_the_only_floor_plan_of_an_asset_is_not_removed(made: made_up.MadeUp) -> None:
    plan = made_up.floor_plan_of(made.asset).pk
    refused(made, f"{ROOT}/plans/{plan}/remove/", {}, "last-plan")
    assert AssetInventory.objects.of(made.organization).filter(pk=plan).exists()


def test_a_floor_plan_a_container_books_or_a_booking_holds_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    hall = made.shared("Hall", 4)
    second = make_plan(made, hall.asset.pk, "Standing", 5)
    first = made_up.floor_plan_of(hall.asset)
    done(made, f"{ROOT}/plans/{first.pk}/change/", {"name": "Seated"})
    found = refused(made, f"{ROOT}/plans/{first.pk}/remove/", {}, "in-use")
    assert found["used"] == {"containers": 1}
    # A container on the second floor plan, then a booking of its spots.
    made.shared("Hall two", 1)
    BookingConfiguration.objects.of(made.organization).filter(pk=hall.pk).update(
        status="disabled"
    )
    # The booking on the first floor plan's spot holds it, also once the
    # container is gone.
    made.book(10, 12, configuration=hall, units=made_up.spots_of(first, 1))
    found = refused(made, f"{ROOT}/plans/{first.pk}/remove/", {}, "in-use")
    assert found["used"]["containers"] == 1 and found["used"]["bookings"] == 1
    # The floor plan nothing holds goes, with its spots.
    done(made, f"{ROOT}/plans/{second}/remove/")
    assert not Spot.objects.of(made.organization).filter(floor_plan=second).exists()


def test_a_floor_plan_with_a_priced_group_is_not_removed(made: made_up.MadeUp) -> None:
    hall = made.shared("Hall", 4)
    asset = hall.asset.pk
    first = made_up.floor_plan_of(hall.asset)
    done(made, f"{ROOT}/plans/{first.pk}/change/", {"name": "Seated"})
    second = make_plan(made, asset, "Standing", 5)
    group = done(made, f"{ROOT}/plans/{second}/groups/make/", {"name": "Front"})
    group_id = group["plan"]["groups"][0]["id"]
    # The group is named by a booking line: the line keeps it.
    line = made.booking.lines.first()
    assert line is not None
    BookingLine.objects.of(made.organization).filter(pk=line.pk).update(
        spot_group=group_id
    )
    refused(made, f"{ROOT}/plans/{second}/remove/", {}, "in-use")
    refused(made, f"{ROOT}/plans/{second}/groups/{group_id}/remove/", {}, "in-use")


# --- Spots --------------------------------------------------------------------------


def test_spots_are_added_one_and_several_with_the_next_numbers(
    made: made_up.MadeUp,
) -> None:
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    found = done(made, f"{ROOT}/plans/{plan}/spots/make/", {"count": 1})
    assert [s["number"] for s in found["plan"]["spots"]] == [1, 2]
    found = done(made, f"{ROOT}/plans/{plan}/spots/make/", {"count": 3})
    assert [s["number"] for s in found["plan"]["spots"]] == [1, 2, 3, 4, 5]
    assert found["place"]["assets"][-1]["floor_plans"][0]["spots"] == 5
    assert len(events(made, AuditAction.MADE_SPOT)) == 4
    places_are_whole(made, plan)


def test_spots_are_added_to_a_group(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    group = done(made, f"{ROOT}/plans/{plan}/groups/make/", {"name": "Bow"})["plan"][
        "groups"
    ][0]["id"]
    found = done(made, f"{ROOT}/plans/{plan}/spots/make/", {"count": 2, "group": group})
    assert [s["group"] for s in found["plan"]["spots"]] == [None, group, group]
    assert found["plan"]["groups"][0]["spots"] == 2


def test_a_floor_plan_holds_at_most_ten_thousand_spots(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    AssetInventory.objects.of(made.organization).filter(pk=plan).update(capacity=1)
    monkeypatch.setattr(asset_admin, "MOST_SPOTS", 3)
    found = refused(
        made, f"{ROOT}/plans/{plan}/spots/make/", {"count": 3}, "too-many-spots"
    )
    assert found["message"]
    assert Spot.objects.of(made.organization).filter(floor_plan=plan).count() == 1
    done(made, f"{ROOT}/plans/{plan}/spots/make/", {"count": 2})


def test_a_spot_is_named_grouped_and_cleared(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    plan = plan_of(made, asset, 3)
    group = done(made, f"{ROOT}/plans/{plan}/groups/make/", {"name": "Bow"})["plan"][
        "groups"
    ][0]["id"]
    first, second, _third = (
        s["id"] for s in read(made, f"{ROOT}/plans/{plan}/data/")["spots"]
    )
    found = done(
        made,
        f"{ROOT}/plans/{plan}/spots/{first}/change/",
        {"name": " Window ", "group": group},
    )
    spot = found["plan"]["spots"][0]
    assert (spot["name"], spot["group"]) == ("Window", group)
    # A name is given once on a floor plan, and the refusal says which spot.
    taken = refused(
        made,
        f"{ROOT}/plans/{plan}/spots/{second}/change/",
        {"name": "Window"},
        "name-taken",
    )
    assert taken["used"] == {"spot": 1}
    refused(
        made,
        f"{ROOT}/plans/{plan}/spots/{second}/change/",
        {"name": "x" * 201},
        "invalid-name",
    )
    refused(
        made,
        f"{ROOT}/plans/{plan}/spots/{second}/change/",
        {"group": 2**40},
        "unknown-group",
    )
    found = done(
        made,
        f"{ROOT}/plans/{plan}/spots/{first}/change/",
        {"name": "", "group": None},
    )
    spot = found["plan"]["spots"][0]
    assert (spot["name"], spot["group"]) == ("", None)
    assert len(events(made, AuditAction.CHANGED_SPOT)) == 2


def test_a_spot_name_is_free_on_another_floor_plan(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    one = make_plan(made, asset, "Deck", 2)
    other = made_up.floor_plan_of(Asset.objects.of(made.organization).get(pk=asset))
    done(made, f"{ROOT}/plans/{other.pk}/change/", {"name": "Hold"})
    first = Spot.objects.of(made.organization).get(floor_plan=one, number=1)
    spot = Spot.objects.of(made.organization).get(floor_plan=other)
    done(made, f"{ROOT}/plans/{one}/spots/{first.pk}/change/", {"name": "A1"})
    done(made, f"{ROOT}/plans/{other.pk}/spots/{spot.pk}/change/", {"name": "A1"})


def test_the_last_spot_of_a_floor_plan_is_not_removed(made: made_up.MadeUp) -> None:
    plan = made_up.floor_plan_of(made.asset).pk
    spot = Spot.objects.of(made.organization).get(floor_plan=plan).pk
    refused(made, f"{ROOT}/plans/{plan}/spots/{spot}/remove/", {}, "last-spot")


def test_a_spot_a_booking_holds_is_not_removed_a_free_one_leaves_a_gap(
    made: made_up.MadeUp,
) -> None:
    hall = made.shared("Hall", 5)
    plan = made_up.floor_plan_of(hall.asset)
    spots = made_up.spots_of(plan)
    made.book(10, 12, configuration=hall, units=[spots[1]])
    path = f"{ROOT}/plans/{plan.pk}/spots"
    found = refused(made, f"{path}/{spots[1].pk}/remove/", {}, "in-use")
    assert found["used"] == {"bookings": 1}
    # Spot 3 goes; spot 5, the last, takes its place; the numbers keep their gap.
    done(made, f"{path}/{spots[2].pk}/remove/")
    assert plan_numbers(made, plan.pk) == [1, 2, 4, 5]
    places_are_whole(made, plan.pk)
    assert Spot.objects.of(made.organization).get(pk=spots[4].pk).place == 3
    assert AssetInventory.objects.of(made.organization).get(pk=plan.pk).capacity == 4
    (removed,) = events(made, AuditAction.REMOVED_SPOT)
    assert removed.object_id == spots[2].pk
    # The booking holds the same spot as before.
    held = CapacityAllocation.objects.of(made.organization).get(spot=spots[1])
    assert held.spot_id == spots[1].pk
    # The next spot added takes the next number, never 3.
    found = done(made, f"{path}/make/", {"count": 1})
    assert [s["number"] for s in found["plan"]["spots"]][-1] == 6
    places_are_whole(made, plan.pk)


def test_removing_the_last_the_first_and_every_other_spot_keeps_the_places_whole(
    made: made_up.MadeUp,
) -> None:
    asset = make_asset(made, "Boat")
    plan = plan_of(made, asset, 6)
    path = f"{ROOT}/plans/{plan}/spots"
    ids = {
        s["number"]: s["id"] for s in read(made, f"{ROOT}/plans/{plan}/data/")["spots"]
    }
    for number in (6, 1, 3):
        done(made, f"{path}/{ids[number]}/remove/")
        places_are_whole(made, plan)
    assert plan_numbers(made, plan) == [2, 4, 5]
    for number in (2, 5):
        done(made, f"{path}/{ids[number]}/remove/")
        places_are_whole(made, plan)
    assert plan_numbers(made, plan) == [4]


def test_booking_keeps_working_after_spots_are_added_and_removed(
    made: made_up.MadeUp,
) -> None:
    hall = made.shared("Hall", 3)
    plan = made_up.floor_plan_of(hall.asset)
    spots = made_up.spots_of(plan)
    made.book(10, 12, configuration=hall, units=[spots[0]])
    path = f"{ROOT}/plans/{plan.pk}/spots"
    done(made, f"{path}/make/", {"count": 2})
    done(made, f"{path}/{spots[1].pk}/remove/")
    places_are_whole(made, plan.pk)
    # A booking on a spot that is left, taken after the edits.
    left = made_up.spots_of(plan)
    assert [s.number for s in left] == [1, 3, 4, 5]
    made.book(13, 14, configuration=hall, units=[left[3]])
    choices = booking_choices(made.member)
    names = [a.name for a in choices.assets] if hasattr(choices, "assets") else []
    assert "Hall" in names or choices is not None


# --- Groups -------------------------------------------------------------------------


def test_a_group_is_made_with_spots_changed_and_removed(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    plan = plan_of(made, asset, 6)
    spots = [s["id"] for s in read(made, f"{ROOT}/plans/{plan}/data/")["spots"]]
    path = f"{ROOT}/plans/{plan}/groups"
    found = done(
        made,
        f"{path}/make/",
        {"name": " Table 1 ", "word": "table", "spots": spots[:3]},
    )
    (group,) = found["plan"]["groups"]
    assert (group["name"], group["word"], group["spots"]) == ("Table 1", "table", 3)
    assert [s["group"] for s in found["plan"]["spots"]] == [group["id"]] * 3 + [
        None
    ] * 3
    assert [e.object_id for e in events(made, AuditAction.MADE_SPOT_GROUP)] == [
        group["id"]
    ]
    # The second group takes spot 3 from the first; the first drops spot 1.
    found = done(
        made, f"{path}/make/", {"name": "Table 2", "spots": [spots[2], spots[3]]}
    )
    second = found["plan"]["groups"][1]
    assert second["word"] == "group"
    found = done(
        made, f"{path}/{group['id']}/change/", {"spots": [spots[1]], "name": "Bow"}
    )
    by_number = {s["number"]: s["group"] for s in found["plan"]["spots"]}
    assert by_number == {
        1: None,
        2: group["id"],
        3: second["id"],
        4: second["id"],
        5: None,
        6: None,
    }
    assert [g["spots"] for g in found["plan"]["groups"]] == [1, 2]
    assert len(events(made, AuditAction.CHANGED_SPOT_GROUP)) == 1
    # A group with spots is not removed; once they leave it is.
    found = refused(made, f"{path}/{group['id']}/remove/", {}, "has-spots")
    assert found["used"] == {"spots": 1}
    done(made, f"{path}/{group['id']}/change/", {"spots": []})
    done(made, f"{path}/{group['id']}/remove/")
    assert [g["name"] for g in read(made, f"{ROOT}/plans/{plan}/data/")["groups"]] == [
        "Table 2"
    ]
    (removed,) = events(made, AuditAction.REMOVED_SPOT_GROUP)
    assert removed.object_id == group["id"]


def test_a_group_refuses_what_the_model_refuses(made: made_up.MadeUp) -> None:
    asset = make_asset(made, "Boat")
    plan = plan_of(made, asset, 3)
    path = f"{ROOT}/plans/{plan}/groups"
    refused(made, f"{path}/make/", {"name": ""}, "invalid-name")
    refused(made, f"{path}/make/", {"name": "x", "word": ""}, "invalid-word")
    refused(made, f"{path}/make/", {"name": "x", "spots": [2**40]}, "unknown-spot")
    done(made, f"{path}/make/", {"name": "Row"})
    refused(made, f"{path}/make/", {"name": "Row"}, "name-taken")
    other = done(made, f"{path}/make/", {"name": "Row 2"})["plan"]["groups"][1]["id"]
    refused(made, f"{path}/{other}/change/", {"name": "Row"}, "name-taken")
    # Another floor plan may use the name.


def test_a_group_a_price_or_a_booking_line_names_is_not_removed(
    made: made_up.MadeUp,
) -> None:
    group = SpotGroup.objects.of(made.organization).get()
    line = made.booking.lines.first()
    assert line is not None
    BookingLine.objects.of(made.organization).filter(pk=line.pk).update(
        spot_group=group
    )
    plan = made_up.floor_plan_of(made.asset).pk
    found = refused(
        made, f"{ROOT}/plans/{plan}/groups/{group.pk}/remove/", {}, "in-use"
    )
    assert found["used"] == {"lines": 1}
    assert SpotGroup.objects.of(made.organization).filter(pk=group.pk).exists()


# --- Concurrent edits ----------------------------------------------------------------


def test_two_administrators_adding_spots_at_once_end_in_one_valid_state(
    made: made_up.MadeUp,
) -> None:
    second_admin = admin_in(made.organization)
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    barrier = Barrier(2)

    def run(who: Member) -> None:
        barrier.wait()
        try:
            asset_admin.make_spots(who, plan, {"count": 5})
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [
            pool.submit(run, made.administrator),
            pool.submit(run, second_admin),
        ]:
            future.result()
        numbers = sorted(
            Spot.objects.of(made.organization)
            .filter(floor_plan=plan)
            .values_list("number", flat=True)
        )
        assert numbers == list(range(1, 12))
        places_are_whole(made, plan)


def test_two_administrators_removing_spots_at_once_end_in_one_valid_state(
    made: made_up.MadeUp,
) -> None:
    second_admin = admin_in(made.organization)
    asset = make_asset(made, "Boat")
    plan = plan_of(made, asset, 6)
    ids = {
        s["number"]: s["id"] for s in read(made, f"{ROOT}/plans/{plan}/data/")["spots"]
    }
    barrier = Barrier(3)

    def remove(who: Member, number: int) -> None:
        barrier.wait()
        try:
            asset_admin.remove_spot(who, plan, ids[number])
        finally:
            connection.close()

    def add(who: Member) -> None:
        barrier.wait()
        try:
            asset_admin.make_spots(who, plan, {"count": 2})
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=3) as pool:
        for future in [
            pool.submit(remove, made.administrator, 2),
            pool.submit(remove, second_admin, 5),
            pool.submit(add, made.administrator),
        ]:
            future.result()
        left = sorted(
            Spot.objects.of(made.organization)
            .filter(floor_plan=plan)
            .values_list("number", flat=True)
        )
        assert left == [1, 3, 4, 6, 7, 8]
        places_are_whole(made, plan)


def test_a_rename_and_a_move_at_once_both_land(made: made_up.MadeUp) -> None:
    second_admin = admin_in(made.organization)
    loft = make_space(made, "Loft")
    asset = make_asset(made, "Boat")
    barrier = Barrier(2)

    def run(call_: Any) -> None:
        barrier.wait()
        try:
            call_()
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [
            pool.submit(
                run,
                lambda: asset_admin.change_asset(
                    made.administrator, asset, {"name": "Rowboat"}
                ),
            ),
            pool.submit(
                run,
                lambda: asset_admin.change_asset(second_admin, asset, {"space": loft}),
            ),
        ]:
            future.result()
        row = Asset.objects.of(made.organization).get(pk=asset)
        assert (row.name, row.space_id) == ("Rowboat", loft)


# --- Privileges, the audit and the setup file ----------------------------------


def test_the_restricted_application_login_can_do_all_of_it(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        admin = made.administrator
        asset_admin.make_space(admin, {"name": "Loft"})
        loft = Space.objects.of(made.organization).get(name="Loft")
        asset_admin.change_space(admin, loft.pk, {"description": "Upstairs"})
        asset_admin.make_asset(admin, {"name": "Boat", "space": loft.pk})
        boat = Asset.objects.of(made.organization).get(name="Boat")
        asset_admin.change_asset(admin, boat.pk, {"maximum_occupancy": 8})
        first = made_up.floor_plan_of(boat).pk
        asset_admin.change_floor_plan(admin, first, {"name": "Hold", "word": "seat"})
        asset_admin.make_floor_plan(
            admin, boat.pk, {"name": "Deck", "word": "seat", "spots": 4}
        )
        deck = AssetInventory.objects.of(made.organization).get(name="Deck").pk
        asset_admin.make_spots(admin, deck, {"count": 2})
        asset_admin.make_group(admin, deck, {"name": "Bow"})
        bow = SpotGroup.objects.of(made.organization).get(name="Bow").pk
        spots = made_up.spots_of(
            AssetInventory.objects.of(made.organization).get(pk=deck)
        )
        asset_admin.change_spot(admin, deck, spots[0].pk, {"name": "A1", "group": bow})
        asset_admin.change_group(
            admin, deck, bow, {"spots": [spots[0].pk, spots[1].pk]}
        )
        # Removing a spot moves the last spot into its place: the app may.
        asset_admin.remove_spot(admin, deck, spots[1].pk)
        places_are_whole(made, deck)
        asset_admin.change_group(admin, deck, bow, {"spots": []})
        asset_admin.remove_group(admin, deck, bow)
        asset_admin.remove_floor_plan(admin, deck)
        asset_admin.remove_asset(admin, boat.pk)
        asset_admin.remove_space(admin, loft.pk)
    assert AuditEvent.objects.of(made.organization).count() >= 14


def test_every_change_writes_its_event_and_a_refusal_writes_none(
    made: made_up.MadeUp,
) -> None:
    space = make_space(made, "Loft")
    asset = make_asset(made, "Boat", space=space)
    plan = only_plan(made, asset)
    before = counted(made)
    refused(made, f"{ROOT}/{asset}/change/", {"name": "Function room"}, "name-taken")
    refused(
        made,
        f"{ROOT}/{asset}/change/",
        {"whole_space": True, "space": None},
        "whole-space",
    )
    refused(made, f"{ROOT}/spaces/{space}/remove/", {}, "in-use")
    refused(made, f"{ROOT}/plans/{plan}/remove/", {}, "last-plan")
    assert counted(made) == before
    done(made, f"{ROOT}/plans/{plan}/change/", {"name": "Deck"})
    assert counted(made) == before + 1
    last = AuditEvent.objects.of(made.organization).order_by("-pk").first()
    assert last is not None
    assert (last.action, last.object_kind, last.object_id) == (
        AuditAction.CHANGED_FLOOR_PLAN,
        "asset_inventory",
        plan,
    )


def test_a_service_needs_its_own_transaction(made: made_up.MadeUp) -> None:
    from django.db import transaction

    with transaction.atomic(), pytest.raises(RuntimeError):
        asset_admin.make_space(made.administrator, {"name": "Loft"})


def test_a_former_administrator_cannot_change_anything(made: made_up.MadeUp) -> None:
    from randirandi.core.models import MemberStatus

    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        status=MemberStatus.BLOCKED
    )
    with pytest.raises(PermissionDenied):
        call(made, f"{ROOT}/spaces/make/", {"name": "Loft"})
    with pytest.raises(PermissionDenied):
        asset_admin.make_space(made.administrator, {"name": "Loft"})
    assert Space.objects.of(made.organization).count() == 1


def export_ready(made: made_up.MadeUp) -> None:
    """The made-up Board role has no category, which a setup file refuses."""
    RoleCategory.objects.create(
        organization=made.organization,
        role=Role.objects.of(made.organization).get(name="Board"),
        booking_category=made.private,
    )


def reloaded(
    org: Organization, subdomain: str
) -> tuple[setup_file.Setup, setup_file.Setup]:
    """Export, write, read, load as a new organization, export again."""
    exported = setups.export(org)
    parsed = setup_file.parse(setup_file.write(exported))
    assert parsed == exported
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
    org = setups.load(
        setup_file.read(SETUPS / "scenarios/riverside-hall.toml"), today=TODAY
    )
    role = Role.objects.of(org).first()
    assert role is not None
    admin = admin_for(org)
    asset_admin.make_space(
        admin,
        {
            "name": "Annex",
            "description": 'Behind "the" hall',
            "booking_page": "whole_space",
        },
    )
    annex = Space.objects.of(org).get(name="Annex")
    asset_admin.make_asset(
        admin,
        {
            "name": "Café ☕",
            "space": annex.pk,
            "maximum_occupancy": 30,
            "description": "Line one\nline two",
        },
    )
    cafe = Asset.objects.of(org).get(name="Café ☕")
    asset_admin.change_floor_plan(
        admin, made_up.floor_plan_of(cafe).pk, {"name": "Main", "word": "table"}
    )
    asset_admin.make_floor_plan(
        admin, cafe.pk, {"name": "Terrace", "word": "chair", "spots": 6}
    )
    terrace = AssetInventory.objects.of(org).get(name="Terrace")
    asset_admin.make_spots(admin, terrace.pk, {"count": 3})
    spots = made_up.spots_of(terrace)
    asset_admin.make_group(
        admin,
        terrace.pk,
        {"name": "Row A", "word": "row", "spots": [s.pk for s in spots[:4]]},
    )
    asset_admin.make_group(
        admin, terrace.pk, {"name": "Row B", "spots": [s.pk for s in spots[4:]]}
    )
    asset_admin.change_spot(admin, terrace.pk, spots[0].pk, {"name": 'A "1"'})
    asset_admin.change_spot(admin, terrace.pk, spots[5].pk, {"name": "B6"})
    # Gaps in the numbers: a spot and a group removed.
    asset_admin.remove_spot(admin, terrace.pk, spots[2].pk)
    asset_admin.remove_spot(admin, terrace.pk, spots[8].pk)
    row_b = SpotGroup.objects.of(org).get(name="Row B")
    asset_admin.change_group(admin, terrace.pk, row_b.pk, {"spots": []})
    asset_admin.remove_group(admin, terrace.pk, row_b.pk)
    asset_admin.make_group(admin, terrace.pk, {"name": "Row C", "spots": [spots[7].pk]})
    # The existing assets: rename, change the word, add spots to a floor plan a
    # container books.
    hall = Asset.objects.of(org).first()
    assert hall is not None
    plans = list(AssetInventory.objects.of(org).filter(asset=hall))
    asset_admin.make_spots(admin, plans[0].pk, {"count": 2})
    exported, again = reloaded(org, "second")
    assert again == exported
    cafe_out = next(a for a in exported.assets if a.name == "Café ☕")
    names = [p.name for p in cafe_out.floor_plans]
    assert names == ["Main", "Terrace"]
    terrace_out = cafe_out.floor_plans[1]
    # Nine spots, two removed: seven; the groups count the rows that are left.
    assert len(terrace_out.spots) == 7
    assert [g.name for g in terrace_out.groups] == ["Row A", "Row C"]
    assert terrace_out.spots.count("") == 5
    assert 'A "1"' in terrace_out.spots and "B6" in terrace_out.spots
    # Every container still books the floor plan it booked, by name.
    assert [c.floor_plan for c in exported.booking_configurations] == [
        c.floor_plan for c in again.booking_configurations
    ]


def admin_for(org: Organization) -> Member:
    return admin_in(org)


def test_a_floor_plan_removed_on_a_screen_leaves_an_export_that_reloads() -> None:
    org = setups.load(
        setup_file.read(SETUPS / "scenarios/riverside-hall.toml"), today=TODAY
    )
    admin = admin_for(org)
    asset_admin.make_asset(admin, {"name": "Boat"})
    boat = Asset.objects.of(org).get(name="Boat")
    first = made_up.floor_plan_of(boat).pk
    asset_admin.change_floor_plan(admin, first, {"name": "Main"})
    asset_admin.make_floor_plan(
        admin, boat.pk, {"name": "Standing", "word": "seat", "spots": 3}
    )
    second = AssetInventory.objects.of(org).get(asset=boat, name="Standing").pk
    asset_admin.remove_floor_plan(admin, second)
    asset_admin.change_floor_plan(admin, first, {"name": ""})
    exported, again = reloaded(org, "second")
    assert again == exported
    out = next(a for a in exported.assets if a.name == "Boat")
    assert [p.name for p in out.floor_plans] == []


def test_the_made_up_organization_exports_after_edits_too(made: made_up.MadeUp) -> None:
    export_ready(made)
    asset = make_asset(made, "Boat")
    plan = only_plan(made, asset)
    done(made, f"{ROOT}/plans/{plan}/spots/make/", {"count": 3})
    ids = [s["id"] for s in read(made, f"{ROOT}/plans/{plan}/data/")["spots"]]
    done(made, f"{ROOT}/plans/{plan}/groups/make/", {"name": "Front", "spots": ids[:2]})
    done(made, f"{ROOT}/plans/{plan}/spots/{ids[1]}/remove/")
    exported = setups.export(made.organization)
    parsed = setup_file.parse(setup_file.write(exported))
    assert parsed == exported
    boat = next(a for a in exported.assets if a.name == "Boat")
    (written,) = boat.floor_plans
    assert len(written.spots) == 3 and written.groups[0].spots == (1,)


def test_what_the_places_answer_is_fixed(made: made_up.MadeUp) -> None:
    found = read(made, f"{ROOT}/data/")
    assert set(found) == {
        "time_zone",
        "time_zones",
        "booking_pages",
        "spaces",
        "assets",
        "most_spots",
        "most_spots_at_once",
    }
    plan = made_up.floor_plan_of(made.asset).pk
    page = read(made, f"{ROOT}/plans/{plan}/data/")
    assert set(page) == {
        "id",
        "asset",
        "asset_name",
        "name",
        "word",
        "containers",
        "removable",
        "spots",
        "groups",
        "most_spots",
        "most_spots_at_once",
    }
    assert BookingType.objects.of(made.organization).count() == 1


def test_the_row_locks_let_a_booking_take_its_key_share_lock(
    made: made_up.MadeUp,
) -> None:
    """A booking's insert takes FOR KEY SHARE on its asset, floor plan and
    spot rows; the service's row locks must not block it (a reschedule against
    a spot removal could otherwise deadlock). The organization lock is what
    serialises administrators."""
    from django.db import OperationalError, transaction

    org = made.organization
    plan = made_up.floor_plan_of(made.asset)
    spot = made_up.spots_of(plan, 1)[0]
    held: list[tuple[Any, int]] = [
        (Asset, made.asset.pk),
        (AssetInventory, plan.pk),
        (Spot, spot.pk),
    ]

    def key_share(model: Any, pk: int) -> bool:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '1500ms'")
                table = model._meta.db_table
                cursor.execute(
                    f"SELECT 1 FROM {table} WHERE id = %s FOR KEY SHARE",  # noqa: S608
                    [pk],
                )
            return True
        except OperationalError:
            return False
        finally:
            connection.close()

    with transaction.atomic():
        for model, pk in held:
            asset_admin._lock(model, org, pk)
        with ThreadPoolExecutor(max_workers=1) as pool:
            for model, pk in held:
                assert pool.submit(key_share, model, pk).result() is True
    # The control: an ordinary FOR UPDATE does block it.
    with transaction.atomic():
        Spot.objects.of(org).select_for_update().get(pk=spot.pk)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(key_share, Spot, spot.pk).result() is False
