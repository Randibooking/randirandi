# SPDX-License-Identifier: AGPL-3.0-only
"""What it costs: price books, their entries and the price tiers."""

from collections.abc import Collection
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q

from randirandi.core.models.base import (
    DAY,
    DatabaseRule,
    Owned,
    clock,
    length,
    organization_rules,
)
from randirandi.core.models.bookable import (
    BookingCategory,
    BookingConfiguration,
    ConfigurationCategory,
)
from randirandi.core.models.capacity import ConsumptionMode, SpotGroup
from randirandi.core.models.organization import Currency, Numbered, Organization


class PriceBook(Numbered):
    """A set of prices. An organization starts with one and can have several."""

    name = models.CharField(max_length=200)
    # What its amounts are in (ISO 4217): one the payment provider takes. The
    # organization's when left empty. Every entry keeps a copy
    # (`pricebookentry_book_currency`), so the database refuses a change once
    # the book has an entry; another currency is another book.
    currency = models.CharField(max_length=3, choices=Currency, blank=True)

    counter = "last_price_book_number"

    class Meta(Numbered.Meta):
        ordering = ["number"]
        constraints = [
            *organization_rules(),
            models.CheckConstraint(
                condition=Q(currency="NOK") | Q(currency="DKK") | Q(currency="EUR"),
                name="%(class)s_currency_is_known",
            ),
            # What an entry's copy of the book's currency points at.
            models.UniqueConstraint(
                fields=["organization", "id", "currency"],
                name="pricebook_currency_key",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"], name="%(class)s_number_once"
            ),
            models.UniqueConstraint(
                fields=["organization", "name"], name="%(class)s_name_once"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        self._default_currency()

    def _default_currency(self) -> None:
        """Made without a currency, the book takes the organization's."""
        if not self.currency and self.organization_id is not None:
            self.currency = Organization.objects.values_list("currency", flat=True).get(
                pk=self.organization_id
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        using = kwargs.get("using") or self._state.db or "default"
        self._default_currency()
        if not self._state.adding:
            stored = (
                PriceBook.objects.of(self.organization_id)
                .using(using)
                .filter(pk=self.pk)
                .values_list("currency", flat=True)
                .first()
            )
            if (
                stored is not None
                and stored != self.currency
                and PriceBookEntry.objects.of(self.organization_id)
                .using(using)
                .filter(price_book=self.pk)
                .exists()
            ):
                raise ValidationError({"currency": ENTRY_FIXES_CURRENCY})
        try:
            with transaction.atomic(using=using):
                super().save(*args, **kwargs)
        except IntegrityError as error:
            cause = getattr(
                getattr(error.__cause__, "diag", None), "constraint_name", ""
            )
            if cause != "pricebookentry_book_currency":
                raise
            raise ValidationError({"currency": ENTRY_FIXES_CURRENCY}) from None


ENTRY_FIXES_CURRENCY = (
    "It has a price, so its currency stays as it is; make another price book "
    "for the other currency."
)


class TiersGoBy(models.TextChoices):
    LENGTH = "length", "Length"
    TIME_OF_DAY = "time of day", "Time of day"


class BillingBasis(models.TextChoices):
    PER_BOOKING = "per_booking", "Per booking"
    PER_CAPACITY_UNIT = "per_capacity_unit", "Per capacity unit"


def billed_as_its_mode(config: BookingConfiguration, billing_basis: str) -> bool:
    """A price per spot is for a shared configuration only: an exclusive one
    is one column, the whole asset, priced per booking ."""
    return (
        billing_basis != BillingBasis.PER_CAPACITY_UNIT
        or config.consumption_mode == ConsumptionMode.SHARED
    )


def entry_fits_its_mode(config: BookingConfiguration, entry: PriceBookEntry) -> bool:
    """Whether an entry can price at all: its tiers go by something known and
    its billing basis is one the configuration's mode has."""
    return entry.tiers_go_by in TiersGoBy.values and billed_as_its_mode(
        config, entry.billing_basis
    )


def what_the_tiers_leave_out(
    configuration: BookingConfiguration, tiers_go_by: str, up_to: Collection[int]
) -> str | None:
    """Says what is wrong when some booking under the configuration would have
    no price: the price tiers must reach its maximum length, or the end of
    its period. `up_to` holds each tier's "up to".
    """
    if not up_to:
        return "It has no price tier."
    reach = max(up_to)
    if tiers_go_by == TiersGoBy.TIME_OF_DAY and reach > DAY:
        return "A time-of-day tier cannot end after 24:00."
    if tiers_go_by == TiersGoBy.LENGTH:
        if reach < configuration.maximum_length:
            return (
                f"The price tiers reach {length(reach)}, and the configuration "
                f"can be booked for {length(configuration.maximum_length)}."
            )
    elif configuration.end_day:
        # The model does not say how tiers by time of day are ordered across
        # midnight. Until it does, such a price is refused and not guessed.
        return (
            "Price tiers by time of day are not worked out for a period that "
            "ends on a later day."
        )
    elif reach < configuration.period_end:
        return (
            f"The price tiers reach {clock(reach)}, and the period ends at "
            f"{clock(configuration.period_end)}."
        )
    return None


class PriceBookEntry(Owned):
    """What one booking configuration costs in one booking category.

    Seen as a grid, the configuration is the row and the booking category
    the column. The price itself is in the entry's price tiers.

    An entry that names a group of spots is the price of a spot in that
    group : a spot in the group takes it, a spot in
    no group or in a group without one takes the entry that names none.
    """

    price_book = models.ForeignKey(
        PriceBook, on_delete=models.PROTECT, related_name="entries"
    )
    booking_configuration = models.ForeignKey(
        BookingConfiguration, on_delete=models.PROTECT
    )
    booking_category = models.ForeignKey(BookingCategory, on_delete=models.PROTECT)
    # Empty for the container's normal price. A group is of the floor plan
    # of the configuration, and its price is per spot.
    spot_group = models.ForeignKey(
        SpotGroup, null=True, blank=True, on_delete=models.PROTECT
    )
    # What the entry's price tiers are measured by.
    tiers_go_by = models.CharField(max_length=11, choices=TiersGoBy)
    billing_basis = models.CharField(
        max_length=17, choices=BillingBasis, default=BillingBasis.PER_BOOKING
    )
    # Its copy of the price book's currency, which holds the book's currency
    # still (`pricebookentry_book_currency`).
    currency = models.CharField(max_length=3, editable=False)

    class Meta(Owned.Meta):
        constraints = [
            # The book's currency as it is: an entry written with another copy
            # is refused, and the book's currency is not changed while it has
            # an entry (no cascade: the amounts were written in it).
            DatabaseRule(
                name="pricebookentry_book_currency",
                definition=(
                    "FOREIGN KEY (organization_id, price_book_id, currency) "
                    "REFERENCES core_pricebook (organization_id, id, currency)"
                ),
            ),
            models.CheckConstraint(
                condition=Q(billing_basis__in=BillingBasis.values),
                name="priceentry_billing_basis",
            ),
            *organization_rules(
                "price_book", "booking_configuration", "booking_category", "spot_group"
            ),
            # One price for each group, and one that names none.
            models.UniqueConstraint(
                fields=[
                    "price_book",
                    "booking_configuration",
                    "booking_category",
                    "spot_group",
                ],
                nulls_distinct=False,
                name="%(class)s_once",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding and self.price_book_id is not None:
            # A book of another organization, or none, leaves the copy empty,
            # and the database refuses the entry.
            self.currency = (
                PriceBook.objects.of(self.organization_id)
                .filter(pk=self.price_book_id)
                .values_list("currency", flat=True)
                .first()
                or ""
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        found = f"{self.booking_configuration} · {self.booking_category}"
        return found if self.spot_group_id is None else f"{found} · {self.spot_group}"

    def clean(self) -> None:
        """Prices only where they apply: the configuration has payment (W12),
        the booking category is one of the configuration's (docs/data-model.md,
        check 3), and a price per spot is a shared configuration's.
        """
        super().clean()
        if None in (
            self.organization_id,
            self.booking_configuration_id,
            self.booking_category_id,
        ):
            return
        if not self.booking_configuration.with_payment:
            raise ValidationError(
                {
                    "booking_configuration": "The configuration is without "
                    "payment: it has no price book, and no price applies to it."
                }
            )
        if not billed_as_its_mode(self.booking_configuration, self.billing_basis):
            raise ValidationError(
                {
                    "billing_basis": "An exclusive configuration books the whole "
                    "asset, never a spot, so it is priced per booking."
                }
            )
        if self.spot_group_id is not None:
            if self.billing_basis != BillingBasis.PER_CAPACITY_UNIT:
                raise ValidationError(
                    {
                        "billing_basis": "A price for a group is for a spot: it is "
                        "priced per capacity unit."
                    }
                )
            group = (
                SpotGroup.objects.of(self.organization_id)
                .filter(pk=self.spot_group_id)
                .first()
            )
            if (
                group is None
                or group.floor_plan_id != self.booking_configuration.floor_plan_id
            ):
                raise ValidationError(
                    {
                        "spot_group": "The group is not on the floor plan the "
                        "configuration books."
                    }
                )
        applies = ConfigurationCategory.objects.of(self.organization_id).filter(
            booking_configuration=self.booking_configuration_id,
            booking_category=self.booking_category_id,
        )
        if not applies.exists():
            raise ValidationError(
                {
                    "booking_category": (
                        "The configuration does not apply to this booking category."
                    )
                }
            )

    def what_its_tiers_leave_out(self) -> str | None:
        """The other half of check 3, over the tiers the entry has now."""
        return what_the_tiers_leave_out(
            self.booking_configuration,
            self.tiers_go_by,
            [tier.up_to for tier in self.tiers.all()],
        )


class PriceTier(Owned):
    """One step of a price.

    A booking is cut at each tier's "up to". Each part is paid for each
    started "paid per" length, and the parts add up.
    """

    price_book_entry = models.ForeignKey(
        PriceBookEntry, on_delete=models.CASCADE, related_name="tiers"
    )
    # Minutes. A length counted from the start of the booking where the
    # entry's tiers go by length, a time of day where they go by time of day.
    up_to = models.PositiveIntegerField()
    # Minutes: the length one amount pays for.
    paid_per = models.PositiveIntegerField()
    # In the smallest unit of the organization's currency. May be zero.
    amount = models.PositiveIntegerField()

    class Meta(Owned.Meta):
        ordering = ["up_to"]
        constraints = [
            *organization_rules("price_book_entry"),
            models.UniqueConstraint(
                fields=["price_book_entry", "up_to"], name="%(class)s_up_to_once"
            ),
            models.CheckConstraint(
                condition=Q(up_to__gte=1) & Q(paid_per__gte=1),
                name="%(class)s_lengths_above_zero",
            ),
        ]

    def __str__(self) -> str:
        return f"up to {self.up_to}: {self.amount} per {self.paid_per}"

    def clean(self) -> None:
        super().clean()
        # Django still calls clean() after a foreign-key field fails validation.
        # Preserve that field error instead of dereferencing an absent entry.
        if not isinstance(self.price_book_entry_id, int) or not isinstance(
            self.up_to, int
        ):
            return
        try:
            entry = self.price_book_entry
        except PriceBookEntry.DoesNotExist:
            return
        if entry.tiers_go_by == TiersGoBy.TIME_OF_DAY and self.up_to > DAY:
            raise ValidationError(
                {"up_to": "A time-of-day tier cannot end after 24:00."}
            )
