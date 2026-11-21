# SPDX-License-Identifier: AGPL-3.0-only
"""Shared browser presentation, including errors that need no database access."""

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.db import DatabaseError
from django.http import Http404, HttpRequest, HttpResponse
from django.middleware.csp import get_nonce
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import never_cache

from randirandi import labels
from randirandi.core.models import Organization
from randirandi.core.organization_request import organization_for_request
from randirandi.environment import DEFAULT_DOCUMENTATION_URL
from randirandi.version import current

# The shared Help site (live since 21 November 2026); an operator may choose
# another destination with RANDIRANDI_DOCUMENTATION_URL.
DOCUMENTATION_URL = DEFAULT_DOCUMENTATION_URL


# The error pages' keys by status: the title and the message.
ERRORS = {
    400: ("login.error-400-title", "login.error-400-message"),
    403: ("login.error-403-title", "login.error-403-message"),
    404: ("login.error-404-title", "login.error-404-message"),
    500: ("login.error-500-title", "login.error-500-message"),
}


def browser_context(
    request: HttpRequest, organization: Organization | None = None
) -> dict[str, object]:
    beta_before, _, beta_after = labels.word(organization, "shell.beta").partition(
        "{address}"
    )
    return {
        # The page's language, and the texts the page draws itself: the
        # organization's own words and its language's catalogue.
        "lang": labels.language_of(organization),
        "labels": labels.valid(organization),
        "logo": static("core/brand/logo.svg"),
        "paymark": static("core/brand/vipps/Vipps_Pay_Mark_Small_RGB_290125.svg"),
        "documentation": settings.DOCUMENTATION_URL,
        "source": current().source_url,
        "version": current().version,
        "footer_source": labels.word(organization, "shell.source"),
        "nonce": str(get_nonce(request)),
        "action": "/login/",
        # What sits beside the sign-in card and in the bar below it, in the
        # organization's language (English for a page with none).
        "about_name": labels.word(organization, "shell.name"),
        # Randi's intro names the organization (the operator's own line, the term
        # list's row 35); a page with none names the product.
        "about_intro": labels.word(
            organization,
            "shell.intro",
            organization=(
                organization.name
                if organization
                else labels.word(None, "shell.product")
            ),
        ),
        "about_free": labels.word(organization, "shell.free"),
        "about_book": labels.word(organization, "shell.book"),
        "about_pay": labels.word(organization, "shell.pay"),
        "company": labels.word(organization, "shell.company"),
        # The beta bar: shell.beta around the organization's contact address,
        # as the React shell (BetaBanner.jsx). Every address a member sees is
        # the organization's own; a page with no organization has no bar.
        "beta_before": beta_before,
        "beta_after": beta_after,
        "contact_email": organization.contact_email if organization else "",
        "footer_documentation": labels.word(organization, "shell.documentation"),
        # The footer's two links beside Documentation : the public price
        # list and booking conditions of the address's organization. The
        # error page, which may have none, turns them off.
        "footer_links": True,
        "footer_prices": labels.word(organization, "shell.prices"),
        "footer_conditions": labels.word(organization, "shell.conditions"),
    }


def host_organization(request: HttpRequest) -> Organization | None:
    """The organization the host names, or None: an error page may come from
    a host that names none, or from a database that does not answer, and then
    it is the product's own English ."""
    try:
        return organization_for_request(request)
    except Http404, DisallowedHost, DatabaseError:
        return None


@never_cache
def error_page(request: HttpRequest, status: int) -> HttpResponse:
    organization = host_organization(request)
    config = {
        **browser_context(request, organization),
        "screen": "error",
        "title": labels.word(organization, ERRORS[status][0]),
        "message": labels.word(organization, ERRORS[status][1]),
        "back": labels.word(organization, "login.back"),
        # The page names no contact and links no price list: it is the same
        # page whatever went wrong.
        "contact_email": "",
        "footer_links": False,
    }
    response = render(request, "core/error.html", {"ui": config}, status=status)
    response["Referrer-Policy"] = "same-origin"
    return response


def bad_request(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_page(request, 400)


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_page(request, 403)


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_page(request, 404)


def server_error(request: HttpRequest) -> HttpResponse:
    return error_page(request, 500)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    return error_page(request, 403)
