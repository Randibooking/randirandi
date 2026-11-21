# SPDX-License-Identifier: AGPL-3.0-only
"""The calls of the Price books . Administrators of their own
organization only (the gate of `admin_views`); a change needs the page's CSRF
token and answers the place as it is after the change, a refusal is 409 with
its code and, where the model gives one, its reason."""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import price_admin
from randirandi.core.admin_setup_views import _change, _gate


@never_cache
@login_required
@require_GET
def prices_data(request: HttpRequest) -> HttpResponse:
    """The price books, the containers with payment and their prices."""
    member = _gate(request)
    return JsonResponse(price_admin.prices_place(member.organization))


@never_cache
@login_required
@require_POST
def book_make(request: HttpRequest) -> HttpResponse:
    """Add a price book {"name", "currency"?}. A refusal is 409 with its code:
    "invalid-name", "name-taken", "unknown-currency", "invalid" or "busy"."""
    return _change(
        request,
        None,
        lambda who, body: price_admin.make_book(who, body),
        price_admin.prices_place,
    )


@never_cache
@login_required
@require_POST
def book_change(request: HttpRequest, pk: int) -> HttpResponse:
    """Rename a price book or change its currency: any of "name" and
    "currency" (refused with "currency-fixed" once the book has a price)."""
    return _change(
        request,
        pk,
        lambda who, body: price_admin.change_book(who, pk, body),
        price_admin.prices_place,
    )


@never_cache
@login_required
@require_POST
def book_remove(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a price book. A refusal is 409 "in-use" with "used": the
    containers that name it and the booking lines made at it."""
    return _change(
        request,
        pk,
        lambda who, body: price_admin.remove_book(who, pk),
        price_admin.prices_place,
    )


def _stamp_asked(body: dict[str, object]) -> str:
    """The stamp of the prices the sheet saw: the screens always send it, so a
    request without one is malformed (the service leaves it optional)."""
    seen = price_admin.the_stamp(body)
    if seen is None:
        raise ValueError("The stamp of what the sheet saw is missing.")
    return seen


@never_cache
@login_required
@require_POST
def prices_save(request: HttpRequest, container: int, category: int) -> HttpResponse:
    """Save the prices of a container in a booking category, as a set:
    {"entries": [{"group", "tiers_go_by", "billing_basis", "tiers": [...]}]}.
    A refusal is 409 with its code and the model's reason: "container-without-
    payment", "category-not-on-container", "billing-basis",
    "group-not-on-floor-plan", "group-twice", "needs-normal-price",
    "groups-per-spot", "no-tier", "tiers-overlap", "tiers-incomplete",
    "invalid-tier", "invalid-tiers-go-by", "invalid-billing-basis"."""
    if not 1 <= category <= 2**62:
        return JsonResponse({"error": "not-found"}, status=404)
    return _change(
        request,
        container,
        lambda who, body: price_admin.save_prices(
            who, container, category, {**body, "stamp": _stamp_asked(body)}
        ),
        price_admin.prices_place,
    )


@never_cache
@login_required
@require_POST
def prices_remove(request: HttpRequest, container: int, category: int) -> HttpResponse:
    """Remove the prices of a container in a booking category."""
    if not 1 <= category <= 2**62:
        return JsonResponse({"error": "not-found"}, status=404)
    return _change(
        request,
        container,
        lambda who, body: price_admin.remove_prices(
            who, container, category, _stamp_asked(body)
        ),
        price_admin.prices_place,
    )
