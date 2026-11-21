# SPDX-License-Identifier: AGPL-3.0-only
from django.apps import AppConfig


class OpsConfig(AppConfig):
    """Running the service: the health page and the job runner."""

    name = "randirandi.ops"
