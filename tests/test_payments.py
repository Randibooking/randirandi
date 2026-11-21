# SPDX-License-Identifier: AGPL-3.0-only
"""Committed checkout intents, recovery, authorization and real worker races."""

import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Event
from typing import Any

import psycopg
import pytest
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest
from django.test import override_settings

from randirandi.core import payments
from randirandi.core.admin import InspectionAdmin, site
from randirandi.core.models import (
    Member,
    Organization,
    Payment,
    PaymentProviderAccount,
    TakenTime,
)
from randirandi.core.retention import purge_organization
from randirandi.environment import Environment
from randirandi.payment_configuration import PaymentConfigurationError, Secrets
from randirandi.vipps import Client, CreatedPayment, VippsError, VippsResponseError
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_ordering import NOW, checkout
from tests.test_payment_accounts import values
from tests.test_pricing import tiers

pytestmark = pytest.mark.django_db(transaction=True)
REDIRECT = "https://apitest.vipps.no/checkout?token=fictional-temporary-token"
REAL_CREATE = Client.create


def saved_rows(organization: Organization) -> list[dict[str, Any]]:
    """The payments' columns for a round trip through an older migration, which
    cannot keep `details_sent_at`: that column is newer than the ones it tests.
    """
    rows = Payment.objects.of(organization).values()
    return [{k: v for k, v in row.items() if k != "details_sent_at"} for row in rows]


@dataclass
class Provider:
    calls: list[dict[str, Any]] = field(default_factory=list)
    details: list[tuple[str, str]] = field(default_factory=list)
    during: Callable[[], None] = lambda: None


@pytest.fixture
def provider(made: Example, monkeypatch: pytest.MonkeyPatch) -> Iterator[Provider]:
    PaymentProviderAccount.objects.create(
        organization=made.org,
        payment_provider="vipps",
        merchant_number="123456",
        credentials="VIPPS",
        test_mode=True,
    )
    provider = Provider()

    def create(
        self: Client,
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
        assert not connection.in_atomic_block and connection.get_autocommit()
        # A different actual connection can see the intent before the call.
        with psycopg.connect(**connection.get_connection_params()) as independent:
            saved = independent.execute(
                "SELECT amount, create_key, provider_reference, return_url "
                "FROM core_payment WHERE create_key = %s",
                [key],
            ).fetchone()
        assert saved == (amount, uuid.UUID(key), reference, return_url)
        assert phone is None  # the example member has no phone
        provider.calls.append(
            {
                "reference": reference,
                "amount": amount,
                "currency": currency,
                "return_url": return_url,
                "key": key,
            }
        )
        provider.during()
        return CreatedPayment(reference, REDIRECT)

    def send_details(self: Client, reference: str, url: str) -> None:
        provider.details.append((reference, url))

    monkeypatch.setattr(Client, "create", create)
    monkeypatch.setattr(Client, "send_details", send_details)
    with override_settings(VIPPS_SECRETS=Secrets(values(), Environment.SANDBOX)):
        yield provider


def test_paid_create_is_committed_before_network_and_reuses_redirect(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)
    with application_login():
        first = payments.checkout(
            member=made.member, order_public_id=order.public_id, now=NOW
        )
        second = payments.checkout(
            member=made.member, order_public_id=order.public_id, now=NOW
        )
    assert first == second and first is not None
    assert first.redirect_url == REDIRECT and REDIRECT not in repr(first)
    assert len(provider.calls) == 1
    payment = Payment.objects.of(made.org).get(order=order)
    assert payment.amount == order.total == 200
    assert payment.state == "created" and payment.create_completed_at == NOW
    assert payment.currency == "NOK"
    assert payment.merchant_number == "123456" and payment.credential_name == "VIPPS"
    assert payment.return_url == (
        f"https://{made.org.subdomain}.sandbox.example.org/payments/return/?order={order.public_id}"
    )
    assert order.bookings.get().taken_time.state == "held"


def test_free_order_needs_neither_account_nor_secrets(made: Example) -> None:
    tiers(made, [(240, 60, 0)])
    order = checkout(made)
    assert (
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
        is None
    )
    assert not Payment.objects.of(made.org).exists()
    assert order.bookings.get().taken_time.state == "confirmed"


@pytest.mark.parametrize("length", [2048, 2049, 4096, 4097])
def test_provider_redirect_parser_and_storage_share_the_limit(
    made: Example,
    provider: Provider,
    monkeypatch: pytest.MonkeyPatch,
    length: int,
) -> None:
    order = checkout(made)
    prefix = "https://apitest.vipps.no/checkout?token="
    redirect_url = prefix + "a" * (length - len(prefix))

    def response(self: Client, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "reference": kwargs["body"]["reference"],
            "redirectUrl": redirect_url,
        }

    monkeypatch.setattr(Client, "create", REAL_CREATE)
    monkeypatch.setattr(Client, "_payment", response)
    if length > 4096:
        with pytest.raises(VippsResponseError):
            payments.checkout(
                member=made.member, order_public_id=order.public_id, now=NOW
            )
        assert Payment.objects.of(made.org).get().redirect_url == ""
    else:
        result = payments.checkout(
            member=made.member, order_public_id=order.public_id, now=NOW
        )
        assert result is not None and result.redirect_url == redirect_url
        assert Payment.objects.of(made.org).get().redirect_url == redirect_url


def test_ambiguous_create_retries_exact_committed_payload(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)

    def fail() -> None:
        raise VippsError()

    provider.during = fail
    with pytest.raises(VippsError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    pending = Payment.objects.of(made.org).get(order=order)
    assert not pending.redirect_url and pending.create_completed_at is None
    # Mutable organization/account values must not change an in-flight request.
    made.org.subdomain, made.org.currency = "changed-name", "DKK"
    made.org.save(update_fields=["subdomain", "currency"])
    PaymentProviderAccount.objects.of(made.org).update(
        credentials="VIPPS_OTHER", merchant_number="654321", test_mode=False
    )
    provider.during = lambda: None
    assert payments.checkout(
        member=made.member, order_public_id=order.public_id, now=NOW
    )
    assert provider.calls[0] == provider.calls[1]
    assert pending.test_mode is True
    assert Payment.objects.of(made.org).count() == 1


@pytest.mark.parametrize(
    "change",
    [
        "revoked",
        "expired",
        "cancelled",
        "amount",
        "other_member",
        "other_org",
        "missing",
    ],
)
def test_ineligible_order_makes_no_payment(
    made: Example, provider: Provider, change: str
) -> None:
    order = checkout(made)
    member = made.member
    public_id = order.public_id
    if change == "revoked":
        Member.objects.of(made.org).filter(pk=member.pk).update(status="disabled")
    elif change == "expired":
        order.hold_expires_at = NOW
        order.save(update_fields=["hold_expires_at"])
    elif change == "cancelled":
        TakenTime.objects.of(made.org).update(state="cancelled")
    elif change == "amount":
        order.total += 1
        order.save(update_fields=["total"])
    elif change == "other_member":
        member = Member.objects.create(
            organization=made.org,
            role=member.role,
            email="another@example.test",
            status="active",
            origin="member import",
        )
    elif change == "other_org":
        member = made_up.everything("elsewhere").member
    else:
        public_id = uuid.uuid4()
    with pytest.raises(payments.PaymentUnavailableError):
        payments.checkout(member=member, order_public_id=public_id, now=NOW)
    assert not Payment.objects.of(made.org).filter(order=order).exists()
    assert provider.calls == []


def test_missing_secrets_fail_before_any_intent(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)
    with (
        override_settings(VIPPS_SECRETS=Secrets({}, Environment.SANDBOX)),
        pytest.raises(PaymentConfigurationError),
    ):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    assert not Payment.objects.of(made.org).exists()
    assert provider.calls == []


def test_cached_redirect_still_checks_member_and_secret_binding(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    with (
        override_settings(
            VIPPS_SECRETS=Secrets(values(merchant="654321"), Environment.SANDBOX)
        ),
        pytest.raises(PaymentConfigurationError),
    ):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    Member.objects.of(made.org).filter(pk=made.member.pk).update(status="disabled")
    with pytest.raises(payments.PaymentUnavailableError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("change", ["revoked", "expired"])
def test_change_during_network_retains_outcome_but_refuses_redirect(
    made: Example, provider: Provider, change: str
) -> None:
    order = checkout(made)

    def invalidate() -> None:
        if change == "revoked":
            Member.objects.of(made.org).filter(pk=made.member.pk).update(
                status="disabled"
            )
        else:
            order.hold_expires_at = NOW
            order.save(update_fields=["hold_expires_at"])

    provider.during = invalidate
    with pytest.raises(payments.PaymentUnavailableError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    assert Payment.objects.of(made.org).get().redirect_url == REDIRECT


@pytest.mark.parametrize("manual", [False, True])
def test_caller_transaction_is_preserved(
    made: Example, provider: Provider, manual: bool
) -> None:
    order = checkout(made)

    def attempt() -> None:
        Member.objects.of(made.org).filter(pk=made.member.pk).update(
            first_name="Pending"
        )
        with pytest.raises(RuntimeError, match="own transaction"):
            payments.checkout(
                member=made.member, order_public_id=order.public_id, now=NOW
            )
        assert (
            Member.objects.of(made.org).get(pk=made.member.pk).first_name == "Pending"
        )
        assert not Payment.objects.of(made.org).exists()

    if manual:
        connection.set_autocommit(False)
        try:
            attempt()
            assert not connection.get_autocommit()
        finally:
            connection.rollback()
            connection.set_autocommit(True)
    else:
        with transaction.atomic():
            attempt()
            transaction.set_rollback(True)
    assert provider.calls == []


def test_two_workers_cannot_create_two_payments(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)
    entered, release = Event(), Event()

    def pause() -> None:
        entered.set()
        assert release.wait(10)

    provider.during = pause

    def start() -> payments.Checkout | None:
        try:
            return payments.checkout(
                member=made.member, order_public_id=order.public_id, now=NOW
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(start)
        try:
            assert entered.wait(10)
            with pytest.raises(payments.PaymentBusyError):
                payments.checkout(
                    member=made.member, order_public_id=order.public_id, now=NOW
                )
        finally:
            release.set()
        assert pending.result(timeout=10) is not None
    assert len(provider.calls) == 1
    assert payments.checkout(
        member=made.member, order_public_id=order.public_id, now=NOW
    )


def test_redirect_is_hidden_from_inspection(made: Example, provider: Provider) -> None:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    admin = InspectionAdmin(Payment, site)
    assert "redirect_url" not in admin.get_fields(HttpRequest())


def test_legacy_payment_is_never_resubmitted(made: Example, provider: Provider) -> None:
    order = checkout(made)
    Payment.objects.create(
        currency="NOK",
        organization=made.org,
        order=order,
        payment_provider="vipps",
        provider_reference="legacy-record",
        amount=order.total,
        state="created",
    )
    with pytest.raises(payments.PaymentUnavailableError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    assert provider.calls == []


def test_downgrade_refuses_intent_without_discarding_it(
    made: Example, provider: Provider
) -> None:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    before = saved_rows(made.org)
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="retained payment create intents"):
            MigrationExecutor(connection).migrate(
                [("core", "0007_administrator_authenticator")]
            )
        MigrationExecutor(connection).migrate(latest)
        assert saved_rows(made.org) == before
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.parametrize("state", ["created", "authorized"])
def test_retention_keeps_unsettled_payment_and_order(
    made: Example, provider: Provider, state: str
) -> None:
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    Payment.objects.of(made.org).filter(order=order).update(state=state)
    later = NOW + timedelta(days=365 * 5)
    result = purge_organization(made.org, now=later)
    assert result["orders"] == result["bookings"] == 0
    assert Payment.objects.of(made.org).filter(order=order).exists()
    # Once settled, the ordinary retention rule applies to the same graph.
    Payment.objects.of(made.org).filter(order=order).update(
        state="cancelled", settled_at=NOW
    )
    result = purge_organization(made.org, now=later)
    assert result["orders"] == result["bookings"] == 1
    assert not Payment.objects.of(made.org).filter(order_id=order.pk).exists()


@pytest.mark.parametrize("currency", ["NOK", "DKK", "EUR"])
def test_legacy_currency_backfill_preserves_organization_value(
    made: Example, currency: str
) -> None:
    # Only NOK is currently exposed as a product choice. A retained database
    # value must nevertheless not be silently relabelled by the migration.
    order = checkout(made)
    old = [("core", "0007_administrator_authenticator")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core_organization SET currency = %s WHERE id = %s",
                [currency, made.org.pk],
            )
        legacy = executor.loader.project_state(old).apps.get_model("core", "Payment")
        legacy._default_manager.create(
            organization_id=made.org.pk,
            order_id=order.pk,
            payment_provider="vipps",
            provider_reference="legacy-payment-before-m5",
            amount=order.total,
            state="captured",
            captured_amount=order.total,
            refunded_amount=0,
        )
        MigrationExecutor(connection).migrate(latest)
        payment = Payment.objects.of(made.org).get(order=order)
        assert payment.currency == currency and payment.create_key is None
        if currency != "NOK":
            with pytest.raises(RuntimeError, match="retained payment create intents"):
                MigrationExecutor(connection).migrate(old)
            MigrationExecutor(connection).migrate(latest)
            payment.refresh_from_db()
            assert payment.currency == currency
    finally:
        MigrationExecutor(connection).migrate(latest)
