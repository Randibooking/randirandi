# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded ePayment calls; no redirects, proxy inheritance or response logging.

The caller persists each mutation's key before calling. Retrying an uncertain
operation must use that same key and payload. The client never invents keys.
"""

import base64
import hashlib
import hmac
import http.client
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from randirandi.environment import Environment
from randirandi.limits import CURRENCIES

TIMEOUT = 10
_timeout: ContextVar[float] = ContextVar("vipps_timeout", default=TIMEOUT)


@contextmanager
def call_timeout(seconds: float | None) -> Iterator[None]:
    """Every call made by this thread inside the block uses `seconds` as its
    socket timeout (None: the default `TIMEOUT`). The member's page read uses a
    shorter one than the worker ."""
    if seconds is None:
        yield
        return
    token = _timeout.set(seconds)
    try:
        yield
    finally:
        _timeout.reset(token)


MAX_RESPONSE = 256 * 1024
MAX_WEBHOOK = 16 * 1024
MAX_AGE = timedelta(minutes=5)
STATES = frozenset({"CREATED", "AUTHORIZED", "ABORTED", "EXPIRED", "TERMINATED"})
EVENTS = STATES | {"CANCELLED", "CAPTURED", "REFUNDED"}
AUTH_PREFIX = "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256&Signature="


def vipps_phone(phone: str) -> str:
    """A phone as Vipps takes it: digits with the country code, no plus.

    Takes a stored E.164 number or the digits themselves. Empty when Vipps
    would refuse the number.
    """
    digits = phone.removeprefix("+")
    return digits if re.fullmatch(r"[1-9][0-9]{7,14}", digits) else ""


class VippsError(Exception):
    """Only a bounded status, never a provider body or credential."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        super().__init__(f"Vipps request failed ({status}).")


class VippsResponseError(VippsError):
    """An unknown response cannot authorize a booking or money movement."""


@dataclass(frozen=True, repr=False)
class Credentials:
    client_id: str
    client_secret: str
    subscription_key: str
    merchant_number: str

    def __post_init__(self) -> None:
        if any(
            not value
            or len(value) > 4096
            or not value.isascii()
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            for value in (
                self.client_id,
                self.client_secret,
                self.subscription_key,
                self.merchant_number,
            )
        ) or not re.fullmatch(r"[0-9]{4,10}", self.merchant_number):
            raise ValueError("Incomplete or invalid Vipps credentials.")


class Transport(Protocol):
    def __call__(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]: ...


def _send(
    host: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> tuple[int, bytes]:
    # HTTPSConnection validates certificates and neither follows redirects nor
    # reads HTTP(S)_PROXY. The host is selected by deployment below.
    conn = http.client.HTTPSConnection(host, timeout=_timeout.get())
    try:
        conn.request(method, path, body=body, headers=dict(headers))
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise VippsResponseError(response.status)
        return response.status, raw
    except OSError, http.client.HTTPException:
        raise VippsError() from None
    finally:
        conn.close()


def _text(value: Any, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VippsResponseError()
    return value


def _reference(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", value):
        raise ValueError("Invalid payment reference.")
    return value


def _amount(value: Any, currency: str) -> int:
    if (
        not isinstance(value, dict)
        or value.get("currency") != currency
        or type(value.get("value")) is not int
        or not 0 <= value["value"] <= 2_147_483_647
    ):
        raise VippsResponseError()
    return int(value["value"])


def _public_https(url: str, what: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.fragment
        or any(ord(char) < 33 for char in url)
    ):
        raise ValueError(f"A public HTTPS {what} is required.")


def _receipt_adds_up(receipt: Mapping[str, Any], amount: int) -> bool:
    """Vipps starts a payment whose receipt does not add up, but drops the receipt."""
    lines = receipt.get("orderLines")
    return (
        isinstance(lines, list)
        and bool(lines)
        and all(isinstance(line, dict) for line in lines)
        and sum(line.get("totalAmount", -1) for line in lines) == amount
    )


def _money(value: int, currency: str) -> dict[str, str | int]:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError("The payment amount must be positive minor units.")
    if currency not in CURRENCIES:
        raise ValueError("Unsupported payment currency.")
    return {"value": value, "currency": currency}


@dataclass(frozen=True)
class PaymentStatus:
    reference: str
    state: str
    amount: int
    authorized: int
    captured: int
    cancelled: int
    refunded: int

    @classmethod
    def read(cls, data: Any, reference: str, currency: str) -> PaymentStatus:
        if not isinstance(data, dict) or data.get("reference") != reference:
            raise VippsResponseError()
        state = data.get("state")
        aggregate = data.get("aggregate")
        if (
            not isinstance(state, str)
            or state not in STATES
            or not isinstance(aggregate, dict)
        ):
            raise VippsResponseError()
        result = cls(
            reference,
            state,
            _amount(data.get("amount"), currency),
            *(
                _amount(aggregate.get(name + "Amount"), currency)
                for name in ("authorized", "captured", "cancelled", "refunded")
            ),
        )
        consistent = (
            result.refunded <= result.captured
            and result.captured + result.cancelled <= result.authorized <= result.amount
        )
        if not (consistent or result.cancelled_before_authorization):
            raise VippsResponseError()
        return result

    @property
    def cancelled_before_authorization(self) -> bool:
        # Observed before authorization: merchant cancellation (TERMINATED),
        # provider expiry (EXPIRED), and payer cancellation (ABORTED) can
        # report the full amount cancelled with no money reserved or captured.
        return (
            self.state in {"TERMINATED", "EXPIRED", "ABORTED"}
            and self.amount > 0
            and self.cancelled == self.amount
            and not (self.authorized or self.captured or self.refunded)
        )

    @property
    def open_authorization(self) -> int:
        """Authorized money that is neither captured nor cancelled."""
        if self.cancelled_before_authorization:
            return 0
        return self.authorized - self.captured - self.cancelled


@dataclass(frozen=True)
class PaymentEvent:
    reference: str
    provider_reference: str
    name: str
    amount: int
    success: bool
    idempotency_key: str | None


@dataclass(frozen=True)
class CreatedPayment:
    reference: str
    redirect_url: str = field(repr=False)


class Client:
    def __init__(
        self,
        environment: Environment,
        credentials: Credentials,
        *,
        transport: Transport = _send,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # A row or caller-supplied URL can never select a payment host.
        if not isinstance(environment, Environment):
            raise ValueError("The deployment must select the Vipps environment.")
        self.environment = environment
        self._host = (
            "apitest.vipps.no" if environment is Environment.SANDBOX else "api.vipps.no"
        )
        self._credentials = credentials
        self._transport = transport
        self._clock = clock
        self._token: str = ""
        self._expires = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": self._credentials.subscription_key,
            "Merchant-Serial-Number": self._credentials.merchant_number,
            "Vipps-System-Name": "randirandi",
            "Vipps-System-Version": "0.1.0",
            "Vipps-System-Plugin-Name": "randirandi",
            "Vipps-System-Plugin-Version": "0.1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        raw_body = (
            None if body is None else json.dumps(body, separators=(",", ":")).encode()
        )
        try:
            status, raw = self._transport(self._host, method, path, headers, raw_body)
        except OSError, http.client.HTTPException:
            raise VippsError() from None
        if not 200 <= status < 300:
            raise VippsError(status)
        if len(raw) > MAX_RESPONSE:
            raise VippsResponseError(status)
        if not raw.strip():
            # Some calls answer with no body. Callers that read one check its shape.
            return None
        try:
            return json.loads(raw)
        except ValueError, RecursionError:
            raise VippsResponseError(status) from None

    def access_token(self) -> str:
        if self._token and self._clock() < self._expires:
            return self._token
        started = self._clock()
        data = self._request(
            "POST",
            "/accesstoken/get",
            self._headers()
            | {
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
            },
        )
        if not isinstance(data, dict):
            raise VippsResponseError()
        token, lifetime = data.get("access_token"), data.get("expires_in")
        # Vipps' access-token endpoint returns seconds as a decimal string.
        if isinstance(lifetime, str) and re.fullmatch(r"[0-9]{1,5}", lifetime):
            lifetime = int(lifetime)
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", token)
            or type(lifetime) is not int
            or not 1 <= lifetime <= 86400
        ):
            raise VippsResponseError()
        self._token = token
        self._expires = started + max(0, lifetime - 60)
        return token

    def _payment(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        headers = self._headers()
        if method == "POST":
            if key is None or not re.fullmatch(r"[A-Za-z0-9-]{1,50}", key):
                raise ValueError("A persisted idempotency key is required.")
            headers["Idempotency-Key"] = key
        # Refresh once on an explicit auth refusal, preserving request bytes/key.
        for attempt in range(2):
            headers["Authorization"] = "Bearer " + self.access_token()
            try:
                return self._request(method, path, headers, body)
            except VippsError as error:
                if error.status != 401 or attempt:
                    raise
                self._token = ""
        raise VippsError(401)

    def get(self, path: str) -> Any:
        """An authorised read of any path of the provider (the report API)."""
        return self._payment("GET", path)

    def create(
        self,
        reference: str,
        amount: int,
        currency: str,
        return_url: str,
        *,
        key: str,
        description: str,
        receipt: Mapping[str, Any] | None = None,
        phone: str | None = None,
    ) -> CreatedPayment:
        _reference(reference)
        _public_https(return_url, "return URL")
        if not description or len(description) > 100:
            raise ValueError("The payment description is 1 to 100 characters.")
        body: dict[str, Any] = {
            "amount": _money(amount, currency),
            "reference": reference,
            "paymentMethod": {"type": "WALLET"},
            "userFlow": "WEB_REDIRECT",
            "returnUrl": return_url,
            "paymentDescription": description,
        }
        if receipt is not None and _receipt_adds_up(receipt, amount):
            body["receipt"] = receipt
        if phone is not None:
            if "+" in phone or vipps_phone(phone) != phone:
                raise ValueError("The payment phone needs a country code.")
            body["customer"] = {"phoneNumber": phone}
        data = self._payment("POST", "/epayment/v1/payments", key=key, body=body)
        if not isinstance(data, dict) or data.get("reference") != reference:
            raise VippsResponseError()
        redirect = _text(data.get("redirectUrl"), 4096)
        try:
            parsed = urlsplit(redirect)
        except ValueError:
            raise VippsResponseError() from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith((".vipps.no", ".vippsmobilepay.com"))
            or parsed.username
            or parsed.password
            or parsed.netloc != parsed.hostname
            or any(ord(char) < 33 for char in redirect)
        ):
            raise VippsResponseError()
        return CreatedPayment(reference, redirect)

    def send_details(self, reference: str, url: str) -> None:
        """Link the payment in the Vipps app to the organization's site.

        A new call for the same reference overwrites the old one.
        """
        _public_https(url, "details URL")
        self._payment(
            "PUT",
            f"/order-management/v2/ecom/categories/{_reference(reference)}",
            body={"category": "BOOKING", "orderDetailsUrl": url},
        )

    def status(self, reference: str, currency: str) -> PaymentStatus:
        data = self._payment("GET", f"/epayment/v1/payments/{_reference(reference)}")
        return PaymentStatus.read(data, reference, currency)

    def events(self, reference: str, currency: str) -> tuple[PaymentEvent, ...]:
        data = self._payment(
            "GET", f"/epayment/v1/payments/{_reference(reference)}/events"
        )
        if not isinstance(data, list):
            raise VippsResponseError()
        events = []
        for item in data:
            if (
                not isinstance(item, dict)
                or item.get("reference") != reference
                or not isinstance(item.get("name"), str)
                or item["name"] not in EVENTS
                or type(item.get("success")) is not bool
            ):
                raise VippsResponseError()
            key = item.get("idempotencyKey")
            events.append(
                PaymentEvent(
                    reference,
                    _text(item.get("pspReference")),
                    item["name"],
                    _amount(item.get("amount"), currency),
                    item["success"],
                    None if key is None else _text(key, 50),
                )
            )
        return tuple(events)

    def capture(
        self, reference: str, amount: int, currency: str, *, key: str
    ) -> PaymentStatus:
        return self._adjust("capture", reference, currency, key, amount)

    def cancel(self, reference: str, currency: str, *, key: str) -> PaymentStatus:
        return self._adjust("cancel", reference, currency, key)

    def refund(
        self, reference: str, amount: int, currency: str, *, key: str
    ) -> PaymentStatus:
        return self._adjust("refund", reference, currency, key, amount)

    def _adjust(
        self,
        action: str,
        reference: str,
        currency: str,
        key: str,
        amount: int | None = None,
    ) -> PaymentStatus:
        body = (
            None if amount is None else {"modificationAmount": _money(amount, currency)}
        )
        data = self._payment(
            "POST",
            f"/epayment/v1/payments/{_reference(reference)}/{action}",
            key=key,
            body=body,
        )
        return PaymentStatus.read(data, reference, currency)


def verify_webhook(
    secret: str,
    *,
    method: str,
    target: str,
    host: str,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
) -> str:
    """Verify exact wire bytes and freshness; return a body hash for deduplication.

    The receiving service must persist a unique event identity as well. A fresh
    valid signature alone does not prevent a provider redelivery.
    """

    def refuse() -> None:
        raise ValueError("Invalid Vipps webhook.")

    if (
        not secret
        or method != "POST"
        or not target.startswith("/")
        or not host
        or len(body) > MAX_WEBHOOK
        or now.utcoffset() is None
        or any(char in target + host for char in "\r\n")
    ):
        refuse()
    lower = {key.lower(): value for key, value in headers.items()}
    date, claimed_hash, authorization = (
        lower.get(name, "")
        for name in ("x-ms-date", "x-ms-content-sha256", "authorization")
    )
    try:
        moment = parsedate_to_datetime(date)
        if moment.utcoffset() is None or abs(now - moment) > MAX_AGE:
            refuse()
    except ValueError, TypeError, OverflowError:
        raise ValueError("Invalid Vipps webhook.") from None
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    signed = f"POST\n{target}\n{date};{host};{digest}".encode()
    signature = base64.b64encode(hmac.digest(secret.encode(), signed, "sha256")).decode(
        "ascii"
    )
    if not hmac.compare_digest(
        digest.encode(), claimed_hash.encode()
    ) or not hmac.compare_digest(
        (AUTH_PREFIX + signature).encode(), authorization.encode()
    ):
        refuse()
    return hashlib.sha256(body).hexdigest()
