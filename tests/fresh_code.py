# SPDX-License-Identifier: AGPL-3.0-only
"""A current authenticator code for an administrator's data action ."""

from datetime import datetime
from typing import TypedDict

from django_otp.oath import TOTP  # type: ignore[import-untyped]

from randirandi.core.member_data import utc_now
from randirandi.core.models import Authenticator, Member
from tests.sealed import sealed

NETWORK = "192.0.2.9"


class Fresh(TypedDict):
    code: str
    network: str


def fresh(
    administrator: Member, *, enrolled: bool = True, now: datetime | None = None
) -> Fresh:
    """The administrator's code now, on a confirmed device made if missing.

    A test that checks the counter's use makes its own codes; this one starts
    each code from a clean counter so that calls in one 30-second step work.
    """
    moment = utc_now(now)
    device = Authenticator.objects.of(administrator.organization_id).filter(
        member=administrator
    ).first() or Authenticator.objects.create(
        organization_id=administrator.organization_id,
        member=administrator,
        secret=sealed("00" * 20, administrator.organization_id, administrator.pk),
        created_at=moment,
    )
    if enrolled:
        device.confirmed, device.last_counter = True, 0
        device.save(update_fields=["confirmed", "last_counter"])
    clock = TOTP(bytes.fromhex(device.plain_secret()), step=30, digits=6)
    clock.time = moment.timestamp()
    return {"code": f"{clock.token():06d}", "network": NETWORK}
