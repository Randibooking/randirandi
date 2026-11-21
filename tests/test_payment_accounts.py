# SPDX-License-Identifier: AGPL-3.0-only
"""Organization agreements select named, deployment-bound credential groups."""

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from randirandi.core.models import PaymentProvider, PaymentProviderAccount
from randirandi.core.payment_accounts import account_for, client_for, webhook_secret_for
from randirandi.environment import Environment
from randirandi.payment_configuration import PaymentConfigurationError, Secrets
from tests.made_up import organization


def values(prefix: str = "VIPPS", merchant: str = "123456") -> dict[str, str]:
    return {
        f"{prefix}_CLIENT_ID": "invented-client-id",
        f"{prefix}_CLIENT_SECRET": "invented-client-secret",
        f"{prefix}_SUBSCRIPTION_KEY": "invented-subscription-key",
        f"{prefix}_MSN": merchant,
        f"{prefix}_WEBHOOK_SECRET": "invented-webhook-secret",
    }


def test_secrets_are_private_and_a_missing_webhook_does_not_disable_payments() -> None:
    source = values()
    del source["VIPPS_WEBHOOK_SECRET"]
    secrets = Secrets(source, Environment.SANDBOX)
    credentials = secrets.credentials(
        prefix="VIPPS", merchant_number="123456", test_mode=True
    )
    assert credentials.client_id == "invented-client-id"
    assert "invented" not in repr(secrets) + repr(credentials)
    with pytest.raises(PaymentConfigurationError, match="webhook"):
        secrets.webhook_secret(prefix="VIPPS", merchant_number="123456", test_mode=True)
    # The store owns a copy, not a live reference to a mutable environment.
    source["VIPPS_MSN"] = "654321"
    assert (
        secrets.credentials(prefix="VIPPS", merchant_number="123456", test_mode=True)
        == credentials
    )


@pytest.mark.parametrize(
    "field", ["CLIENT_ID", "CLIENT_SECRET", "SUBSCRIPTION_KEY", "MSN"]
)
@pytest.mark.parametrize("bad", ["", "private\nvalue", "private value", "ø"])
def test_missing_or_malformed_credentials_are_sanitized(field: str, bad: str) -> None:
    source = values() | {f"VIPPS_{field}": bad}
    with pytest.raises(PaymentConfigurationError, match="missing or invalid") as caught:
        Secrets(source, Environment.SANDBOX).credentials(
            prefix="VIPPS",
            merchant_number="123456",
            test_mode=True,
        )
    assert "private" not in str(caught.value)


def test_absent_group_and_missing_field_refuse_without_fallback() -> None:
    source = values()
    del source["VIPPS_CLIENT_SECRET"]
    secrets = Secrets(source, Environment.SANDBOX)
    for prefix in ("VIPPS", "VIPPS_MISSING"):
        with pytest.raises(PaymentConfigurationError, match="missing or invalid"):
            secrets.credentials(prefix=prefix, merchant_number="123456", test_mode=True)


@pytest.mark.parametrize(
    "prefix", ["", "OTHER", "VIPPS_../", "VIPPS\n", "vipps", "VIPPS__ORG"]
)
def test_unrecognized_group_name_is_refused(prefix: str) -> None:
    with pytest.raises(PaymentConfigurationError):
        Secrets(values(), Environment.SANDBOX).credentials(
            prefix=prefix,
            merchant_number="123456",
            test_mode=True,
        )


def test_sandbox_never_accepts_a_production_row_or_mismatched_merchant() -> None:
    secrets = Secrets(values(), Environment.SANDBOX)
    with pytest.raises(PaymentConfigurationError, match="deployment"):
        secrets.credentials(prefix="VIPPS", merchant_number="123456", test_mode=False)
    with pytest.raises(PaymentConfigurationError, match="merchant"):
        secrets.credentials(prefix="VIPPS", merchant_number="654321", test_mode=True)


def test_production_accepts_its_real_account_and_refuses_test_accounts() -> None:
    secrets = Secrets(values(), Environment.PRODUCTION)
    client = secrets.client(prefix="VIPPS", merchant_number="123456", test_mode=False)
    assert client.environment is Environment.PRODUCTION
    assert (
        secrets.webhook_secret(
            prefix="VIPPS", merchant_number="123456", test_mode=False
        )
        == "invented-webhook-secret"
    )
    with pytest.raises(PaymentConfigurationError, match="deployment"):
        secrets.client(prefix="VIPPS", merchant_number="123456", test_mode=True)
    with pytest.raises(PaymentConfigurationError, match="merchant"):
        secrets.client(prefix="VIPPS", merchant_number="654321", test_mode=False)


@pytest.mark.parametrize(
    "field", ["CLIENT_ID", "CLIENT_SECRET", "SUBSCRIPTION_KEY", "MSN"]
)
def test_production_missing_credentials_refuse_the_account(field: str) -> None:
    source = values()
    del source[f"VIPPS_{field}"]
    with pytest.raises(PaymentConfigurationError, match="missing or invalid"):
        Secrets(source, Environment.PRODUCTION).credentials(
            prefix="VIPPS", merchant_number="123456", test_mode=False
        )


@pytest.mark.parametrize("environment", list(Environment))
@pytest.mark.parametrize("mode", [0, 1, "false", "true", None])
def test_account_mode_requires_a_boolean(environment: Environment, mode: Any) -> None:
    with pytest.raises(PaymentConfigurationError, match="deployment"):
        Secrets(values(), environment).credentials(
            prefix="VIPPS", merchant_number="123456", test_mode=mode
        )


def test_payment_environment_cannot_come_from_an_arbitrary_string() -> None:
    with pytest.raises(ImproperlyConfigured, match="deployment"):
        Secrets(values(), "production")  # type: ignore[arg-type]


def test_production_without_payments_starts_but_cannot_resolve_test_account() -> None:
    secrets = Secrets({"UNRELATED_SECRET": "private"}, Environment.PRODUCTION)
    with pytest.raises(PaymentConfigurationError, match="deployment"):
        secrets.credentials(prefix="VIPPS", merchant_number="123456", test_mode=True)
    assert "private" not in repr(secrets)


def test_distinct_groups_get_distinct_clients_with_process_lifetime_caches() -> None:
    secrets = Secrets(values() | values("VIPPS_SECOND", "654321"), Environment.SANDBOX)
    first = secrets.client(prefix="VIPPS", merchant_number="123456", test_mode=True)
    assert first is secrets.client(
        prefix="VIPPS", merchant_number="123456", test_mode=True
    )
    second = secrets.client(
        prefix="VIPPS_SECOND", merchant_number="654321", test_mode=True
    )
    assert first is not second
    # Validation runs even when the named group already has a cached client.
    with pytest.raises(PaymentConfigurationError):
        secrets.client(prefix="VIPPS", merchant_number="654321", test_mode=True)


@pytest.mark.parametrize("bad", ["", "too-short", "private\nwebhook-key", "ø" * 20])
def test_webhook_secret_refuses_missing_or_invalid_values(bad: str) -> None:
    secrets = Secrets(values() | {"VIPPS_WEBHOOK_SECRET": bad}, Environment.SANDBOX)
    with pytest.raises(PaymentConfigurationError, match="webhook"):
        secrets.webhook_secret(prefix="VIPPS", merchant_number="123456", test_mode=True)


@pytest.mark.django_db
def test_accounts_resolve_only_the_named_organizations_group() -> None:
    first, second = organization(), organization("pinehill")
    account = PaymentProviderAccount.objects.create(
        organization=first,
        payment_provider=PaymentProvider.VIPPS,
        merchant_number="123456",
        credentials="VIPPS",
        test_mode=True,
    )
    other = PaymentProviderAccount.objects.create(
        organization=second,
        payment_provider=PaymentProvider.VIPPS,
        merchant_number="654321",
        credentials="VIPPS_SECOND",
        test_mode=True,
    )
    with override_settings(
        VIPPS_SECRETS=Secrets(
            values() | values("VIPPS_SECOND", "654321"),
            Environment.SANDBOX,
        )
    ):
        assert account_for(first.pk) == account
        assert account_for(second.pk) == other
        assert client_for(account) is not client_for(other)
        assert webhook_secret_for(account) == "invented-webhook-secret"
        other.credentials = "VIPPS"
        other.save(update_fields=["credentials"])
        with pytest.raises(PaymentConfigurationError, match="merchant"):
            account_for(second.pk)
        PaymentProviderAccount.objects.of(first).filter(pk=account.pk).delete()
        with pytest.raises(PaymentConfigurationError, match="no payment agreement"):
            account_for(first.pk)


@pytest.mark.parametrize("value", [0, -1, True, "1", None])
def test_invalid_organization_never_queries_or_uses_global_account(value: Any) -> None:
    with pytest.raises(PaymentConfigurationError, match="organization"):
        account_for(value)
