# SPDX-License-Identifier: AGPL-3.0-only
"""The calls of the Containers . Administrators of their own
organization only (the gate of `admin_views`); a change needs the page's CSRF
token and answers the place as it is after the change. A refusal is 409 with
its code, and with the model's words as "reasons" when the validator or the
model's own validation refused."""

from collections.abc import Callable

from django.contrib.auth.decorators import login_required
from django.db import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import container_admin, setup_admin
from randirandi.core.admin_setup_views import LARGEST_ID, _body, _gate
from randirandi.core.admin_views import BAD_REQUEST, NOT_FOUND
from randirandi.core.models import Member, Organization


def _change(
    request: HttpRequest,
    pk: int | None,
    run: Callable[[Member, dict[str, object]], object],
) -> HttpResponse:
    """The gate, the body, the refusals, and the place read again after the
    change (with the new container's id after an add)."""
    member = _gate(request)
    if pk is not None and not 1 <= pk <= LARGEST_ID:
        return JsonResponse(NOT_FOUND, status=404)
    made = None
    try:
        made = run(member, _body(request))
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    except setup_admin.SetupNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except setup_admin.SetupRefusedError as refused:
        answer: dict[str, object] = {"error": refused.code}
        if refused.used:
            answer["used"] = refused.used
        if isinstance(refused, container_admin.ContainerRefusedError):
            answer["reasons"] = refused.reasons
        return JsonResponse(answer, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    place = container_admin.containers_place(
        Organization.objects.get(pk=member.organization_id)
    )
    if isinstance(made, int):
        place["made"] = made
    return JsonResponse(place)


@never_cache
@login_required
@require_GET
def containers_data(request: HttpRequest) -> HttpResponse:
    """Every container with its categories, and the lists it chooses from
    (reading writes no event)."""
    member = _gate(request)
    return JsonResponse(container_admin.containers_place(member.organization))


@never_cache
@login_required
@require_POST
def container_make(request: HttpRequest) -> HttpResponse:
    """Add a container {"asset", "booking_type", "period_type", "uses"}: it
    starts disabled with every booking category. The answer carries "made",
    its id."""
    return _change(
        request,
        None,
        lambda who, body: container_admin.make_container(who, body),
    )


@never_cache
@login_required
@require_POST
def container_change(request: HttpRequest, pk: int) -> HttpResponse:
    """Change a container's details, time, weeks, payment or status; any of
    them left out stays. A refusal is 409 "refused" with "reasons"."""
    return _change(
        request,
        pk,
        lambda who, body: container_admin.change_container(who, pk, body),
    )


@never_cache
@login_required
@require_POST
def container_categories(request: HttpRequest, pk: int) -> HttpResponse:
    """Set a container's booking categories and their own hours (the whole
    list, with "seen", the categories and hours the sheet saw; a stale sheet is
    refused under the lock).
    A refusal is 409 "refused" with "reasons", "no-category" or
    "unknown-category"."""

    return _change(
        request,
        pk,
        lambda who, body: container_admin.change_categories(who, pk, body),
    )


@never_cache
@login_required
@require_POST
def container_remove(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a container nothing names. A refusal is 409 "in-use" with
    "used": bookings, blocks, prices."""
    return _change(
        request,
        pk,
        lambda who, body: container_admin.remove_container(who, pk),
    )
