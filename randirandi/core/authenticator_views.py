# SPDX-License-Identifier: AGPL-3.0-only
"""Second-factor pages require an unexpired server-held email proof."""

import math
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Concatenate

from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from randirandi import labels
from randirandi.core import authenticator_secret
from randirandi.core.authentication import establish_session, landing
from randirandi.core.login_codes import VerifiedCode, record_refusal
from randirandi.core.login_session_views import CodeForm
from randirandi.core.member_data import utc_now
from randirandi.core.models import Authenticator, Member, Organization
from randirandi.core.network import client_address
from randirandi.core.organization_request import organization_for_request
from randirandi.core.otp import enrollment
from randirandi.core.presentation import browser_context
from randirandi.core.second_factor import prepare_authenticator, verify_authenticator


def without_key[**P](
    view: Callable[Concatenate[HttpRequest, P], HttpResponse],
) -> Callable[Concatenate[HttpRequest, P], HttpResponse]:
    """A server with no authenticator key (a laptop that did not set one) cannot
    seal or open a secret: say so plainly, as a refusal and not a server error."""

    @wraps(view)
    def guarded(
        request: HttpRequest, /, *args: P.args, **kwargs: P.kwargs
    ) -> HttpResponse:
        try:
            return view(request, *args, **kwargs)
        except authenticator_secret.KeyMissingError:
            return HttpResponse(
                labels.word(
                    organization_for_request(request), "login.auth-unavailable"
                ),
                status=503,
                content_type="text/plain",
            )

    return guarded


def pending_proof(request: HttpRequest, org: Organization) -> VerifiedCode | None:
    started = request.session.get("pending_at")
    member_id = request.session.get("pending_member")
    authority = request.session.get("pending_authority")
    if (
        request.session.get("pending_organization") != org.pk
        or type(member_id) is not int
        or not isinstance(authority, str)
        or not isinstance(started, (int, float))
        or isinstance(started, bool)
        or not math.isfinite(started)
        or not 0 <= utc_now(None).timestamp() - started < 600
    ):
        return None
    member = Member.objects.of(org).select_related("role").filter(pk=member_id).first()
    if (
        member is None
        or not member.is_staff
        or not constant_time_compare(authority, member.get_session_auth_hash())
    ):
        return None
    return VerifiedCode(member, authority)


@never_cache
@sensitive_post_parameters("code")
@require_http_methods(["GET", "HEAD", "POST"])
@login_not_required
@without_key
def authenticator(request: HttpRequest) -> HttpResponse:
    org = organization_for_request(request)
    if request.user.is_authenticated:
        return redirect("home")
    proof = pending_proof(request, org)
    if proof is None:
        if request.method == "POST":
            member_id = request.session.get("pending_member")
            record_refusal(
                organization=org,
                kind="mfa",
                network=client_address(request),
                member_id=(
                    member_id
                    if request.session.get("pending_organization") == org.pk
                    and type(member_id) is int
                    else None
                ),
            )
        request.session.flush()
        return redirect("login")
    device = prepare_authenticator(proof)
    if device is None:
        if request.method == "POST":
            record_refusal(
                organization=org,
                kind="mfa",
                network=client_address(request),
                member_id=proof.member.pk,
            )
        request.session.flush()
        return redirect("login")
    form = CodeForm(request.POST if request.method == "POST" else None)
    error, status = "", 200
    if request.method == "POST":
        valid = form.is_valid()
        confirmed = verify_authenticator(
            proof,
            form.cleaned_data["code"] if valid else request.POST.get("code", ""),
            network=client_address(request),
        )
        if confirmed is not None and establish_session(
            request, confirmed, admin_confirmed=True
        ):
            return redirect(landing(request))
        error, status = labels.word(org, "login.auth-error"), 400
        form = CodeForm()
        device = Authenticator.objects.of(org).filter(pk=device.pk).first()
        if device is None or pending_proof(request, org) is None:
            request.session.flush()
            return redirect("login")
    form.fields["code"].label = labels.word(org, "login.auth-code")
    key = (
        enrollment(
            device.plain_secret(), organization=org.name, member_id=proof.member.pk
        )[0]
        if not device.confirmed
        else ""
    )
    ui = {
        **browser_context(request, org),
        "screen": "authenticator",
        "organization": org.name,
        "action": "/login/authenticator/",
        "csrf": get_token(request),
        "error": error,
        "enroll": not device.confirmed,
        "setupKey": key,
        "qr": f"/login/authenticator/qr/{device.pk}/" if key else "",
    }
    words = {
        name: labels.word(org, key)
        for name, key in {
            "page_title": "login.auth-page-title",
            "title": "login.auth-setup-title" if key else "login.auth-title",
            "help": "login.auth-setup-help" if key else "login.auth-help",
            "qr": "login.auth-qr",
            "key": "login.auth-key",
            "submit": "login.title",
            "restart": "login.auth-restart",
        }.items()
    }
    response = render(
        request,
        "core/authenticator.html",
        {"ui": ui, "form": form, "words": words},
        status=status,
    )
    response["Referrer-Policy"] = "same-origin"
    return response


@never_cache
@require_http_methods(["GET", "HEAD"])
@login_not_required
@without_key
def authenticator_qr(request: HttpRequest, device_id: int) -> HttpResponse:
    org = organization_for_request(request)
    proof = pending_proof(request, org)
    if proof is None:
        raise Http404
    device = (
        Authenticator.objects.of(org)
        .filter(
            pk=device_id,
            member=proof.member,
            confirmed=False,
            created_at__gt=utc_now(None) - timedelta(minutes=10),
        )
        .first()
    )
    if device is None:
        raise Http404
    _key, svg = enrollment(
        device.plain_secret(), organization=org.name, member_id=proof.member.pk
    )
    response = HttpResponse(svg, content_type="image/svg+xml")
    response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response["X-Content-Type-Options"] = "nosniff"
    return response
