# SPDX-License-Identifier: AGPL-3.0-only
"""Bookings and money: taken time, orders, bookings, what is bought,
payments and blocks.
"""

import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Self

from django.contrib.postgres.fields import (
    ArrayField,
    BigIntegerRangeField,
    DateTimeRangeField,
)
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Case, F, Func, Q, Value, When
from django.utils import timezone

from randirandi.core.models.base import (
    DatabaseRule,
    MemberManager,
    OrganizationManager,
    Owned,
    organization_rules,
    restrict_updates,
)
from randirandi.core.models.bookable import (
    Asset,
    BookingCategory,
    BookingConfiguration,
    BookingType,
)
from randirandi.core.models.capacity import (
    CapacityAllocation,
    ConsumptionMode,
    Spot,
    SpotGroup,
)
from randirandi.core.models.members import Member
from randirandi.core.models.organization import Currency, Numbered, Organization
from randirandi.core.models.prices import BillingBasis, PriceBook


class Span(Func):
    """The time from one moment up to another, the end not included. A
    booking that ends at 14:00 and one that starts at 14:00 do not overlap.
    """

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class OneAsset(Func):
    """One asset as a span that only the same asset overlaps: from its id up
    to its id, both included. With it the rule against double booking is
    built from what Postgres itself has. Comparing the ids for equality in
    such a rule needs an extension, and what stands on an extension goes
    when the extension is removed.
    """

    function = "INT8RANGE"
    template = "%(function)s(%(expressions)s, '[]')"
    output_field = BigIntegerRangeField()

    def __init__(self, asset: str) -> None:
        super().__init__(asset, asset)


# What a person reads and says for an order "; a short random code "says nothing
# about how many bookings there are and nobody can guess it"). Six of 31
# characters, none that looks like another: no 0/O, no 1/I/L.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
REFERENCE_LENGTH = 6
REFERENCE_PATTERN = r"^[23-9A-HJKMNP-Z]{6}$"


def draw_reference() -> str:
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


class BookingConditions(Owned):
    """The filled-in text of an organization's booking conditions, as a member
    accepted it . One row for each
    distinct text of an organization, made when the first order accepts it and
    never changed: the application login has no UPDATE on it. It holds no
    personal data.
    """

    # The version of the product's standard text the words come from.
    standard_version = models.PositiveSmallIntegerField()
    # SHA-256 of the whole text with its values: what the cart shows and the
    # order call carries as `accepted_version`.
    digest = models.CharField(max_length=64)
    # The values that were filled in.
    organization_name = models.CharField(max_length=200)
    contact_email = models.EmailField()
    free_cancellation_days = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=200)
    intro = models.TextField()
    # A list of [heading, text].
    sections = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[OrganizationManager[Self]] = OrganizationManager()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "digest"], name="%(class)s_text_once"
            ),
            models.CheckConstraint(
                condition=Q(digest__regex=r"^[0-9a-f]{64}$"),
                name="%(class)s_digest_is_sha256",
            ),
        ]

    def __str__(self) -> str:
        return f"booking conditions {self.digest[:12]}"


class Order(Numbered):
    """One checkout: one or more bookings, paid with one payment when there
    is something to pay. It has no state of its own.
    """

    # For addresses. It cannot be guessed, as the number can.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # What people read and say: a random code (REFERENCE_ALPHABET), given
    # when the order is made, never changed, unique in the organization. The
    # number stays the internal count, for addresses and sorting.
    reference = models.CharField(max_length=REFERENCE_LENGTH, editable=False)
    member = models.ForeignKey(Member, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    # When unpaid bookings in the order are released.
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    # The sum of its booking lines, quantity times amount. Zero means no
    # payment.
    total = models.PositiveIntegerField()
    # The confirmation email : due from the moment the order is
    # confirmed, by payment or at once when free; tried when a send began;
    # sent when the mail service took it. None of them on an order confirmed
    # before the email existed, so it is never sent one. Due is cleared when
    # it is no longer sent (cancelled since, no address, given up).
    confirmation_due_at = models.DateTimeField(null=True, blank=True)
    confirmation_tried_at = models.DateTimeField(null=True, blank=True)
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    # The booking conditions the member accepted when the order was made, and
    # when. Both are empty on legacy orders without recorded conditions.
    conditions = models.ForeignKey(
        BookingConditions,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="+",
    )
    conditions_accepted_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects: ClassVar[MemberManager[Self]] = MemberManager()

    counter = "last_order_number"

    class Meta(Numbered.Meta):
        constraints = [
            *organization_rules("member", "conditions"),
            models.UniqueConstraint(
                fields=["organization", "number"], name="%(class)s_number_once"
            ),
            models.CheckConstraint(
                condition=Q(conditions__isnull=True, conditions_accepted_at=None)
                | Q(conditions__isnull=False, conditions_accepted_at__isnull=False),
                name="%(class)s_conditions_both_or_neither",
            ),
            models.UniqueConstraint(
                fields=["organization", "reference"], name="%(class)s_reference_once"
            ),
            models.CheckConstraint(
                condition=Q(reference__regex=REFERENCE_PATTERN),
                name="%(class)s_reference_is_a_code",
            ),
        ]
        # The confirmations still to send, which a sent one leaves.
        indexes = [
            models.Index(
                fields=["organization", "confirmation_due_at"],
                name="order_confirmation_due",
                condition=Q(
                    confirmation_sent_at=None, confirmation_due_at__isnull=False
                ),
            )
        ]

    def __str__(self) -> str:
        return f"order {self.reference}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        using = kwargs.get("using") or self._state.db or "default"
        if not self._state.adding:
            stored = (
                type(self)
                .objects.of(self.organization_id)
                .using(using)
                .values_list("reference", flat=True)
                .get(pk=self.pk)
            )
            if stored != self.reference:
                raise ValidationError("An order's reference cannot be changed.")
            super().save(*args, **kwargs)
            return
        wanted = self.number
        given = bool(self.reference)
        # A clash draws again: the draw is tried in a savepoint, which also
        # gives the number back.
        for _ in range(20):
            if not given:
                self.reference = draw_reference()
            self.number = wanted
            try:
                with transaction.atomic(using=using):
                    super().save(*args, **kwargs)
                return
            except IntegrityError as fault:
                # The driver names the refusing constraint (psycopg's diag).
                diag = getattr(fault.__cause__, "diag", None)
                if (
                    given
                    or getattr(diag, "constraint_name", None) != "order_reference_once"
                ):
                    raise
        raise RuntimeError("No free order reference found.")


class BookingState(models.TextChoices):
    # Waiting for payment.
    HELD = "held"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# The state of a block's taken time. A booking's taken time has the
# booking's state.
BLOCKED = "blocked"

# The state of an event's taken time: the whole asset, no member and no
# payment .
EVENT_STATE = "event"

# The states in which taken time holds the asset.
HOLDING = (BookingState.HELD, BookingState.CONFIRMED, BLOCKED, EVENT_STATE)


class TakenTimeManager[T: "TakenTime"](OrganizationManager[T]):
    def take(self, *, units: Sequence[Spot] | None = None, **fields: Any) -> T:
        """Persist a claim with the spots it holds: a shared claim holds
        `units`, one for each of its quantity, from its start to its end; an
        exclusive claim holds the whole asset and names none.

        Which spots are free is the order's question (`ordering`); the
        database refuses a spot that is not.
        """
        with transaction.atomic(using=self.db):
            row = self.create(**fields)
            if row.mode == ConsumptionMode.SHARED or units is not None:
                row.hold(units, using=self.db)
            return row


class TakenTime(Owned):
    """Time on an asset that a booking, a block or an event day takes. Every
    booking, every block and every event day on an asset has one row here and
    keeps its asset, its time and its state in it, with the spots it holds
    (`CapacityAllocation`). So one rule on one table keeps them all apart: a
    booking from a booking, a booking from a block, a block from a block and
    each of them from an event day.
    """

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    # Copies of the asset's space key and mark, which follow the asset.
    scope_key = models.BigIntegerField(editable=False)
    whole_space = models.BooleanField(editable=False)
    # Its copy of the asset's time zone, which holds the asset's zone still
    # (`takentime_asset_time_zone`).
    time_zone = models.CharField(max_length=64, editable=False)
    mode = models.CharField(
        max_length=9, choices=ConsumptionMode, default=ConsumptionMode.EXCLUSIVE
    )
    quantity = models.PositiveIntegerField(default=1)
    required_claims = models.GeneratedField(
        expression=Case(
            When(mode=ConsumptionMode.EXCLUSIVE, then=Value(1)),
            default=F("quantity"),
            output_field=models.IntegerField(),
        ),
        output_field=models.IntegerField(),
        db_persist=True,
    )
    starts_at = models.DateTimeField()
    # For a booking: the booked end, whatever length is paid for.
    ends_at = models.DateTimeField()
    state = models.CharField(
        max_length=9,
        choices=[
            *BookingState.choices,
            (BLOCKED, "Blocked"),
            (EVENT_STATE, "Event"),
        ],
    )
    objects: ClassVar[TakenTimeManager[Self]] = TakenTimeManager()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("asset"),
            models.CheckConstraint(
                condition=Q(starts_at__lt=F("ends_at")),
                name="%(class)s_ends_after_it_starts",
            ),
            # A state the rule below does not know would hold no time.
            models.CheckConstraint(
                condition=Q(state__in=[*BookingState.values, BLOCKED, EVENT_STATE]),
                name="%(class)s_state_is_known",
            ),
            DatabaseRule(
                name="takentime_finite",
                definition="CHECK (isfinite(starts_at) AND isfinite(ends_at))",
            ),
            DatabaseRule(
                name="takentime_quantity_by_mode",
                definition=(
                    "CHECK (mode IN ('shared','exclusive') AND quantity > 0 AND "
                    "(mode <> 'exclusive' OR quantity = 1))"
                ),
            ),
            # What each spot it holds copies.
            models.UniqueConstraint(
                fields=[
                    "id",
                    "organization",
                    "asset",
                    "scope_key",
                    "whole_space",
                    "mode",
                    "quantity",
                    "state",
                    "starts_at",
                    "ends_at",
                ],
                name="takentime_claim_key",
            ),
            # The asset's space and mark as they are: a claim written with
            # another copy is refused, and the copies of every claim follow
            # the asset when it moves.
            DatabaseRule(
                name="takentime_asset_placement",
                definition=(
                    "FOREIGN KEY (organization_id, asset_id, scope_key, whole_space) "
                    "REFERENCES core_asset (organization_id, id, scope_key, "
                    "whole_space) ON UPDATE CASCADE"
                ),
            ),
            # The asset's time zone as it is: a claim written with another
            # copy is refused, and the asset's zone is not changed while it
            # has a claim, a past one too (no cascade: the zone is what its
            # times were read in).
            DatabaseRule(
                name="takentime_asset_time_zone",
                definition=(
                    "FOREIGN KEY (organization_id, asset_id, time_zone) "
                    "REFERENCES core_asset (organization_id, id, time_zone)"
                ),
            ),
            # Its last spot is held; `allocation_previous_claim` holds the rest.
            DatabaseRule(
                name="takentime_every_spot_held",
                definition=(
                    "FOREIGN KEY (id, required_claims) REFERENCES "
                    "core_capacityallocation (taken_time_id, ordinal) "
                    "DEFERRABLE INITIALLY DEFERRED"
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"taken time {self.pk}"

    def hold(self, units: Sequence[Spot] | None, *, using: str) -> None:
        """Write the spots the claim holds. An exclusive claim holds the
        whole asset: its one row is written with it (`save`)."""
        if self.mode == ConsumptionMode.EXCLUSIVE:
            if units is not None:
                raise ValueError("An exclusive claim holds the whole asset.")
            CapacityAllocation.objects.db_manager(using).create(
                organization_id=self.organization_id, taken_time=self, ordinal=1
            )
            return
        if units is None or len(units) != self.quantity:
            raise ValueError("A shared claim holds one spot for each of its quantity.")
        for ordinal, spot in enumerate(units, 1):
            CapacityAllocation.objects.db_manager(using).create(
                organization_id=self.organization_id,
                taken_time=self,
                ordinal=ordinal,
                inventory_id=spot.floor_plan_id,
                spot=spot,
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            creating = self._state.adding
            if creating:
                asset = (
                    Asset.objects.of(self.organization_id)
                    .using(using)
                    .get(pk=self.asset_id)
                )
                self.scope_key, self.whole_space = asset.scope_key, asset.whole_space
                self.time_zone = asset.time_zone
            restrict_updates(
                self,
                kwargs,
                writable=("state", "mode", "quantity", "starts_at", "ends_at"),
                identity=("organization_id", "asset_id"),
            )
            super().save(*args, **kwargs)
            if creating and self.mode == ConsumptionMode.EXCLUSIVE:
                self.hold(None, using=using)

    def revise(
        self,
        *,
        starts_at: datetime,
        ends_at: datetime,
        mode: str,
        quantity: int,
        units: Sequence[Spot] | None = None,
    ) -> None:
        """Replace the claim's time and the spots it holds in one
        transaction."""
        using = self._state.db or "default"
        with transaction.atomic(using=using):
            row = (
                TakenTime.objects.of(self.organization_id)
                .using(using)
                .select_for_update()
                .get(pk=self.pk)
            )
            row.allocations.all().delete()
            row.starts_at, row.ends_at, row.mode, row.quantity = (
                starts_at,
                ends_at,
                mode,
                quantity,
            )
            row.save(using=using)
            row.hold(units, using=using)
        self.refresh_from_db(using=using)


def _on_its_asset(booking_configuration: BookingConfiguration, asset: Asset) -> None:
    """A booking and a block belong to one container, on their own asset."""
    if booking_configuration.asset_id != asset.pk:
        raise ValueError("The container is on another asset.")


def _demand(
    booking_configuration: BookingConfiguration, units: Sequence[Spot] | None
) -> tuple[str, int]:
    """The mode and quantity of a claim under the container: the spots it
    holds on a shared container, the whole asset on an exclusive one."""
    if booking_configuration.consumption_mode == ConsumptionMode.EXCLUSIVE:
        if units is not None:
            raise ValueError(
                "An exclusive container books the whole asset, never a spot."
            )
        return ConsumptionMode.EXCLUSIVE, 1
    if not units:
        raise ValueError("A shared container books spots of its floor plan.")
    if any(spot.floor_plan_id != booking_configuration.floor_plan_id for spot in units):
        raise ValueError("The spots are on the container's floor plan.")
    return ConsumptionMode.SHARED, len(units)


class BookingManager[T: "Booking"](MemberManager[T]):
    def take(
        self,
        *,
        organization: Organization,
        asset: Asset,
        booking_configuration: BookingConfiguration,
        starts_at: datetime,
        ends_at: datetime,
        state: BookingState,
        units: Sequence[Spot] | None = None,
        **fields: Any,
    ) -> T:
        """Make a booking under its container, together with the time it
        takes and the spots it holds: all or none. On a shared container it
        holds `units`, spots of the container's floor plan; on an exclusive
        one the whole asset."""
        _on_its_asset(booking_configuration, asset)
        mode, quantity = _demand(booking_configuration, units)
        with transaction.atomic(using=self.db):
            taken_time = TakenTime.objects.db_manager(self.db).take(
                organization=organization,
                asset=asset,
                starts_at=starts_at,
                ends_at=ends_at,
                state=state,
                mode=mode,
                quantity=quantity,
                units=units,
            )
            return self.create(
                organization=organization,
                taken_time=taken_time,
                booking_configuration=booking_configuration,
                **fields,
            )


class Booking(Owned):
    """One reserved time on one asset for one member. The booking is the
    time only. What is bought is on its booking lines.
    """

    # For addresses and for the calendar's data. It cannot be guessed.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # The asset, the time and the booking's state.
    taken_time = models.OneToOneField(
        TakenTime, on_delete=models.PROTECT, related_name="booking"
    )
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="bookings")
    # The container it was booked under.
    booking_configuration = models.ForeignKey(
        BookingConfiguration, on_delete=models.PROTECT, related_name="bookings"
    )
    member = models.ForeignKey(Member, on_delete=models.PROTECT)
    booking_type = models.ForeignKey(BookingType, on_delete=models.PROTECT)
    booking_category = models.ForeignKey(BookingCategory, on_delete=models.PROTECT)
    # Copies, kept if the booking type or the booking category is renamed.
    booking_type_name = models.CharField(max_length=200)
    booking_category_name = models.CharField(max_length=200)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # The administrator who cancelled it.
    cancelled_by = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # The row of the member's cart it was ordered from; empty when it was not
    # ordered from a cart. Another request of the same browser can save an
    # older copy of the session, so the cart reads its order from here .
    cart_row = models.CharField(max_length=16, blank=True, default="", editable=False)
    # The reminder email : tried when a send naming it
    # began; reminded when a reminder naming it was sent, its own or an
    # earlier booking's, after which it is named in none.
    reminder_tried_at = models.DateTimeField(null=True, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)
    # The administrator's cancel that cancelled it : the request's
    # key, shared by every booking that one cancel took, and the cancellation
    # email of that cancel: due from the cancel, tried when a send began,
    # sent when the mail service took it. Due is cleared when no email is
    # sent (a member without an address, or not active).
    cancellation_key = models.UUIDField(null=True, blank=True, editable=False)
    cancel_mail_due_at = models.DateTimeField(null=True, blank=True)
    cancel_mail_tried_at = models.DateTimeField(null=True, blank=True)
    cancel_mailed_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[BookingManager[Self]] = BookingManager()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(
                "taken_time",
                "order",
                "booking_configuration",
                "member",
                "booking_type",
                "booking_category",
                "cancelled_by",
            ),
            models.CheckConstraint(
                condition=Q(cancellation_key__isnull=False)
                | Q(
                    cancel_mail_due_at__isnull=True,
                    cancel_mail_tried_at__isnull=True,
                    cancel_mailed_at__isnull=True,
                ),
                name="booking_cancel_mail_needs_cancel",
            ),
        ]
        indexes = [
            models.Index(fields=["cart_row"], name="booking_cart_row"),
            # The cancellation emails still to send, which a sent one leaves.
            models.Index(
                fields=["organization", "cancel_mail_due_at"],
                name="booking_cancel_mail_due",
                condition=Q(
                    cancel_mailed_at__isnull=True, cancel_mail_due_at__isnull=False
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"booking {self.pk}"


class BookingLine(Owned):
    """What is bought with a booking: one line for each price tier, and for
    each group of spots with a price of its own, that is paid for. The line
    keeps its own copies, so a later change to a price book changes no line
    that exists.
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="lines")
    # The configuration the time was booked under, and that
    # configuration's price book when the booking was made.
    booking_configuration = models.ForeignKey(
        BookingConfiguration, on_delete=models.PROTECT
    )
    price_book = models.ForeignKey(PriceBook, on_delete=models.PROTECT)
    # Copies of the price tier's "up to" and amount.
    up_to = models.PositiveIntegerField()
    # How many times the tier's amount is paid.
    quantity = models.PositiveIntegerField()
    amount = models.PositiveIntegerField()
    billing_basis = models.CharField(
        max_length=17, choices=BillingBasis, default=BillingBasis.PER_BOOKING
    )
    capacity_quantity = models.PositiveIntegerField(default=1)
    capacity_unit = models.CharField(max_length=80, default="booking")
    # Copies of the two numbers.
    configuration_number = models.PositiveIntegerField()
    price_book_number = models.PositiveIntegerField()
    # The group of spots the line is for, when the container has prices for
    # groups ; empty for spots in no group, and otherwise.
    spot_group = models.ForeignKey(
        SpotGroup, null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(
                "booking", "booking_configuration", "price_book", "spot_group"
            ),
            models.CheckConstraint(
                condition=Q(billing_basis__in=BillingBasis.values)
                & Q(capacity_quantity__gt=0)
                & ~Q(capacity_unit="")
                & (~Q(billing_basis=BillingBasis.PER_BOOKING) | Q(capacity_quantity=1)),
                name="bookingline_capacity_charge",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=1), name="%(class)s_quantity_above_zero"
            ),
        ]

    def __str__(self) -> str:
        return f"booking line {self.pk}"

    @property
    def total(self) -> int:
        return self.quantity * self.amount * self.capacity_quantity


class PaymentProvider(models.TextChoices):
    VIPPS = "vipps", "Vipps"


class PaymentState(models.TextChoices):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class TroubleStep(models.TextChoices):
    """The step of a provider check that failed (named as the worker's steps)."""

    READ = "read"
    CAPTURE = "capture"
    REFUND = "refund"
    CANCEL = "cancel"
    # The database work around the calls, and checking what Vipps answered
    # against what is saved.
    SAVE = "save"


class TroubleKind(models.TextChoices):
    """Why: fixed words, never a provider's text, body or exception message."""

    UNANSWERED = "unanswered"
    UNACCEPTED = "unaccepted"
    DATABASE = "database"
    OTHER = "other"


class Payment(Owned):
    """The money movement for one order at a payment provider. An order with
    nothing to pay has no payment.
    """

    order = models.OneToOneField(Order, on_delete=models.PROTECT)
    payment_provider = models.CharField(max_length=5, choices=PaymentProvider)
    # The provider's id for the payment.
    provider_reference = models.CharField(max_length=200)
    amount = models.PositiveIntegerField()
    state = models.CharField(max_length=10, choices=PaymentState)
    captured_amount = models.PositiveIntegerField(default=0)
    # When a provider read first showed money captured ; empty for
    # a payment captured before the field existed, and until then.
    captured_at = models.DateTimeField(null=True, blank=True)
    refunded_amount = models.PositiveIntegerField(default=0)
    # What Vipps charged for the capture and the payout that took its money, both
    # from the daily report ; empty until the report shows them.
    fee_amount = models.PositiveIntegerField(null=True, blank=True)
    payout = models.ForeignKey(
        "Payout",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    # Nonpersonal totals retained when older bookings leave a mixed-age order.
    retired_booking_amount = models.PositiveIntegerField(default=0)
    retired_refund_amount = models.PositiveIntegerField(default=0)
    # A null key identifies historical records, never a new provider request.
    create_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    capture_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    cancel_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    settled_at = models.DateTimeField(null=True, blank=True)
    credential_name = models.CharField(max_length=200, blank=True)
    merchant_number = models.CharField(max_length=10, blank=True)
    # A saved intent keeps its provider environment when its account is edited.
    # Existing payments were made before production admission, in the sandbox.
    test_mode = models.BooleanField(default=True)
    # The currency of the order's price book, kept for identical retries.
    currency = models.CharField(max_length=3, choices=Currency)
    return_url = models.CharField(max_length=2048, blank=True)
    # Temporary provider access: excluded from inspection and member exports.
    redirect_url = models.CharField(max_length=4096, blank=True)
    create_completed_at = models.DateTimeField(null=True, blank=True)
    # The member's phone as Vipps takes it (digits, country code), frozen with
    # the create intent so a retry sends the same body. Emptied when the create
    # completes or the member is erased; excluded from inspection and exports.
    create_phone = models.CharField(max_length=15, blank=True)
    # What the member sees in the Vipps app, frozen with the create intent so a
    # retry sends the same bytes: the description, and the receipt as the JSON
    # text sent (a JSON column would reorder its keys). No personal data. Both
    # are empty on an intent made before they existed, and the receipt when its
    # lines did not add up to the amount.
    create_description = models.CharField(max_length=100, blank=True)
    create_receipt = models.TextField(blank=True)
    # When Vipps accepted the link back to the organization's site.
    details_sent_at = models.DateTimeField(null=True, blank=True)
    webhook_events = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    callback_received_at = models.DateTimeField(null=True, blank=True)
    provider_check_attempted_at = models.DateTimeField(null=True, blank=True)
    provider_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("order", "payout"),
            DatabaseRule(
                name="payment_event_limit",
                definition="CHECK (cardinality(webhook_events) <= 1024)",
            ),
            models.UniqueConstraint(
                fields=["payment_provider", "provider_reference"],
                name="%(class)s_reference_once",
            ),
            models.CheckConstraint(
                condition=Q(captured_amount__lte=F("amount"))
                & Q(refunded_amount__lte=F("captured_amount")),
                name="%(class)s_amounts_add_up",
            ),
            models.CheckConstraint(
                condition=Q(currency="NOK") | Q(currency="DKK") | Q(currency="EUR"),
                name="payment_currency_is_known",
            ),
            models.CheckConstraint(
                condition=Q(retired_booking_amount__lte=F("amount"))
                & Q(retired_refund_amount__lte=F("refunded_amount")),
                name="payment_retired_amounts",
            ),
        ]

    def __str__(self) -> str:
        return f"payment {self.pk}"


class PaymentTrouble(Owned):
    """One spell of failed provider checks on a payment, kept by the worker
    (payment_trouble.py): since when checks failed with no success between, the
    step and kind of the latest failure, the step and kind it began with (which
    the alert carries, so one spell is one alert), the tries, and when a success
    ended it.
    Fixed words only, never a provider's text. Every spell is kept, so the
    booking's History shows each one's start and end."""

    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="troubles"
    )
    since = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    step = models.CharField(max_length=10, choices=TroubleStep)
    kind = models.CharField(max_length=10, choices=TroubleKind)
    first_step = models.CharField(max_length=10, choices=TroubleStep)
    first_kind = models.CharField(max_length=10, choices=TroubleKind)
    tries = models.PositiveIntegerField()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("payment"),
            models.CheckConstraint(
                condition=Q(step__in=TroubleStep.values)
                & Q(kind__in=TroubleKind.values)
                & Q(first_step__in=TroubleStep.values)
                & Q(first_kind__in=TroubleKind.values)
                & Q(tries__gte=1),
                name="payment_trouble_is_fixed_words",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("since")),
                name="payment_trouble_ends_after_it_began",
            ),
            models.UniqueConstraint(
                fields=["payment"],
                condition=Q(ended_at__isnull=True),
                name="payment_trouble_one_open",
            ),
        ]

    def __str__(self) -> str:
        return f"payment trouble {self.pk}"


class Refund(Owned):
    """Money returned to the member for all or part of an order."""

    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="refunds"
    )
    # The cancelled booking it was returned for.
    booking = models.ForeignKey(
        Booking, null=True, blank=True, on_delete=models.PROTECT
    )
    amount = models.PositiveIntegerField()
    # The administrator.
    made_by = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    automatic = models.BooleanField(default=False)
    refund_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    refunded_before = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    alerted_at = models.DateTimeField(null=True, blank=True)
    made_at = models.DateTimeField(default=timezone.now)
    # The provider's id for the refund.
    provider_reference = models.CharField(max_length=200, blank=True)
    # When the provider's report showed the refund (its entry's moment), once the
    # daily read paired the two ; empty until then.
    reported_at = models.DateTimeField(null=True, blank=True)
    # The payout the refund was deducted from, from the daily report .
    payout = models.ForeignKey(
        "Payout",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refunds",
    )

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("payment", "booking", "made_by", "payout"),
            models.CheckConstraint(
                condition=Q(amount__gte=1), name="%(class)s_amount_above_zero"
            ),
            models.CheckConstraint(
                condition=Q(automatic=False, made_by__isnull=False)
                | Q(automatic=True, made_by__isnull=True, refund_key__isnull=False),
                name="refund_real_actor",
            ),
            models.UniqueConstraint(
                fields=["payment"],
                condition=Q(refund_key__isnull=False, completed_at__isnull=True),
                name="refund_one_pending",
            ),
            models.CheckConstraint(
                condition=Q(alerted_at__isnull=True) | Q(completed_at__isnull=False),
                name="refund_alert_after_completion",
            ),
        ]

    def __str__(self) -> str:
        return f"refund {self.pk}"


class BlockManager[T: "Block"](OrganizationManager[T]):
    def take(
        self,
        *,
        organization: Organization,
        asset: Asset,
        booking_configuration: BookingConfiguration,
        starts_at: datetime,
        ends_at: datetime,
        units: Sequence[Spot] | None = None,
        **fields: Any,
    ) -> T:
        """Make a block on its container, together with the time it takes:
        both or neither. It takes cells of the container's grid like a
        booking without a member: its time is whole cells of the
        container's date periods (one after another when they touch), and
        on a shared container it holds `units`,
        spots of the container's floor plan. On an exclusive container it
        takes the one column, the whole asset: a block on one spot there is
        refused.
        """
        # Imported here: the calendar reads these models.
        from randirandi.core.calendar import on_its_grid

        _on_its_asset(booking_configuration, asset)
        mode, quantity = _demand(booking_configuration, units)
        zone = asset.zone
        if not on_its_grid(booking_configuration, starts_at, ends_at, zone):
            raise ValueError(
                "A block takes whole cells of its container's date periods."
            )
        with transaction.atomic(using=self.db):
            taken_time = TakenTime.objects.db_manager(self.db).take(
                organization=organization,
                asset=asset,
                starts_at=starts_at,
                ends_at=ends_at,
                state=BLOCKED,
                mode=mode,
                quantity=quantity,
                units=units,
            )
            return self.create(
                organization=organization,
                taken_time=taken_time,
                booking_configuration=booking_configuration,
                **fields,
            )


class Block(Owned):
    """Time an administrator reserves on an asset without a member and
    without payment, for example for maintenance. It belongs to one
    container and takes cells of its grid (`BlockManager.take`).
    """

    # The asset and the time. Removing the taken time removes the block.
    taken_time = models.OneToOneField(
        TakenTime, on_delete=models.CASCADE, related_name="block"
    )
    # Its container.
    booking_configuration = models.ForeignKey(
        BookingConfiguration, on_delete=models.PROTECT, related_name="blocks"
    )
    # Why.
    note = models.TextField(blank=True)
    # The administrator.
    made_by = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="+")
    # When it was made ; none for a block from before.
    created_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[BlockManager[Self]] = BlockManager()

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("taken_time", "booking_configuration", "made_by"),
        ]

    def __str__(self) -> str:
        return f"block {self.pk}"

    def delete(
        self, using: Any = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        """Remove the block with the time it took, so the asset is free
        again. A block deleted any other way would leave its time taken.
        """
        return self.taken_time.delete(using=using, keep_parents=keep_parents)


class PaymentProviderAccount(Owned):
    """An organization's own agreement with a payment provider. An
    organization that charges nothing has none.
    """

    payment_provider = models.CharField(max_length=5, choices=PaymentProvider)
    # The organization's number at the provider.
    merchant_number = models.CharField(max_length=50)
    # The name of the secret in the secret store. The secret itself is never
    # stored here.
    credentials = models.CharField(max_length=200)
    # Whether it points at the provider's test environment.
    test_mode = models.BooleanField()
    # The daily read of the provider's report : when the last read
    # began and when the last one reached today. Never set for a test account.
    report_attempted_at = models.DateTimeField(null=True, blank=True)
    report_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "payment_provider"], name="%(class)s_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_payment_provider_display()} account {self.pk}"


class PaymentLedger(Owned):
    """One ledger the provider keeps for the organization's merchant number
    : how far each topic of its report has been read whole.
    """

    account = models.ForeignKey(
        PaymentProviderAccount, on_delete=models.PROTECT, related_name="+"
    )
    ledger_id = models.CharField(max_length=64)
    currency = models.CharField(max_length=3, choices=Currency)
    funds_through = models.DateField(null=True, blank=True)
    fees_through = models.DateField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("account"),
            models.UniqueConstraint(
                fields=["organization", "ledger_id"], name="paymentledger_once"
            ),
        ]

    def __str__(self) -> str:
        return f"ledger {self.pk}"


class LedgerTopic(models.TextChoices):
    FUNDS = "funds"
    FEES = "fees"


class LedgerEntry(Owned):
    """One entry of a ledger as the provider gave it. It holds no personal
    data and is never changed: the same entry read twice is one row.
    """

    ledger = models.ForeignKey(
        PaymentLedger, on_delete=models.PROTECT, related_name="entries"
    )
    topic = models.CharField(max_length=5, choices=LedgerTopic)
    entry_type = models.CharField(max_length=64)
    # The provider's reference for the entry.
    psp_reference = models.CharField(max_length=100)
    # The payment reference sent when the payment was created; empty for some.
    reference = models.CharField(max_length=200, blank=True)
    ledger_date = models.DateField()
    at = models.DateTimeField()
    # Minor units; a refund or a fee lowers the balance, so it is negative.
    amount = models.BigIntegerField()
    balance_before = models.BigIntegerField()
    balance_after = models.BigIntegerField()

    class Meta(Owned.Meta):
        indexes = [
            models.Index(
                fields=["ledger", "topic", "ledger_date"], name="ledgerentry_day"
            ),
            models.Index(fields=["organization", "reference"], name="ledgerentry_ref"),
        ]
        constraints = [
            *organization_rules("ledger"),
            models.UniqueConstraint(
                fields=[
                    "ledger",
                    "topic",
                    "entry_type",
                    "psp_reference",
                    "at",
                    "amount",
                    "balance_before",
                ],
                name="ledgerentry_once",
            ),
            models.CheckConstraint(
                condition=Q(topic="funds") | Q(topic="fees"),
                name="ledgerentry_topic_is_known",
            ),
        ]

    def __str__(self) -> str:
        return f"ledger entry {self.pk}"


class Payout(Owned):
    """A payout the provider scheduled to the organization's bank account:
    one `payout-scheduled` entry and the totals of the entries since the
    previous payout of the ledger. The totals are kept here, so they stay valid
    when old bookings, with their payments, are erased.
    """

    ledger = models.ForeignKey(
        PaymentLedger, on_delete=models.PROTECT, related_name="payouts"
    )
    # The provider's reference: the ledger's id and the payout's number.
    psp_reference = models.CharField(max_length=100)
    ledger_date = models.DateField()
    scheduled_at = models.DateTimeField()
    # Set when the provider aborted the payout and put the money back on the
    # ledger; the payout is then void: not listed, and its payments wait.
    aborted_at = models.DateTimeField(null=True)
    currency = models.CharField(max_length=3, choices=Currency)
    amount = models.BigIntegerField()
    # The text on the bank statement; empty where the provider gives none.
    statement_text = models.CharField(max_length=200, blank=True)
    captured_total = models.BigIntegerField()
    refunded_total = models.BigIntegerField()
    fee_total = models.BigIntegerField()
    other_total = models.BigIntegerField()
    # How many captures and refunds the totals are of, kept for the same reason.
    captured_count = models.PositiveIntegerField(default=0)
    refunded_count = models.PositiveIntegerField(default=0)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("ledger"),
            models.UniqueConstraint(
                fields=["ledger", "psp_reference"], name="payout_once"
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0)
                & Q(
                    amount=F("captured_total")
                    - F("refunded_total")
                    - F("fee_total")
                    + F("other_total")
                ),
                name="payout_adds_up",
            ),
            models.CheckConstraint(
                condition=Q(currency="NOK") | Q(currency="DKK") | Q(currency="EUR"),
                name="payout_currency_is_known",
            ),
        ]

    def __str__(self) -> str:
        return f"payout {self.pk}"
