# SPDX-License-Identifier: AGPL-3.0-only
"""Named Vipps credential groups, held in memory by the Infisical startup path."""

import re
from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured

from randirandi.environment import Environment
from randirandi.vipps import Client, Credentials

PREFIX = r"VIPPS(?:_[A-Z0-9]+)*"
FIELDS = ("CLIENT_ID", "CLIENT_SECRET", "SUBSCRIPTION_KEY", "MSN", "WEBHOOK_SECRET")


class PaymentConfigurationError(ValueError):
    """A fixed configuration refusal, containing no supplied value."""


class Secrets:
    """Settings value with a safe representation and no inherited subprocess env."""

    def __init__(self, values: Mapping[str, str], environment: Environment) -> None:
        if not isinstance(environment, Environment):
            raise ImproperlyConfigured("Payment environment must come from deployment.")
        self.environment = environment
        self._values = {
            name: value
            for name, value in values.items()
            if re.fullmatch(rf"{PREFIX}_(?:{'|'.join(FIELDS)})", name)
        }
        self._clients: dict[str, Client] = {}

    def __repr__(self) -> str:
        return "<Vipps secrets held in memory>"

    def credentials(
        self, *, prefix: str, merchant_number: str, test_mode: bool
    ) -> Credentials:
        if (
            test_mode is not (self.environment is Environment.SANDBOX)
            or re.fullmatch(PREFIX, prefix) is None
        ):
            raise PaymentConfigurationError(
                "Payment account and deployment do not agree."
            )
        try:
            credentials = Credentials(
                *(self._values[f"{prefix}_{name}"] for name in FIELDS[:4])
            )
        except KeyError, ValueError:
            raise PaymentConfigurationError(
                "Payment credentials are missing or invalid."
            ) from None
        if credentials.merchant_number != merchant_number:
            raise PaymentConfigurationError(
                "Payment merchant and credentials do not agree."
            )
        return credentials

    def client(self, *, prefix: str, merchant_number: str, test_mode: bool) -> Client:
        credentials = self.credentials(
            prefix=prefix,
            merchant_number=merchant_number,
            test_mode=test_mode,
        )
        if prefix not in self._clients:
            self._clients[prefix] = Client(self.environment, credentials)
        return self._clients[prefix]

    def webhook_secret(
        self, *, prefix: str, merchant_number: str, test_mode: bool
    ) -> str:
        self.credentials(
            prefix=prefix, merchant_number=merchant_number, test_mode=test_mode
        )
        value = self._values.get(f"{prefix}_WEBHOOK_SECRET", "")
        if (
            not value
            or not value.isascii()
            or not 16 <= len(value) <= 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise PaymentConfigurationError(
                "Payment webhook secret is missing or invalid."
            )
        return value
