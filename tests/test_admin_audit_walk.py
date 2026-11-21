# SPDX-License-Identifier: AGPL-3.0-only
"""Every administrator change is an audit event, and no way out with the whole
member list .

The tests walk the URL configuration, never a hand list of routes. Each
administrator route is in exactly one group below, with its reason; a route
added later that is in none of them fails `test_every_admin_route_is_in_a_group`.
"""

import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
from django.apps import apps
from django.contrib.sessions.backends.db import SessionStore
from django.db import DatabaseError, connection
from django.http import Http404, HttpResponse
from django.test import RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from randirandi.core import (
    audit_log,
    block_admin,
    event_admin,
    member_admin,
    organization_admin,
    price_admin,
    setup_admin,
)
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    AssetInventory,
    AuditEvent,
    Block,
    BookingCategory,
    Event,
    EventDay,
    LoginAttempt,
    LoginCode,
    Member,
    MemberStatus,
    Organization,
    Origin,
    PriceBook,
    Role,
    SecurityNotice,
    Spot,
    SpotGroup,
)
from tests import made_up, payouts_made
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_admin_bookings import book, order_of
from tests.test_admin_member_imports import file_of
from tests.test_login_sessions import HOST, page_settings  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# --- The groups ---

# Routes that change something an administrator owns, or hand out a member's
# data: POST only, each writes the named action(s) in the transaction of the
# change (proved below by making the audit write fail).
CHANGES: dict[str, set[AuditAction]] = {
    "admin-member-add": {AuditAction.ADD_MEMBER},
    "admin-member-import-start": {AuditAction.IMPORT_MEMBERS},
    "admin-member-block": {AuditAction.BLOCK_MEMBER},
    "admin-member-unblock": {AuditAction.UNBLOCK_MEMBER},
    "admin-member-correct": {AuditAction.CORRECT_MEMBER},
    "admin-member-email": {AuditAction.CHANGE_MEMBER_EMAIL},
    "admin-member-reset": {AuditAction.RESET_AUTHENTICATOR},
    "admin-member-export": {AuditAction.EXPORT_MEMBER},
    "admin-booking-cancel": {AuditAction.CANCEL_BOOKING_LINE},
    "admin-blocks-make": {AuditAction.MADE_BLOCK},
    "admin-blocks-remove": {AuditAction.REMOVED_BLOCK},
    "admin-events-make": {AuditAction.MADE_EVENT, AuditAction.REMOVED_EVENT_DAY},
    "admin-events-change": {AuditAction.CHANGED_EVENT, AuditAction.REMOVED_EVENT_DAY},
    "admin-events-remove": {AuditAction.REMOVED_EVENT},
    "admin-events-day-remove": {AuditAction.REMOVED_EVENT_DAY},
    "admin-events-day-hours": {AuditAction.CHANGED_EVENT_DAY},
    "admin-availability-apply": {
        AuditAction.MADE_AVAILABILITY,
        AuditAction.CHANGED_AVAILABILITY,
        AuditAction.REMOVED_AVAILABILITY,
        AuditAction.MADE_CLOSURE,
        AuditAction.CHANGED_CLOSURE,
        AuditAction.REMOVED_CLOSURE,
        AuditAction.REMOVED_EVENT_DAY,
    },
    "admin-types-make": {AuditAction.MADE_BOOKING_TYPE},
    "admin-types-change": {AuditAction.CHANGED_BOOKING_TYPE},
    "admin-types-move": {AuditAction.CHANGED_BOOKING_TYPE},
    "admin-types-remove": {AuditAction.REMOVED_BOOKING_TYPE},
    "admin-roles-make": {AuditAction.MADE_ROLE},
    "admin-roles-change": {AuditAction.CHANGED_ROLE},
    "admin-roles-remove": {AuditAction.REMOVED_ROLE},
    "admin-organization-change": {AuditAction.CHANGED_ORGANIZATION},
    "admin-assets-space-make": {AuditAction.MADE_SPACE},
    "admin-assets-space-change": {AuditAction.CHANGED_SPACE},
    "admin-assets-space-remove": {AuditAction.REMOVED_SPACE},
    "admin-assets-make": {AuditAction.MADE_ASSET},
    "admin-assets-change": {AuditAction.CHANGED_ASSET},
    "admin-assets-remove": {AuditAction.REMOVED_ASSET},
    "admin-assets-plan-make": {AuditAction.MADE_FLOOR_PLAN},
    "admin-assets-plan-change": {AuditAction.CHANGED_FLOOR_PLAN},
    "admin-assets-plan-remove": {AuditAction.REMOVED_FLOOR_PLAN},
    "admin-assets-spots-make": {AuditAction.MADE_SPOT},
    "admin-assets-spot-change": {AuditAction.CHANGED_SPOT},
    "admin-assets-spot-remove": {AuditAction.REMOVED_SPOT},
    "admin-assets-group-make": {AuditAction.MADE_SPOT_GROUP},
    "admin-assets-group-change": {AuditAction.CHANGED_SPOT_GROUP},
    "admin-assets-group-remove": {AuditAction.REMOVED_SPOT_GROUP},
    "admin-containers-make": {AuditAction.MADE_BOOKING_CONFIGURATION},
    "admin-containers-change": {AuditAction.CHANGED_BOOKING_CONFIGURATION},
    "admin-containers-categories": {AuditAction.CHANGED_BOOKING_CONFIGURATION},
    "admin-containers-remove": {AuditAction.REMOVED_BOOKING_CONFIGURATION},
    "admin-prices-book-make": {AuditAction.MADE_PRICE_BOOK},
    "admin-prices-book-change": {AuditAction.CHANGED_PRICE_BOOK},
    "admin-prices-book-remove": {AuditAction.REMOVED_PRICE_BOOK},
    "admin-prices-save": {
        AuditAction.MADE_PRICE,
        AuditAction.CHANGED_PRICE,
        AuditAction.REMOVED_PRICE,
    },
    "admin-prices-remove": {AuditAction.REMOVED_PRICE},
}

# POST routes that only read: the preview of an event is a body too large for
# a query. They write no event, are checked like a change (CSRF) and answer no
# member data except the one look-up named below.
POSTED_READS = {
    "admin-events-preview": "what an event would do: its time, what it leaves, "
    "what is in the way; a booking in the way names its member and hours, so "
    "that one answer writes one listed bookings event, otherwise nothing",
}

# GET routes that show who booked or a member's personal data for one named
# thing: opening it is a look-up, one INSPECT event in the transaction of the
# read.
INSPECTED = {
    "admin-member-data": "a member's page: contact details, units, logins",
    "admin-booking-data": "a booking's page: who booked, the member's email",
    "admin-member-import-data": "a run's page: the members it disabled, by name",
}

# GET lists that name who booked or expose contact details record one
# organization-scoped lookup event in the same transaction as the read.
LISTED = {
    "admin-members-list": AuditAction.LISTED_MEMBERS,
    "admin-bookings-list": AuditAction.LISTED_BOOKINGS,
    "admin-payments-list": AuditAction.LISTED_PAYMENTS,
    "admin-schedule-list": AuditAction.LISTED_SCHEDULE,
    "admin-payout-data": AuditAction.LISTED_PAYOUTS,
}

# GET routes that answer one page of at most `PER_PAGE` rows (or a short fixed
# list) and write no event. The reason says what a row carries.
LISTS = {
    "admin-blocks-list": "asset, times, note, the administrator's name: 25 a page",
    "admin-audit-events": "ids, fixed words and names of administrators: 25 a page",
    "admin-member-imports-list": "counts and file names of the runs: 25 a page",
    "admin-payouts-list": "days, counts and amounts of the payouts: 25 a page",
    "admin-types-data": "booking types and categories: no member data",
    "admin-roles-data": "roles and categories: no member data",
    "admin-organization-data": "the details, roles and time zones: no member data",
    "admin-availability-data": "weeks, closures, owed refunds: no member data",
    "admin-availability-month": "a month's days, open or closed: no member data",
    "admin-availability-holidays": "a year's public holidays: no member data",
    "admin-blocks-lay": "what a stretch would be laid as: pieces, no member data",
    "admin-assets-data": "spaces, assets, floor plans: no member data",
    "admin-assets-plan-data": "one floor plan's spots and groups: no member data",
    "admin-containers-data": "containers and their categories: no member data",
    "admin-events-list": "events: titles, the contact's name, 25 a page",
    "admin-events-data": "one event: titles, the contact's and maker's name, no "
    "email or phone of anyone",
    "admin-events-contacts": "up to 20 active members' names to pick a contact from",
    "admin-events-in-the-way": "event days on an asset between dates: titles",
    "admin-prices-data": "price books and prices: no member data",
}

# GET routes that answer the page's HTML shell; the data comes from the routes
# above, which are in the groups.
SHELLS = {
    "admin": "redirects to the first place",
    "admin-place": "a place's shell",
    "admin-booking": "a booking page's shell (data: admin-booking-data)",
    "admin-member": "a member page's shell (data: admin-member-data)",
    "admin-member-imports": "the imports tab's shell",
    "admin-member-import": "a run page's shell (data: admin-member-import-data)",
    "admin-payouts": "the Payouts tab's shell",
    "admin-payout": "a payout page's shell (data: admin-payout-data)",
    "admin-event": "an event page's shell (data: admin-events-data)",
}

# Routes outside the administrators' screens that change state; they change
# nothing an administrator owns. The sign-in steps write their own
# ADMIN_LOGIN / ENROLL_AUTHENTICATOR events inside `authentication` and
# `second_factor`; the walk names them so that a rename cannot hide one.
NOT_ADMIN_OWNED = {
    "login": "sign in: asks for a code",
    "email-code": "sign in: the code; an administrator's sign-in is audited there",
    "authenticator": "sign in: the authenticator step, audited in second_factor",
    "authenticator-qr": "sign in: the enrolment image, a read",
    "logout": "sign out: ends the caller's own session",
}

# Actions no administrator route writes, and who does.
OTHER_WRITERS = {
    AuditAction.BOOTSTRAP: "operator command first_administrator",
    AuditAction.BROUGHT_IN_BOOKING: "operator command import_old_bookings",
    AuditAction.GAVE_ADMIN_ACCESS: "written by member correction and role change",
    AuditAction.ERASE_MEMBER: "member_data.erase_identity, not an admin route",
    AuditAction.OPERATOR_ERASE_MEMBER: "operator command erase_member",
    AuditAction.INSPECT: "the INSPECTED group and the model inspection",
    AuditAction.ADMIN_LOGIN: "the sign-in's second step",
    AuditAction.ENROLL_AUTHENTICATOR: "the sign-in's authenticator step",
    AuditAction.OPERATOR_RESET_AUTHENTICATOR: "operator command reset_authenticator",
    AuditAction.REQUEST_REFUND: "written by the cancel when it refunds",
    AuditAction.LEFT_REFUND_BY_HAND: "written by the availability change's cancel",
    AuditAction.CANCEL_PAYMENT: "written by the cancel of an unpaid booking",
    AuditAction.MADE_BOOKING_CATEGORY: "admin-types-make, kind booking-categories",
    AuditAction.CHANGED_BOOKING_CATEGORY: "admin-types-change/move, categories",
    AuditAction.REMOVED_BOOKING_CATEGORY: "admin-types-remove, categories",
}

# The member pages and the member-side payment start and sign-out: every other
# route that needs a sign-in is walked as an administrator's, so a new route
# under any prefix, in any module, is in a group or fails.
MEMBER_PAGES = frozenset(
    {
        "booking/choices/",
        "booking/choices/category/",
        "booking/calendar/",
        "booking/price/",
        "booking/cart/",
        "booking/cart/add/",
        "booking/cart/<str:row_id>/change/",
        "booking/cart/<str:row_id>/remove/",
        "booking/cart/empty/",
        "booking/cart/order/",
        "booking/bookings/",
        "booking/order/<uuid:order_public_id>/",
        "booking/order/<uuid:order_public_id>/cancel/",
        "booking/order/<uuid:order_public_id>/calendar/",
        "payments/start/<uuid:order_public_id>/",
        "logout/",
        "book/",
        "book/cart/",
        "book/prices/",
        "book/order/<uuid:order_public_id>/",
        "book/order/<uuid:order_public_id>/conditions/",
        "me/",
        "events/",
        "events/data/",
        "events/<int:event>/",
        "",
    }
)

# --- The walk ---


@dataclass(frozen=True)
class Route:
    name: str
    route: str
    callback: Callable[..., HttpResponse]
    converters: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        """Every route is an administrator's unless it is public (the reviewed
        list of `tests/test_route_requirements.py`) or a member page."""
        public = not getattr(self.callback, "login_required", True)
        return not public and self.route not in MEMBER_PAGES

    def path(self) -> tuple[str, dict[str, Any]]:
        """An address with ids no row has, and the keyword arguments for it."""
        kwargs: dict[str, Any] = {}

        def sample(found: re.Match[str]) -> str:
            kind, name = found.group(1) or "str", found.group(2)
            value: Any = {
                "int": 999999,
                "uuid": uuid.uuid4(),
            }.get(kind, "nothing")
            kwargs[name] = value
            return str(value)

        return "/" + re.sub(r"<(?:(\w+):)?(\w+)>", sample, self.route), kwargs


def walk(patterns: list[Any] | None = None, prefix: str = "") -> Iterator[Route]:
    for found in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(found, URLResolver):
            yield from walk(found.url_patterns, prefix + str(found.pattern))
        elif isinstance(found, URLPattern):
            assert found.name, f"{prefix}{found.pattern} has no name"
            yield Route(
                found.name,
                prefix + str(found.pattern),
                found.callback,
            )


def all_routes() -> list[Route]:
    return list(walk())


def admin_routes() -> dict[str, Route]:
    found = {route.name: route for route in all_routes() if route.is_admin}
    return found


# --- Requests and snapshots ---


def request_to(
    route: Route,
    method: str,
    admin: Member,
    *,
    path: str | None = None,
    body: Any = None,
    raw: bytes | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    address, _ = route.path()
    address = path or address
    if query:
        address = f"{address}?{urlencode(query)}"
    data = (
        raw
        if raw is not None
        else (json.dumps(body).encode() if body is not None else b"")
    )
    request: Any = getattr(RequestFactory(), method.lower())(
        address,
        data,
        content_type="application/octet-stream"
        if raw is not None
        else "application/json",
        HTTP_HOST=HOST,
        secure=True,
    )
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = admin
    return request


def call(
    route: Route, kwargs: dict[str, Any] | None, request: Any
) -> HttpResponse | int:
    """The view's answer, 404 for Http404 (a status for a probe to compare)."""
    try:
        return route.callback(request, **(kwargs if kwargs is not None else {}))
    except Http404:
        return 404


def status_of(answer: HttpResponse | int) -> int:
    return answer if isinstance(answer, int) else answer.status_code


# What a refused or failed request must leave as it found it. The sign-in
# mechanics are left out: a refused code is meant to be counted.
LEFT_OUT = {LoginAttempt, LoginCode, SecurityNotice}


def snapshot() -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for model in apps.get_app_config("core").get_models():
        if model in LEFT_OUT:
            continue
        rows = []
        for row in model._base_manager.order_by("pk").values():
            row.pop("last_counter", None)
            rows.append(row)
        found[model.__name__] = rows
    return found


def events() -> list[tuple[str, str, int]]:
    return [
        (e.action, e.object_kind, e.object_id)
        for e in AuditEvent.across_organizations.order_by("pk")
    ]


# --- Group tests ---


def test_every_admin_route_is_in_a_group() -> None:
    found = admin_routes()
    groups: list[Mapping[str, object]] = [
        CHANGES,
        POSTED_READS,
        INSPECTED,
        LISTED,
        LISTS,
        SHELLS,
    ]
    named = [name for group in groups for name in group]
    assert len(named) == len(set(named)), "a route is in two groups"
    outside = {name for name in found if name not in named}
    assert not outside, (
        f"admin routes in no group, each needs its audit proof: {sorted(outside)}"
    )
    assert not set(named) - set(found), "a group names a route that is gone"
    names = [route.name for route in all_routes()]
    assert len(names) == len(set(names)), "route names must be unique"
    for name in NOT_ADMIN_OWNED:
        assert name in names, f"{name} is gone; the list names it"
        assert name not in found


def test_every_changing_route_has_its_scenario_and_every_action_is_placed() -> None:
    assert set(SCENARIOS) == set(CHANGES)
    written = {action for actions in CHANGES.values() for action in actions}
    written |= set(LISTED.values())
    unplaced = set(AuditAction) - written - set(OTHER_WRITERS)
    assert not unplaced, f"actions nobody is named for: {sorted(unplaced)}"


def test_methods_are_the_declared_ones_and_a_refused_call_writes_nothing() -> None:
    """Each route answers its group's one method; the others are 405. A call
    that finds nothing or is refused leaves the database as it was."""
    made = made_up.everything()
    routes = admin_routes()
    with application_login():
        before, before_events = snapshot(), events()
        for name, route in routes.items():
            _, kwargs = route.path()
            allowed = set()
            for method in METHODS:
                request = request_to(route, method, made.administrator)
                answer = call(route, kwargs, request)
                if status_of(answer) != 405:
                    allowed.add(method)
            posts = name in CHANGES or name in POSTED_READS
            assert allowed == ({"POST"} if posts else {"GET"}), (
                name,
                allowed,
            )
        # Only the named lists wrote anything: their look-up events.
        after = snapshot()
        after.pop("AuditEvent"), before.pop("AuditEvent")
        assert after == before
        added = events()[len(before_events) :]
        assert events()[: len(before_events)] == before_events
        assert {action for action, _, _ in added} <= {
            str(action) for action in LISTED.values()
        }


# --- A change and its event are one transaction ---


@dataclass
class Prepared:
    kwargs: dict[str, Any]
    body: Callable[[], Any] | None = None
    raw: bytes | None = None
    query: dict[str, str] | None = None
    ok: tuple[int, ...] = (200,)


def _second_administrator(made: made_up.MadeUp) -> Member:
    other = Member.objects.create(
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
    fresh(other)
    return other


def _code(made: made_up.MadeUp) -> str:
    return fresh(made.administrator)["code"]


def _later(made: made_up.MadeUp) -> str:
    return str(made.day + timedelta(days=7))


def _new_block(made: made_up.MadeUp) -> Block:
    from datetime import datetime

    from randirandi.core.models import Asset

    asset = Asset.objects.of(made.organization).get(pk=made.asset.pk)
    day = made.day + timedelta(days=14)
    start = datetime.combine(day, datetime.min.time(), tzinfo=asset.zone).replace(
        hour=10
    )
    (block,) = block_admin.make(
        made.administrator, asset.pk, start, start + timedelta(hours=1), ""
    )
    return block


def _new_type(made: made_up.MadeUp, name: str = "Tent") -> int:
    setup_admin.make_item(
        made.administrator, setup_admin.LISTS["booking-types"], {"name": name}
    )
    return (
        setup_admin.BookingType.objects.of(made.organization).get(name=name).pk  # type: ignore[attr-defined]
    )


def _new_role(made: made_up.MadeUp) -> int:
    setup_admin.make_role(
        made.administrator,
        {"name": "Helper", "categories": [made.private.pk]},
    )
    return Role.objects.of(made.organization).get(name="Helper").pk


def _prepare_add(made: made_up.MadeUp) -> Prepared:
    return Prepared(
        {},
        lambda: {
            "full_name": "Robin Hale",
            "email": "robin@harbourview.example",
            "role": made.standard.pk,
            "code": _code(made),
        },
        ok=(201,),
    )


def _prepare_import(made: made_up.MadeUp) -> Prepared:
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        origin=Origin.ADMINISTRATOR
    )
    return Prepared(
        {},
        raw=file_of(1, 2, 3),
        query={"parser": "unit-members-xlsx", "name": "members.xlsx"},
    )


def _prepare_unblock(made: made_up.MadeUp) -> Prepared:
    member_admin.block_member(actor=made.administrator, member_id=made.member.pk)
    return Prepared({"member": made.member.pk}, dict)


def _prepare_reset(made: made_up.MadeUp) -> Prepared:
    other = _second_administrator(made)
    return Prepared({"member": other.pk}, lambda: {"code": _code(made)})


def _prepare_cancel(made: made_up.MadeUp) -> Prepared:
    order = order_of(made)
    line = book(made, order, day=5)
    return Prepared(
        {"number": order.number},
        lambda: {"key": str(uuid.uuid4()), "line": line.pk, "refund": False},
    )


def _event_form(made: made_up.MadeUp, title: str = "Spring fair") -> dict[str, Any]:
    day = made.day + timedelta(days=400)
    return {
        "category": "social",
        "title": title,
        "description": "",
        "asset": None,
        "contact": made.member.pk,
        "show_email": True,
        "show_phone": False,
        "when": {
            "repeat": False,
            "start": f"{day}T10:00",
            "end": f"{day}T12:00",
        },
    }


def _new_event(made: made_up.MadeUp, *, repeat: bool = False) -> Event:
    """An event without an asset, made through its service."""
    start = datetime.combine(made.day + timedelta(days=400), time(10, 0))
    when: event_admin.OneOff | event_admin.Repeat = (
        event_admin.Repeat(
            frozenset(range(7)),
            timezone.localdate() + timedelta(days=1),
            None,
            600,
            720,
        )
        if repeat
        else event_admin.OneOff(start, start + timedelta(hours=2))
    )
    return event_admin.make_event(
        made.administrator,
        event_admin.EventSpec(
            category="social",
            title="Spring fair",
            description="",
            contact_id=made.member.pk,
            when=when,
        ),
    )


def _new_day(made: made_up.MadeUp) -> int:
    event = _new_event(made, repeat=True)
    found = (
        EventDay.objects.of(made.organization)
        .filter(event=event)
        .order_by("day")
        .values_list("pk", flat=True)
        .first()
    )
    assert found is not None
    return found


def _prepare_blocks_make(made: made_up.MadeUp) -> Prepared:
    return Prepared(
        {},
        lambda: {
            "asset": made.asset.pk,
            "start": f"{_later(made)}T10:00",
            "end": f"{_later(made)}T11:00",
        },
    )


def _do(
    made: made_up.MadeUp, name: str, kwargs: dict[str, Any], body: Any = None
) -> dict[str, Any]:
    """A route called to prepare state; it must succeed."""
    route = admin_routes()[name]
    request = request_to(route, "POST", made.administrator, body=body or {})
    answer = route.callback(request, **kwargs)
    assert answer.status_code in (200, 201), (name, answer.content)
    found = json.loads(answer.content)
    assert isinstance(found, dict)
    return found


def _space(made: made_up.MadeUp, name: str = "Hall") -> int:
    found = _do(made, "admin-assets-space-make", {}, {"name": name})
    return int(next(x["id"] for x in found["place"]["spaces"] if x["name"] == name))


def _asset(made: made_up.MadeUp, name: str = "Boat") -> int:
    found = _do(made, "admin-assets-make", {}, {"name": name})
    return int(next(x["id"] for x in found["place"]["assets"] if x["name"] == name))


def _plan(made: made_up.MadeUp, asset: int) -> int:
    (plan,) = AssetInventory.objects.of(made.organization).filter(asset=asset)
    return plan.pk


def _spots(made: made_up.MadeUp, plan: int, count: int = 3) -> list[int]:
    _do(made, "admin-assets-spots-make", {"plan": plan}, {"count": count})
    return list(
        Spot.objects.of(made.organization)
        .filter(floor_plan=plan)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _second_plan(made: made_up.MadeUp) -> int:
    asset = _asset(made)
    first = _plan(made, asset)
    _do(made, "admin-assets-plan-change", {"plan": first}, {"name": "Main"})
    found = _do(
        made,
        "admin-assets-plan-make",
        {"asset": asset},
        {"name": "Loft", "word": "seat", "spots": 2},
    )
    return int(found["plan"]["id"])


def _group(made: made_up.MadeUp, plan: int) -> int:
    _do(made, "admin-assets-group-make", {"plan": plan}, {"name": "Row"})
    return SpotGroup.objects.of(made.organization).get(floor_plan=plan, name="Row").pk


def _container(made: made_up.MadeUp) -> int:
    found = _do(
        made,
        "admin-containers-make",
        {},
        {
            "asset": made.asset.pk,
            "booking_type": made.booking_type.pk,
            "period_type": "fixed",
        },
    )
    return int(found["made"])


def _book(made: made_up.MadeUp, name: str = "Spring") -> int:
    _do(made, "admin-prices-book-make", {}, {"name": name})
    return PriceBook.objects.of(made.organization).get(name=name).pk


def _stamp(made: made_up.MadeUp) -> str:
    place: Any = price_admin.prices_place(made.organization)
    for each in place["containers"]:
        if each["id"] == made.configuration.pk:
            return str(each["stamps"].get(str(made.private.pk), ""))
    return ""


def _prepare_categories(made: made_up.MadeUp) -> Prepared:
    BookingCategory.objects.create(organization=made.organization, name="Guest")
    pk = _container(made)
    return Prepared(
        {"pk": pk},
        lambda: {
            "categories": [{"category": made.private.pk, "hours": []}],
            "seen": made_up.seen_categories(made.organization, pk),
        },
    )


def _prepare_spot(made: made_up.MadeUp, **extra: Any) -> tuple[int, list[int]]:
    plan = _plan(made, _asset(made))
    return plan, _spots(made, plan)


def _spot_change(made: made_up.MadeUp) -> Prepared:
    plan, spots = _prepare_spot(made)
    return Prepared({"plan": plan, "spot": spots[0]}, lambda: {"name": "A"})


def _spot_remove(made: made_up.MadeUp) -> Prepared:
    plan, spots = _prepare_spot(made)
    return Prepared({"plan": plan, "spot": spots[-1]}, dict)


def _group_change(made: made_up.MadeUp) -> Prepared:
    plan = _plan(made, _asset(made))
    return Prepared(
        {"plan": plan, "group": _group(made, plan)}, lambda: {"name": "Rank"}
    )


def _group_remove(made: made_up.MadeUp) -> Prepared:
    plan = _plan(made, _asset(made))
    return Prepared({"plan": plan, "group": _group(made, plan)}, dict)


def _prepare_plan_make(made: made_up.MadeUp) -> Prepared:
    asset = _asset(made)
    _do(
        made, "admin-assets-plan-change", {"plan": _plan(made, asset)}, {"name": "Main"}
    )
    return Prepared(
        {"asset": asset}, lambda: {"name": "Loft", "word": "seat", "spots": 2}
    )


THE_PRICE = {
    "group": None,
    "tiers_go_by": "length",
    "billing_basis": "per_booking",
    "tiers": [{"up_to": 720, "paid_per": 720, "amount": 26000}],
}


SCENARIOS: dict[str, Callable[[made_up.MadeUp], Prepared]] = {
    "admin-assets-space-make": lambda m: Prepared({}, lambda: {"name": "Annex"}),
    "admin-assets-space-change": lambda m: Prepared(
        {"pk": _space(m)}, lambda: {"name": "Hall 2"}
    ),
    "admin-assets-space-remove": lambda m: Prepared({"pk": _space(m)}, dict),
    "admin-assets-make": lambda m: Prepared({}, lambda: {"name": "Canoe"}),
    "admin-assets-change": lambda m: Prepared(
        {"pk": _asset(m)}, lambda: {"name": "Barge"}
    ),
    "admin-assets-remove": lambda m: Prepared({"pk": _asset(m)}, dict),
    "admin-assets-plan-make": _prepare_plan_make,
    "admin-assets-plan-change": lambda m: Prepared(
        {"plan": _plan(m, _asset(m))}, lambda: {"name": "Main"}
    ),
    "admin-assets-plan-remove": lambda m: Prepared({"plan": _second_plan(m)}, dict),
    "admin-assets-spots-make": lambda m: Prepared(
        {"plan": _plan(m, _asset(m))}, lambda: {"count": 2}
    ),
    "admin-assets-spot-change": _spot_change,
    "admin-assets-spot-remove": _spot_remove,
    "admin-assets-group-make": lambda m: Prepared(
        {"plan": _plan(m, _asset(m))}, lambda: {"name": "Row"}
    ),
    "admin-assets-group-change": _group_change,
    "admin-assets-group-remove": _group_remove,
    "admin-containers-make": lambda m: Prepared(
        {},
        lambda: {
            "asset": m.asset.pk,
            "booking_type": m.booking_type.pk,
            "period_type": "fixed",
        },
    ),
    "admin-containers-change": lambda m: Prepared(
        {"pk": m.configuration.pk}, lambda: {"status": "disabled"}
    ),
    "admin-containers-categories": _prepare_categories,
    "admin-containers-remove": lambda m: Prepared({"pk": _container(m)}, dict),
    "admin-prices-book-make": lambda m: Prepared({}, lambda: {"name": "Winter"}),
    "admin-prices-book-change": lambda m: Prepared(
        {"pk": _book(m)}, lambda: {"name": "Autumn"}
    ),
    "admin-prices-book-remove": lambda m: Prepared({"pk": _book(m)}, dict),
    "admin-prices-save": lambda m: Prepared(
        {"container": m.configuration.pk, "category": m.private.pk},
        lambda: {"entries": [THE_PRICE], "stamp": _stamp(m)},
    ),
    "admin-prices-remove": lambda m: Prepared(
        {"container": m.configuration.pk, "category": m.private.pk},
        lambda: {"stamp": _stamp(m)},
    ),
    "admin-member-add": _prepare_add,
    "admin-member-import-start": _prepare_import,
    "admin-member-block": lambda m: Prepared({"member": m.member.pk}, dict),
    "admin-member-unblock": _prepare_unblock,
    "admin-member-correct": lambda m: Prepared(
        {"member": m.member.pk}, lambda: {"full_name": "Alex Q", "code": _code(m)}
    ),
    "admin-member-email": lambda m: Prepared(
        {"member": m.member.pk},
        lambda: {"email": "alex.new@harbourview.example", "code": _code(m)},
    ),
    "admin-member-reset": _prepare_reset,
    "admin-member-export": lambda m: Prepared(
        {"member": m.member.pk}, lambda: {"code": _code(m)}
    ),
    "admin-booking-cancel": _prepare_cancel,
    "admin-availability-apply": lambda m: Prepared(
        {"asset": m.asset.pk},
        lambda: {
            "kind": "closure-make",
            "first_date": _later(m),
            "last_date": _later(m),
            "reason": "repair",
        },
    ),
    "admin-blocks-make": _prepare_blocks_make,
    "admin-events-make": lambda m: Prepared({}, lambda: _event_form(m)),
    "admin-events-change": lambda m: Prepared(
        {"event": _new_event(m).pk}, lambda: _event_form(m, "Autumn fair")
    ),
    "admin-events-remove": lambda m: Prepared({"event": _new_event(m).pk}, dict),
    "admin-events-day-remove": lambda m: Prepared({"day": _new_day(m)}, dict),
    "admin-events-day-hours": lambda m: Prepared(
        {"day": _new_day(m)}, lambda: {"from": "11:00", "to": "13:00"}
    ),
    "admin-blocks-remove": lambda m: Prepared({"block": _new_block(m).pk}, dict),
    "admin-types-make": lambda m: Prepared(
        {"kind": "booking-types"}, lambda: {"name": "Tent", "description": ""}
    ),
    "admin-types-change": lambda m: Prepared(
        {"kind": "booking-types", "pk": _new_type(m)}, lambda: {"name": "Marquee"}
    ),
    "admin-types-move": lambda m: Prepared(
        {"kind": "booking-types", "pk": _new_type(m)}, lambda: {"direction": "up"}
    ),
    "admin-types-remove": lambda m: Prepared(
        {"kind": "booking-types", "pk": _new_type(m)}, dict
    ),
    "admin-roles-make": lambda m: Prepared(
        {}, lambda: {"name": "Helper", "categories": [m.private.pk]}
    ),
    "admin-roles-change": lambda m: Prepared(
        {"pk": _new_role(m)}, lambda: {"name": "Helpers"}
    ),
    "admin-roles-remove": lambda m: Prepared({"pk": _new_role(m)}, dict),
    "admin-organization-change": lambda m: Prepared(
        {},
        lambda: {
            "version": organization_admin.version_of(
                Organization.objects.get(pk=m.organization.pk)
            ),
            "contact_email": "desk@example.com",
        },
    ),
}


def audit_fails() -> Any:
    """The audit writer's insert fails, as a database fault would."""
    stand_in = MagicMock()
    stand_in.objects.create.side_effect = DatabaseError("audit insert failed")
    return patch("randirandi.core.audit.AuditEvent", stand_in)


@pytest.mark.parametrize("name", sorted(CHANGES))
def test_a_change_and_its_event_are_one_transaction(name: str) -> None:
    made = made_up.everything()
    route = admin_routes()[name]
    with application_login():
        fresh(made.administrator)
        prepared = SCENARIOS[name](made)

        def send() -> HttpResponse | int:
            request = request_to(
                route,
                "POST",
                made.administrator,
                body=prepared.body() if prepared.body else None,
                raw=prepared.raw,
                query=prepared.query,
            )
            return call(route, prepared.kwargs, request)

        before, before_events = snapshot(), events()
        with audit_fails():
            try:
                failed = status_of(send())
            except DatabaseError:
                failed = 500
        assert failed >= 500 or failed == 409, (name, failed)
        assert snapshot() == before, f"{name}: a change stayed without its event"
        assert events() == before_events

        done = send()
        assert status_of(done) in prepared.ok, (name, status_of(done))
        added = events()[len(before_events) :]
        assert added, f"{name} changed something and wrote no event"
        assert {action for action, _, _ in added} <= {str(a) for a in CHANGES[name]} | {
            str(AuditAction.GAVE_ADMIN_ACCESS)
        }, (name, added)


# --- A look-up is an event ---


def _inspect_kwargs(made: made_up.MadeUp) -> dict[str, dict[str, Any]]:
    order = order_of(made)
    book(made, order, day=5)
    return {
        "admin-member-data": {"member": made.member.pk},
        "admin-booking-data": {"number": order.number},
        "admin-member-import-data": {"number": 1},
    }


@pytest.mark.parametrize("name", sorted(INSPECTED))
def test_opening_one_thing_is_a_look_up_that_cannot_be_read_unrecorded(
    name: str,
) -> None:
    made = made_up.everything()
    route = admin_routes()[name]
    with application_login():
        kwargs = _inspect_kwargs(made)[name]
        before, before_events = snapshot(), events()
        with audit_fails():
            try:
                failed = status_of(
                    call(route, kwargs, request_to(route, "GET", made.administrator))
                )
            except DatabaseError:
                failed = 500
        assert failed >= 500, f"{name} answered without its event"
        assert events() == before_events and snapshot() == before
        answer = call(route, kwargs, request_to(route, "GET", made.administrator))
        assert status_of(answer) == 200
        added = events()[len(before_events) :]
        assert [a for a, _, _ in added] == [str(AuditAction.INSPECT)]


@pytest.mark.parametrize("name", sorted(LISTED))
def test_showing_a_page_of_a_named_list_is_a_look_up(name: str) -> None:
    made = made_up.everything()
    route = admin_routes()[name]
    # A payout's page needs a payout to open.
    kwargs = (
        {"payout": payouts_made.payout(made).pk} if name == "admin-payout-data" else {}
    )
    with application_login():
        before, before_events = snapshot(), events()
        with audit_fails():
            try:
                failed = status_of(
                    call(route, kwargs, request_to(route, "GET", made.administrator))
                )
            except DatabaseError:
                failed = 500
        assert failed >= 500, f"{name} answered without its event"
        assert events() == before_events and snapshot() == before
        for _ in range(2):
            answer = call(route, kwargs, request_to(route, "GET", made.administrator))
            assert status_of(answer) == 200
        added = events()[len(before_events) :]
        assert added == [(str(LISTED[name]), "organization", made.organization.pk)] * 2


# --- The audit log cannot be edited ---


def test_no_route_edits_or_removes_an_event_and_the_login_cannot() -> None:
    audit_routes = [
        route.name
        for route in all_routes()
        if route.is_admin and re.search(r"audit", route.route + route.name)
    ]
    # Only the Audit log's list and its shell (admin/<place>/): reads.
    assert sorted(audit_routes) == ["admin-audit-events"]
    assert "admin-audit-events" in LISTS and "admin-audit-events" not in CHANGES
    assert not [name for name in CHANGES if "audit" in name or name in INSPECTED]
    made = made_up.everything()
    with application_login():
        with connection.cursor() as cursor:
            for privilege in ("UPDATE", "TRUNCATE"):
                cursor.execute(
                    "SELECT has_table_privilege('randirandi_app','core_auditevent',%s)",
                    [privilege],
                )
                assert cursor.fetchone() == (False,), privilege
            # Retention: the one deletion. The row policy lets the login
            # delete only events 36 months old.
            cursor.execute(
                "SELECT has_table_privilege('randirandi_app',"
                "'core_auditevent','DELETE')"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "DELETE FROM core_auditevent WHERE organization_id=%s",
                [made.organization.pk],
            )
            assert cursor.rowcount == 0
        assert AuditEvent.objects.of(made.organization).exists()


# --- Step 9: no way out with the whole member list ---


def test_no_bulk_or_export_route_besides_one_members_export() -> None:
    routes = admin_routes()
    exports = [
        name
        for name, route in routes.items()
        if re.search(
            r"(?<![a-z])(all|export|csv|download|bulk|dump|backup)(?![a-z])",
            route.route + name,
        )
    ]
    assert exports == ["admin-member-export"], exports
    export = routes["admin-member-export"]
    assert "<int:member>" in export.route and export.route.count("<") == 1
    assert "admin-member-export" in CHANGES
    # The one export is one member's, behind a fresh authenticator code, and the
    # only admin route that answers as an attachment.
    made = made_up.everything()
    with application_login():
        for name, route in routes.items():
            if name in CHANGES:
                continue
            _, kwargs = route.path()
            answer = call(route, kwargs, request_to(route, "GET", made.administrator))
            if isinstance(answer, HttpResponse):
                assert "attachment" not in answer.headers.get(
                    "Content-Disposition", ""
                ), name


def _many_members(made: made_up.MadeUp, count: int) -> None:
    Member.across_organizations.bulk_create(
        [
            Member(
                organization=made.organization,
                role=made.standard,
                full_name=f"Person {number:03d}",
                first_name="Person",
                last_name=f"{number:03d}",
                email=f"p{number}@harbourview.example",
                phone=f"+4791{number:06d}",
                status=MemberStatus.ACTIVE,
                origin=Origin.MEMBER_IMPORT,
            )
            for number in range(count)
        ]
    )


ROW_KEYS = {
    "id",
    "name",
    "email",
    "phone",
    "units",
    "role",
    "status",
    "status_label",
    "last_login",
}


def test_the_member_list_is_a_page_of_the_fixed_cap_with_the_rows_it_shows() -> None:
    made = made_up.everything()
    _many_members(made, 80)
    route = admin_routes()["admin-members-list"]
    cap = audit_log.PER_PAGE
    assert cap == 25
    with application_login():
        for query in (
            {},
            {"page": "2"},
            {"per_page": "1000"},
            {"limit": "1000", "page_size": "1000", "all": "1", "size": "1000"},
            {"page": "0"},
            {"page": "99999999"},
            {"search": "Person", "sort": "login"},
        ):
            answer = call(
                route, {}, request_to(route, "GET", made.administrator, query=query)
            )
            assert isinstance(answer, HttpResponse)
            if answer.status_code != 200:
                assert answer.status_code == 400, query
                continue
            found = json.loads(answer.content)
            assert len(found["members"]) <= cap, query
            assert found["per_page"] == cap
            for row in found["members"]:
                assert set(row) <= ROW_KEYS, set(row) - ROW_KEYS
            assert "attachment" not in answer.headers.get("Content-Disposition", "")
        first = json.loads(
            call(route, {}, request_to(route, "GET", made.administrator)).content  # type: ignore[union-attr]
        )
        assert first["count"] > cap and len(first["members"]) == cap


def test_every_list_answers_at_most_a_page_whatever_is_asked() -> None:
    made = made_up.everything()
    _many_members(made, 40)
    with application_login():
        for name, route in admin_routes().items():
            if name not in LISTS and name not in LISTED:
                continue
            for query in ({}, {"per_page": "500", "limit": "500", "all": "1"}):
                _, kwargs = route.path()
                answer = call(
                    route,
                    kwargs,
                    request_to(route, "GET", made.administrator, query=query),
                )
                if not isinstance(answer, HttpResponse) or answer.status_code != 200:
                    continue
                found = json.loads(answer.content)
                if "per_page" not in found:
                    continue
                assert found["per_page"] == audit_log.PER_PAGE, name
                for key, value in found.items():
                    if isinstance(value, list) and key not in {
                        "statuses",
                        "roles",
                        "parsers",
                        "kinds",
                        "actions",
                        "administrators",
                    }:
                        assert len(value) <= audit_log.PER_PAGE, (name, key)


def test_a_runs_page_names_at_most_a_page_of_the_members_it_disabled() -> None:
    from randirandi.core import member_imports
    from randirandi.core.models import MemberImport

    made = made_up.everything()
    _many_members(made, 60)
    ids = list(
        Member.objects.of(made.organization)
        .filter(full_name__startswith="Person")
        .values_list("pk", flat=True)
    )
    run = MemberImport.objects.of(made.organization).get()
    run.report = {"disabled": ids, "future_bookings": []}
    run.save()
    found = member_imports.run_page(made.organization, 1)
    assert found is not None
    assert len(found["disabled"]) == audit_log.PER_PAGE
    assert found["disabled_total"] == 60


def test_the_page_is_read_before_the_event_and_neither_without_the_other() -> None:
    from unittest.mock import patch as patched

    from randirandi.core import member_pages
    from randirandi.core.audit import record_event as write

    made = made_up.everything()
    route = admin_routes()["admin-members-list"]
    order: list[str] = []
    read = member_pages.members

    def spy_read(*args: Any, **kwargs: Any) -> Any:
        order.append("read")
        return read(*args, **kwargs)

    def spy_write(*args: Any, **kwargs: Any) -> Any:
        order.append("event")
        return write(*args, **kwargs)

    with application_login():
        with (
            patched.object(member_pages, "members", spy_read),
            patched("randirandi.core.admin_views.record_event", spy_write),
        ):
            answer = call(route, {}, request_to(route, "GET", made.administrator))
        assert status_of(answer) == 200 and order == ["read", "event"]
