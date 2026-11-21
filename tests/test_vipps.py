# SPDX-License-Identifier: AGPL-3.0-only
"""Payment boundary contracts with invented credentials and provider replies."""

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from randirandi import vipps
from randirandi.environment import Environment

CREDENTIALS = vipps.Credentials(
    "fake-client", "fake-secret", "fake-subscription", "123456"
)
REFERENCE = "rr-1234567890"
KEY = "persisted-key-1"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
REDIRECT = "https://apitest.vipps.no/dwo-api-application/v1/deeplink/token"


def status(**changes: Any) -> dict[str, Any]:
    return {
        "reference": REFERENCE,
        "state": "AUTHORIZED",
        "amount": {"value": 500, "currency": "NOK"},
        "aggregate": {
            name + "Amount": {"value": amount, "currency": "NOK"}
            for name, amount in (
                ("authorized", 500),
                ("captured", 500),
                ("cancelled", 0),
                ("refunded", 0),
            )
        },
        **changes,
    }


class Provider:
    def __init__(self, *replies: Any, lifetime: Any = "3600") -> None:
        self.replies = list(replies)
        self.lifetime = lifetime
        self.calls: list[tuple[str, str, str, dict[str, str], bytes | None]] = []
        self.tokens = 0

    def __call__(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        self.calls.append((host, method, path, dict(headers), body))
        if path == "/accesstoken/get":
            self.tokens += 1
            return 200, json.dumps(
                {
                    "access_token": f"fictional-token-{self.tokens}",
                    "expires_in": self.lifetime,
                }
            ).encode()
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            return reply
        return 200, json.dumps(reply).encode()


def client(provider: Provider) -> vipps.Client:
    return vipps.Client(Environment.SANDBOX, CREDENTIALS, transport=provider)


def test_redirect_create_keeps_server_amount_phone_and_persisted_key() -> None:
    redirect = "https://apitest.vipps.no/dwo-api-application/v1/deeplink/token"
    provider = Provider({"reference": REFERENCE, "redirectUrl": redirect})
    result = client(provider).create(
        REFERENCE,
        500,
        "NOK",
        "https://example.org/payments/return/",
        key=KEY,
        description="Hall, booking 1",
        phone="4799999999",
    )
    assert result.redirect_url == redirect
    assert redirect not in repr(result)
    host, method, path, headers, body = provider.calls[-1]
    assert (host, method, path) == ("apitest.vipps.no", "POST", "/epayment/v1/payments")
    assert body is not None
    payload = json.loads(body)
    assert payload["amount"] == {"value": 500, "currency": "NOK"}
    assert payload["customer"] == {"phoneNumber": "4799999999"}
    assert payload["userFlow"] == "WEB_REDIRECT"
    assert headers["Idempotency-Key"] == KEY
    assert headers["Merchant-Serial-Number"] == "123456"
    assert all(
        name in headers
        for name in (
            "Vipps-System-Name",
            "Vipps-System-Version",
            "Vipps-System-Plugin-Name",
            "Vipps-System-Plugin-Version",
        )
    )
    assert "client_secret" not in headers
    assert "fake-secret" not in repr(CREDENTIALS)


RETURN = "https://example.org/payments/return/"


def line(total: int) -> dict[str, Any]:
    return {
        "name": "Hall",
        "id": "1",
        "totalAmount": total,
        "totalAmountExcludingTax": total,
        "totalTaxAmount": 0,
        "taxRate": 0,
    }


def sent_body(provider: Provider) -> dict[str, Any]:
    body = provider.calls[-1][4]
    assert body is not None
    return json.loads(body)  # type: ignore[no-any-return]


@pytest.mark.parametrize(("total", "kept"), [(500, True), (499, False)])
def test_receipt_is_sent_only_when_its_lines_add_up_to_the_amount(
    total: int, kept: bool
) -> None:
    provider = Provider({"reference": REFERENCE, "redirectUrl": REDIRECT})
    receipt = {
        "orderLines": [line(total - 100), line(100)],
        "bottomLine": {"currency": "NOK", "receiptNumber": "7"},
    }
    client(provider).create(
        REFERENCE,
        500,
        "NOK",
        RETURN,
        key=KEY,
        description="Hall, booking 7",
        receipt=receipt,
    )
    body = sent_body(provider)
    assert body["paymentDescription"] == "Hall, booking 7"
    assert ("receipt" in body) is kept
    assert not kept or body["receipt"] == receipt


@pytest.mark.parametrize("description", ["", "x" * 101])
def test_description_must_fit(description: str) -> None:
    provider = Provider()
    with pytest.raises(ValueError):
        client(provider).create(
            REFERENCE, 500, "NOK", RETURN, key=KEY, description=description
        )
    assert not provider.calls


def test_details_link_puts_the_category_with_the_usual_headers() -> None:
    provider = Provider((204, b""))
    client(provider).send_details(REFERENCE, "https://example.org/")
    host, method, path, headers, body = provider.calls[-1]
    assert (host, method) == ("apitest.vipps.no", "PUT")
    assert path == f"/order-management/v2/ecom/categories/{REFERENCE}"
    assert json.loads(body or b"") == {
        "category": "BOOKING",
        "orderDetailsUrl": "https://example.org/",
    }
    assert headers["Authorization"] == "Bearer fictional-token-1"
    assert headers["Merchant-Serial-Number"] == "123456"
    assert "Idempotency-Key" not in headers


@pytest.mark.parametrize(
    "url", ["http://example.com/", "https://u:p@example.com/", "https:///x", "x y"]
)
def test_details_link_must_be_public_https(url: str) -> None:
    provider = Provider()
    with pytest.raises(ValueError):
        client(provider).send_details(REFERENCE, url)
    assert not provider.calls


def test_details_link_refusal_is_a_bounded_error() -> None:
    with pytest.raises(vipps.VippsError) as error:
        client(Provider((403, b"private-body"))).send_details(
            REFERENCE, "https://example.org/"
        )
    assert error.value.status == 403 and "private-body" not in str(error.value)


@pytest.mark.parametrize("lifetime", [3600, "3600"])
def test_expiring_token_is_refreshed_before_deadline(lifetime: Any) -> None:
    clock = [1000.0]
    provider = Provider(status(), status(), status(), lifetime=lifetime)
    service = vipps.Client(
        Environment.SANDBOX,
        CREDENTIALS,
        transport=provider,
        clock=lambda: clock[0],
    )
    service.status(REFERENCE, "NOK")
    clock[0] += 3539
    service.status(REFERENCE, "NOK")
    assert provider.tokens == 1
    clock[0] += 1
    service.status(REFERENCE, "NOK")
    assert provider.tokens == 2


@pytest.mark.parametrize("lifetime", [1, "1", 60, "60", 86400, "86400"])
def test_token_lifetime_boundaries_keep_refresh_margin(lifetime: Any) -> None:
    clock = [1000.0]
    provider = Provider(status(), status(), status(), lifetime=lifetime)
    service = vipps.Client(
        Environment.SANDBOX, CREDENTIALS, transport=provider, clock=lambda: clock[0]
    )
    service.status(REFERENCE, "NOK")
    margin = max(0, int(lifetime) - 60)
    if margin:
        clock[0] += margin - 1
        service.status(REFERENCE, "NOK")
        assert provider.tokens == 1
        clock[0] += 1
    service.status(REFERENCE, "NOK")
    assert provider.tokens == 2


@pytest.mark.parametrize(
    "lifetime",
    [
        None,
        True,
        False,
        0,
        -1,
        86401,
        3600.0,
        "",
        "0",
        "-1",
        "86401",
        "3600.0",
        "+3600",
        " 3600",
        "3600 ",
        "3600\n",
        "٣٦٠٠",
        "\uff13\uff16\uff10\uff10",
        "9" * 5000,
        [],
        {},
    ],
)
def test_invalid_token_lifetime_prevents_payment_request(lifetime: Any) -> None:
    provider = Provider(lifetime=lifetime)
    service = client(provider)
    for _ in range(2):
        with pytest.raises(vipps.VippsResponseError):
            service.capture(REFERENCE, 500, "NOK", key=KEY)
    assert provider.tokens == 2
    assert all(call[2] == "/accesstoken/get" for call in provider.calls)


def test_explicit_auth_refusal_refreshes_once_with_identical_operation() -> None:
    provider = Provider((401, b"untrusted credential echo"), status())
    result = client(provider).capture(REFERENCE, 500, "NOK", key=KEY)
    assert result.captured == 500
    assert provider.tokens == 2
    first, second = provider.calls[1], provider.calls[3]
    assert first[:3] == second[:3]
    assert first[4] == second[4]
    assert first[3]["Idempotency-Key"] == second[3]["Idempotency-Key"] == KEY


@pytest.mark.parametrize(
    "reply",
    [
        (401, b"private"),
        (429, b"private"),
        (500, b"private"),
        (302, b"private"),
        OSError("private"),
        (200, b"invalid-json-private"),
        (200, b"x" * (vipps.MAX_RESPONSE + 1)),
    ],
)
def test_errors_are_bounded_and_never_echo_provider_values(reply: Any) -> None:
    provider = Provider(reply, reply)
    with pytest.raises(vipps.VippsError) as caught:
        client(provider).refund(REFERENCE, 100, "NOK", key=KEY)
    assert "private" not in str(caught.value)
    assert len(provider.calls) <= 4


@pytest.mark.parametrize(
    "environment,host",
    [
        (Environment.SANDBOX, "apitest.vipps.no"),
        (Environment.PRODUCTION, "api.vipps.no"),
    ],
)
def test_deployment_selects_host(environment: Environment, host: str) -> None:
    provider = Provider(status())
    vipps.Client(environment, CREDENTIALS, transport=provider).status(REFERENCE, "NOK")
    assert {call[0] for call in provider.calls} == {host}


@pytest.mark.parametrize(
    "environment", ["sandbox", "production", "https://example.com", None]
)
def test_untyped_host_selection_is_refused(environment: Any) -> None:
    with pytest.raises(ValueError, match="deployment"):
        vipps.Client(environment, CREDENTIALS)


@pytest.mark.parametrize("operation", ["capture", "refund", "cancel"])
def test_adjustments_use_exact_amounts_and_require_keys(operation: str) -> None:
    provider = Provider(status())
    service = client(provider)
    if operation == "cancel":
        service.cancel(REFERENCE, "NOK", key=KEY)
    else:
        getattr(service, operation)(REFERENCE, 200, "NOK", key=KEY)
    call = provider.calls[-1]
    assert call[2].endswith("/" + operation)
    assert call[3]["Idempotency-Key"] == KEY
    assert call[4] == (
        None
        if operation == "cancel"
        else b'{"modificationAmount":{"value":200,"currency":"NOK"}}'
    )
    with pytest.raises(ValueError, match="idempotency"):
        service.cancel(REFERENCE, "NOK", key="")
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "reference", ["../other", "x" * 65, "short", "12345678?extra=1", "12345678\n"]
)
def test_reference_cannot_change_request_path(reference: str) -> None:
    provider = Provider()
    with pytest.raises(ValueError):
        client(provider).status(reference, "NOK")
    assert provider.calls == []


@pytest.mark.parametrize("amount", [True, 0, -1, 0.5, 2_147_483_648])
def test_invalid_money_never_reaches_provider(amount: Any) -> None:
    provider = Provider()
    with pytest.raises(ValueError):
        client(provider).capture(REFERENCE, amount, "NOK", key=KEY)
    assert not provider.calls


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/",
        "https://vipps.no.evil.example/",
        "http://apitest.vipps.no/",
        "https://u:p@apitest.vipps.no/",
        "https://apitest.vipps.no:8443/",
        "javascript:alert(1)",
    ],
)
def test_invalid_provider_redirect_is_refused(redirect: str) -> None:
    provider = Provider({"reference": REFERENCE, "redirectUrl": redirect})
    with pytest.raises(vipps.VippsResponseError):
        client(provider).create(
            REFERENCE, 500, "NOK", "https://example.com/", key=KEY, description="d"
        )


def test_malformed_provider_url_never_exposes_its_value() -> None:
    provider = Provider(
        {
            "reference": REFERENCE,
            "redirectUrl": "https://[invented-private-value]/",
        }
    )
    with pytest.raises(vipps.VippsResponseError) as caught:
        client(provider).create(
            REFERENCE, 500, "NOK", "https://example.com/", key=KEY, description="d"
        )
    assert "invented-private-value" not in str(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"state": "CAPTURED"},
        {"reference": "another-payment"},
        {"aggregate": {}},
        {"amount": {"value": 500, "currency": "EUR"}},
        {"amount": {"value": True, "currency": "NOK"}},
    ],
)
def test_unknown_or_mismatched_provider_state_fails_closed(
    mutation: dict[str, Any],
) -> None:
    with pytest.raises(vipps.VippsResponseError):
        client(Provider(status(**mutation))).status(REFERENCE, "NOK")


def test_provider_aggregate_after_capture_and_refund_is_authorized() -> None:
    data = status()
    data["aggregate"]["refundedAmount"]["value"] = 200
    result = client(Provider(data)).status(REFERENCE, "NOK")
    assert result.state == "AUTHORIZED"
    assert (result.captured, result.refunded) == (500, 200)
    data["aggregate"]["refundedAmount"]["value"] = 501
    with pytest.raises(vipps.VippsResponseError):
        client(Provider(data)).status(REFERENCE, "NOK")


def totals(
    state: str, authorized: int, captured: int, cancelled: int, refunded: int
) -> dict[str, Any]:
    return status(
        state=state,
        aggregate={
            name + "Amount": {"value": amount, "currency": "NOK"}
            for name, amount in (
                ("authorized", authorized),
                ("captured", captured),
                ("cancelled", cancelled),
                ("refunded", refunded),
            )
        },
    )


def test_payment_cancelled_before_authorization_is_read_as_released() -> None:
    # The shape the Vipps test environment returned on 2026-10-14 for an
    # abandoned CREATED payment after the worker cancelled it: nothing
    # authorized, the whole amount cancelled. Both reads must accept it.
    data = totals("TERMINATED", 0, 0, 500, 0)
    for result in (
        client(Provider(data)).status(REFERENCE, "NOK"),
        client(Provider(data)).cancel(REFERENCE, "NOK", key=KEY),
    ):
        assert (result.state, result.authorized, result.cancelled) == (
            "TERMINATED",
            0,
            500,
        )
        assert result.cancelled_before_authorization
        assert result.open_authorization == 0


def test_payment_expired_by_the_provider_is_read_as_released() -> None:
    # The shape the Vipps test environment returned on 2026-10-15 for an
    # abandoned CREATED payment that Vipps itself expired before any cancel:
    # nothing authorized, the whole amount cancelled.
    result = client(Provider(totals("EXPIRED", 0, 0, 500, 0))).status(REFERENCE, "NOK")
    assert (result.state, result.authorized, result.cancelled) == ("EXPIRED", 0, 500)
    assert result.cancelled_before_authorization
    assert result.open_authorization == 0


def test_payment_expired_with_nothing_cancelled_is_unchanged() -> None:
    result = client(Provider(totals("EXPIRED", 0, 0, 0, 0))).status(REFERENCE, "NOK")
    assert not result.cancelled_before_authorization
    assert result.open_authorization == 0


@pytest.mark.parametrize("amount", [35000, 45000, 50000, 115000, 150000, 195000])
def test_payment_aborted_before_authorization_is_read_as_released(amount: int) -> None:
    # Six sandbox status replies observed on 2026-11-06: ABORTED, the full
    # amount cancelled, and nothing authorized, captured or refunded.
    data = totals("ABORTED", 0, 0, amount, 0)
    data["amount"]["value"] = amount
    result = client(Provider(data)).status(REFERENCE, "NOK")
    assert (result.state, result.authorized, result.cancelled) == ("ABORTED", 0, amount)
    assert result.cancelled_before_authorization
    assert result.open_authorization == 0


def test_payment_aborted_with_nothing_cancelled_is_unchanged() -> None:
    result = client(Provider(totals("ABORTED", 0, 0, 0, 0))).status(REFERENCE, "NOK")
    assert not result.cancelled_before_authorization
    assert result.open_authorization == 0


@pytest.mark.parametrize(
    "data",
    [totals(state, 0, 0, 500, 0) for state in ("CREATED", "AUTHORIZED")]
    + [
        totals(state, *amounts)
        for state in ("TERMINATED", "EXPIRED", "ABORTED")
        for amounts in (
            (0, 0, 1, 0),
            (0, 0, 499, 0),
            (0, 0, 501, 0),
            (1, 0, 500, 0),
            (0, 1, 500, 0),
            (0, 0, 500, 1),
            (0, 1, 500, 1),
        )
    ],
)
def test_other_cancelled_without_authorization_shapes_still_fail_closed(
    data: dict[str, Any],
) -> None:
    with pytest.raises(vipps.VippsResponseError):
        client(Provider(data)).status(REFERENCE, "NOK")


@pytest.mark.parametrize(
    "authorized,captured,cancelled,remaining",
    [(0, 0, 0, 0), (500, 0, 0, 500), (500, 200, 0, 300), (500, 200, 300, 0)],
)
def test_open_authorization_is_unchanged_for_authorized_payments(
    authorized: int, captured: int, cancelled: int, remaining: int
) -> None:
    data = totals("AUTHORIZED", authorized, captured, cancelled, 0)
    result = client(Provider(data)).status(REFERENCE, "NOK")
    assert not result.cancelled_before_authorization
    assert result.open_authorization == remaining


def test_event_log_keeps_operation_identity_without_customer_fields() -> None:
    provider = Provider(
        [
            {
                "reference": REFERENCE,
                "pspReference": "provider-event-id",
                "name": "CANCELLED",
                "success": True,
                "amount": {"currency": "NOK", "value": 500},
                "customer": {"phoneNumber": "private"},
            }
        ]
    )
    (event,) = client(provider).events(REFERENCE, "NOK")
    assert event.provider_reference == "provider-event-id"
    assert event.idempotency_key is None
    assert "private" not in repr(event)
    assert provider.calls[-1][2].endswith("/events")


def signed(date: datetime = NOW) -> dict[str, Any]:
    body = b'{"reference":"rr-1234567890","name":"AUTHORIZED"}'
    host, target = "example.org", "/payments/webhook/?a=b%2Fc"
    stamp = format_datetime(date, usegmt=True)
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    # This test key is deliberately shaped like base64. It is used as literal
    # UTF-8, not decoded to bytes; a decoder would verify the wrong signature.
    secret = "not-a-real-webhook-key=="  # noqa: S105
    message = f"POST\n{target}\n{stamp};{host};{digest}".encode()
    signature = base64.b64encode(
        hmac.digest(secret.encode(), message, "sha256")
    ).decode()
    return {
        "secret": secret,
        "method": "POST",
        "target": target,
        "host": host,
        "headers": {
            "X-Ms-Date": stamp,
            "X-Ms-Content-Sha256": digest,
            "Authorization": vipps.AUTH_PREFIX + signature,
        },
        "body": body,
        "now": NOW,
    }


def test_webhook_verifies_wire_bytes_target_and_literal_secret() -> None:
    args = signed()
    assert vipps.verify_webhook(**args) == hashlib.sha256(args["body"]).hexdigest()


@pytest.mark.parametrize(
    "changed",
    [
        {"method": "GET"},
        {"host": "other.example.com"},
        {"body": b"{}"},
        {"target": "/payments/webhook/?a=b/c"},
        {"secret": "another-key"},
        {"headers": {}},
        {"body": b"x" * (vipps.MAX_WEBHOOK + 1)},
        {"now": NOW.replace(tzinfo=None)},
    ],
)
def test_tampered_missing_or_oversized_webhook_is_refused(
    changed: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="Invalid Vipps webhook"):
        vipps.verify_webhook(**(signed() | changed))


@pytest.mark.parametrize("seconds", [-301, 301])
def test_old_or_future_webhook_is_refused(seconds: int) -> None:
    with pytest.raises(ValueError, match="Invalid Vipps webhook"):
        vipps.verify_webhook(**signed(NOW + timedelta(seconds=seconds)))


@pytest.mark.parametrize(
    "header", ["X-Ms-Date", "X-Ms-Content-Sha256", "Authorization"]
)
def test_malformed_unicode_headers_are_ordinary_refusals(header: str) -> None:
    args = signed()
    args["headers"][header] = "ø"
    with pytest.raises(ValueError, match="Invalid Vipps webhook"):
        vipps.verify_webhook(**args)
