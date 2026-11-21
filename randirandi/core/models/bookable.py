# SPDX-License-Identifier: AGPL-3.0-only
"""What can be booked: assets, when they are available, and how they are booked."""

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from math import lcm
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from randirandi.core.models.base import (
    DAY,
    WEEK,
    DatabaseRule,
    Owned,
    clock,
    organization_rules,
    restrict_updates,
)
from randirandi.core.models.capacity import AssetInventory, ConsumptionMode, Space
from randirandi.core.models.organization import (
    TIME_ZONE_FAULT,
    Numbered,
    Organization,
    knows_time_zone,
)
from randirandi.limits import CATEGORY_ICONS
from randirandi.space_rule import NEEDS_SPACE, SPACE_ALREADY_MARKED, space_mark_fault


class Weekday(models.IntegerChoices):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


def week_of(day: date) -> int:
    """The calendar week a day is in, counted straight on from the first
    Monday there is, so that two weeks are always a whole number apart,
    across New Year and week 53 too.
    """
    # The first day there is, 1 January of year 1, is a Monday.
    return (day.toordinal() - date.min.toordinal()) // 7


# Every N weeks is at most a year's weeks: a longer cycle means nothing to an
# organization, and the overlap test steps through one cycle of both.
MOST_WEEKS = 52


type Stretch = tuple[int, int]
"""A stretch of the week that repeats every week: where it starts and where it
ends, in minutes after Monday 00:00. It may run past the end of the week.
"""


def share_time(one: Iterable[Stretch], other: Iterable[Stretch]) -> bool:
    """Whether two sets of weekly stretches share any time."""
    others = list(other)
    return any(
        start < other_end + shift and other_start + shift < end
        for start, end in one
        for other_start, other_end in others
        for shift in (-WEEK, 0, WEEK)
    )


ONE_MARK = (
    "Another asset already covers this space: one asset per space carries the "
    "mark, and different whole-space offers are its containers."
)


class Asset(Numbered):
    """What is booked: a room, a sauna, a hall. It has its availability and
    one or more floor plans, and may belong to a space and cover its whole
    space.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    space = models.ForeignKey(
        Space, null=True, blank=True, on_delete=models.PROTECT, related_name="assets"
    )
    # "Covers its whole space": kept apart from every asset of its space (the
    # model, 3; W16, W17). It needs a space.
    whole_space = models.BooleanField(default=False)
    # How many people the law allows. Information only: nothing is booked
    # against it, and a floor plan may hold more spots (W3, W14).
    maximum_occupancy = models.PositiveIntegerField(null=True, blank=True)
    # The zone its containers, hours, closures and holidays are read in, and
    # the one members see its times in (an IANA name). The organization's when
    # left empty. Every claim keeps a copy (`takentime_asset_time_zone`), so
    # the database refuses a change once the asset has a booking or a block.
    time_zone = models.CharField(max_length=64, blank=True)
    # What the rule against double booking keys the asset's claims on: its
    # space, or the asset alone when it has none. Every claim keeps a copy,
    # which follows the asset when it moves (`takentime_asset_placement`).
    scope_key = models.GeneratedField(
        expression=Coalesce(F("space_id"), -F("id")),
        output_field=models.BigIntegerField(),
        db_persist=True,
    )

    counter = "last_asset_number"

    class Meta(Numbered.Meta):
        constraints = [
            *organization_rules("space"),
            models.UniqueConstraint(
                fields=["organization", "number"], name="%(class)s_number_once"
            ),
            models.UniqueConstraint(
                fields=["organization", "name"], name="%(class)s_name_once"
            ),
            # What a claim's copy of the asset's space and mark points at.
            models.UniqueConstraint(
                fields=["organization", "id", "scope_key", "whole_space"],
                name="asset_placement_key",
            ),
            models.CheckConstraint(
                condition=Q(whole_space=False) | Q(space__isnull=False),
                name="asset_mark_needs_a_space",
            ),
            # One asset per space carries the mark; different whole-space
            # offers are its containers. With two, the rule against double
            # booking could not keep their shared claims apart natively.
            models.UniqueConstraint(
                fields=["space"],
                condition=Q(whole_space=True),
                name="asset_whole_space_once",
            ),
            models.CheckConstraint(
                condition=Q(maximum_occupancy__isnull=True)
                | Q(maximum_occupancy__gte=1),
                name="asset_occupancy_above_zero",
            ),
            models.CheckConstraint(
                condition=~Q(time_zone=""), name="asset_has_a_time_zone"
            ),
            # What a claim's copy of the asset's time zone points at.
            models.UniqueConstraint(
                fields=["organization", "id", "time_zone"],
                name="asset_time_zone_key",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def zone(self) -> ZoneInfo:
        """The asset's time zone."""
        return ZoneInfo(self.time_zone)

    def clean(self) -> None:
        super().clean()
        self._default_time_zone()
        if not knows_time_zone(self.time_zone):
            raise ValidationError({"time_zone": TIME_ZONE_FAULT})
        self._may_cover_its_space()

    def _default_time_zone(self) -> None:
        """Made without a time zone, the asset takes the organization's."""
        if not self.time_zone and self.organization_id is not None:
            self.time_zone = Organization.objects.values_list(
                "time_zone", flat=True
            ).get(pk=self.organization_id)

    def save(
        self,
        *args: Any,
        now: datetime | None = None,
        floor_plan: AssetInventory | None = None,
        **kwargs: Any,
    ) -> None:
        """Saves the asset. A new one gets `floor_plan`, unsaved, as its
        first floor plan, or, without one, a floor plan with a single spot
        without a name (builder's pick, W9). Moving it to another space, or
        changing whether it covers its whole space, goes through the
        validator (`validator.check_place`); its claims follow it, and the
        move is refused when its past bookings or blocks would then overlap
        others.
        """
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            adding = self._state.adding
            # The field a move is refused on.
            moving = "whole_space"
            self._default_time_zone()
            if adding:
                self._may_cover_its_space()
                self._shares_its_space_zone()
            else:
                # Imported here: the validator reads the calendar, which
                # reads these models.
                from randirandi.core import validator

                stored = (
                    Asset.objects.of(self.organization_id).using(using).get(pk=self.pk)
                )
                if stored.space_id != self.space_id:
                    moving = "space"
                if stored.time_zone != self.time_zone:
                    validator.check_time_zone(
                        stored, time_zone=self.time_zone, now=now or timezone.now()
                    )
                if (
                    stored.space_id != self.space_id
                    or stored.time_zone != self.time_zone
                ):
                    self._shares_its_space_zone()
                if moving == "space" or stored.whole_space != self.whole_space:
                    self._may_cover_its_space()
                    validator.check_place(
                        self,
                        space_id=self.space_id,
                        whole_space=self.whole_space,
                        now=now or timezone.now(),
                    )
            restrict_updates(
                self,
                kwargs,
                writable=(
                    "name",
                    "description",
                    "space",
                    "whole_space",
                    "maximum_occupancy",
                    "time_zone",
                ),
                identity=("organization_id", "number"),
            )
            try:
                with transaction.atomic(using=using):
                    super().save(*args, **kwargs)
            except IntegrityError as error:
                if _rule(error) == "asset_whole_space_once":
                    raise ValidationError({"whole_space": ONE_MARK}) from None
                if _rule(error) == "takentime_asset_time_zone":
                    raise ValidationError(
                        {
                            "time_zone": "It has a booking or a block, also a past "
                            "one, so its time zone stays as it is; make a new "
                            "asset in that zone instead."
                        }
                    ) from None
                if _rule(error) != "capacity_no_double_booking":
                    raise
                instead = (
                    "make a new asset in that space instead"
                    if moving == "space"
                    else "make a new asset with the mark instead"
                )
                raise ValidationError(
                    {
                        moving: "Its bookings or blocks, also past ones, would "
                        f"then overlap others in its space; {instead}."
                    }
                ) from None
            # The generated key, for what is saved with the asset next.
            self.refresh_from_db(using=using, fields=["scope_key"])
            if adding:
                plan = floor_plan or AssetInventory()
                plan.organization_id = self.organization_id
                plan.asset = self
                plan.save(using=using)

    def _shares_its_space_zone(self) -> None:
        """The assets of one space share one time zone: the space's calendar
        answers a date period for all of them ."""
        if self.space_id is None:
            return
        other = (
            Asset.objects.of(self.organization_id)
            .filter(space_id=self.space_id)
            .exclude(pk=self.pk)
            .exclude(time_zone=self.time_zone)
            .first()
        )
        if other is not None:
            raise ValidationError(
                {
                    "time_zone": f"{other} in the same space is in {other.time_zone}, "
                    "and the assets of a space share one time zone; to change it, "
                    "take the assets out of the space, change each, and put them "
                    "back."
                }
            )

    def _may_cover_its_space(self) -> None:
        if not self.whole_space:
            return
        marked: set[int] = set()
        if self.space_id is not None:
            marked = set(
                Asset.objects.of(self.organization_id)
                .filter(space_id=self.space_id, whole_space=True)
                .exclude(pk=self.pk)
                .values_list("space_id", flat=True)
            )
        fault = space_mark_fault(
            whole_space=True, space=self.space_id, marked_spaces=marked
        )
        if fault == NEEDS_SPACE:
            raise ValidationError(
                {
                    "whole_space": "It needs a space: an asset without one cannot "
                    "cover its whole space."
                }
            )
        if fault == SPACE_ALREADY_MARKED:
            raise ValidationError({"whole_space": ONE_MARK})


def _rule(error: IntegrityError) -> str | None:
    """The name of the database rule that refused a write."""
    return getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)


def kept_apart(asset: Asset) -> Q:
    """The assets whose bookings never share a moment with this asset's: the
    asset itself, and every asset of its space when either of the two covers
    the whole space (the model, 5; W17). Their containers may stack ; the database keeps
    the bookings apart.

    "The same space" is worked out here and nowhere else (W21). The space is
    the asset's own and the mark is `whole_space`. Every place that holds the
    rule:
    - this function, for queries over assets;
    - `kept_whole`, for claims held in memory (both calendars);
    - `space_mark_fault` (`randirandi/space_rule.py`, free of Django), for who
      may carry the mark (`Asset` on save and the setup file's check);
    - the database rule `capacity_no_double_booking`, keyed on the asset's
      space (`Asset.scope_key`) and its mark, and `asset_whole_space_once`.
    `tests/test_space_rule.py` runs one table of cases through this function
    and through the database rule and fails when they disagree. A space
    inside a space later changes these functions and that key only.
    """
    apart = same_asset(asset)
    if asset.space_id is not None:
        same_space = Q(space=asset.space_id)
        if not kept_whole(asset.whole_space, False):
            # Only another asset that covers the space is kept apart.
            same_space &= Q(whole_space=True)
        apart |= same_space
    return Q(organization=asset.organization_id) & apart


def kept_whole(marked: bool, other_marked: bool) -> bool:
    """Whether two different assets of one space are kept apart, whole: when
    either covers the whole space (W17). `kept_apart` and the calendar's
    count of held spots (`calendar._held`) both ask it."""
    return marked or other_marked


def on_other_floor_plans(
    one: BookingConfiguration, other: BookingConfiguration
) -> bool:
    """Whether two containers may not cover the same time : both shared,
    on one asset, booking different floor plans. Their spots cannot be
    compared, so the database could sell both into one room. Every other
    pair may stack: the database keeps their bookings apart.
    """
    return (
        one.pk != other.pk
        and one.asset_id == other.asset_id
        and one.consumption_mode == other.consumption_mode == ConsumptionMode.SHARED
        and one.floor_plan_id != other.floor_plan_id
    )


def same_asset(asset: Asset) -> Q:
    """The asset itself, wherever it is."""
    return Q(organization=asset.organization_id) & Q(pk=asset.pk)


class AvailabilityCalendar(Owned):
    """When an asset can be rented at all, from a date on.

    An asset can have several over time. The one with the latest start date
    that has been reached applies.
    """

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    start_date = models.DateField()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("asset"),
            models.UniqueConstraint(
                fields=["asset", "start_date"], name="%(class)s_start_date_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.asset} from {self.start_date}"


class AvailableSpan(Owned):
    """One available stretch of one weekday in an availability calendar.

    A weekday with no span is not available.
    """

    availability_calendar = models.ForeignKey(
        AvailabilityCalendar, on_delete=models.CASCADE, related_name="spans"
    )
    weekday = models.PositiveSmallIntegerField(choices=Weekday)
    # Minutes after midnight. The end may be 24:00.
    start_time = models.PositiveSmallIntegerField()
    end_time = models.PositiveSmallIntegerField()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("availability_calendar"),
            models.UniqueConstraint(
                fields=["availability_calendar", "weekday", "start_time"],
                name="%(class)s_start_once",
            ),
            models.CheckConstraint(
                condition=Q(weekday__lte=Weekday.SUNDAY), name="%(class)s_weekday"
            ),
            models.CheckConstraint(
                condition=Q(start_time__lt=F("end_time")) & Q(end_time__lte=DAY),
                name="%(class)s_ends_after_it_starts",
            ),
        ]

    def __str__(self) -> str:
        day = Weekday(self.weekday).label
        return f"{day} {clock(self.start_time)} to {clock(self.end_time)}"


# The earliest and latest day a closure may name; a closure outside them means
# a typing mistake, and the dates stay far from what Python can count.
CLOSURE_FIRST_DAY = date(2000, 1, 1)
CLOSURE_LAST_DAY = date(2100, 12, 31)


class ClosureReason(models.TextChoices):
    """Why a date is closed, from a fixed short list (no free text, which would
    carry names). A closure may also have none."""

    HOLIDAY = "holiday", "Holiday"
    REPAIR = "repair", "Repair"
    # The organization's own holiday closure, a different thing from a public
    # holiday in Norwegian .
    VACATION = "vacation", "Vacation"


class AvailabilityClosure(Owned):
    """A date, or a run of dates, closed beside the weekly availability.

    For a holiday or a repair . An event is not a closure .
    The dates are whole days on the asset's clock, first and last included.
    Nothing checks a closure when a container is saved: at run time a fixed
    date period that no longer fits whole is not offered, and a flexible one is
    cut to what is left (BOOKING-MODEL, rules 2 and 3). Closures of one asset
    may overlap; a day is closed when any covers it.
    """

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    first_date = models.DateField()
    last_date = models.DateField()
    reason = models.CharField(max_length=10, blank=True, choices=ClosureReason)

    class Meta(Owned.Meta):
        ordering = ["first_date", "last_date", "id"]
        constraints = [
            *organization_rules("asset"),
            models.CheckConstraint(
                condition=Q(first_date__lte=F("last_date")),
                name="%(class)s_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(first_date__gte=CLOSURE_FIRST_DAY)
                & Q(last_date__lte=CLOSURE_LAST_DAY),
                name="%(class)s_in_range",
            ),
            models.CheckConstraint(
                condition=Q(reason__in=ClosureReason.values) | Q(reason=""),
                name="%(class)s_reason",
            ),
        ]

    def __str__(self) -> str:
        if self.first_date == self.last_date:
            return f"{self.asset} closed {self.first_date}"
        return f"{self.asset} closed {self.first_date} to {self.last_date}"


class BookingType(Owned):
    """What is booked, in the organization's own words."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # Its place in the list members choose from.
    position = models.PositiveSmallIntegerField(default=0)

    class Meta(Owned.Meta):
        ordering = ["position", "id"]
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "name"], name="%(class)s_name_once"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class BookingCategory(Owned):
    """What a booking is for, in the organization's own words."""

    name = models.CharField(max_length=200)
    # Its place in lists.
    position = models.PositiveSmallIntegerField(default=0)
    # The icon beside its name on the member's page: blank, or one of
    # `CATEGORY_ICONS`.
    icon = models.CharField(
        max_length=8,
        blank=True,
        default="",
        choices=[(key, key) for key in CATEGORY_ICONS],
    )

    class Meta(Owned.Meta):
        ordering = ["position", "id"]
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "name"], name="%(class)s_name_once"
            ),
            models.CheckConstraint(
                condition=Q(icon="") | Q(icon__in=CATEGORY_ICONS),
                name="%(class)s_icon_is_known",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PeriodType(models.TextChoices):
    FIXED = "fixed"
    FLEXIBLE = "flexible"


class Status(models.TextChoices):
    ENABLED = "enabled"
    DISABLED = "disabled"


class BookingConfiguration(Numbered):
    """When one asset can be booked under one booking type, and for which
    booking categories, including its shared or exclusive consumption mode.
    """

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    booking_type = models.ForeignKey(BookingType, on_delete=models.PROTECT)
    consumption_mode = models.CharField(
        max_length=9, choices=ConsumptionMode, default=ConsumptionMode.EXCLUSIVE
    )
    # The floor plan whose spots a shared container books, one of its
    # asset's. An exclusive container has none: it uses the whole asset (W3).
    floor_plan = models.ForeignKey(
        AssetInventory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="booking_configurations",
    )
    period_type = models.CharField(max_length=8, choices=PeriodType)
    # Minutes after midnight.
    period_start = models.PositiveSmallIntegerField()
    period_end = models.PositiveSmallIntegerField()
    # Days after the start day: 0 is the same day, 1 the next day.
    end_day = models.PositiveSmallIntegerField(default=0)
    # The days the period starts on.
    period_start_weekdays = ArrayField(
        models.PositiveSmallIntegerField(choices=Weekday)
    )
    # Minutes. Entered for a flexible period, where the minimum may be left
    # out and is then the interval. For a fixed period the system sets all
    # three to the period length.
    interval = models.PositiveIntegerField(blank=True)
    minimum_length = models.PositiveIntegerField(blank=True)
    maximum_length = models.PositiveIntegerField(blank=True)
    # The calendar weeks a period may start in: the Monday of the first and
    # the Sunday of the last. Either may be left out: from always, or without
    # end.
    start_date = models.DateField(null=True, blank=True)
    stop_date = models.DateField(null=True, blank=True)
    # A period starts in every Nth week from the first, which it then needs.
    every_weeks = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=8, choices=Status, default=Status.DISABLED)
    # The prices of a container with payment. A container without payment
    # has none: no price applies, and its bookings are confirmed without
    # Vipps (W12).
    price_book = models.ForeignKey(
        "core.PriceBook", null=True, blank=True, on_delete=models.PROTECT
    )
    booking_categories = models.ManyToManyField(
        BookingCategory,
        through="core.ConfigurationCategory",
        related_name="booking_configurations",
    )

    counter = "last_booking_configuration_number"

    class Meta(Numbered.Meta):
        ordering = ["number"]
        constraints = [
            *organization_rules("asset", "booking_type", "price_book", "floor_plan"),
            models.CheckConstraint(
                condition=Q(consumption_mode__in=ConsumptionMode.values),
                name="configuration_consumption_mode",
            ),
            models.CheckConstraint(
                condition=Q(
                    consumption_mode=ConsumptionMode.SHARED, floor_plan__isnull=False
                )
                | Q(
                    consumption_mode=ConsumptionMode.EXCLUSIVE, floor_plan__isnull=True
                ),
                name="configuration_floor_plan_when_shared",
            ),
            # The floor plan is one of its asset's.
            DatabaseRule(
                name="configuration_floor_plan_of_its_asset",
                definition=(
                    "FOREIGN KEY (organization_id, asset_id, floor_plan_id) "
                    "REFERENCES core_assetinventory (organization_id, asset_id, id)"
                ),
            ),
            models.UniqueConstraint(
                fields=["organization", "number"], name="%(class)s_number_once"
            ),
            models.CheckConstraint(
                condition=Q(period_start__lt=DAY) & Q(period_end__lt=DAY),
                name="%(class)s_times_are_on_the_clock",
            ),
            models.CheckConstraint(
                condition=Q(period_start__lt=F("end_day") * DAY + F("period_end")),
                name="%(class)s_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(period_start_weekdays__len__gte=1)
                & Q(period_start_weekdays__contained_by=list(Weekday.values)),
                name="%(class)s_starts_on_a_weekday",
            ),
            models.CheckConstraint(
                condition=Q(interval__gte=1)
                & Q(minimum_length__gte=1)
                & Q(minimum_length__lte=F("maximum_length"))
                & Q(
                    maximum_length__lte=F("end_day") * DAY
                    + F("period_end")
                    - F("period_start")
                ),
                name="%(class)s_lengths_fit_the_period",
            ),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True)
                | Q(stop_date__isnull=True)
                | Q(start_date__lte=F("stop_date")),
                name="%(class)s_stops_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(start_date__iso_week_day=1),
                name="%(class)s_starts_on_a_monday",
            ),
            models.CheckConstraint(
                condition=Q(stop_date__isnull=True) | Q(stop_date__iso_week_day=7),
                name="%(class)s_stops_on_a_sunday",
            ),
            models.CheckConstraint(
                condition=Q(every_weeks__gte=1, every_weeks__lte=MOST_WEEKS)
                & (Q(every_weeks=1) | Q(start_date__isnull=False)),
                name="%(class)s_counts_weeks_from_the_first",
            ),
        ]

    def __str__(self) -> str:
        # It has no number until it is saved.
        return "new" if self.number is None else f"{self.number:03d}"

    def save(self, *args: Any, now: datetime | None = None, **kwargs: Any) -> None:
        """Saves the container through the validator (docs/booking-model.md, "The
        validator"), which refuses it with a `ValidationError` naming the
        reason. Only date periods ending after `now`, by default the moment
        of the save, are checked.
        """
        # Imported here: the validator reads the calendar, which reads these
        # models.
        from randirandi.core import validator

        self._set_what_the_system_sets()
        with transaction.atomic():
            validator.check(self, now=now or timezone.now())
            super().save(*args, **kwargs)

    @property
    def with_payment(self) -> bool:
        """Whether its bookings are paid: it has a price book (W12)."""
        return self.price_book_id is not None

    @property
    def period_length(self) -> int:
        """Minutes from the period's start to its end."""
        return self.end_day * DAY + self.period_end - self.period_start

    def stretches(self) -> list[Stretch]:
        """The time the configuration covers in a week: its period, once for
        each weekday it starts on.
        """
        return [
            (day * DAY + self.period_start, day * DAY + self.period_start + length)
            for day in sorted(set(self.period_start_weekdays))
            for length in [self.period_length]
        ]

    def starts_on(self, day: date) -> bool:
        """Whether one of its date periods starts on the day: a weekday it
        starts on, inside its calendar weeks, and in one of every N weeks
        from the first. A period that runs into the next week belongs to
        the week it starts in.
        """
        return (
            day.weekday() in self.period_start_weekdays
            and (self.start_date is None or self.start_date <= day)
            and (self.stop_date is None or day <= self.stop_date)
            and (
                self.start_date is None
                or (week_of(day) - week_of(self.start_date)) % self.every_weeks == 0
            )
        )

    def starts_from(self, day: date) -> bool:
        """Whether one of its date periods starts on the day or later."""
        if self.stop_date is None:
            return True
        # Its last start, if it has one, is within one cycle before the stop.
        last = self.stop_date.toordinal()
        for ordinal in range(last, last - 7 * self.every_weeks, -1):
            if ordinal < day.toordinal():
                return False
            if self.starts_on(date.fromordinal(ordinal)):
                return True
        return False

    def covers_the_same_time_as(
        self, other: BookingConfiguration, after: datetime | None = None
    ) -> bool:
        """Whether any full date periods overlap (rule 4). With `after`, a
        wall moment in the organization's time zone, only date periods that
        end after it count.

        The calendar weeks limit when periods start, not when their occupied
        time ends. Two starts can only meet when their weeks are a few apart,
        however long the periods: for each such distance and each pair of
        weekdays, the clock decides whether the starts fall inside each
        other's time, and the weeks whether both are in force that far apart.
        """
        for apart in range(
            -(other.period_length // WEEK) - 1, self.period_length // WEEK + 2
        ):
            for day in self.period_start_weekdays:
                for other_day in other.period_start_weekdays:
                    # Minutes from this start to the other's, weeks apart.
                    gap = (
                        apart * WEEK
                        + (other_day * DAY + other.period_start)
                        - (day * DAY + self.period_start)
                    )
                    # Strict overlap: moments here are integer minutes, and
                    # equality only touches.
                    if -other.period_length < gap < self.period_length and (
                        self._in_force_with(other, apart, day, other_day, after)
                    ):
                        return True
        return False

    def _weeks(
        self, weekday: int, after: datetime | None = None
    ) -> tuple[int, int, int]:
        """The week its every N weeks count from, and the first and the last
        week a period of it may start on the weekday; with `after`, the
        first whose period ends after that moment.
        """
        counted = week_of(self.start_date or date.min)
        starts = self.start_date or date.min
        stops = self.stop_date or date.max
        if after is not None:
            # A period that starts on a day ends this long after its midnight.
            ends = timedelta(minutes=self.end_day * DAY + self.period_end)
            starts = max(starts, (after - ends).date() + timedelta(days=1))
        return (
            counted,
            week_of(starts) + (1 if starts.weekday() > weekday else 0),
            week_of(stops) - (1 if stops.weekday() < weekday else 0),
        )

    def _in_force_with(
        self,
        other: BookingConfiguration,
        apart: int,
        day: int,
        other_day: int,
        after: datetime | None = None,
    ) -> bool:
        """Whether, in some week, this one starts on the weekday and the
        other starts on its weekday the given number of weeks later.

        Both repeat their weeks, so whether they ever meet is settled within
        one cycle of both, the least common multiple of their N weeks.
        """
        counted, first, last = self._weeks(day, after)
        other_counted, other_first, other_last = other._weeks(other_day, after)
        earliest = max(first, other_first - apart)
        latest = min(last, other_last - apart)
        if earliest > latest:
            return False
        # This one's first week in force from the earliest week both reach.
        start = earliest + (counted - earliest) % self.every_weeks
        cycle = lcm(self.every_weeks, other.every_weeks)
        return any(
            (week + apart - other_counted) % other.every_weeks == 0
            for week in range(
                start, min(latest, start + cycle - 1) + 1, self.every_weeks
            )
        )

    def _periods_overlap_each_other(self) -> bool:
        starts = [start for start, _ in self.stretches()]
        following = [*starts[1:], starts[0] + WEEK]
        return any(
            start + self.period_length > after
            for start, after in zip(starts, following, strict=True)
        )

    def _set_what_the_system_sets(self) -> None:
        self.period_start_weekdays = sorted(set(self.period_start_weekdays))
        if self.period_type == PeriodType.FIXED:
            # A fixed period is booked as one, exactly the period's length.
            self.interval = self.minimum_length = self.maximum_length = (
                self.period_length
            )
        elif self.minimum_length is None:
            # A flexible period may leave its minimum out: it is then one
            # interval, and makes no gap of its own.
            self.minimum_length = self.interval

    def clean(self) -> None:
        """docs/booking-model.md, "Checks when saving": 1 to 4 on the configuration
        by itself. That it names a booking category (5) is checked where they
        are entered. Check 6, against the organization's other containers and
        the asset's availability, is the validator's, which every save runs.
        """
        super().clean()
        if (
            None in (self.period_start, self.period_end, self.end_day)
            or not self.period_start_weekdays
        ):
            # A field is missing, and is reported as that.
            return
        faults = {}
        length = self.period_length
        if length <= 0:
            raise ValidationError({"period_end": "The period ends after it starts."})
        self._set_what_the_system_sets()
        if self.period_type == PeriodType.FLEXIBLE:
            entered = {
                "interval": self.interval,
                "minimum_length": self.minimum_length,
                "maximum_length": self.maximum_length,
            }
            for field, value in entered.items():
                if value is None:
                    # A minimum left out follows the interval; only a missing
                    # interval leaves it unset, and that is reported there.
                    if field != "minimum_length":
                        faults[field] = "A flexible period needs it."
                elif value < 1:
                    faults[field] = "It is above zero."
            if not faults:
                if length > DAY:
                    faults["period_end"] = "A flexible period is at most 24 hours."
                if length % self.interval:
                    faults["interval"] = "The period is a whole number of intervals."
                if self.minimum_length % self.interval:
                    faults["minimum_length"] = "It is a whole number of intervals."
                elif self.minimum_length > self.maximum_length:
                    faults["minimum_length"] = "It is at most the maximum length."
                if self.maximum_length % self.interval:
                    faults["maximum_length"] = "It is a whole number of intervals."
                elif self.maximum_length > length:
                    faults["maximum_length"] = "It is at most the period's length."
        if self.start_date and self.start_date.weekday() != Weekday.MONDAY:
            faults["start_date"] = "It is the Monday of the first calendar week."
        elif self.start_date is None and self.every_weeks != 1:
            faults["start_date"] = (
                "Every N weeks counts from a first calendar week, so it needs one."
            )
        if self.stop_date and self.stop_date.weekday() != Weekday.SUNDAY:
            faults["stop_date"] = "It is the Sunday of the last calendar week."
        elif self.start_date and self.stop_date and self.stop_date < self.start_date:
            faults["stop_date"] = "The last calendar week is not before the first."
        if self.every_weeks is None or not 1 <= self.every_weeks <= MOST_WEEKS:
            faults["every_weeks"] = f"It is 1 to {MOST_WEEKS}."
        if not faults and self._periods_overlap_each_other():
            faults["period_start_weekdays"] = (
                "The period is longer than the time to the next weekday it starts on."
            )
        if faults:
            raise ValidationError(faults)


class ConfigurationCategory(Owned):
    """A booking category that a booking configuration applies to."""

    booking_configuration = models.ForeignKey(
        BookingConfiguration, on_delete=models.CASCADE
    )
    booking_category = models.ForeignKey(BookingCategory, on_delete=models.PROTECT)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("booking_configuration", "booking_category"),
            models.UniqueConstraint(
                fields=["booking_configuration", "booking_category"],
                name="%(class)s_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.booking_configuration} · {self.booking_category}"


class CategoryHoursSpan(Owned):
    """One stretch of one weekday when a container offers one of its booking
    categories . A category with no span has the container's hours; once
    it has one, a weekday without a span is closed to it.
    """

    configuration_category = models.ForeignKey(
        ConfigurationCategory, on_delete=models.CASCADE, related_name="hours"
    )
    weekday = models.PositiveSmallIntegerField(choices=Weekday)
    # Minutes after midnight. The end may be 24:00.
    start_time = models.PositiveSmallIntegerField()
    end_time = models.PositiveSmallIntegerField()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("configuration_category"),
            models.UniqueConstraint(
                fields=["configuration_category", "weekday", "start_time"],
                name="%(class)s_start_once",
            ),
            models.CheckConstraint(
                condition=Q(weekday__lte=Weekday.SUNDAY), name="%(class)s_weekday"
            ),
            models.CheckConstraint(
                condition=Q(start_time__lt=F("end_time")) & Q(end_time__lte=DAY),
                name="%(class)s_ends_after_it_starts",
            ),
        ]

    def __str__(self) -> str:
        day = Weekday(self.weekday).label
        return f"{day} {clock(self.start_time)} to {clock(self.end_time)}"
