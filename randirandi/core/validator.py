# SPDX-License-Identifier: AGPL-3.0-only
"""The validator: whether a container may be saved .

A container is refused, naming the reason, when a date period of it is not
covered without a gap by its asset's weekly availability, or when it is
shared and covers the same time as another shared container on the same
asset that books a different floor plan (`on_other_floor_plans`): their spots
cannot be compared. Any other containers may stack: the database keeps their
bookings apart. Another container is one that is enabled, or disabled with a
held or confirmed booking, or a block, still to come: it stops selling and
keeps its time.
Only date periods that end after the save are checked, and none that starts
before the asset's first availability calendar. Closures by date are never
checked, and saving availability is never refused. A stored container
that drops its price book while it still has prices is refused too (W12).

A booking category's own hours on a container  are
set as one replace and refused, naming the reason, when a span lies outside the
container's hours or would leave a booking still to come of that category
outside them.

Every check runs inside the saving transaction, under the organization row
lock that ordering takes (`ordering._attempt`): two saves that would clash
wait for each other, and the second is refused.
"""

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from randirandi import category_hours_rule, labels
from randirandi.core import calendar
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    AvailableSpan,
    Block,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingState,
    CategoryHoursSpan,
    ConfigurationCategory,
    ConsumptionMode,
    Organization,
    PriceBookEntry,
    Status,
    TakenTime,
    Weekday,
)
from randirandi.core.models.base import DAY, clock
from randirandi.core.models.bookable import on_other_floor_plans, week_of
from randirandi.core.models.orders import EVENT_STATE, HOLDING

# A booking in these states is still to come until it ends.
TO_COME = (BookingState.HELD, BookingState.CONFIRMED)

# What stays as it is while a container has a booking still to come, each
# with the field it is reported on : each change
# would leave the booking outside its own grid. The stop week may move, but
# not before the last booking.
FIXED = (
    ("asset", "asset_id"),
    ("floor_plan", "floor_plan_id"),
    ("consumption_mode", "consumption_mode"),
    ("period_type", "period_type"),
    ("period_start", "period_start"),
    ("period_end", "period_end"),
    ("end_day", "end_day"),
    ("period_start_weekdays", "period_start_weekdays"),
    ("interval", "interval"),
    ("start_date", "start_date"),
    ("every_weeks", "every_weeks"),
)

# Each of those in plain words, as a refusal names it .
FIXED_WORDS = {
    "asset": "asset",
    "floor_plan": "floor plan",
    "consumption_mode": "consumption mode",
    "period_type": "period type",
    "period_start": "period start",
    "period_end": "period end",
    "end_day": "end day",
    "period_start_weekdays": "period start weekdays",
    "interval": "interval",
    "start_date": "first calendar week",
    "every_weeks": "number of weeks between periods",
}

# What the asset's availability must cover is worked out from these. A save
# of an enabled container that changes none of them is not refused for time
# a later availability change took away (builder's pick, review of
# 2026-10-17): availability wins and is never refused (W10), so the time it
# took is left unoffered, and the container's other fields can still change,
# even while its times stay as they are for a booking still to come.
# The floor plan is not among them: it changes which spots are booked, not
# when.
COVERED = (
    "asset_id",
    "period_type",
    "period_start",
    "period_end",
    "end_day",
    "period_start_weekdays",
    "start_date",
    "stop_date",
    "every_weeks",
)

# No date period is checked this close to the end of the calendar.
LAST_DAY = date.max - timedelta(days=8)


@dataclass(frozen=True)
class Clash:
    """Two containers that cover the same time although they book different
    floor plans of one asset: one of them was written past the validator."""

    one: BookingConfiguration
    other: BookingConfiguration


def check(config: BookingConfiguration, *, now: datetime) -> None:
    """Refuse to save the container, with a `ValidationError` that names the
    reason and the other container. Runs in the saving transaction and takes
    the organization row lock first.
    """
    if timezone.is_naive(now):
        raise ValueError("The current moment must include a time zone.")
    if config.organization_id is None or config.asset_id is None:
        # A missing link is the database's to refuse.
        return
    organization = _lock(config.organization_id)
    after = _wall(now, config.asset)
    faults: dict[str, str] = _floor_plan(config)
    if not config._state.adding:
        faults |= _fixed(config, now, organization)
        faults |= _priced(config, organization)
    complete = None not in (
        config.period_start,
        config.period_end,
        config.end_day,
        config.interval,
        config.minimum_length,
        config.maximum_length,
        config.every_weeks,
    ) and bool(config.period_start_weekdays)
    # An incomplete or impossible container is refused by its own checks and
    # the database's, and is never offered.
    if (
        complete
        and config.status == Status.ENABLED
        and calendar.valid_configuration(config)
    ):
        other = next(
            (
                other
                for other in others(config, now)
                if config.covers_the_same_time_as(other, after)
            ),
            None,
        )
        if other is not None:
            faults["status"] = (
                f"It covers the same time as {_named(other)}; the two book "
                "different floor plans of the asset."
            )
        gap = (
            _uncovered(config, after) if _changes_cover(config, organization) else None
        )
        if gap is not None:
            day, moment = gap
            faults[NON_FIELD_ERRORS] = (
                f"Its date period that starts on {_day(day)} at "
                f"{clock(config.period_start)} is not covered by the asset's "
                f"availability: the asset is not available from "
                f"{clock(moment.hour * 60 + moment.minute)} on {_day(moment.date())}."
            )
    if (
        complete
        and not config._state.adding
        and (outside := _hours_left_outside(config, organization))
    ):
        faults[NON_FIELD_ERRORS] = " ".join(
            filter(None, (faults.get(NON_FIELD_ERRORS), outside))
        )
    if faults:
        raise ValidationError(faults)


def _hours_left_outside(
    config: BookingConfiguration, organization: Organization
) -> str:
    """Why the container's new hours would leave a booking category's own
    hours outside them , naming each span; empty when none would. The
    category's hours are narrowed first, with `set_category_hours`."""
    spans: dict[BookingCategory, list[category_hours_rule.Hours]] = defaultdict(list)
    for span in (
        CategoryHoursSpan.objects.of(organization)
        .filter(configuration_category__booking_configuration=config.pk)
        .select_related("configuration_category__booking_category")
        .order_by("weekday", "start_time")
    ):
        spans[span.configuration_category.booking_category].append(
            (span.weekday, span.start_time, span.end_time)
        )
    faults = [
        fault
        for category, hours in spans.items()
        for fault in _category_hours_faults(config, category, hours)
    ]
    if not faults:
        return ""
    return " ".join(faults) + " Narrow the category's own hours first."


def set_category_hours(
    config: BookingConfiguration,
    category: BookingCategory,
    hours: Sequence[category_hours_rule.Hours],
    *,
    now: datetime,
) -> None:
    """Replace the container's own hours for a booking category it names with
    `hours`, `(weekday, start, end)` with the times in minutes, or give the
    category the container's hours again with none . Refused, with a
    `ValidationError` that names the reason, when the container does not name
    the category, a span is not a stretch of one weekday, two spans of a
    weekday share time, a span does not lie inside the container's hours on a
    generic week, or the change leaves a booking of the category still to
    come outside the new hours. Takes the organization row lock first.
    """
    with transaction.atomic():
        organization = _lock(config.organization_id)
        stored = BookingConfiguration.objects.of(organization).get(pk=config.pk)
        link = (
            ConfigurationCategory.objects.of(organization)
            .filter(booking_configuration=stored.pk, booking_category=category.pk)
            .first()
        )
        if link is None:
            raise ValidationError(
                {
                    "booking_category": f"Container {stored} does not name "
                    f"{category.name}: name it before it gets hours."
                }
            )
        faults = _category_hours_faults(stored, category, hours)
        if not faults:
            faults = _orphaned_by_hours(stored, category, hours, now, organization)
        if faults:
            raise ValidationError({"category_hours": faults})
        hours_of = CategoryHoursSpan.objects.of(organization)
        hours_of.filter(configuration_category=link).delete()
        hours_of.bulk_create(
            CategoryHoursSpan(
                organization=organization,
                configuration_category=link,
                weekday=weekday,
                start_time=start,
                end_time=end,
            )
            for weekday, start, end in hours
        )


def _words(category: BookingCategory, hours: category_hours_rule.Hours) -> str:
    weekday, start, end = hours
    day = Weekday(weekday).label if weekday in Weekday.values else f"weekday {weekday}"
    return f"{category.name} {day} {clock(start)} to {clock(end)}"


def _category_hours_faults(
    config: BookingConfiguration,
    category: BookingCategory,
    hours: Sequence[category_hours_rule.Hours],
) -> list[str]:
    """Why the hours are no stretches of a weekday, share time, or lie outside
    the container's hours; empty when none does."""
    faults = [
        f"{_words(category, span)} is not a stretch of one weekday: the weekday "
        "is 0 to 6 and it ends after it starts, at 24:00 at the latest."
        for span in hours
        if not (0 <= span[0] <= Weekday.SUNDAY and 0 <= span[1] < span[2] <= DAY)
    ]
    if faults:
        return faults
    if shared := category_hours_rule.overlapping(hours):
        return [
            f"{_words(category, shared[0])} and {_words(category, shared[1])} "
            "share time."
        ]
    return [
        f"{_words(category, span)} is not inside the hours of container {config}."
        for span in category_hours_rule.hours_outside(
            config.period_start,
            config.period_end,
            config.end_day,
            config.period_start_weekdays,
            hours,
        )
    ]


def _orphaned_by_hours(
    config: BookingConfiguration,
    category: BookingCategory,
    hours: Sequence[category_hours_rule.Hours],
    now: datetime,
    organization: Organization,
) -> list[str]:
    """The bookings of the category still to come on the container that the
    new hours would leave outside them, asked of the calendar's own test
    (`calendar.category_allows`), so a day the clocks change answers as the
    calendar does. Nothing for no hours: the container's hours hold them."""
    if not hours:
        return []
    zone = config.asset.zone
    spans: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for weekday, start, end in hours:
        spans[weekday].append((start, end))
    grid = calendar.hours_grid(config.asset, zone, config, category.pk, spans)
    return [
        f"The hours of {category.name} stay as they are for a booking still to "
        f"come: order {booking.order.reference}, from {_day(first.date())} at "
        f"{clock(first.hour * 60 + first.minute)}, would lie outside them."
        for booking in (
            Booking.objects.of(organization)
            .filter(
                booking_configuration=config.pk,
                booking_category=category.pk,
                taken_time__state__in=TO_COME,
                taken_time__ends_at__gt=now,
            )
            .select_related("order", "taken_time")
            .order_by("taken_time__starts_at")
        )
        for first in [booking.taken_time.starts_at.astimezone(zone)]
        if not calendar.category_allows(
            grid,
            config,
            category.pk,
            booking.taken_time.starts_at,
            booking.taken_time.ends_at,
        )
    ]


def _floor_plan(config: BookingConfiguration) -> dict[str, str]:
    """A shared container books the spots of one of its asset's floor plans;
    an exclusive one uses the whole asset and names none (W3)."""
    if config.consumption_mode == ConsumptionMode.EXCLUSIVE:
        if config.floor_plan_id is None:
            return {}
        return {
            "floor_plan": "It is left out: an exclusive container uses the whole asset."
        }
    if (
        config.floor_plan_id is None
        or not AssetInventory.objects.of(config.organization_id)
        .filter(pk=config.floor_plan_id, asset=config.asset_id)
        .exists()
    ):
        return {
            "floor_plan": "It is one of the asset's floor plans: a shared container "
            "books its spots."
        }
    return {}


def others(
    config: BookingConfiguration, now: datetime
) -> QuerySet[BookingConfiguration]:
    """The other containers the container may not cover the same time as
    : `on_other_floor_plans`, and enabled or still holding a booking to
    come.
    """
    if config.consumption_mode != ConsumptionMode.SHARED:
        return BookingConfiguration.objects.of(config.organization_id).none()
    return (
        _kept(config.organization_id, now)
        .filter(asset=config.asset_id, consumption_mode=ConsumptionMode.SHARED)
        .exclude(floor_plan=config.floor_plan_id)
        .exclude(pk=config.pk)
    )


def rivals(
    asset: Asset,
    configurations: list[BookingConfiguration],
    now: datetime,
    zone: ZoneInfo,
) -> dict[int, list[BookingConfiguration]]:
    """For each of the asset's configurations, the other containers that
    cover the same time from `now` on although they may not
    (`on_other_floor_plans`). The run-time guard leaves the time they share
    unoffered for both.
    """
    after = now.astimezone(zone).replace(tzinfo=None)
    kept = list(_kept(asset.organization_id, now).filter(asset=asset.pk))
    return {
        config.pk: [
            other
            for other in kept
            if on_other_floor_plans(config, other)
            and config.covers_the_same_time_as(other, after)
        ]
        for config in configurations
    }


def clashes(organization: Organization, *, now: datetime) -> list[Clash]:
    """Every pair of the organization's containers that cover the same time
    from `now` on although they may not (`on_other_floor_plans`). The admin
    screens show the same list.
    """
    containers = list(_kept(organization.pk, now).select_related("asset"))
    return [
        Clash(one, other)
        for index, one in enumerate(containers)
        for other in containers[index + 1 :]
        if on_other_floor_plans(one, other)
        # The two share an asset or a space, so a zone: the first one's.
        and one.covers_the_same_time_as(other, _wall(now, one.asset))
    ]


def check_place(
    asset: Asset, *, space_id: int | None, whole_space: bool, now: datetime
) -> None:
    """Refuse to move the asset to another space, or to change whether it
    covers its whole space, while it has a booking still to come.
    """
    organization = _lock(asset.organization_id)
    stored = Asset.objects.of(organization).get(pk=asset.pk)
    _check_place(
        stored,
        organization,
        now=now,
        field="space" if space_id != stored.space_id else "whole_space",
    )


def check_time_zone(asset: Asset, *, time_zone: str, now: datetime) -> None:
    """Refuse to change the asset's time zone while it has a booking or a
    block still to come . `asset` is the stored
    one. The database goes further: a claim keeps a copy of the zone, so no
    change is made once the asset has any claim, a past one too."""
    organization = _lock(asset.organization_id)
    if (
        asset.time_zone != time_zone
        and TakenTime.objects.of(organization)
        .filter(asset=asset.pk, state__in=HOLDING, ends_at__gt=now)
        .exists()
    ):
        raise ValidationError(
            {
                "time_zone": f"{asset} has a booking or a block, also a past one, so "
                "its time zone stays as it is; make a new asset in that zone instead."
            }
        )


def _check_place(
    asset: Asset,
    organization: Organization,
    *,
    now: datetime,
    field: str,
) -> None:
    if (
        Booking.objects.of(organization)
        .filter(
            taken_time__asset=asset.pk,
            taken_time__state__in=TO_COME,
            taken_time__ends_at__gt=now,
        )
        .exists()
    ):
        raise ValidationError(
            {
                field: f"{asset} has a booking still to come, so its space and "
                "whether it covers its whole space stay as they are."
            }
        )
    if (
        TakenTime.objects.of(organization)
        .filter(asset=asset.pk, state=EVENT_STATE, ends_at__gt=now)
        .exists()
    ):
        raise ValidationError(
            {
                field: f"{asset} has an event still to come, so its space and "
                "whether it covers its whole space stay as they are."
            }
        )


def _changes_cover(config: BookingConfiguration, organization: Organization) -> bool:
    """Whether the save asks the availability to cover more or other time:
    a new container, one being enabled, or a change to what it covers.
    """
    if config._state.adding:
        return True
    stored = BookingConfiguration.objects.of(organization).filter(pk=config.pk).first()
    return (
        stored is None
        or stored.status != Status.ENABLED
        or any(getattr(stored, name) != getattr(config, name) for name in COVERED)
    )


def _lock(organization_id: int) -> Organization:
    """The organization row, locked the way ordering locks it."""
    organization = (
        Organization.objects.select_for_update(no_key=True)
        .filter(pk=organization_id)
        .first()
    )
    if organization is None:
        raise ValidationError("There is no such organization.")
    return organization


def _kept(organization_id: int, now: datetime) -> QuerySet[BookingConfiguration]:
    """The organization's containers that keep their time: enabled, or
    disabled with a held or confirmed booking, or a block, still to come."""
    sold = Booking.objects.of(organization_id).filter(
        booking_configuration=OuterRef("pk"),
        taken_time__state__in=TO_COME,
        taken_time__ends_at__gt=now,
    )
    blocked = Block.objects.of(organization_id).filter(
        booking_configuration=OuterRef("pk"), taken_time__ends_at__gt=now
    )
    return (
        BookingConfiguration.objects.of(organization_id)
        .filter(Q(status=Status.ENABLED) | Exists(sold) | Exists(blocked))
        .select_related("asset")
        .order_by("number")
    )


def _fixed(
    config: BookingConfiguration, now: datetime, organization: Organization
) -> dict[str, str]:
    """The fields that stay as they are while the stored container has a
    booking still to come, and that the save would change."""
    stored = BookingConfiguration.objects.of(organization).get(pk=config.pk)
    bookings = list(
        Booking.objects.of(organization)
        .filter(
            booking_configuration=config.pk,
            taken_time__state__in=TO_COME,
            taken_time__ends_at__gt=now,
        )
        .select_related("order", "taken_time")
        .order_by("taken_time__starts_at")
    )
    if not bookings:
        return {}
    zone = stored.asset.zone
    first = bookings[0].taken_time.starts_at.astimezone(zone)
    booking = (
        f"order {bookings[0].order.reference}, from {_day(first.date())} at "
        f"{clock(first.hour * 60 + first.minute)}"
    )
    faults = {
        field: f"The {FIXED_WORDS[field]} stays as it is while the container has "
        f"a booking still to come: {booking}."
        for field, attribute in FIXED
        if getattr(stored, attribute) != getattr(config, attribute)
    }
    if config.stop_date is not None and config.stop_date != stored.stop_date:
        last = bookings[-1]
        day = calendar.period_day(
            stored, last.taken_time.starts_at.astimezone(zone).replace(tzinfo=None)
        )
        if config.stop_date < day:
            faults["stop_date"] = (
                "The last calendar week is not before the week of the last "
                f"booking still to come: order {last.order.reference}, in the date "
                f"period that starts on {_day(day)}."
            )
    return faults


def _priced(config: BookingConfiguration, organization: Organization) -> dict[str, str]:
    """Refuses a container without a price book that still has prices: no
    price applies without payment (W12), and dropping the book would make
    its bookings free without a word. Adding a book is always allowed."""
    if config.with_payment:
        return {}
    if not (
        PriceBookEntry.objects.of(organization)
        .filter(booking_configuration=config.pk)
        .exists()
    ):
        return {}
    return {
        "price_book": "It cannot be without payment while it has prices: "
        "remove its prices first."
    }


def _uncovered(
    config: BookingConfiguration, after: datetime
) -> tuple[date, datetime] | None:
    """The first date period ending after the wall moment that the asset's
    weekly availability does not cover, and the first moment of it the
    asset is not available; None when every one is covered.

    The availability calendar in force on a day applies to it: the one with
    the latest start date reached. Inside one calendar's days, a date
    period's cover depends only on its weekday, so the first two weeks in
    force there settle every weekday; the periods that run into the next
    calendar are checked one by one. The work does not grow with the number
    of weeks.
    """
    org_id = config.organization_id
    calendars = list(
        AvailabilityCalendar.objects.of(org_id)
        .filter(asset=config.asset_id)
        .order_by("start_date")
    )
    if not calendars:
        return None
    spans: dict[int, dict[int, list[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for cal_id, weekday, first, last in (
        AvailableSpan.objects.of(org_id)
        .filter(availability_calendar__in=calendars)
        .values_list("availability_calendar_id", "weekday", "start_time", "end_time")
    ):
        spans[cal_id][weekday].append((first, last))
    starts = [row.start_date for row in calendars]
    ends = timedelta(minutes=config.end_day * DAY + config.period_end)
    lower = max(
        starts[0],
        config.start_date or date.min,
        (after - ends).date() + timedelta(days=1),
    )
    upper = min(config.stop_date or date.max, LAST_DAY)
    days: set[date] = set()
    for index, begin in enumerate(starts):
        following = index + 1 < len(starts)
        finish = starts[index + 1] - timedelta(days=1) if following else upper
        begin, finish = max(begin, lower), min(finish, upper)
        if begin > finish:
            continue
        days.update(_first_starts(config, begin, finish))
        if following:
            # The periods that start late enough to run into the next calendar.
            days.update(
                day
                for ordinal in range(
                    max(begin, finish - timedelta(days=config.end_day)).toordinal(),
                    finish.toordinal() + 1,
                )
                if config.starts_on(day := date.fromordinal(ordinal))
            )
    for day in sorted(days):
        midnight = datetime.combine(day, time())
        start = midnight + timedelta(minutes=config.period_start)
        end = midnight + ends
        parts: list[calendar.Span] = []
        for ordinal in range(day.toordinal(), end.date().toordinal() + 1):
            touched = date.fromordinal(ordinal)
            applies = calendars[bisect_right(starts, touched) - 1]
            base = datetime.combine(touched, time())
            parts.extend(
                (base + timedelta(minutes=first), base + timedelta(minutes=last))
                for first, last in spans[applies.pk][touched.weekday()]
            )
        merged = calendar._merge(parts)
        if not calendar._covered(start, end, merged):
            return day, next((b for a, b in merged if a <= start < b), start)
    return None


def _first_starts(
    config: BookingConfiguration, begin: date, finish: date
) -> list[date]:
    """The days from `begin` to `finish` that a date period starts on in the
    first two weeks in force there."""
    week = week_of(begin)
    if config.start_date is not None:
        week += (week_of(config.start_date) - week) % config.every_weeks
    found = []
    for in_force in (week, week + config.every_weeks):
        for weekday in config.period_start_weekdays:
            ordinal = date.min.toordinal() + 7 * in_force + weekday
            if begin.toordinal() <= ordinal <= finish.toordinal():
                day = date.fromordinal(ordinal)
                if config.starts_on(day):
                    found.append(day)
    return found


def _wall(moment: datetime, asset: Asset) -> datetime:
    """The moment on the asset's clock, without its time zone."""
    return moment.astimezone(asset.zone).replace(tzinfo=None)


def _named(other: BookingConfiguration) -> str:
    """The other container, as a refusal names it."""
    if other.status == Status.ENABLED:
        return f"container {other} on {other.asset}"
    return (
        f"container {other} on {other.asset}, which is disabled and has a "
        "booking or a block still to come"
    )


def _day(day: date) -> str:
    """A date as an administrator reads it: Monday 1 March 2027. The refusals
    are English sentences, so the date is the product's own English, from the
    catalogue's `weekdays.*` and `months.*`, whatever the organization's
    language."""
    return labels.date_words(None, day, year=True)
