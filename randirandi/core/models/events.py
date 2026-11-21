# SPDX-License-Identifier: AGPL-3.0-only
"""Events : an object of its own, on an asset or on none.

An event of one organization has a category, a title, a description, an
asset or none, a contact person and which of the contact's details members
see. It is one day (a one-off) or a weekly repeat (`EventRepeat`); each day
it falls on is an `EventDay` of its own, and a day on an asset holds the
whole asset as a claim in `TakenTime`, in the state `event`. A day a repeat
could not take is an `EventSkippedDay`, with its reason, and the people to
tell are `EventNotice`s.
"""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from randirandi.core.models.base import DAY, Owned, organization_rules
from randirandi.core.models.bookable import CLOSURE_FIRST_DAY, CLOSURE_LAST_DAY


class EventCategory(models.TextChoices):
    """The four categories, by key. The words come from the organization's
    labels, never from the server ."""

    SOCIAL = "social", "Social event"
    VOLUNTEER_WORK = "volunteer_work", "Volunteer work"
    ORGANIZATION = "organization", "The organization's own"
    OPEN_HOUSE = "open_house", "Open house"


class SkipReason(models.TextChoices):
    """Why a repeat's day was not made: the asset is closed, or a booking, a
    block or another event holds the time."""

    CLOSED = "closed", "Closed"
    BOOKED = "booked", "Booked"
    BLOCKED = "blocked", "Blocked"
    EVENT = "event", "Event"


# The most a title and a description hold.
LONGEST_TITLE = 200
LONGEST_DESCRIPTION = 2000


class Event(Owned):
    """A gathering of the organization. With an asset, each day of it holds
    the asset; without one it is recorded all the same and takes no time."""

    category = models.CharField(max_length=14, choices=EventCategory)
    title = models.CharField(max_length=LONGEST_TITLE)
    description = models.TextField(blank=True)
    asset = models.ForeignKey(
        "core.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # A member of the organization, read when shown, never copied onto the
    # event. Empty once the member is erased: admin lists the event as
    # needing one (S13).
    contact = models.ForeignKey(
        "core.Member", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # What members see of the contact beside the name, which is always shown.
    show_email = models.BooleanField(default=True)
    show_phone = models.BooleanField(default=False)
    # The administrator who made it; never shown to members.
    made_by = models.ForeignKey(
        "core.Member", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("asset", "contact", "made_by"),
            models.CheckConstraint(
                condition=Q(category__in=EventCategory.values),
                name="%(class)s_category_is_known",
            ),
            models.CheckConstraint(
                condition=~Q(title=""), name="%(class)s_has_a_title"
            ),
        ]

    def __str__(self) -> str:
        return f"event {self.pk}"


class EventRepeat(Owned):
    """The weekly rule of a repeating event: on chosen weekdays (bit 0 is
    Monday), from a first date to an end date or none, between two local
    times of day in minutes (the end may be 24:00). Read on the asset's
    clock, or the organization's for an event without an asset."""

    event = models.OneToOneField(Event, on_delete=models.CASCADE, related_name="repeat")
    weekdays = models.PositiveSmallIntegerField()
    first_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    start_minute = models.PositiveSmallIntegerField()
    end_minute = models.PositiveSmallIntegerField()
    # The last date whose days have been worked out; the daily top-up goes on
    # from the next. Empty until the first work.
    made_through = models.DateField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("event"),
            models.CheckConstraint(
                condition=Q(weekdays__gte=1, weekdays__lte=127),
                name="%(class)s_weekdays",
            ),
            models.CheckConstraint(
                condition=Q(start_minute__gte=0)
                & Q(start_minute__lt=F("end_minute"))
                & Q(end_minute__lte=DAY),
                name="%(class)s_hours",
            ),
            models.CheckConstraint(
                condition=Q(first_date__gte=CLOSURE_FIRST_DAY)
                & Q(first_date__lte=CLOSURE_LAST_DAY)
                & (
                    Q(end_date__isnull=True)
                    | (
                        Q(end_date__gte=F("first_date"))
                        & Q(end_date__lte=CLOSURE_LAST_DAY)
                    )
                ),
                name="%(class)s_dates",
            ),
        ]

    def __str__(self) -> str:
        return f"event repeat {self.pk}"


class EventDay(Owned):
    """One day of an event: the only day of a one-off, or a day a repeat
    falls on. A day on an asset holds its own claim (`taken_time`); a removed
    day keeps its row, with no claim, so the repeat never makes it again."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="days")
    # The local date the day starts on.
    day = models.DateField()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    # The hours an administrator gave this day alone, in local minutes; both
    # empty while the day has the repeat's. A one-off keeps none.
    own_start = models.PositiveSmallIntegerField(null=True, blank=True)
    own_end = models.PositiveSmallIntegerField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    taken_time = models.OneToOneField(
        "core.TakenTime",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="event_day",
    )

    class Meta(Owned.Meta):
        indexes = [models.Index(fields=["organization", "day"], name="eventday_day")]
        constraints = [
            *organization_rules("event", "taken_time"),
            models.UniqueConstraint(
                fields=["event", "day"], name="%(class)s_once_a_day"
            ),
            models.CheckConstraint(
                condition=Q(starts_at__lt=F("ends_at")),
                name="%(class)s_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(own_start__isnull=True, own_end__isnull=True)
                | (
                    Q(own_start__gte=0)
                    & Q(own_start__lt=F("own_end"))
                    & Q(own_end__lte=DAY)
                ),
                name="%(class)s_own_hours",
            ),
            # A removed day holds no time.
            models.CheckConstraint(
                condition=Q(removed_at__isnull=True) | Q(taken_time__isnull=True),
                name="%(class)s_removed_holds_nothing",
            ),
        ]

    def __str__(self) -> str:
        return f"event day {self.pk}"


class EventSkippedDay(Owned):
    """A day of a repeat that could not be made, with why and what was in the
    way: the order number of a booking, the id of a block or of another
    event; for a closure, its id, or none when the weekly hours are closed.
    Admin lists it among the event's skipped days."""

    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="skipped_days"
    )
    day = models.DateField()
    reason = models.CharField(max_length=7, choices=SkipReason)
    in_the_way = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("event"),
            models.UniqueConstraint(
                fields=["event", "day"], name="%(class)s_once_a_day"
            ),
            models.CheckConstraint(
                condition=Q(reason__in=SkipReason.values),
                name="%(class)s_reason_is_known",
            ),
        ]

    def __str__(self) -> str:
        return f"event skipped day {self.pk}"


class EventNotice(Owned):
    """One email to one administrator about a skipped day, sent by the job
    and tried again until it is sent."""

    skipped_day = models.ForeignKey(
        EventSkippedDay, on_delete=models.CASCADE, related_name="notices"
    )
    recipient = models.ForeignKey("core.Member", on_delete=models.CASCADE)
    claimed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("skipped_day", "recipient"),
            models.UniqueConstraint(
                fields=["skipped_day", "recipient"], name="%(class)s_recipient_once"
            ),
        ]

    def __str__(self) -> str:
        return f"event notice {self.pk}"
