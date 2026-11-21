# SPDX-License-Identifier: AGPL-3.0-only
"""The operator's erasure, for tests that erase a member on the way ."""

from randirandi.core.member_data import operator_erase_member
from randirandi.core.models import BookingState, Member, MemberStatus, TakenTime
from tests import made_up


def settle_coming(made: made_up.MadeUp, member_id: int) -> None:
    """Make the member fit for erasure: nothing to come.

    The bookings stay, cancelled. An administrator is disabled first, which
    keeps the many tests that erase one on the way off the last-administrator path.
    """
    TakenTime.objects.of(made.organization).filter(
        booking__member_id=member_id,
        state__in=(BookingState.HELD, BookingState.CONFIRMED),
    ).update(state=BookingState.CANCELLED)
    Member.objects.of(made.organization).filter(
        pk=member_id, role__admin_access=True
    ).update(status=MemberStatus.DISABLED)


def erase(made: made_up.MadeUp, member_id: int) -> None:
    settle_coming(made, member_id)
    operator_erase_member(organization=made.organization, member_id=member_id)
