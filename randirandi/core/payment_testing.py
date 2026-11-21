# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-only sandbox approval; no HTTP route and no production operation."""

import http.client
import json
import re
import uuid
from urllib.parse import parse_qs, urlsplit

from django.conf import settings

from randirandi.core.models import Payment
from randirandi.core.payments import PaymentUnavailableError, client_for, order_lock
from randirandi.environment import Environment
from randirandi.vipps import MAX_RESPONSE, PaymentStatus, VippsError, VippsResponseError


def approve(
    *,
    organization_id: int,
    order_public_id: uuid.UUID,
    phone: str,
    request_key: uuid.UUID,
) -> PaymentStatus:
    """Use a test identity and caller-retained key; inspect authoritative status.

    The operator keeps the request key with the sandbox scenario before calling.
    The redirect token and test phone remain in memory and are never logged.
    A returned CREATED status means approval has not yet been observed.
    """
    if (
        settings.ENVIRONMENT != "sandbox"
        or type(organization_id) is not int
        or organization_id < 1
        or not isinstance(order_public_id, uuid.UUID)
        or not isinstance(request_key, uuid.UUID)
        or not re.fullmatch(r"[1-9][0-9]{7,14}", phone)
    ):
        raise PaymentUnavailableError("Sandbox approval arguments are invalid.")
    payment = (
        Payment.objects.of(organization_id)
        .filter(order__public_id=order_public_id, create_key__isnull=False)
        .first()
    )
    if payment is None:
        raise PaymentUnavailableError("No sandbox payment intent exists.")
    with order_lock(payment.order_id):
        payment.refresh_from_db()
        client = client_for(payment)
        if (
            client.environment is not Environment.SANDBOX
            or client._host != "apitest.vipps.no"
        ):
            raise PaymentUnavailableError("Sandbox approval is unavailable.")
        status = client.status(payment.provider_reference, payment.currency)
        if status.state == "AUTHORIZED":
            return status
        if status.state != "CREATED" or payment.create_completed_at is None:
            raise PaymentUnavailableError("This test payment cannot be approved.")
        try:
            redirect = urlsplit(payment.redirect_url)
            tokens = parse_qs(redirect.query, max_num_fields=20).get("token", [])
        except ValueError:
            raise PaymentUnavailableError(
                "Test redirect token is unavailable."
            ) from None
        if (
            redirect.scheme != "https"
            or not redirect.hostname
            or not redirect.hostname.endswith((".vipps.no", ".vippsmobilepay.com"))
            or redirect.netloc != redirect.hostname
            or redirect.fragment
            or len(tokens) != 1
            or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,4096}", tokens[0])
        ):
            raise PaymentUnavailableError("Test redirect token is unavailable.")
        # This test endpoint may return HTTP 200 with no JSON body. Keep that
        # exception here; product payment responses still require valid JSON.
        headers = client._headers() | {
            "Authorization": "Bearer " + client.access_token(),
            "Idempotency-Key": str(request_key),
        }
        body = json.dumps(
            {"customer": {"phoneNumber": phone}, "token": tokens[0]},
            separators=(",", ":"),
        ).encode()
        try:
            code, raw = client._transport(
                "apitest.vipps.no",
                "POST",
                f"/epayment/v1/test/payments/{payment.provider_reference}/approve",
                headers,
                body,
            )
        except OSError, http.client.HTTPException:
            raise VippsError() from None
        if code != 200:
            raise VippsError(code)
        if len(raw) > MAX_RESPONSE:
            raise VippsResponseError(code)
        return client.status(payment.provider_reference, payment.currency)
