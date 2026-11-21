# SPDX-License-Identifier: AGPL-3.0-only
"""The price list and the booking conditions .

Two public pages of the organization of the address, readable without signing
in: `/prices/` (the standard role's categories and prices, as members see
them in Book, from public answers that hold no member data) and
`/conditions/` (the conditions in force, written into the page so that it
reads without JavaScript). An order's own frozen text is by its link, to its
member only: `/book/order/<id>/conditions/`.
"""

import uuid

from django.contrib.auth.decorators import login_not_required, login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from randirandi import labels
from randirandi.core import booking_conditions, booking_views, price_table
from randirandi.core.booking_views import _signed_in, choices_payload
from randirandi.core.login_session_views import _price_categories, member_page_config
from randirandi.core.models import Member, MemberStatus, Order, Organization
from randirandi.core.organization_request import organization_for_request
from randirandi.core.presentation import browser_context


def _stand_in(org: Organization) -> Member | None:
    """The member whose role the price list is read for: an active member of
    the organization's standard role, the first made. The calendar and the
    choices are worked out for a member; the public answers keep nothing of
    the member in them (`booking_views.calendar_answer`, `public`)."""
    if org.standard_role_id is None:
        return None
    return (
        Member.objects.of(org)
        .filter(role_id=org.standard_role_id, status=MemberStatus.ACTIVE)
        .order_by("pk")
        .first()
    )


def _page(
    request: HttpRequest,
    org: Organization,
    screen: str,
    template: str,
    **more: object,
) -> HttpResponse:
    """A public page. A signed-in member of the organization of the address gets
    it inside the member shell, with Book, Events and My page as everywhere
    else (the prices as the screen `prices`, the conditions as `conditions`
    with `member`); anyone else gets the header with Sign in."""
    signed_in = _signed_in(request)
    if signed_in is not None:
        shell = "prices" if screen == "prices-public" else screen
        ui = {
            **member_page_config(request, signed_in[0], org, shell),
            **more,
            **({"member": True} if shell == "conditions" else {}),
        }
    else:
        ui = {
            **browser_context(request, org),
            "screen": screen,
            "organization": org.name,
            "csrf": get_token(request),
            # The words of the organization's language and its own: the page
            # holds the English defaults.
            "labels": labels.valid(org),
            **more,
        }
    return render(
        request,
        template,
        {
            "ui": ui,
            "word_organization": org,
            "text": ui.get("conditions"),
            "member": signed_in is not None,
            "table": _table(org, signed_in[0] if signed_in else None)
            if screen == "prices-public"
            else None,
        },
    )


def _public_choices(org: Organization) -> dict[str, object] | None:
    """What `/booking/choices/` answers for the standard role, to anyone: the
    containers and their categories, none of a member. None without a standard
    member to read it for."""
    member = _stand_in(org)
    if member is None:
        return None
    found = choices_payload(member)
    # The stand-in's own last choice is no one's to read.
    categories = found["categories"]
    assert isinstance(categories, list)  # noqa: S101
    found["category"] = categories[0]["id"] if categories else None
    return found


def _table(org: Organization, signed_in: Member | None) -> price_table.Data | None:
    """The price table the server draws for a browser without JavaScript, from
    the answers the app reads. The app reads the signed-in member's own choices
    (`choices_payload`: the page it opens on, the types' order) and the standard
    role's prices; a visitor's page is the standard role's own
    (`prices_choices`, `prices_calendar`)."""
    if signed_in is not None:
        member: Member | None = signed_in
        choices: dict[str, object] | None = choices_payload(signed_in)
    else:
        member = _stand_in(org)
        choices = _public_choices(org)
    if member is None or choices is None:
        return None
    found = price_table.table(org, member, choices, _price_categories(org))
    return None if found is None else price_table.kept(found)


@never_cache
@require_GET
@login_not_required
def prices_page(request: HttpRequest) -> HttpResponse:
    """The price list: a shell the app draws from the two answers below; the
    page itself holds the standard role's table for a browser without JavaScript."""
    org = organization_for_request(request)
    return _page(
        request,
        org,
        "prices-public",
        "core/public_prices.html",
        price_categories=_price_categories(org),
    )


@never_cache
@require_GET
@login_not_required
def prices_choices(request: HttpRequest) -> HttpResponse:
    """What `/booking/choices/` answers for the standard role, to anyone: the
    containers and their categories, none of a member."""
    found = _public_choices(organization_for_request(request))
    if found is None:
        return JsonResponse(
            {"categories": [], "category": None, "assets": [], "spaces": []}
        )
    return JsonResponse(found)


@never_cache
@require_GET
@login_not_required
def prices_calendar(request: HttpRequest) -> HttpResponse:
    """What `/booking/calendar/?prices=1` answers, to anyone: the standard
    role's categories and prices, no taken time and no member."""
    org = organization_for_request(request)
    member = _stand_in(org)
    if member is None:
        return JsonResponse({"error": "not-found"}, status=404)
    return booking_views.calendar_answer(
        request, member, org, price_list=True, public=True
    )


@never_cache
@require_GET
@login_not_required
def conditions_page(request: HttpRequest) -> HttpResponse:
    """The booking conditions in force for the organization of the address."""
    org = organization_for_request(request)
    text = booking_conditions.current(org).page()
    return _page(request, org, "conditions", "core/conditions.html", conditions=text)


@never_cache
@require_GET
@login_required
def order_conditions_page(
    request: HttpRequest, order_public_id: uuid.UUID
) -> HttpResponse:
    """The text an order accepted, as it was frozen; to the member whose order
    it is. Another member's order, an order that does not exist and an order
    from before M11, which accepted nothing, are the same 404."""
    signed_in = _signed_in(request)
    if signed_in is None:
        raise Http404
    member, org = signed_in
    found = (
        Order.objects.of(org)
        .filter(public_id=order_public_id, member_id=member.pk)
        .select_related("conditions")
        .first()
    )
    if found is None or found.conditions is None:
        raise Http404
    text = booking_conditions.kept(found.conditions).page()
    return _page(request, org, "conditions", "core/conditions.html", conditions=text)
