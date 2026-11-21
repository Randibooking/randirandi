# SPDX-License-Identifier: AGPL-3.0-only
"""Small payment endpoints; member-facing payment screens belong to M6."""

import uuid

from django.contrib.auth.decorators import login_not_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import payment_callbacks, payments
from randirandi.core.authentication import keep_order
from randirandi.core.login_codes import allow_return
from randirandi.core.models import Member
from randirandi.core.network import client_address
from randirandi.core.organization_request import organization_for_request
from randirandi.payment_configuration import PaymentConfigurationError
from randirandi.vipps import MAX_WEBHOOK, VippsError


@never_cache
@require_POST
def start(request: HttpRequest, order_public_id: uuid.UUID) -> HttpResponse:
    org = organization_for_request(request)
    if not isinstance(request.user, Member) or request.user.organization_id != org.pk:
        return HttpResponse(status=403)
    try:
        result = payments.checkout(member=request.user, order_public_id=order_public_id)
    except payments.PaymentUnavailableError, payments.PaymentBusyError:
        return JsonResponse({"error": "Payment is unavailable."}, status=409)
    except PaymentConfigurationError, VippsError:
        return JsonResponse(
            {"error": "Payment is temporarily unavailable."}, status=503
        )
    # The browser navigates after fetching this response. No cross-origin
    # form redirect or change to the site's form-action policy is needed.
    return JsonResponse({"redirect": result.redirect_url if result else "/"})


@never_cache
@login_not_required
@require_GET
def returned(request: HttpRequest) -> HttpResponse:
    org = organization_for_request(request)
    try:
        public_id = uuid.UUID(request.GET.get("order", ""))
    except ValueError:
        return redirect("home")
    payment_callbacks.request_reconciliation(org, public_id)
    # The return can use another browser, and an unknown order and another
    # member's go the same way: nothing here looks at who owns the order.
    # The outcome read (`/booking/order/<id>/`) decides what a member sees.
    user = request.user
    if isinstance(user, Member) and user.organization_id == org.pk:
        return redirect("order", order_public_id=public_id)
    # Keeping the id writes a session row: limited per network address, as the
    # login's request limit is. Over it the order is not kept and no row is
    # written; the member logs in and finds the order in My page.
    if allow_return(org, client_address(request)):
        keep_order(request.session, public_id)
    return redirect("login")


@never_cache
@csrf_exempt
@login_not_required
@require_POST
def webhook(request: HttpRequest) -> HttpResponse:
    org = organization_for_request(request)
    body = request.read(MAX_WEBHOOK + 1)
    try:
        payment_callbacks.receive_webhook(
            organization=org,
            method=request.method or "",
            target=request.get_full_path(),
            host=request.get_host(),
            headers=request.headers,
            body=body,
        )
    except payment_callbacks.WebhookRejectedError:
        return HttpResponse(status=403)
    except payment_callbacks.WebhookRepeatedError:
        return HttpResponse(status=409)
    except PaymentConfigurationError:
        return HttpResponse(status=503)
    return HttpResponse(status=202)
