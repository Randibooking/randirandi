# SPDX-License-Identifier: AGPL-3.0-only
"""The calls of the Booking types and categories and the Roles ,
and the Organization's own details (7.99).
Administrators of their own organization only (the gate of `admin_views`);
a change needs the page's CSRF token and answers the place as it is after the
change, a refusal is 409 with its code."""

import json
from collections.abc import Callable

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import organization_admin, setup_admin
from randirandi.core.admin_views import BAD_REQUEST, NOT_FOUND, confirmed_administrator
from randirandi.core.models import Member, Organization
from randirandi.core.network import client_address
from randirandi.core.second_factor import CodeRefusedError

LARGEST_ID = 2**62


def _gate(request: HttpRequest) -> Member:
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    return member


def _body(request: HttpRequest) -> dict[str, object]:
    body = json.loads(request.body)
    if not isinstance(body, dict):
        raise ValueError("The body is an object.")
    return body


def _change(
    request: HttpRequest,
    pk: int | None,
    run: Callable[[Member, dict[str, object]], None],
    place: Callable[[Organization], dict[str, object]],
) -> HttpResponse:
    """The common part of every change: the gate, the body, the refusals, and
    the place read again after the change."""
    member = _gate(request)
    if pk is not None and not 1 <= pk <= LARGEST_ID:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        body = _body(request)
        run(member, body)
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    except setup_admin.SetupNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except CodeRefusedError as refused_code:
        code = "code-limited" if refused_code.limited else "code-refused"
        return JsonResponse({"error": code}, status=409)
    except setup_admin.SetupRefusedError as refused:
        answer: dict[str, object] = {"error": refused.code}
        if refused.used:
            answer["used"] = refused.used
        if refused.reason:
            answer["reason"] = refused.reason
        answer.update(refused.details)
        return JsonResponse(answer, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(place(Organization.objects.get(pk=member.organization_id)))


def _list(name: str) -> type[setup_admin.Item] | None:
    return setup_admin.LISTS.get(name)


@never_cache
@login_required
@require_GET
def types_data(request: HttpRequest) -> HttpResponse:
    """Both lists, in the order members see them (reading writes no event)."""
    member = _gate(request)
    return JsonResponse(setup_admin.types_place(member.organization))


@never_cache
@login_required
@require_POST
def item_make(request: HttpRequest, kind: str) -> HttpResponse:
    """Add a booking type {"name", "description"} or a booking category
    {"name", "icon"} at the end of its list. A refusal is 409 with its code:
    "invalid-name", "name-taken", "unknown-icon", "invalid" or "busy"."""
    model = _list(kind)
    if model is None:
        _gate(request)
        return JsonResponse(NOT_FOUND, status=404)
    return _change(
        request,
        None,
        lambda who, body: setup_admin.make_item(who, model, body),
        setup_admin.types_place,
    )


@never_cache
@login_required
@require_POST
def item_change(request: HttpRequest, kind: str, pk: int) -> HttpResponse:
    """Change a booking type's name or description, or a booking category's
    name or icon; any of them left out stays. Refusals as the make's."""
    model = _list(kind)
    if model is None:
        _gate(request)
        return JsonResponse(NOT_FOUND, status=404)
    return _change(
        request,
        pk,
        lambda who, body: setup_admin.change_item(who, model, pk, body),
        setup_admin.types_place,
    )


@never_cache
@login_required
@require_POST
def item_move(request: HttpRequest, kind: str, pk: int) -> HttpResponse:
    """Move a row one place: {"direction": "up" | "down"}, as the list is when
    the call is served. The answer is the lists after the move."""
    model = _list(kind)
    if model is None:
        _gate(request)
        return JsonResponse(NOT_FOUND, status=404)

    def run(who: Member, body: dict[str, object]) -> None:
        direction = body.get("direction")
        if set(body) != {"direction"} or not isinstance(direction, str):
            raise ValueError("The body is a direction.")
        setup_admin.move_item(who, model, pk, direction)

    return _change(request, pk, run, setup_admin.types_place)


@never_cache
@login_required
@require_POST
def item_remove(request: HttpRequest, kind: str, pk: int) -> HttpResponse:
    """Remove a booking type or category. A refusal is 409 "in-use" with
    "used": what holds it (containers, roles, prices, bookings)."""
    model = _list(kind)
    if model is None:
        _gate(request)
        return JsonResponse(NOT_FOUND, status=404)
    return _change(
        request,
        pk,
        lambda who, body: setup_admin.remove_item(who, model, pk),
        setup_admin.types_place,
    )


@never_cache
@login_required
@require_GET
def roles_data(request: HttpRequest) -> HttpResponse:
    """The roles and the booking categories they may be given."""
    member = _gate(request)
    return JsonResponse(setup_admin.roles_place(member.organization))


@never_cache
@login_required
@require_POST
def role_make(request: HttpRequest) -> HttpResponse:
    """Add a role {"name", "categories": [ids], "admin_access"?, "standard"?}.
    A refusal is 409 with its code: "invalid-name", "name-taken",
    "no-category", "unknown-category", "standard-admin", "invalid" or "busy"."""
    return _change(
        request,
        None,
        lambda who, body: setup_admin.make_role(who, body),
        setup_admin.roles_place,
    )


@never_cache
@login_required
@require_POST
def role_change(request: HttpRequest, pk: int) -> HttpResponse:
    """Change a role: any of "name", "categories", "admin_access" and
    "standard" (true makes it the standard role). Refusals as the make's, and
    "standard-needed" (the standard role stays one until another is made so)
    and "last-administrator"."""
    return _change(
        request,
        pk,
        lambda who, body: setup_admin.change_role(who, pk, body),
        setup_admin.roles_place,
    )


@never_cache
@login_required
@require_POST
def role_remove(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a role. A refusal is 409 "is-standard" or "has-members"."""
    return _change(
        request,
        pk,
        lambda who, body: setup_admin.remove_role(who, pk),
        setup_admin.roles_place,
    )


@never_cache
@login_required
@require_GET
def organization_data(request: HttpRequest) -> HttpResponse:
    """The organization's details with their version, its roles, and the time
    zones and currencies it may choose from (reading writes no event)."""
    member = _gate(request)
    return JsonResponse(
        organization_admin.place(Organization.objects.get(pk=member.organization_id))
    )


@never_cache
@login_required
@require_POST
def organization_change(request: HttpRequest) -> HttpResponse:
    """Save details: {"version", any of "name", "contact_email",
    "standard_role", "max_bookings_a_day", "book_ahead_months", "tax_rate",
    "member_retention_months", "booking_retention_months", "time_zone",
    "currency", and, for a lowered retention, "confirm": {"members",
    "bookings"} with "code", the authenticator code}. The answer is the
    place after the save. A refusal is 409 with its code: "invalid-<field>"
    with its "reason", "unknown-role", "standard-admin", "stale" (read again)
    and "confirm-erasure" with "erase": {"members", "bookings"} (a retention
    is lowered; send those numbers as "confirm" with a "code" to save),
    "code-refused", "code-limited" or "busy"."""
    return _change(
        request,
        None,
        lambda who, body: organization_admin.change(
            who, body, network=client_address(request)
        ),
        organization_admin.place,
    )
