# SPDX-License-Identifier: AGPL-3.0-only
import os
import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from randirandi.core.payment_testing import approve
from randirandi.core.payments import PaymentBusyError, PaymentUnavailableError
from randirandi.payment_configuration import PaymentConfigurationError
from randirandi.vipps import VippsError


class Command(BaseCommand):
    help = "Approve an existing sandbox payment with a prepared test identity."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("organization_id", type=int)
        parser.add_argument("order_public_id", type=uuid.UUID)
        parser.add_argument(
            "--request-key",
            type=uuid.UUID,
            required=True,
            help="Keep this key in the test scenario before the first attempt.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        phone = os.environ.pop("RANDIRANDI_TEST_PHONE", "")
        try:
            status = approve(
                organization_id=options["organization_id"],
                order_public_id=options["order_public_id"],
                request_key=options["request_key"],
                phone=phone,
            )
        except (
            PaymentBusyError,
            PaymentUnavailableError,
            PaymentConfigurationError,
            VippsError,
        ):
            raise CommandError(
                "Test approval unresolved. Check sandbox configuration and the "
                "prepared test identity; retry the same order and request key."
            ) from None
        self.stdout.write(f"Test payment provider state: {status.state}.")
