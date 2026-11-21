# SPDX-License-Identifier: AGPL-3.0-only
"""The calls of the Assets and Spaces places : spaces, assets,
floor plans, spots and groups of spots. Administrators of their own
organization only (the gate of `admin_views`); a change needs the page's CSRF
token and answers {"place": the place as it is after the change, "plan": the
floor plan it changed, with its spots and groups, or null}. A refusal is 409
with its code, and the model's own words when it has them."""

from collections.abc import Callable

from django.contrib.auth.decorators import login_required
from django.db import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import asset_admin
from randirandi.core.admin_setup_views import LARGEST_ID, _body, _gate
from randirandi.core.admin_views import BAD_REQUEST, NOT_FOUND
from randirandi.core.models import Member, Organization
from randirandi.core.setup_admin import SetupNotFoundError, SetupRefusedError


def _change(
    request: HttpRequest,
    ids: tuple[int, ...],
    run: Callable[[Member, dict[str, object]], int | None],
) -> HttpResponse:
    """The common part of every change: the gate, the ids, the body, the
    refusals, and the place read again after the change."""
    member = _gate(request)
    if any(not 1 <= pk <= LARGEST_ID for pk in ids):
        return JsonResponse(NOT_FOUND, status=404)
    try:
        body = _body(request)
        plan = run(member, body)
        org = Organization.objects.get(pk=member.organization_id)
        answer = {
            "place": asset_admin.assets_place(org),
            "plan": asset_admin.plan_place(org, plan) if plan is not None else None,
        }
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    except SetupNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except SetupRefusedError as refused:
        refusal: dict[str, object] = {"error": refused.code}
        if refused.used:
            refusal["used"] = refused.used
        message = getattr(refused, "message", "")
        if message:
            refusal["message"] = message
        return JsonResponse(refusal, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(answer)


@never_cache
@login_required
@require_GET
def place_data(request: HttpRequest) -> HttpResponse:
    """The spaces and the assets (reading writes no event)."""
    member = _gate(request)
    return JsonResponse(asset_admin.assets_place(member.organization))


@never_cache
@login_required
@require_GET
def plan_data(request: HttpRequest, plan: int) -> HttpResponse:
    """One floor plan with its spots and groups."""
    member = _gate(request)
    if not 1 <= plan <= LARGEST_ID:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        return JsonResponse(asset_admin.plan_place(member.organization, plan))
    except SetupNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)


# --- Spaces ---------------------------------------------------------------------


@never_cache
@login_required
@require_POST
def space_make(request: HttpRequest) -> HttpResponse:
    """Add a space {"name", "description"?, "booking_page"?}. A refusal is 409:
    "invalid-name", "name-taken", "unknown-booking-page", "invalid" or "busy"."""
    return _change(request, (), lambda who, body: asset_admin.make_space(who, body))


@never_cache
@login_required
@require_POST
def space_change(request: HttpRequest, pk: int) -> HttpResponse:
    """Change a space's name, description or booking page; any left out stays."""
    return _change(
        request, (pk,), lambda who, body: asset_admin.change_space(who, pk, body)
    )


@never_cache
@login_required
@require_POST
def space_remove(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a space no asset is in. A refusal is 409 "in-use" with "used"."""
    return _change(request, (pk,), lambda who, body: asset_admin.remove_space(who, pk))


# --- Assets ---------------------------------------------------------------------


@never_cache
@login_required
@require_POST
def asset_make(request: HttpRequest) -> HttpResponse:
    """Add an asset {"name", "description"?, "space"? (id or null),
    "whole_space"?, "maximum_occupancy"? (a number or null), "time_zone"? ("" is
    the organization's)}. It gets a floor plan of one spot without a name.
    Refusals: "invalid-name", "name-taken", "unknown-space", "whole-space",
    "time-zone", "space", "invalid-occupancy" and "invalid", the model's words
    in "message"."""
    return _change(request, (), lambda who, body: asset_admin.make_asset(who, body))


@never_cache
@login_required
@require_POST
def asset_change(request: HttpRequest, pk: int) -> HttpResponse:
    """Change any of an asset's fields. Refusals as the make's."""
    return _change(
        request, (pk,), lambda who, body: asset_admin.change_asset(who, pk, body)
    )


@never_cache
@login_required
@require_POST
def asset_remove(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove an asset nothing holds. A refusal is 409 "in-use" with "used":
    containers, availability, bookings."""
    return _change(request, (pk,), lambda who, body: asset_admin.remove_asset(who, pk))


# --- Floor plans ----------------------------------------------------------------


@never_cache
@login_required
@require_POST
def plan_make(request: HttpRequest, asset: int) -> HttpResponse:
    """Add a floor plan to an asset {"name"?, "word", "spots": a count}.
    Refusals: "invalid-name", "name-needed", "name-taken", "other-unnamed",
    "invalid-word", "too-many-spots"."""
    return _change(
        request,
        (asset,),
        lambda who, body: asset_admin.make_floor_plan(who, asset, body),
    )


@never_cache
@login_required
@require_POST
def plan_change(request: HttpRequest, plan: int) -> HttpResponse:
    """Change a floor plan's name or its spot word."""
    return _change(
        request,
        (plan,),
        lambda who, body: asset_admin.change_floor_plan(who, plan, body),
    )


@never_cache
@login_required
@require_POST
def plan_remove(request: HttpRequest, plan: int) -> HttpResponse:
    """Remove a floor plan. A refusal is 409 "last-plan" or "in-use" with
    "used": containers, bookings, prices, lines."""
    return _change(
        request, (plan,), lambda who, body: asset_admin.remove_floor_plan(who, plan)
    )


# --- Spots ----------------------------------------------------------------------


@never_cache
@login_required
@require_POST
def spots_make(request: HttpRequest, plan: int) -> HttpResponse:
    """Add spots {"count", "group"?}; each takes the next number."""
    return _change(
        request, (plan,), lambda who, body: asset_admin.make_spots(who, plan, body)
    )


@never_cache
@login_required
@require_POST
def spot_change(request: HttpRequest, plan: int, spot: int) -> HttpResponse:
    """Change a spot's "name" ("" for none) or "group" (an id or null).
    Refusals: "invalid-name", "name-taken" (with "used": {"spot": number}),
    "unknown-group"."""
    return _change(
        request,
        (plan, spot),
        lambda who, body: asset_admin.change_spot(who, plan, spot, body),
    )


@never_cache
@login_required
@require_POST
def spot_remove(request: HttpRequest, plan: int, spot: int) -> HttpResponse:
    """Remove a spot. A refusal is 409 "last-spot" or "in-use" with "used":
    bookings."""
    return _change(
        request,
        (plan, spot),
        lambda who, body: asset_admin.remove_spot(who, plan, spot),
    )


# --- Groups ---------------------------------------------------------------------


@never_cache
@login_required
@require_POST
def group_make(request: HttpRequest, plan: int) -> HttpResponse:
    """Add a group {"name", "word"?, "spots"?: [spot ids]}."""
    return _change(
        request, (plan,), lambda who, body: asset_admin.make_group(who, plan, body)
    )


@never_cache
@login_required
@require_POST
def group_change(request: HttpRequest, plan: int, group: int) -> HttpResponse:
    """Change a group's "name", "word" or "spots" (the spot ids it holds
    after the change)."""
    return _change(
        request,
        (plan, group),
        lambda who, body: asset_admin.change_group(who, plan, group, body),
    )


@never_cache
@login_required
@require_POST
def group_remove(request: HttpRequest, plan: int, group: int) -> HttpResponse:
    """Remove a group. A refusal is 409 "has-spots" or "in-use"."""
    return _change(
        request,
        (plan, group),
        lambda who, body: asset_admin.remove_group(who, plan, group),
    )
