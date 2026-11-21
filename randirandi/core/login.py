# SPDX-License-Identifier: AGPL-3.0-only
"""Organization-aware sign-in entry; delivery runs outside the HTTP response."""

import logging
import re
import secrets
from typing import Any

from django import forms
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from randirandi import labels
from randirandi.core.login_codes import LIFETIME, record_refusal
from randirandi.core.login_delivery import enqueue_code, wake_delivery
from randirandi.core.models import Organization
from randirandi.core.network import client_address
from randirandi.core.organization_request import organization_for_request
from randirandi.core.presentation import browser_context

logger = logging.getLogger(__name__)


class LoginForm(forms.Form):
    identifier = forms.CharField(
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "fallback-input",
                "placeholder": "name@domain",
                "autocomplete": "username",
                "autocapitalize": "none",
                "spellcheck": "false",
                "aria-describedby": "identifier-help",
            }
        ),
    )

    def __init__(
        self, *args: Any, organization: Organization | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        # What the person reads is in the organization's language.
        self.organization = organization
        field = self.fields["identifier"]
        field.label = labels.word(organization, "login.email")
        field.error_messages["required"] = labels.word(
            organization, "login.error-required"
        )

    def clean_identifier(self) -> str:
        value: str = self.cleaned_data["identifier"]
        if "@" in value:
            value = value.lower()
            try:
                validate_email(value)
            except ValidationError:
                raise ValidationError(
                    labels.word(self.organization, "login.error-email")
                ) from None
            return value
        # Match member import/storage: spaces are removed, country code required.
        # No organization or country is inferred from the visitor's location.
        value = value.replace(" ", "")
        if not re.fullmatch(r"\+[1-9][0-9]{5,14}", value):
            raise ValidationError(
                labels.word(self.organization, "login.error-identifier")
            )
        return value


@never_cache
@sensitive_post_parameters("identifier")
@require_http_methods(["GET", "HEAD", "POST"])
@login_not_required
def login(request: HttpRequest) -> HttpResponse:
    organization = organization_for_request(request)
    if request.user.is_authenticated:
        return redirect("home")
    form = LoginForm(
        request.POST if request.method == "POST" else None, organization=organization
    )
    status = 200
    unavailable = False
    if request.method == "POST":
        if form.is_valid():
            browser = request.session.get("login_browser")
            if not isinstance(browser, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{32,128}", browser
            ):
                browser = secrets.token_urlsafe(32)
                request.session["login_browser"] = browser
            request.session["login_challenge"] = enqueue_code(
                organization=organization,
                identifier=form.cleaned_data["identifier"],
                browser=browser,
                network=client_address(request),
            )
            request.session["login_organization"] = organization.pk
            # Delivery may wait one lifetime in the queue, then issue a code
            # with its own full lifetime. Retain only the opaque browser binding
            # for both windows; code verification still enforces its own expiry.
            request.session.set_expiry(int((2 * LIFETIME).total_seconds()))
            logger.info("login request: organization %s", organization.pk)
            wake_delivery()
            return redirect("email-code")
        else:
            record_refusal(
                organization=organization,
                kind="request",
                network=client_address(request),
            )
            status = 400
            form.fields["identifier"].widget.attrs.update(
                {
                    "aria-invalid": "true",
                    "aria-describedby": "identifier-help identifier-error",
                }
            )
    ui = {
        **browser_context(request, organization),
        "screen": "login",
        "organization": organization.name,
        "identifier": form["identifier"].value() or "",
        "errors": list(form.errors.get("identifier", [])),
        "unavailable": unavailable,
        "csrf": get_token(request),
    }
    response = render(
        request,
        "core/login.html",
        {
            "organization": organization,
            "form": form,
            "unavailable": unavailable,
            "ui": ui,
            "words": {
                "page_title": labels.word(
                    organization, "login.page-title", organization=organization.name
                ),
                "title": labels.word(organization, "login.title"),
                "unavailable": labels.word(organization, "login.unavailable"),
                # The address is a link: the text is escaped, then it is filled.
                "help": labels.word_html(
                    organization,
                    "login.help-page",
                    address=format_html(
                        '<a href="mailto:{0}">{0}</a>', organization.contact_email
                    ),
                ),
                "continue": labels.word(organization, "login.continue"),
                "what_login": labels.word(organization, "login.what-login"),
            },
        },
        status=status,
    )
    response["Referrer-Policy"] = "same-origin"
    return response
