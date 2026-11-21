# SPDX-License-Identifier: AGPL-3.0-only
"""Spaces, floor plans with their spots, and the spots a claim holds.

The rule against double booking is native: one exclusion constraint on the
spots claims hold, keyed by the asset, the spot and the asset's space and
mark . Runtime grants are installed with the
migration; the copies below are checked witnesses.
"""

from typing import Any, ClassVar

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest

from randirandi.core.booking_words import DEFAULT_SPOT_WORD
from randirandi.core.models.base import (
    DatabaseRule,
    Owned,
    organization_rules,
    restrict_updates,
)
from randirandi.core.models.organization import Numbered
from randirandi.floor_plan_rule import (
    NAME_USED,
    NEEDS_NAME,
    SIBLING_UNNAMED,
    floor_plan_name_fault,
)
from randirandi.limits import MOST_SPOTS, TOO_MANY_SPOTS


def _within_most_spots(field: str, capacity: int) -> None:
    if capacity > MOST_SPOTS:
        raise ValidationError({field: TOO_MANY_SPOTS})


class ConsumptionMode(models.TextChoices):
    SHARED = "shared", "Shared capacity"
    EXCLUSIVE = "exclusive", "Exclusive use"


class BookingPage(models.TextChoices):
    """What a space's booking page shows (W15)."""

    ONE_ASSET = "one_asset", "One asset at a time"
    WHOLE_SPACE = "whole_space", "The whole space"


class Space(Numbered):
    """A grouping of assets: where they are. Being a location reserves
    nothing."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # Whether its booking page shows one asset at a time, or every asset's
    # containers side by side.
    booking_page = models.CharField(
        max_length=11, choices=BookingPage, default=BookingPage.ONE_ASSET
    )
    counter = "last_space_number"

    class Meta(Numbered.Meta):
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "number"], name="space_number_once"
            ),
            models.UniqueConstraint(
                fields=["organization", "name"], name="space_name_once"
            ),
            models.CheckConstraint(condition=Q(id__gt=0), name="space_positive_id"),
            models.CheckConstraint(
                condition=Q(booking_page__in=BookingPage.values),
                name="space_booking_page_known",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        restrict_updates(
            self,
            kwargs,
            writable=("name", "description", "booking_page"),
            identity=("organization_id", "number"),
        )
        super().save(*args, **kwargs)


class AssetInventory(Owned):
    """A floor plan: one asset's spots, counted, with the word for them
    ("seat", "bed", "machine"). `capacity` is its number of spots and `unit`
    their word; the names hold from when this was the asset's inventory. An
    asset may have several; a shared container books the spots of one of
    them, and the validator keeps the containers of two floor plans of one
    asset apart. It is the asset's own: deleting the asset deletes it, with
    its spots and groups.

    The database holds its capacity to its number of spots at every commit,
    whatever writes it (`floor_plan_counts_its_spots` with the spots' rules).
    No trigger: nothing of ours stands on an extension, which any database
    user may remove at Scaleway (found 2026-10-10).
    """

    asset = models.ForeignKey(
        "core.Asset", on_delete=models.CASCADE, related_name="floor_plans"
    )
    capacity = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=80, default=DEFAULT_SPOT_WORD)
    name = models.CharField(max_length=200, blank=True)
    # The last numbers given to its spots and its groups. A number is never
    # given twice, also not when its spot is gone.
    last_spot_number = models.PositiveIntegerField(default=0, editable=False)
    last_group_number = models.PositiveIntegerField(default=0, editable=False)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("asset"),
            models.CheckConstraint(
                condition=Q(id__gt=0) & Q(capacity__gt=0) & ~Q(unit=""),
                name="inventory_positive",
            ),
            # What a container and a claim point at: a floor plan of their
            # own asset.
            models.UniqueConstraint(
                fields=["organization", "asset", "id"], name="floor_plan_of_asset_key"
            ),
            # What a spot's copy of the capacity points at.
            models.UniqueConstraint(
                fields=["organization", "id", "capacity"],
                name="floor_plan_capacity_key",
            ),
            # Its capacity is the place of its last spot. With the spots'
            # rules (places from 1, each but the first after another, none
            # above the capacity) its spots are exactly as many.
            DatabaseRule(
                name="floor_plan_counts_its_spots",
                definition=(
                    "FOREIGN KEY (organization_id, id, capacity) REFERENCES "
                    "core_spot (organization_id, floor_plan_id, place) "
                    "DEFERRABLE INITIALLY DEFERRED"
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.capacity} {self.unit}"

    def clean(self) -> None:
        super().clean()
        _within_most_spots("capacity", self.capacity)
        if self.asset_id is not None:
            self._named_beside_its_siblings()

    def _named_beside_its_siblings(self) -> None:
        """An asset with more than one floor plan names each, once ."""
        siblings = list(
            AssetInventory.objects.of(self.organization_id)
            .filter(asset=self.asset_id)
            .exclude(pk=self.pk)
            .values_list("name", flat=True)
        )
        fault = floor_plan_name_fault(self.name, siblings)
        if fault == NEEDS_NAME:
            raise ValidationError(
                {"name": "An asset with more than one floor plan names each."}
            )
        if fault == NAME_USED:
            raise ValidationError(
                {"name": "Another floor plan of this asset has this name."}
            )
        if fault == SIBLING_UNNAMED:
            raise ValidationError(
                {
                    "name": "Another floor plan of this asset has no name, and an "
                    "asset with more than one names each."
                }
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Saves the floor plan. A new one is made with its spots, numbered
        from 1 to its capacity, without names. Its capacity is its number of
        spots: a spot is added on its own (`Spot`), and a capacity that
        differs from its spots, or is above the most a floor plan holds, is
        refused.
        """
        _within_most_spots("capacity", self.capacity)
        with transaction.atomic():
            if self._state.adding:
                self.last_spot_number = self.capacity
                super().save(*args, **kwargs)
                Spot.objects.of(self.organization_id).bulk_create(
                    Spot(
                        organization_id=self.organization_id,
                        floor_plan=self,
                        number=number,
                        place=number,
                        floor_plan_capacity=self.capacity,
                    )
                    for number in range(1, self.capacity + 1)
                )
                return
            spots = Spot.objects.of(self.organization_id).filter(floor_plan=self.pk)
            if self.capacity != (count := spots.count()):
                raise ValidationError(
                    {"capacity": f"It is the number of its spots, {count}."}
                )
            restrict_updates(
                self,
                kwargs,
                writable=("capacity", "unit", "name"),
                identity=("organization_id", "asset_id"),
            )
            super().save(*args, **kwargs)


class FloorPlanNumbered(Owned):
    """A spot or a group: numbered from 1 on its floor plan. The number
    never changes and is never given twice."""

    number = models.PositiveIntegerField(editable=False)

    # The floor plan's field that holds the last number given of this kind.
    counter: ClassVar[str]

    class Meta(Owned.Meta):
        abstract = True

    def take_number(self, floor_plan_id: int, **also: Any) -> None:
        """Give it its number, with the floor plan's row locked until the
        caller's transaction ends. A number given beforehand, as a setup
        file writes it, is taken only if it is above every number given so
        far. `also` changes the floor plan in the same write.
        """
        plans = AssetInventory.objects.of(self.organization_id).filter(pk=floor_plan_id)
        counter = self.counter
        if self.number is None:
            if plans.update(**{counter: F(counter) + 1}, **also) != 1:
                raise ValidationError({"floor_plan": "There is no such floor plan."})
            self.number = plans.values_list(counter, flat=True).get()
        elif (
            plans.filter(**{f"{counter}__lt": self.number}).update(
                **{counter: self.number}, **also
            )
            != 1
        ):
            raise ValidationError(
                {"number": f"The number {self.number} has been given before."}
            )


class SpotGroup(FloorPlanNumbered):
    """Spots of one floor plan that belong together: eight seats at one
    table, a section of a hall (W20, W22). A group is never booked, and a
    spot is in at most one.
    """

    floor_plan = models.ForeignKey(
        AssetInventory, on_delete=models.CASCADE, related_name="groups"
    )
    name = models.CharField(max_length=200)
    # The word for it: "table", "section", "row".
    word = models.CharField(max_length=80, default="group")

    counter = "last_group_number"

    class Meta(FloorPlanNumbered.Meta):
        constraints = [
            *organization_rules("floor_plan"),
            models.UniqueConstraint(
                fields=["floor_plan", "number"], name="%(class)s_number_once"
            ),
            models.UniqueConstraint(
                fields=["floor_plan", "name"], name="%(class)s_name_once"
            ),
            # What a spot's link to its group points at.
            models.UniqueConstraint(
                fields=["organization", "floor_plan", "id"],
                name="%(class)s_floor_plan_key",
            ),
            models.CheckConstraint(
                condition=Q(number__gte=1) & ~Q(name="") & ~Q(word=""),
                name="%(class)s_numbered_and_named",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        with transaction.atomic():
            if self._state.adding:
                self.take_number(self.floor_plan_id)
            restrict_updates(
                self,
                kwargs,
                writable=("name", "word"),
                identity=("organization_id", "floor_plan_id", "number"),
            )
            super().save(*args, **kwargs)


class Spot(FloorPlanNumbered):
    """One spot on a floor plan: a seat, a bed, a workbench. Its number
    never changes, and a removed spot  leaves a gap. A name is optional,
    and once per floor plan when given.
    """

    floor_plan = models.ForeignKey(
        AssetInventory, on_delete=models.CASCADE, related_name="spots"
    )
    # Empty for none.
    name = models.CharField(max_length=200, blank=True)
    # A group goes only with its floor plan, together with its spots.
    group = models.ForeignKey(
        SpotGroup,
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="spots",
    )
    # How the database counts the spots: their places are 1 to the floor
    # plan's capacity, each once. Shown nowhere; a removed spot's place is
    # taken by the last spot , and the number stays.
    place = models.PositiveIntegerField(editable=False)
    previous_place = models.GeneratedField(
        expression=Greatest(Value(1), F("place") - 1),
        output_field=models.IntegerField(),
        db_persist=True,
    )
    # The floor plan's capacity, kept the same by the database.
    floor_plan_capacity = models.PositiveIntegerField(editable=False)

    counter = "last_spot_number"

    class Meta(FloorPlanNumbered.Meta):
        constraints = [
            *organization_rules("floor_plan", "group"),
            models.UniqueConstraint(
                fields=["floor_plan", "number"], name="%(class)s_number_once"
            ),
            models.UniqueConstraint(
                fields=["floor_plan", "name"],
                condition=~Q(name=""),
                name="%(class)s_name_once",
            ),
            models.CheckConstraint(
                condition=Q(number__gte=1), name="%(class)s_numbered_from_one"
            ),
            # What a claim's spot points at: a spot of its floor plan.
            models.UniqueConstraint(
                fields=["organization", "floor_plan", "id"],
                name="%(class)s_floor_plan_key",
            ),
            # A spot's group is a group of its own floor plan.
            DatabaseRule(
                name="spot_group_on_its_floor_plan",
                definition=(
                    "FOREIGN KEY (organization_id, floor_plan_id, group_id) "
                    "REFERENCES core_spotgroup (organization_id, floor_plan_id, id)"
                ),
            ),
            # What its floor plan's capacity and the next place point at.
            models.UniqueConstraint(
                fields=["organization", "floor_plan", "place"], name="spot_place_key"
            ),
            DatabaseRule(
                name="spot_place_within_capacity",
                definition="CHECK (place >= 1 AND place <= floor_plan_capacity)",
            ),
            DatabaseRule(
                name="spot_capacity_bound",
                definition=(
                    "FOREIGN KEY (organization_id, floor_plan_id, "
                    "floor_plan_capacity) REFERENCES core_assetinventory "
                    "(organization_id, id, capacity) ON UPDATE CASCADE"
                ),
            ),
            DatabaseRule(
                name="spot_previous_place",
                definition=(
                    "FOREIGN KEY (organization_id, floor_plan_id, previous_place) "
                    "REFERENCES core_spot (organization_id, floor_plan_id, place) "
                    "DEFERRABLE INITIALLY DEFERRED"
                ),
            ),
        ]

    def __str__(self) -> str:
        return self.name or str(self.number)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Saves the spot. A new one takes the next number, and its floor
        plan's capacity grows by one in the same write: the new spot has the
        last place. A floor plan full to the most spots it holds takes none.
        """
        with transaction.atomic():
            if self._state.adding:
                self.take_number(self.floor_plan_id, capacity=F("capacity") + 1)
                self.place = self.floor_plan_capacity = (
                    AssetInventory.objects.of(self.organization_id)
                    .filter(pk=self.floor_plan_id)
                    .values_list("capacity", flat=True)
                    .get()
                )
                _within_most_spots("floor_plan", self.floor_plan_capacity)
            restrict_updates(
                self,
                kwargs,
                writable=("name", "group"),
                identity=("organization_id", "floor_plan_id", "number"),
            )
            super().save(*args, **kwargs)


class CapacityAllocation(Owned):
    """One spot a claim holds, from the claim's start to its end (R3); for
    an exclusive claim, the whole asset, every spot of every floor plan
    (no spot). A shared claim of quantity q holds q of them, numbered 1 to
    q. The claim's asset, the asset's space and mark, its mode, quantity,
    state and time are copies of the claim's, which follow it.
    """

    taken_time = models.ForeignKey(
        "core.TakenTime", on_delete=models.CASCADE, related_name="allocations"
    )
    ordinal = models.PositiveIntegerField()
    asset = models.ForeignKey(
        "core.Asset", on_delete=models.PROTECT, editable=False, related_name="+"
    )
    # The floor plan of the spot; empty for an exclusive claim.
    inventory = models.ForeignKey(
        AssetInventory, null=True, blank=True, on_delete=models.PROTECT
    )
    spot = models.ForeignKey(
        Spot, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    scope_key = models.BigIntegerField(editable=False)
    whole_space = models.BooleanField(editable=False)
    mode = models.CharField(max_length=9, choices=ConsumptionMode, editable=False)
    quantity = models.PositiveIntegerField(editable=False)
    state = models.CharField(max_length=9, editable=False)
    starts_at = models.DateTimeField(editable=False)
    ends_at = models.DateTimeField(editable=False)
    previous_ordinal = models.GeneratedField(
        expression=Greatest(Value(1), F("ordinal") - 1),
        output_field=models.IntegerField(),
        db_persist=True,
    )

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("taken_time", "asset", "inventory", "spot"),
            models.UniqueConstraint(
                fields=["taken_time", "ordinal"], name="allocation_ordinal_once"
            ),
            DatabaseRule(
                name="allocation_ordinal_in_claim",
                definition=(
                    "CHECK (ordinal >= 1 AND ordinal <= CASE WHEN mode = 'exclusive' "
                    "THEN 1 ELSE quantity END)"
                ),
            ),
            DatabaseRule(
                name="allocation_spot_when_shared",
                definition=(
                    "CHECK ((mode = 'exclusive' AND spot_id IS NULL AND inventory_id "
                    "IS NULL) OR (mode = 'shared' AND spot_id IS NOT NULL AND "
                    "inventory_id IS NOT NULL))"
                ),
            ),
            DatabaseRule(
                name="allocation_claim_header",
                definition=(
                    "FOREIGN KEY (taken_time_id, organization_id, asset_id, scope_key, "
                    "whole_space, mode, quantity, state, starts_at, ends_at) "
                    "REFERENCES core_takentime (id, organization_id, asset_id, "
                    "scope_key, whole_space, mode, quantity, state, starts_at, "
                    "ends_at) ON UPDATE CASCADE"
                ),
            ),
            # With the claim's last one (`takentime_every_spot_held`), every
            # number from 1 to the claim's quantity is held.
            DatabaseRule(
                name="allocation_previous_claim",
                definition=(
                    "FOREIGN KEY (taken_time_id, previous_ordinal) REFERENCES "
                    "core_capacityallocation (taken_time_id, ordinal) "
                    "DEFERRABLE INITIALLY DEFERRED"
                ),
            ),
            DatabaseRule(
                name="allocation_spot_on_its_floor_plan",
                definition=(
                    "FOREIGN KEY (organization_id, inventory_id, spot_id) REFERENCES "
                    "core_spot (organization_id, floor_plan_id, id)"
                ),
            ),
            DatabaseRule(
                name="allocation_floor_plan_of_its_asset",
                definition=(
                    "FOREIGN KEY (organization_id, asset_id, inventory_id) REFERENCES "
                    "core_assetinventory (organization_id, asset_id, id)"
                ),
            ),
            # The rule against double booking. Two holding claims at the same
            # time in the same space key (the asset's space, or the asset
            # alone when it has none) are refused when they are on the same
            # asset and hold the same spot, or either is exclusive; and any
            # claim on an asset that covers its whole space is refused beside
            # any claim on another asset of that space: one asset per space
            # carries the mark (`asset_whole_space_once`). The space key and the
            # mark are the asset's (`kept_apart` says the same of bookings in
            # the application). A range that is everything overlaps every
            # other, so each of the three middle keys is everything where it
            # must not tell two claims apart: the asset for a marked asset, the
            # spot on a marked asset for the unmarked ones and for an
            # exclusive claim, and the spot on an unmarked asset for the
            # marked ones and for an exclusive claim. The order retries a
            # collision under this name.
            DatabaseRule(
                name="capacity_no_double_booking",
                definition=(
                    "EXCLUDE USING gist (int8range(organization_id, organization_id, "
                    "'[]') WITH &&, int8range(scope_key, scope_key, '[]') WITH &&, "
                    "(CASE WHEN whole_space THEN int8range(NULL, NULL) ELSE "
                    "int8range(asset_id, asset_id, '[]') END) WITH &&, (CASE WHEN "
                    "mode = 'exclusive' OR NOT whole_space THEN int8range(NULL, NULL) "
                    "ELSE int8range(spot_id, spot_id, '[]') END) WITH &&, (CASE WHEN "
                    "mode = 'exclusive' OR whole_space THEN int8range(NULL, NULL) "
                    "ELSE int8range(spot_id, spot_id, '[]') END) WITH &&, "
                    "tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (state IN "
                    "('held','confirmed','blocked','event')) "
                    "DEFERRABLE INITIALLY IMMEDIATE"
                ),
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding:
            taken = self.taken_time
            for field in (
                "asset_id",
                "scope_key",
                "whole_space",
                "mode",
                "quantity",
                "state",
                "starts_at",
                "ends_at",
            ):
                setattr(self, field, getattr(taken, field))
        # A claim's spots are replaced, never changed (`TakenTime.revise`).
        restrict_updates(
            self, kwargs, writable=(), identity=("organization_id", "taken_time_id")
        )
        super().save(*args, **kwargs)
