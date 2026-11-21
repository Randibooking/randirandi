# SPDX-License-Identifier: AGPL-3.0-only
"""One-way, organization-bound identity matching for the temporary mail queue."""

from django.utils.crypto import salted_hmac


def identifier_digest(organization_id: int, identifier: str) -> str:
    return salted_hmac(
        f"randirandi.login-identifier.{organization_id}", identifier, algorithm="sha256"
    ).hexdigest()
