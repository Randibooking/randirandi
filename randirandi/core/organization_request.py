# SPDX-License-Identifier: AGPL-3.0-only
"""Hostname resolution shared by the public login and authenticated session guard."""

import re

from django.conf import settings
from django.http import Http404, HttpRequest

from randirandi.core.models import Organization
from randirandi.core.models.organization import RESERVED_SUBDOMAINS


def organization_for_request(request: HttpRequest) -> Organization:
    """Resolve one label under the deployment's domain after host validation.

    A form, query string, referrer or forwarded-host header cannot choose the
    organization. ALLOWED_HOSTS remains the independent first boundary.
    """
    host = request.get_host().partition(":")[0].lower().rstrip(".")
    suffix = "." + settings.ORGANIZATION_DOMAIN
    if not host.endswith(suffix):
        raise Http404
    subdomain = host[: -len(suffix)]
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", subdomain)
        or subdomain in RESERVED_SUBDOMAINS
    ):
        raise Http404
    try:
        return Organization.objects.get(subdomain=subdomain)
    except Organization.DoesNotExist:
        raise Http404 from None
