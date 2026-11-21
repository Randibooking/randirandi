# SPDX-License-Identifier: AGPL-3.0-only
"""Client address at the explicitly selected deployment boundary."""

import ipaddress

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.http import HttpRequest


def client_address(request: HttpRequest) -> str:
    try:
        peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
        if settings.CLIENT_ADDRESS_SOURCE == "direct":
            return str(peer)
        # The observed Scaleway sidecar connects over loopback and overwrites
        # this header. X-Forwarded-For retains visitor-supplied prefixes.
        if not peer.is_loopback:
            raise ValueError("Untrusted ingress")
        return str(
            ipaddress.ip_address(request.META.get("HTTP_X_ENVOY_EXTERNAL_ADDRESS", ""))
        )
    except ValueError:
        raise SuspiciousOperation("The client address could not be verified.") from None
