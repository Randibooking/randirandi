# SPDX-License-Identifier: AGPL-3.0-only
"""Seal an authenticator secret under the test key, as the app stores it."""

from django.conf import settings

from randirandi.core import authenticator_secret


def sealed(plain_hex: str, organization_id: int, member_id: int) -> str:
    return authenticator_secret.seal(
        plain_hex,
        key=settings.AUTHENTICATOR_KEY,
        organization_id=organization_id,
        member_id=member_id,
    )
