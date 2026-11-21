# SPDX-License-Identifier: AGPL-3.0-only
"""The organization, and the numbers it gives its objects."""

from typing import Any, ClassVar
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q

from randirandi import labels as label_set
from randirandi.core.models.base import Owned, SameOrganization
from randirandi.limits import (
    DEFAULT_FREE_CANCELLATION_DAYS,
    MOST_FREE_CANCELLATION_DAYS,
)

# The first part of an address that no organization may take.
RESERVED_SUBDOMAINS = frozenset(
    {"admin", "api", "app", "mail", "sandbox", "static", "status", "www"}
)


TIME_ZONE_FAULT = "Not a time zone the service knows."


def knows_time_zone(name: str) -> bool:
    """Whether the name is an IANA time zone, such as `Europe/Oslo`."""
    return name in available_timezones()


class Currency(models.TextChoices):
    """The currencies the service knows: the ones the payment client takes
    (`vipps.CURRENCIES`, which a test holds this to)."""

    NOK = "NOK", "NOK"
    DKK = "DKK", "DKK"
    EUR = "EUR", "EUR"


class Organization(models.Model):
    """A customer of the service."""

    name = models.CharField(max_length=200)
    # The first part of the address its members use. The deployment adds the
    # rest, so the same setup gives the sandbox and production their own.
    subdomain = models.CharField(max_length=63, unique=True)
    # The defaults for a new asset's time zone and a new price book's
    # currency (7.3); each of those keeps its own after that.
    time_zone = models.CharField(max_length=64)
    currency = models.CharField(max_length=3, choices=Currency)
    contact_email = models.EmailField()
    # The role a member import gives a new member. It is empty only until the
    # organization's roles are made.
    standard_role = models.ForeignKey(
        "core.Role",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    member_retention_months = models.PositiveSmallIntegerField()
    booking_retention_months = models.PositiveSmallIntegerField()
    # The one language of its pages and mails : a catalogue the code
    # ships, `randirandi/locale/<language>.json`.
    language = models.CharField(max_length=16, default=label_set.DEFAULT_LANGUAGE)
    # The organization's own texts for label keys (`randirandi/locale/en.json`),
    # in its language, only the keys it set; the member pages show the
    # language's catalogue text for the rest.
    labels = models.JSONField(default=dict, blank=True)
    # The tax in the prices, in hundredths of a percent: 2500 is 25 %. It goes
    # on the receipt in the Vipps app.
    tax_rate = models.PositiveSmallIntegerField(default=0)
    # The limits on one member's bookings (docs/booking-model.md, "Limits on a
    # member's booking"); empty means no limit. The most bookings a member may
    # make in one calendar day on this organization's clock, whatever day they
    # are for, and how far ahead a booking may start, in whole calendar months.
    max_bookings_a_day = models.PositiveSmallIntegerField(null=True, blank=True)
    book_ahead_months = models.PositiveSmallIntegerField(null=True, blank=True)
    # The days before a booked time's start until which a member who asks by
    # email to cancel it gets back what was paid for it : the booking conditions'
    # `{days}`.
    free_cancellation_days = models.PositiveSmallIntegerField(
        default=DEFAULT_FREE_CANCELLATION_DAYS
    )

    # The last number given of each kind. See `take_number`.
    last_asset_number = models.PositiveIntegerField(default=0, editable=False)
    last_space_number = models.PositiveIntegerField(default=0, editable=False)
    last_booking_configuration_number = models.PositiveIntegerField(
        default=0, editable=False
    )
    last_price_book_number = models.PositiveIntegerField(default=0, editable=False)
    last_order_number = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        constraints = [
            SameOrganization(
                link="standard_role",
                organization="id",
                name="%(class)s_standard_role_same_org",
            ),
            models.CheckConstraint(
                condition=Q(subdomain__regex=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
                name="%(class)s_subdomain_fits_an_address",
            ),
            models.CheckConstraint(
                condition=Q(member_retention_months__gte=1)
                & Q(booking_retention_months__gte=1),
                name="%(class)s_keeps_data_a_month_or_more",
            ),
            models.CheckConstraint(
                condition=Q(tax_rate__lte=10000), name="%(class)s_tax_rate_at_most_all"
            ),
            models.CheckConstraint(
                condition=Q(max_bookings_a_day__isnull=True)
                | Q(max_bookings_a_day__gte=1),
                name="%(class)s_bookings_a_day_one_or_more",
            ),
            models.CheckConstraint(
                condition=Q(book_ahead_months__isnull=True)
                | Q(book_ahead_months__gte=1),
                name="%(class)s_book_ahead_one_month_or_more",
            ),
            models.CheckConstraint(
                condition=Q(free_cancellation_days__gte=0)
                & Q(free_cancellation_days__lte=MOST_FREE_CANCELLATION_DAYS),
                name="%(class)s_free_cancellation_days_in_range",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        faults = {}
        if self.subdomain in RESERVED_SUBDOMAINS:
            faults["subdomain"] = "No organization may take this subdomain."
        if not knows_time_zone(self.time_zone):
            faults["time_zone"] = TIME_ZONE_FAULT
        if fault := label_set.language_fault(self.language):
            faults["language"] = fault
        if not isinstance(self.labels, dict):
            faults["labels"] = "Must be label keys with the organization's texts."
        else:
            for key, text in self.labels.items():
                if fault := label_set.fault(self.language, key, text):
                    faults["labels"] = f"{key}: {fault}"
                    break
        if faults:
            raise ValidationError(faults)


def take_number(organization_id: int, counter: str, wanted: int | None = None) -> int:
    """Give an object its number.

    A number counts per organization and is never given twice, also not when
    the object that had it is gone. Without `wanted` the next number is
    given. A setup file writes its numbers out; such a number is taken only
    if it is above every number given so far.

    The organization's row is locked until the caller's transaction ends, so
    two callers never get the same number.
    """
    rows = Organization.objects.filter(pk=organization_id)
    with transaction.atomic():
        if wanted is None:
            rows.update(**{counter: F(counter) + 1})
            number: int = rows.values_list(counter, flat=True).get()
            return number
        if rows.filter(**{f"{counter}__lt": wanted}).update(**{counter: wanted}) != 1:
            raise ValidationError(
                {"number": f"The number {wanted} has been given before."}
            )
        return wanted


class Numbered(Owned):
    """An object with a number that people see, such as `001`."""

    number = models.PositiveIntegerField(editable=False)

    # The organization's field that holds the last number given of this kind.
    counter: ClassVar[str]

    class Meta(Owned.Meta):
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding:
            self.number = take_number(self.organization_id, self.counter, self.number)
        super().save(*args, **kwargs)
