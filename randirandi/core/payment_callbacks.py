# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticate bounded callback bytes; persist identity, never provider profiles."""

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings

from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import Organization, Payment
from randirandi.payment_configuration import Secrets
from randirandi.vipps import EVENTS, MAX_WEBHOOK, verify_webhook


class WebhookRejectedError(ValueError):
    """Malformed or unauthenticated callback; supplied text is never echoed."""


class WebhookRepeatedError(ValueError):
    """An authenticated event identity has already been accepted."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WebhookRejectedError("Invalid payment callback.")
        result[key] = value
    return result


def _requested_at(payment: Payment, moment: datetime) -> datetime:
    # Monotonic even when events share a clock tick or the wall clock goes
    # backwards. An event during a read remains newer than that attempt.
    previous = (
        payment.callback_received_at,
        payment.provider_check_attempted_at,
        payment.provider_checked_at,
    )
    return max(
        [
            moment,
            *(
                value + timedelta(microseconds=1)
                for value in previous
                if value is not None
            ),
        ]
    )


def receive_webhook(
    *,
    organization: Organization,
    method: str,
    target: str,
    host: str,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime | None = None,
) -> None:
    if len(body) > MAX_WEBHOOK:
        raise WebhookRejectedError("Invalid payment callback.")
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_object)
        if not isinstance(data, dict):
            raise ValueError
        reference, msn, event, operation = (
            data.get(key) for key in ("reference", "msn", "name", "pspReference")
        )
        if (
            not isinstance(reference, str)
            or re.fullmatch(r"[A-Za-z0-9-]{8,64}", reference) is None
            or not isinstance(msn, str)
            or re.fullmatch(r"[0-9]{4,10}", msn) is None
            or not isinstance(event, str)
            or event not in EVENTS
            or not isinstance(operation, str)
            or not 1 <= len(operation) <= 200
            or any(ord(char) < 33 or ord(char) > 126 for char in operation)
            or type(data.get("success")) is not bool
            or not isinstance(data.get("timestamp"), str)
            or len(data["timestamp"]) > 64
            or datetime.fromisoformat(data["timestamp"]).utcoffset() is None
        ):
            raise ValueError
        amount = data.get("amount")
        if (
            not isinstance(amount, dict)
            or type(amount.get("value")) is not int
            or amount["value"] < 0
        ):
            raise ValueError
    except ValueError, TypeError, OverflowError, RecursionError:
        raise WebhookRejectedError("Invalid payment callback.") from None
    payment = (
        Payment.objects.of(organization)
        .filter(
            provider_reference=reference,
            merchant_number=msn,
            payment_provider="vipps",
            create_key__isnull=False,
        )
        .first()
    )
    if (
        payment is None
        or amount.get("currency") != payment.currency
        or amount["value"] > payment.amount
    ):
        raise WebhookRejectedError("Invalid payment callback.")
    secrets: Secrets = settings.VIPPS_SECRETS
    secret = secrets.webhook_secret(
        prefix=payment.credential_name,
        merchant_number=payment.merchant_number,
        test_mode=payment.test_mode,
    )
    moment = utc_now(now)
    try:
        verify_webhook(
            secret,
            method=method,
            target=target,
            host=host,
            headers=headers,
            body=body,
            now=moment,
        )
    except ValueError:
        raise WebhookRejectedError("Invalid payment callback.") from None
    # Provider operation identity survives JSON reordering or added metadata.
    identity = hashlib.sha256(
        json.dumps(
            [msn, reference, operation, event, data["success"]], separators=(",", ":")
        ).encode()
    ).hexdigest()

    def accept(org: Organization) -> None:
        current = Payment.objects.of(org).get(pk=payment.pk)
        if identity in current.webhook_events:
            raise WebhookRepeatedError("Payment callback already accepted.")
        if len(current.webhook_events) >= 1024:
            raise WebhookRejectedError("Payment callback capacity reached.")
        Payment.objects.of(org).filter(pk=current.pk).update(
            webhook_events=[*current.webhook_events, identity],
            callback_received_at=_requested_at(current, moment),
        )

    organization_transaction(organization.pk, accept)


def request_reconciliation(
    organization: Organization, public_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    moment = utc_now(now)

    def enqueue(org: Organization) -> None:
        payment = (
            Payment.objects.of(org)
            .filter(order__public_id=public_id, create_key__isnull=False)
            .first()
        )
        if payment is not None:
            Payment.objects.of(org).filter(pk=payment.pk).update(
                callback_received_at=_requested_at(payment, moment)
            )

    organization_transaction(organization.pk, enqueue)
