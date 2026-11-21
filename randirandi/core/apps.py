# SPDX-License-Identifier: AGPL-3.0-only
from django.apps import AppConfig


class CoreConfig(AppConfig):
    """The objects the product stores, and the rules they keep."""

    name = "randirandi.core"

    def ready(self) -> None:
        # Explicit registration; no default global User/Group admin.
        from randirandi.core import admin  # noqa: F401
