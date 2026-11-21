# SPDX-License-Identifier: AGPL-3.0-only
"""Fictional ePayment wire replies; exercise the real client and service stack."""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from django.db import connection
from django.test import override_settings

from randirandi.core.models import PaymentProviderAccount
from randirandi.environment import Environment
from randirandi.payment_configuration import Secrets
from randirandi.vipps import Client
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_payment_accounts import values


@dataclass
class WirePayment:
    reference: str
    amount: int
    currency: str
    state: str = "CREATED"
    authorized: int = 0
    captured: int = 0
    cancelled: int = 0
    refunded: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "state": self.state,
            "amount": {"value": self.amount, "currency": self.currency},
            "aggregate": {
                name + "Amount": {
                    "value": getattr(self, name),
                    "currency": self.currency,
                }
                for name in ("authorized", "captured", "cancelled", "refunded")
            },
        }


@dataclass
class Wire:
    host: str = "apitest.vipps.no"
    payments: dict[str, WirePayment] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = field(
        default_factory=list
    )
    approval_code: int = 200
    approval_body: bytes = b""
    lose_approval_reply: bool = False
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mutations: list[tuple[str, str, str, int]] = field(default_factory=list)
    replies: dict[tuple[str, str], tuple[bytes | None, bytes]] = field(
        default_factory=dict
    )
    fail_before: set[str] = field(default_factory=set)
    # Order-management calls: references linked, and a refusal to give.
    categories: dict[str, dict[str, Any]] = field(default_factory=dict)
    category_code: int = 204
    lose_after: set[str] = field(default_factory=set)

    def remember(
        self, operation: str, path: str, key: str, body: bytes | None, value: Any
    ) -> tuple[int, bytes]:
        raw = json.dumps(value).encode()
        self.replies[path, key] = body, raw
        if operation in self.lose_after:
            self.lose_after.remove(operation)
            raise OSError("fictional-private-lost-response")
        return 200, raw

    def __call__(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        assert host == self.host
        assert connection.get_autocommit() and not connection.in_atomic_block
        self.calls.append((method, path, dict(headers), body))
        if path == "/accesstoken/get":
            return 200, b'{"access_token":"fictional-token","expires_in":3600}'
        assert headers["Authorization"] == "Bearer fictional-token"
        assert headers["Merchant-Serial-Number"] == "123456"
        if path.startswith("/order-management/v2/ecom/categories/"):
            assert method == "PUT" and body is not None
            assert "Idempotency-Key" not in headers
            if self.category_code == 204:
                self.categories[path.rsplit("/", 1)[-1]] = json.loads(body)
            return self.category_code, b"" if self.category_code == 204 else b"{}"
        key = headers.get("Idempotency-Key", "")
        if method == "POST" and not path.endswith("/approve"):
            assert key
            if (path, key) in self.replies:
                saved_body, reply = self.replies[path, key]
                return (200, reply) if saved_body == body else (409, b"{}")
            operation = (
                "create" if path == "/epayment/v1/payments" else path.rsplit("/", 1)[-1]
            )
            if operation in self.fail_before:
                self.fail_before.remove(operation)
                raise OSError("fictional-private-before-request")
        if path == "/epayment/v1/payments" and method == "POST":
            assert body is not None
            data = json.loads(body)
            reference = data["reference"]
            self.payments.setdefault(
                reference,
                WirePayment(
                    reference, data["amount"]["value"], data["amount"]["currency"]
                ),
            )
            self.events.setdefault(reference, [])
            return self.remember(
                "create",
                path,
                key,
                body,
                {
                    "reference": reference,
                    "redirectUrl": f"https://{self.host}/checkout?token=fictional-token",
                },
            )
        if path.startswith("/epayment/v1/test/payments/") and path.endswith("/approve"):
            assert host == "apitest.vipps.no"
            assert method == "POST" and body is not None
            assert json.loads(body) == {
                "customer": {"phoneNumber": "4799999999"},
                "token": "fictional-token",
            }
            if self.approval_code == 200:
                payment = self.payments[path.split("/")[-2]]
                payment.state, payment.authorized = "AUTHORIZED", payment.amount
            if self.lose_approval_reply:
                raise OSError("fictional-private-response")
            return self.approval_code, self.approval_body
        if path.startswith("/epayment/v1/payments/") and path.endswith("/events"):
            assert method == "GET"
            return 200, json.dumps(self.events[path.split("/")[-2]]).encode()
        if path.startswith("/epayment/v1/payments/") and method == "GET":
            reference = path.split("/")[-1]
            if reference not in self.payments:
                return 404, b"{}"
            return 200, json.dumps(self.payments[reference].snapshot()).encode()
        if path.startswith("/epayment/v1/payments/") and method == "POST":
            reference, operation = path.split("/")[-2:]
            payment = self.payments[reference]
            if operation == "cancel":
                assert body is None
                if payment.state == "CREATED":
                    # Observed in the Vipps test environment (2026-10-14): the
                    # whole amount is reported cancelled, nothing authorized.
                    amount = payment.amount - payment.cancelled
                    payment.state = "TERMINATED"
                else:
                    amount = payment.authorized - payment.captured - payment.cancelled
                payment.cancelled += amount
                event = "CANCELLED"
            else:
                assert body is not None
                modification = json.loads(body)["modificationAmount"]
                amount = modification["value"]
                assert modification["currency"] == payment.currency
                assert type(amount) is int and amount > 0
                if operation == "capture":
                    if (
                        amount
                        > payment.authorized - payment.captured - payment.cancelled
                    ):
                        return 400, b"{}"
                    payment.captured += amount
                    event = "CAPTURED"
                elif operation == "refund":
                    if amount > payment.captured - payment.refunded:
                        return 400, b"{}"
                    payment.refunded += amount
                    event = "REFUNDED"
                else:
                    raise AssertionError("Unexpected fictional mutation.")
            self.mutations.append((operation, reference, key, amount))
            self.events[reference].append(
                {
                    "reference": reference,
                    "pspReference": f"fictional-event-{len(self.mutations)}",
                    "name": event,
                    "amount": {"value": amount, "currency": payment.currency},
                    "success": True,
                    "idempotencyKey": key,
                }
            )
            return self.remember(operation, path, key, body, payment.snapshot())
        raise AssertionError("Unexpected fictional wire request.")


@pytest.fixture
def wire(made: Example) -> Iterator[Wire]:
    PaymentProviderAccount.objects.create(
        organization=made.org,
        payment_provider="vipps",
        merchant_number="123456",
        credentials="VIPPS",
        test_mode=True,
    )
    wire = Wire()
    secrets = Secrets(values(), Environment.SANDBOX)
    credentials = secrets.credentials(
        prefix="VIPPS", merchant_number="123456", test_mode=True
    )
    secrets._clients["VIPPS"] = Client(Environment.SANDBOX, credentials, transport=wire)
    with override_settings(VIPPS_SECRETS=secrets):
        yield wire
