# SPDX-License-Identifier: AGPL-3.0-only
"""The limits on one member's bookings .

Both are the organization's, both optional (empty: no limit), both read on
the organization's clock (`Organization.time_zone`): the most bookings a member
may make in one calendar day, and how far ahead a booking may start. A booking
here is a `Booking` row, one booked time, never its price lines. Administrators'
blocks and the old-bookings import are not bookings made by a member's order
and are never counted or limited.
"""

import calendar as month_calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q

from randirandi.core.models import Booking, BookingState, Member, Organization


def last_date(today: date, months: int) -> date:
    """The last bookable date: today and the months on, a day that the month
    does not have being the month's last."""
    index = today.year * 12 + today.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(today.day, month_calendar.monthrange(year, month)[1]))


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), zone).astimezone(UTC)


def starts_before(organization: Organization, moment: datetime) -> datetime | None:
    """The first moment a start is too far ahead: the end of the last
    bookable date, that whole date included. None without a limit."""
    if organization.book_ahead_months is None:
        return None
    zone = ZoneInfo(organization.time_zone)
    today = moment.astimezone(zone).date()
    after = last_date(today, organization.book_ahead_months) + timedelta(days=1)
    return _midnight(after, zone)


def made_today(member: Member, organization: Organization, moment: datetime) -> int:
    """The member's bookings whose order was made on today's date on the
    organization's clock and that count: held with the hold running, or ever
    confirmed (confirmed, or cancelled after the order was paid or free).

    A held booking counts while its state is held, whatever `hold_expires_at`
    says: a hold can run out while a capture is in flight, before
    `expire_holds` marks it expired, and it then confirms.

    An order that was never paid and was released is not counted: its
    bookings are `expired` (the hold ran out, or the member let it go), or
    `cancelled` while `Order.hold_expires_at` is still set. Payment (or
    being free) clears `hold_expires_at`, so a cancelled booking whose order
    has none was confirmed first.
    """
    zone = ZoneInfo(organization.time_zone)
    today = moment.astimezone(zone).date()
    return (
        Booking.objects.of(organization.pk)
        .filter(
            member_id=member.pk,
            order__created_at__gte=_midnight(today, zone),
            order__created_at__lt=_midnight(today + timedelta(days=1), zone),
        )
        .filter(
            Q(taken_time__state=BookingState.HELD)
            | Q(taken_time__state=BookingState.CONFIRMED)
            | Q(
                taken_time__state=BookingState.CANCELLED,
                order__hold_expires_at__isnull=True,
            )
        )
        .count()
    )


def left_today(
    member: Member, organization: Organization, moment: datetime, adding: int = 0
) -> int | None:
    """How many more bookings the member may make today, less `adding` the
    bookings about to be made; None without a limit. Zero when none is left,
    never below."""
    if organization.max_bookings_a_day is None:
        return None
    left = organization.max_bookings_a_day - made_today(member, organization, moment)
    return max(0, left - adding)
