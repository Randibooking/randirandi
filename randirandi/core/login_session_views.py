# SPDX-License-Identifier: AGPL-3.0-only
"""Email-code verification, pending administrator factor, home and logout."""

import re
import uuid
from typing import Any

from django import forms
from django.contrib.auth import logout as end_session
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from randirandi import labels
from randirandi.core.admin_views import confirmed_administrator
from randirandi.core.authentication import (
    establish_session,
    flush_keeping_order,
    landing,
)
from randirandi.core.booking_views import _signed_in, choices_payload
from randirandi.core.login_codes import record_refusal, verify_code
from randirandi.core.member_data import utc_now
from randirandi.core.models import Member, Membership, Organization
from randirandi.core.network import client_address
from randirandi.core.organization_request import organization_for_request
from randirandi.core.presentation import browser_context


class CodeForm(forms.Form):
    code = forms.CharField(
        label="Email code",
        max_length=6,
        min_length=6,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "class": "fallback-input",
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "pattern": "[0-9]{6}",
                "aria-describedby": "code-help code-error",
            }
        ),
    )

    def __init__(
        self, *args: Any, organization: Organization | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fields["code"].label = labels.word(organization, "login.code-label")


@never_cache
@sensitive_post_parameters("code")
@require_http_methods(["GET", "HEAD", "POST"])
@login_not_required
def email_code(request: HttpRequest) -> HttpResponse:
    org = organization_for_request(request)
    browser = request.session.get("login_browser")
    challenge = request.session.get("login_challenge")
    if (
        request.session.get("login_organization") != org.pk
        or not isinstance(browser, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", browser)
        or not isinstance(challenge, str)
        or not re.fullmatch(r"[0-9a-f]{32}", challenge)
    ):
        if request.method == "POST":
            record_refusal(
                organization=org, kind="verify", network=client_address(request)
            )
        return redirect("login")
    form = CodeForm(
        request.POST if request.method == "POST" else None, organization=org
    )
    status = 200
    error = ""
    if request.method == "POST":
        # Invalid submissions still consume an attempt in the requesting browser.
        valid = form.is_valid()
        proof = verify_code(
            organization=org,
            challenge=request.session["login_challenge"],
            browser=request.session["login_browser"],
            code=form.cleaned_data["code"] if valid else request.POST.get("code", ""),
            network=client_address(request),
        )
        if proof is not None:
            if proof.member.is_staff:
                flush_keeping_order(request.session)
                request.session["pending_member"] = proof.member.pk
                request.session["pending_organization"] = org.pk
                request.session["pending_authority"] = proof.authority
                request.session["pending_at"] = utc_now(None).timestamp()
                request.session.set_expiry(600)
                return redirect("authenticator")
            if establish_session(request, proof):
                return redirect(landing(request))
        status = 400
        error = labels.word(org, "login.code-error")
        form = CodeForm(organization=org)
    ui = {
        **browser_context(request, org),
        "screen": "code",
        "organization": org.name,
        "action": "/login/code/",
        "csrf": get_token(request),
        "error": error,
    }
    words = {
        "page_title": labels.word(org, "login.code-page-title"),
        "title": labels.word(org, "login.code-title"),
        "help": labels.word(org, "login.code-help"),
        "submit": labels.word(org, "login.title"),
        "new": labels.word(org, "login.code-new"),
    }
    response = render(
        request,
        "core/code.html",
        {"ui": ui, "form": form, "words": words},
        status=status,
    )
    response["Referrer-Policy"] = "same-origin"
    return response


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def home(request: HttpRequest) -> HttpResponse:
    if _signed_in(request) is not None:
        # The payment return's `order` goes on with it.
        query = request.GET.urlencode()
        return redirect(f"{reverse('book')}?{query}" if query else "book")
    org = organization_for_request(request)
    ui = {
        **browser_context(request, org),
        "screen": "home",
        "organization": org.name,
        "csrf": get_token(request),
    }
    return render(request, "core/home.html", {"ui": ui, "word_organization": org})


def _memberships(org: Organization, member: Member) -> list[dict[str, str]]:
    """The member's own memberships for My page: the identifier and the address
    with its unit, postal code and city. Nothing of the other members of a
    membership."""
    rows = []
    for membership in (
        Membership.objects.of(org)
        .filter(members=member)
        .order_by("identifier", "address", "pk")
    ):
        street = " ".join(filter(None, [membership.address, membership.address_unit]))
        town = " ".join(filter(None, [membership.postal_code, membership.city]))
        rows.append(
            {
                "identifier": membership.identifier,
                "address": ", ".join(filter(None, [street, town])),
            }
        )
    return rows


def _price_categories(org: Organization) -> list[dict[str, object]]:
    """The columns of the prices page: the booking categories of the
    organization's standard role, the prices its members pay , whatever the signed-in
    member's
    own role holds. Without a standard role, none."""
    standard = org.standard_role
    if standard is None:
        return []
    return [
        {"id": row.pk, "name": row.name, "icon": row.icon}
        for row in standard.booking_categories.order_by("pk")
    ]


def member_page_config(
    request: HttpRequest,
    member: Member,
    org: Organization,
    screen: str,
    order: uuid.UUID | None = None,
) -> dict[str, object]:
    """What a page of the member shell carries for the signed-in `member`: the
    member pages' own, and the price list and the booking conditions when
    a member opens them (`public_pages`), so the shell around them is the same."""
    ui: dict[str, object] = {
        **browser_context(request, org),
        "screen": screen,
        "organization": org.name,
        "csrf": get_token(request),
    }
    if screen in ("book", "cart", "order", "prices", "events"):
        # The calendar's first question starts at once, not after
        # /booking/choices/; the cart's groups follow the types' order.
        ui["choices"] = choices_payload(member)
        ui["price_categories"] = _price_categories(org)
    if order is not None:
        ui["order"] = str(order)
    # Every member page: My page reached from Book in the app is not reloaded,
    # and it names the member. No currency: each booking says its own (7.3).
    ui["member_name"] = (
        member.full_name or f"{member.first_name} {member.last_name}".strip()
    )
    # The role in the setup's own words, under the name on My page.
    ui["member_role"] = member.role.name
    ui["member_email"] = member.email
    ui["member_phone"] = member.phone
    ui["member_memberships"] = _memberships(org, member)
    # The words of the organization's language and its own: the page holds the
    # English defaults itself.
    ui["labels"] = labels.valid(org)
    ui["admin"] = confirmed_administrator(request) is not None
    return ui


def _place(
    request: HttpRequest, screen: str, order: uuid.UUID | None = None
) -> HttpResponse:
    """A member page: the shell with one of its two places, or the cart or an
    order's outcome (Book stays the chosen place). A signed-in non-member gets
    the 403 page."""
    signed_in = _signed_in(request)
    if signed_in is None:
        raise PermissionDenied
    ui = member_page_config(request, signed_in[0], signed_in[1], screen, order)
    return render(
        request,
        f"core/{screen}.html",
        {"ui": ui, "word_organization": signed_in[1]},
    )


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def book(request: HttpRequest) -> HttpResponse:
    return _place(request, "book")


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def cart(request: HttpRequest) -> HttpResponse:
    return _place(request, "cart")


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def prices(request: HttpRequest) -> HttpResponse:
    """The price list : a view of Book, for the signed-in members
    of the organization only; the app draws it from the calendar's own answers."""
    return _place(request, "prices")


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def order(request: HttpRequest, order_public_id: uuid.UUID) -> HttpResponse:
    return _place(request, "order", order_public_id)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def me(request: HttpRequest) -> HttpResponse:
    return _place(request, "me")


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def events(request: HttpRequest) -> HttpResponse:
    """The members' Events : a place beside Book and My
    page; the app draws it from the events call."""
    return _place(request, "events")


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def event(request: HttpRequest, event: int) -> HttpResponse:
    """One event's page in Events; an unknown or foreign event reads as none."""
    return _place(request, "events")


@never_cache
@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    organization_for_request(request)
    end_session(request)
    return redirect("login")
