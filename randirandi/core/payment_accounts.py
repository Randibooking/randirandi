# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve only the named organization's payment agreement ."""

from django.conf import settings

from randirandi.core.models import PaymentProvider, PaymentProviderAccount
from randirandi.payment_configuration import PaymentConfigurationError, Secrets
from randirandi.vipps import Client


def account_for(organization_id: int) -> PaymentProviderAccount:
    if type(organization_id) is not int or organization_id < 1:
        raise PaymentConfigurationError("A saved organization is required.")
    account = (
        PaymentProviderAccount.objects.of(organization_id)
        .filter(
            payment_provider=PaymentProvider.VIPPS,
        )
        .first()
    )
    if account is None:
        raise PaymentConfigurationError("This organization has no payment agreement.")
    secrets: Secrets = settings.VIPPS_SECRETS
    secrets.credentials(
        prefix=account.credentials,
        merchant_number=account.merchant_number,
        test_mode=account.test_mode,
    )
    return account


def client_for(account: PaymentProviderAccount) -> Client:
    secrets: Secrets = settings.VIPPS_SECRETS
    return secrets.client(
        prefix=account.credentials,
        merchant_number=account.merchant_number,
        test_mode=account.test_mode,
    )


def webhook_secret_for(account: PaymentProviderAccount) -> str:
    secrets: Secrets = settings.VIPPS_SECRETS
    return secrets.webhook_secret(
        prefix=account.credentials,
        merchant_number=account.merchant_number,
        test_mode=account.test_mode,
    )
