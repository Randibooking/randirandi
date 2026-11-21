# SPDX-License-Identifier: AGPL-3.0-only
"""Spaces, assets, floor plans, spots and groups of spots, as an administrator
sets them .

Every change runs in `setup_admin.organization_transaction`: the organization's
row is locked, so two administrators take turns and the second works on what the
first left (the 7.94 rule). A change writes its audit event first ,
in the same transaction; a made row's event follows its save, a removal's
precedes the delete. The rules are the model's own: a row is checked with
`full_clean`, as the setup file's loader checks it, and saved by the model's own
`save` (which holds the rules of the space mark, the time zone and the spots'
counting); what the setup file would refuse (a floor plan without a name beside
another, `floor_plan_rule`) the model refuses too, so an export made after any
change on the screens reads and loads again.
"""

from collections.abc import Mapping
from typing import Any
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Count, F
from django.db.models.deletion import ProtectedError, RestrictedError
from django.utils import timezone

from randirandi.core import availability
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    HOLDING,
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    BookingConfiguration,
    BookingLine,
    CapacityAllocation,
    ConfigurationCategory,
    Event,
    Organization,
    PriceBookEntry,
    Space,
    Spot,
    SpotGroup,
    TakenTime,
)
from randirandi.core.models.base import Owned
from randirandi.core.models.capacity import BookingPage, ConsumptionMode
from randirandi.core.setup_admin import (
    SetupNotFoundError,
    SetupRefusedError,
    organization_transaction,
)
from randirandi.floor_plan_rule import (
    NAME_USED,
    NEEDS_NAME,
    SIBLING_UNNAMED,
    floor_plan_name_fault,
)
from randirandi.limits import (
    MOST_SPOTS,
    MOST_SPOTS_AT_ONCE,
    TOO_MANY_SPOTS,
    name_fault,
    word_fault,
)

type Fields = Mapping[str, object]

ACTIONS: dict[type[Owned], tuple[AuditAction, AuditAction, AuditAction]] = {
    Space: (
        AuditAction.MADE_SPACE,
        AuditAction.CHANGED_SPACE,
        AuditAction.REMOVED_SPACE,
    ),
    Asset: (
        AuditAction.MADE_ASSET,
        AuditAction.CHANGED_ASSET,
        AuditAction.REMOVED_ASSET,
    ),
    AssetInventory: (
        AuditAction.MADE_FLOOR_PLAN,
        AuditAction.CHANGED_FLOOR_PLAN,
        AuditAction.REMOVED_FLOOR_PLAN,
    ),
    Spot: (
        AuditAction.MADE_SPOT,
        AuditAction.CHANGED_SPOT,
        AuditAction.REMOVED_SPOT,
    ),
    SpotGroup: (
        AuditAction.MADE_SPOT_GROUP,
        AuditAction.CHANGED_SPOT_GROUP,
        AuditAction.REMOVED_SPOT_GROUP,
    ),
}

# The code of a refusal by the field the model names; the model's own words
# travel with it.
FIELD_CODES = {
    "name": "invalid-name",
    "description": "invalid",
    "unit": "invalid-word",
    "word": "invalid-word",
    "time_zone": "time-zone",
    "maximum_occupancy": "invalid-occupancy",
    "capacity": "too-many-spots",
    "floor_plan": "too-many-spots",
    "booking_page": "unknown-booking-page",
    "space": "space",
    "whole_space": "whole-space",
}
# What a booking or a block holds time on, as the spots' claims say it.
ACTIVE = [str(state) for state in HOLDING]


class LayoutRefusedError(SetupRefusedError):
    """A refusal that also carries the model's words for it."""

    def __init__(
        self, code: str, used: dict[str, int] | None = None, message: str = ""
    ) -> None:
        super().__init__(code, used)
        self.message = message


# --- The fields a body may give -----------------------------------------------


def _only(fields: Fields, allowed: set[str], *, required: set[str]) -> None:
    if set(fields) - allowed or required - set(fields):
        raise ValueError("The body is wrong.")


def _name(value: object) -> str:
    """A name as the setup file keeps it (`limits.name_fault`), trimmed."""
    if not isinstance(value, str):
        raise ValueError("A name is text.")
    name = value.strip()
    if name_fault(name):
        raise LayoutRefusedError("invalid-name")
    return name


def _optional_name(value: object) -> str:
    """A name that may be left empty: a spot's, a floor plan's."""
    if not isinstance(value, str):
        raise ValueError("A name is text.")
    return _name(value) if value.strip() else ""


def _word(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("A word is text.")
    word = value.strip()
    if word_fault(word):
        raise LayoutRefusedError("invalid-word")
    return word


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("A description is text.")
    return value.strip()


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("Yes or no is true or false.")
    return value


def _whole(value: object) -> int:
    if type(value) is not int:
        raise ValueError("A number is a whole number.")
    return value


def _id_or_none(value: object) -> int | None:
    if value is None:
        return None
    return _whole(value)


def _ids(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("Spots are a list of ids.")
    return [_whole(x) for x in value]


def _refusal(error: ValidationError, codes: Mapping[str, str]) -> LayoutRefusedError:
    """The refusal for what the model said: the code of the first field it
    names, with its words."""
    fields = getattr(error, "error_dict", None)
    if not fields:
        return LayoutRefusedError("invalid", message=" ".join(error.messages))
    # The model's own words for a field come before a constraint's generic ones.
    named = [pair for pair in fields.items() if pair[0] != "__all__"]
    field, found = (named or list(fields.items()))[0]
    return LayoutRefusedError(
        codes.get(field, "invalid"), message=" ".join(m for e in found for m in e)
    )


def _checked(row: Any, *, exclude: tuple[str, ...] = ()) -> None:
    """The model's own validation, as the setup file's loader applies it."""
    try:
        row.full_clean(exclude=list(exclude))
    except ValidationError as error:
        raise _refusal(error, FIELD_CODES) from None


def _saved(row: Any) -> None:
    """Save through the model, which holds the rules the validation cannot."""
    try:
        row.save()
    except ValidationError as error:
        raise _refusal(error, FIELD_CODES) from None
    except IntegrityError:
        raise LayoutRefusedError("invalid") from None


def _lock[T: Owned](model: type[T], org: Organization, pk: int, **where: object) -> T:
    row = (
        model.objects.of(org)
        .select_for_update(no_key=True)
        .filter(pk=pk, **where)
        .first()
    )
    if row is None:
        raise SetupNotFoundError
    return row


# --- What holds a row ----------------------------------------------------------


def _claims_of_asset(org: Organization, asset: Asset) -> int:
    return TakenTime.objects.of(org).filter(asset=asset).count()


def _held_asset(org: Organization, asset: Asset) -> dict[str, int]:
    held = {
        "containers": BookingConfiguration.objects.of(org).filter(asset=asset).count(),
        "availability": AvailabilityCalendar.objects.of(org)
        .filter(asset=asset)
        .count(),
        "closures": AvailabilityClosure.objects.of(org).filter(asset=asset).count(),
        "events": Event.objects.of(org).filter(asset=asset).count(),
        "bookings": _claims_of_asset(org, asset),
    }
    return {key: n for key, n in held.items() if n}


def _held_group(org: Organization, group: SpotGroup) -> dict[str, int]:
    held = {
        "spots": Spot.objects.of(org).filter(group=group).count(),
        "prices": PriceBookEntry.objects.of(org).filter(spot_group=group).count(),
        "lines": BookingLine.objects.of(org).filter(spot_group=group).count(),
    }
    return {key: n for key, n in held.items() if n}


def _held_plan(org: Organization, plan: AssetInventory) -> dict[str, int]:
    groups = SpotGroup.objects.of(org).filter(floor_plan=plan)
    held = {
        "containers": BookingConfiguration.objects.of(org)
        .filter(floor_plan=plan)
        .count(),
        "bookings": CapacityAllocation.objects.of(org)
        .filter(inventory=plan)
        .values("taken_time")
        .distinct()
        .count(),
        "prices": PriceBookEntry.objects.of(org).filter(spot_group__in=groups).count(),
        "lines": BookingLine.objects.of(org).filter(spot_group__in=groups).count(),
    }
    return {key: n for key, n in held.items() if n}


def _claims_of_spot(org: Organization, spot: Spot) -> int:
    return CapacityAllocation.objects.of(org).filter(spot=spot).count()


# --- What the places show -------------------------------------------------------


def _counts(model: type[Owned], org: Organization, key: str) -> dict[int, int]:
    rows = model.objects.of(org).values_list(key).annotate(n=Count("pk"))
    return {pk: n for pk, n in rows if pk is not None}


def _offered(org: Organization, assets: list[Asset]) -> dict[int, str]:
    """Whether each asset is offered today: it has an enabled container and the
    availability in force has a weekday open (the Availability card is part
    7.98; this is its one word, read-only)."""
    enabled = set(
        BookingConfiguration.objects.of(org)
        .filter(status="enabled")
        .values_list("asset", flat=True)
    )
    spans = set(
        AvailableSpan.objects.of(org).values_list("availability_calendar", flat=True)
    )
    calendars: dict[int, list[tuple[Any, int]]] = {}
    for calendar_id, asset_id, start in AvailabilityCalendar.objects.of(
        org
    ).values_list("pk", "asset", "start_date"):
        calendars.setdefault(asset_id, []).append((start, calendar_id))
    now = timezone.now()
    answer = {}
    for asset in assets:
        today = now.astimezone(asset.zone).date()
        reached = [x for x in calendars.get(asset.pk, []) if x[0] <= today]
        in_force = max(reached, default=None)
        if in_force is None or in_force[1] not in spans:
            answer[asset.pk] = "no-availability"
        elif asset.pk not in enabled:
            answer[asset.pk] = "no-container"
        else:
            answer[asset.pk] = "yes"
    return answer


def _containers(org: Organization) -> dict[int, list[dict[str, object]]]:
    """Each asset's containers, as the asset's page lists them (read-only here;
    their editing is part 7.96)."""
    categories: dict[int, list[str]] = {}
    for configuration_id, name in (
        ConfigurationCategory.objects.of(org)
        .order_by("booking_category__position", "booking_category_id")
        .values_list("booking_configuration", "booking_category__name")
    ):
        categories.setdefault(configuration_id, []).append(name)
    found: dict[int, list[dict[str, object]]] = {}
    for row in (
        BookingConfiguration.objects.of(org)
        .select_related("booking_type", "floor_plan")
        .order_by("number")
    ):
        found.setdefault(row.asset_id, []).append(
            {
                "id": row.pk,
                "number": row.number,
                "type": row.booking_type.name,
                "period": row.period_type,
                "start": row.period_start,
                "end": row.period_end,
                "end_day": row.end_day,
                "interval": row.interval,
                "weekdays": sorted(set(row.period_start_weekdays)),
                "categories": categories.get(row.pk, []),
                "mode": row.consumption_mode,
                "floor_plan": row.floor_plan_id,
                "payment": row.with_payment,
                "status": row.status,
            }
        )
    return found


def assets_place(org: Organization) -> dict[str, object]:
    """The spaces and the assets with their floor plans (counts), containers
    and what holds each row (a row with anything held is not removable)."""
    spaces = list(Space.objects.of(org).order_by("number"))
    assets = list(Asset.objects.of(org).order_by("number"))
    in_space = _counts(Asset, org, "space")
    spots = _counts(Spot, org, "floor_plan")
    groups = _counts(SpotGroup, org, "floor_plan")
    containers = _containers(org)
    plan_containers = _counts(BookingConfiguration, org, "floor_plan")
    offered = _offered(org, assets)
    held_containers = _counts(BookingConfiguration, org, "asset")
    held_calendars = _counts(AvailabilityCalendar, org, "asset")
    held_closures = _counts(AvailabilityClosure, org, "asset")
    availability_of = availability.summary(org, assets)
    held_claims = _counts(TakenTime, org, "asset")
    held_events = _counts(Event, org, "asset")
    plans: dict[int, list[AssetInventory]] = {}
    for plan in AssetInventory.objects.of(org).order_by("pk"):
        plans.setdefault(plan.asset_id, []).append(plan)
    space_names = {space.pk: space.name for space in spaces}
    return {
        "time_zone": org.time_zone,
        "time_zones": sorted(available_timezones()),
        "booking_pages": [
            {"value": value, "label": str(label)}
            for value, label in BookingPage.choices
        ],
        "spaces": [
            {
                "id": space.pk,
                "number": space.number,
                "name": space.name,
                "description": space.description,
                "booking_page": space.booking_page,
                "assets": in_space.get(space.pk, 0),
                "removable": not in_space.get(space.pk, 0),
            }
            for space in spaces
        ],
        "assets": [
            {
                "id": asset.pk,
                "number": asset.number,
                "name": asset.name,
                "description": asset.description,
                "space": asset.space_id,
                "space_name": space_names.get(asset.space_id or 0, ""),
                "whole_space": asset.whole_space,
                "maximum_occupancy": asset.maximum_occupancy,
                "time_zone": asset.time_zone,
                "offered": offered[asset.pk],
                "availability": availability_of[asset.pk],
                "floor_plans": [
                    {
                        "id": plan.pk,
                        "name": plan.name,
                        "word": plan.unit,
                        "spots": spots.get(plan.pk, 0),
                        "groups": groups.get(plan.pk, 0),
                        "containers": plan_containers.get(plan.pk, 0),
                    }
                    for plan in plans.get(asset.pk, [])
                ],
                "containers": containers.get(asset.pk, []),
                "removable": not (
                    held_containers.get(asset.pk)
                    or held_calendars.get(asset.pk)
                    or held_closures.get(asset.pk)
                    or held_claims.get(asset.pk)
                    or held_events.get(asset.pk)
                ),
            }
            for asset in assets
        ],
        "most_spots": MOST_SPOTS,
        "most_spots_at_once": MOST_SPOTS_AT_ONCE,
    }


def plan_place(org: Organization, plan_id: int) -> dict[str, object]:
    """One floor plan with its spots and groups. Each spot says how many
    claims hold it, and how many of them are still to come."""
    plan = (
        AssetInventory.objects.of(org)
        .select_related("asset")
        .filter(pk=plan_id)
        .first()
    )
    if plan is None:
        raise SetupNotFoundError
    now = timezone.now()
    held = dict(
        CapacityAllocation.objects.of(org)
        .filter(inventory=plan)
        .values_list("spot")
        .annotate(n=Count("pk"))
    )
    coming = dict(
        CapacityAllocation.objects.of(org)
        .filter(inventory=plan, state__in=ACTIVE, ends_at__gt=now)
        .values_list("spot")
        .annotate(n=Count("pk"))
    )
    groups = list(SpotGroup.objects.of(org).filter(floor_plan=plan).order_by("number"))
    in_group = _counts_in(Spot.objects.of(org).filter(floor_plan=plan), "group")
    priced = _counts_in(
        PriceBookEntry.objects.of(org).filter(spot_group__floor_plan=plan), "spot_group"
    )
    lines = _counts_in(
        BookingLine.objects.of(org).filter(spot_group__floor_plan=plan), "spot_group"
    )
    spots = list(Spot.objects.of(org).filter(floor_plan=plan).order_by("number"))
    held_plan = _held_plan(org, plan)
    siblings = AssetInventory.objects.of(org).filter(asset=plan.asset_id).count()
    containers = (
        BookingConfiguration.objects.of(org)
        .select_related("booking_type")
        .filter(floor_plan=plan, consumption_mode=ConsumptionMode.SHARED)
        .order_by("number")
    )
    return {
        "id": plan.pk,
        "asset": plan.asset_id,
        "asset_name": plan.asset.name,
        "name": plan.name,
        "word": plan.unit,
        "containers": [
            {"id": c.pk, "number": c.number, "type": c.booking_type.name}
            for c in containers
        ],
        "removable": siblings > 1 and not held_plan,
        "spots": [
            {
                "id": spot.pk,
                "number": spot.number,
                "name": spot.name,
                "group": spot.group_id,
                "held": held.get(spot.pk, 0),
                "coming": coming.get(spot.pk, 0),
                "removable": len(spots) > 1 and not held.get(spot.pk, 0),
            }
            for spot in spots
        ],
        "groups": [
            {
                "id": group.pk,
                "number": group.number,
                "name": group.name,
                "word": group.word,
                "spots": in_group.get(group.pk, 0),
                "removable": not (
                    in_group.get(group.pk)
                    or priced.get(group.pk)
                    or lines.get(group.pk)
                ),
            }
            for group in groups
        ],
        "most_spots": MOST_SPOTS,
        "most_spots_at_once": MOST_SPOTS_AT_ONCE,
    }


def _counts_in(rows: Any, key: str) -> dict[int, int]:
    return {
        pk: n
        for pk, n in rows.values_list(key).annotate(n=Count("pk"))
        if pk is not None
    }


# --- Spaces ---------------------------------------------------------------------


def _space_values(fields: Fields, *, new: bool) -> dict[str, Any]:
    _only(
        fields,
        {"name", "description", "booking_page"},
        required={"name"} if new else set(),
    )
    found: dict[str, Any] = {}
    if "name" in fields:
        found["name"] = _name(fields["name"])
    if "description" in fields:
        found["description"] = _text(fields["description"])
    if "booking_page" in fields:
        page = fields["booking_page"]
        if not isinstance(page, str) or page not in BookingPage.values:
            raise LayoutRefusedError("unknown-booking-page")
        found["booking_page"] = page
    return found


def _name_free(
    model: type[Owned],
    org: Organization,
    name: str,
    own: int,
    **within: object,
) -> None:
    """A name is given once in its list, exactly as the setup file and the
    database hold it."""
    others = model.objects.of(org).filter(name=name, **within).exclude(pk=own)  # type: ignore[misc]
    if others.exists():
        raise LayoutRefusedError("name-taken")


def make_space(administrator: Any, fields: Fields) -> int | None:
    values = _space_values(fields, new=True)

    def apply(org: Organization) -> None:
        _name_free(Space, org, values["name"], 0)
        row = Space(organization=org, **values)
        _checked(row, exclude=("number",))
        _saved(row)
        record_event(actor=administrator, action=ACTIONS[Space][0], target=row)

    organization_transaction(administrator.organization_id, apply)
    return None


def change_space(administrator: Any, pk: int, fields: Fields) -> int | None:
    values = _space_values(fields, new=False)
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> None:
        row = _lock(Space, org, pk)
        if all(getattr(row, key) == value for key, value in values.items()):
            return
        if "name" in values:
            _name_free(Space, org, values["name"], row.pk)
        record_event(actor=administrator, action=ACTIONS[Space][1], target=row)
        for key, value in values.items():
            setattr(row, key, value)
        _checked(row, exclude=("number",))
        _saved(row)

    organization_transaction(administrator.organization_id, apply)
    return None


def remove_space(administrator: Any, pk: int) -> int | None:
    def apply(org: Organization) -> None:
        row = _lock(Space, org, pk)
        assets = Asset.objects.of(org).filter(space=row).count()
        if assets:
            raise LayoutRefusedError("in-use", {"assets": assets})
        record_event(actor=administrator, action=ACTIONS[Space][2], target=row)
        try:
            row.delete()
        except ProtectedError:
            raise LayoutRefusedError("in-use") from None

    organization_transaction(administrator.organization_id, apply)
    return None


# --- Assets ---------------------------------------------------------------------


def _asset_values(fields: Fields, *, new: bool) -> dict[str, Any]:
    _only(
        fields,
        {
            "name",
            "description",
            "space",
            "whole_space",
            "maximum_occupancy",
            "time_zone",
        },
        required={"name"} if new else set(),
    )
    found: dict[str, Any] = {}
    if "name" in fields:
        found["name"] = _name(fields["name"])
    if "description" in fields:
        found["description"] = _text(fields["description"])
    if "space" in fields:
        found["space"] = _id_or_none(fields["space"])
    if "whole_space" in fields:
        found["whole_space"] = _flag(fields["whole_space"])
    if "maximum_occupancy" in fields:
        found["maximum_occupancy"] = _id_or_none(fields["maximum_occupancy"])
        if found["maximum_occupancy"] is not None and found["maximum_occupancy"] < 1:
            raise LayoutRefusedError("invalid-occupancy")
    if "time_zone" in fields:
        zone = fields["time_zone"]
        if not isinstance(zone, str):
            raise ValueError("A time zone is text.")
        found["time_zone"] = zone.strip()
    return found


def _space_of(org: Organization, pk: object) -> Space | None:
    if pk is None:
        return None
    space = Space.objects.of(org).select_for_update(no_key=True).filter(pk=pk).first()  # type: ignore[misc]
    if space is None:
        raise LayoutRefusedError("unknown-space")
    return space


def _set_asset(row: Asset, values: dict[str, Any], space: Space | None) -> None:
    for key, value in values.items():
        setattr(row, key, space if key == "space" else value)


def make_asset(administrator: Any, fields: Fields) -> int | None:
    values = _asset_values(fields, new=True)

    def apply(org: Organization) -> None:
        _name_free(Asset, org, values["name"], 0)
        row = Asset(organization=org)
        _set_asset(row, values, _space_of(org, values.get("space")))
        _checked(row, exclude=("number",))
        _saved(row)
        record_event(actor=administrator, action=ACTIONS[Asset][0], target=row)

    organization_transaction(administrator.organization_id, apply)
    return None


def change_asset(administrator: Any, pk: int, fields: Fields) -> int | None:
    values = _asset_values(fields, new=False)
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> None:
        row = _lock(Asset, org, pk)
        space = _space_of(org, values.get("space")) if "space" in values else None
        wanted = dict(values)
        if "space" in values:
            wanted["space"] = space.pk if space else None
        # An empty time zone is the organization's, which the model fills in.
        if wanted.get("time_zone") == "":
            wanted["time_zone"] = org.time_zone
        current = {
            key: (row.space_id if key == "space" else getattr(row, key))
            for key in values
        }
        if wanted == current:
            return
        if "name" in values:
            _name_free(Asset, org, values["name"], row.pk)
        record_event(actor=administrator, action=ACTIONS[Asset][1], target=row)
        _set_asset(row, values, space)
        _checked(row, exclude=("number",))
        _saved(row)

    organization_transaction(administrator.organization_id, apply)
    return None


def remove_asset(administrator: Any, pk: int) -> int | None:
    """Remove an asset nothing holds: no container, no availability and no
    booking or block, also a past one. Its floor plans, spots and groups go
    with it."""

    def apply(org: Organization) -> None:
        row = _lock(Asset, org, pk)
        held = _held_asset(org, row)
        if held:
            raise LayoutRefusedError("in-use", held)
        record_event(actor=administrator, action=ACTIONS[Asset][2], target=row)
        try:
            row.delete()
        except ProtectedError, RestrictedError, IntegrityError:
            raise LayoutRefusedError("in-use") from None

    organization_transaction(administrator.organization_id, apply)
    return None


# --- Floor plans ------------------------------------------------------------------


def _name_beside_siblings(
    org: Organization, asset_id: int, own: int, name: str
) -> None:
    """The model's rule for the names of one asset's floor plans
    (`floor_plan_rule`), with its own words for each refusal."""
    siblings = list(
        AssetInventory.objects.of(org)
        .filter(asset=asset_id)
        .exclude(pk=own)
        .values_list("name", flat=True)
    )
    fault = floor_plan_name_fault(name, siblings)
    if fault == NEEDS_NAME:
        raise LayoutRefusedError("name-needed")
    if fault == NAME_USED:
        raise LayoutRefusedError("name-taken")
    if fault == SIBLING_UNNAMED:
        raise LayoutRefusedError("other-unnamed")


def make_floor_plan(administrator: Any, asset_pk: int, fields: Fields) -> int | None:
    """Add a floor plan to an asset, with its spots numbered from 1."""
    _only(fields, {"name", "word", "spots"}, required={"word", "spots"})
    name = _optional_name(fields.get("name", ""))
    word = _word(fields["word"])
    count = _whole(fields["spots"])
    if not 1 <= count <= MOST_SPOTS:
        raise LayoutRefusedError("too-many-spots", message=TOO_MANY_SPOTS)

    def apply(org: Organization) -> int:
        asset = _lock(Asset, org, asset_pk)
        _name_beside_siblings(org, asset.pk, 0, name)
        row = AssetInventory(
            organization=org, asset=asset, name=name, unit=word, capacity=count
        )
        _checked(row)
        _saved(row)
        record_event(actor=administrator, action=ACTIONS[AssetInventory][0], target=row)
        return row.pk

    return organization_transaction(administrator.organization_id, apply)


def change_floor_plan(administrator: Any, pk: int, fields: Fields) -> int | None:
    _only(fields, {"name", "word"}, required=set())
    values: dict[str, Any] = {}
    if "name" in fields:
        values["name"] = _optional_name(fields["name"])
    if "word" in fields:
        values["unit"] = _word(fields["word"])
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> int:
        row = _lock(AssetInventory, org, pk)
        if all(getattr(row, key) == value for key, value in values.items()):
            return row.pk
        if "name" in values:
            _name_beside_siblings(org, row.asset_id, row.pk, values["name"])
        record_event(actor=administrator, action=ACTIONS[AssetInventory][1], target=row)
        for key, value in values.items():
            setattr(row, key, value)
        _checked(row)
        _saved(row)
        return row.pk

    return organization_transaction(administrator.organization_id, apply)


def remove_floor_plan(administrator: Any, pk: int) -> int | None:
    """Remove a floor plan no container books and no booking or block holds a
    spot of, and that is not its asset's only one (an asset has one or more).
    Its spots and groups go with it."""

    def apply(org: Organization) -> None:
        row = _lock(AssetInventory, org, pk)
        others = (
            AssetInventory.objects.of(org).filter(asset=row.asset_id).exclude(pk=pk)
        )
        if not others.exists():
            raise LayoutRefusedError("last-plan")
        held = _held_plan(org, row)
        if held:
            raise LayoutRefusedError("in-use", held)
        record_event(actor=administrator, action=ACTIONS[AssetInventory][2], target=row)
        try:
            row.delete()
        except ProtectedError, RestrictedError, IntegrityError:
            raise LayoutRefusedError("in-use") from None

    organization_transaction(administrator.organization_id, apply)
    return None


# --- Spots --------------------------------------------------------------------------


def _group_of(
    org: Organization, plan: AssetInventory, pk: int | None
) -> SpotGroup | None:
    if pk is None:
        return None
    group = _lock_or_none(SpotGroup, org, pk, floor_plan=plan)
    if group is None:
        raise LayoutRefusedError("unknown-group")
    return group


def _lock_or_none[T: Owned](
    model: type[T], org: Organization, pk: int, **where: object
) -> T | None:
    return (
        model.objects.of(org)
        .select_for_update(no_key=True)
        .filter(pk=pk, **where)
        .first()
    )


def _spot_name_free(
    org: Organization, plan: AssetInventory, name: str, own: int
) -> None:
    if not name:
        return
    other = (
        Spot.objects.of(org)
        .filter(floor_plan=plan, name=name)
        .exclude(pk=own)
        .values_list("number", flat=True)
        .first()
    )
    if other is not None:
        raise LayoutRefusedError("name-taken", {"spot": other})


def make_spots(administrator: Any, plan_pk: int, fields: Fields) -> int | None:
    """Add spots to a floor plan, each with the next number; all in one group
    when a group is given. The floor plan holds at most `MOST_SPOTS`."""
    _only(fields, {"count", "group"}, required={"count"})
    count = _whole(fields["count"])
    group_pk = _id_or_none(fields.get("group"))
    if not 1 <= count <= MOST_SPOTS_AT_ONCE:
        raise ValueError("The count is wrong.")

    def apply(org: Organization) -> int:
        plan = _lock(AssetInventory, org, plan_pk)
        group = _group_of(org, plan, group_pk)
        if plan.capacity + count > MOST_SPOTS:
            raise LayoutRefusedError("too-many-spots", message=TOO_MANY_SPOTS)
        for _ in range(count):
            row = Spot(organization=org, floor_plan=plan, group=group)
            _saved(row)
            record_event(actor=administrator, action=ACTIONS[Spot][0], target=row)
        return plan.pk

    return organization_transaction(administrator.organization_id, apply)


def change_spot(
    administrator: Any, plan_pk: int, pk: int, fields: Fields
) -> int | None:
    _only(fields, {"name", "group"}, required=set())
    if not fields:
        raise ValueError("The body is wrong.")
    name = _optional_name(fields["name"]) if "name" in fields else None
    group_pk = _id_or_none(fields["group"]) if "group" in fields else None

    def apply(org: Organization) -> int:
        plan = _lock(AssetInventory, org, plan_pk)
        row = _lock(Spot, org, pk, floor_plan=plan)
        group = _group_of(org, plan, group_pk) if "group" in fields else row.group
        wanted_name = row.name if name is None else name
        if wanted_name == row.name and (group.pk if group else None) == row.group_id:
            return plan.pk
        _spot_name_free(org, plan, wanted_name, row.pk)
        record_event(actor=administrator, action=ACTIONS[Spot][1], target=row)
        row.name = wanted_name
        row.group = group
        _checked(row, exclude=("number", "place", "floor_plan_capacity"))
        _saved(row)
        return plan.pk

    return organization_transaction(administrator.organization_id, apply)


def remove_spot(administrator: Any, plan_pk: int, pk: int) -> int | None:
    """Remove a spot no booking or block holds, also a past one, and that is
    not its floor plan's last. Its number stays a gap and is never given
    again; the last spot takes its place and the capacity falls by one, in
    this transaction (`Spot.place`)."""

    def apply(org: Organization) -> int:
        plan = _lock(AssetInventory, org, plan_pk)
        row = _lock(Spot, org, pk, floor_plan=plan)
        if Spot.objects.of(org).filter(floor_plan=plan).count() < 2:
            raise LayoutRefusedError("last-spot")
        claims = _claims_of_spot(org, row)
        if claims:
            raise LayoutRefusedError("in-use", {"bookings": claims})
        record_event(actor=administrator, action=ACTIONS[Spot][2], target=row)
        freed = row.place
        last = (
            Spot.objects.of(org)
            .select_for_update(no_key=True)
            .filter(floor_plan=plan, place=plan.capacity)
            .exclude(pk=row.pk)
            .first()
        )
        try:
            row.delete()
        except ProtectedError, RestrictedError, IntegrityError:
            raise LayoutRefusedError("in-use") from None
        if last is not None:
            Spot.objects.of(org).filter(pk=last.pk).update(place=freed)
        AssetInventory.objects.of(org).filter(pk=plan.pk).update(
            capacity=F("capacity") - 1
        )
        return plan.pk

    return organization_transaction(administrator.organization_id, apply)


# --- Groups of spots ------------------------------------------------------


def _group_values(fields: Fields, *, new: bool) -> dict[str, Any]:
    _only(
        fields,
        {"name", "word", "spots"},
        required={"name"} if new else set(),
    )
    found: dict[str, Any] = {}
    if "name" in fields:
        found["name"] = _name(fields["name"])
    if "word" in fields:
        found["word"] = _word(fields["word"])
    if "spots" in fields:
        found["spots"] = _ids(fields["spots"])
    return found


def _spots_of(org: Organization, plan: AssetInventory, ids: list[int]) -> list[Spot]:
    wanted = set(ids)
    chosen = list(
        Spot.objects.of(org)
        .select_for_update(no_key=True)
        .filter(floor_plan=plan, pk__in=wanted)
    )
    if len(chosen) != len(wanted):
        raise LayoutRefusedError("unknown-spot")
    return chosen


def make_group(administrator: Any, plan_pk: int, fields: Fields) -> int | None:
    values = _group_values(fields, new=True)

    def apply(org: Organization) -> int:
        plan = _lock(AssetInventory, org, plan_pk)
        _name_free(SpotGroup, org, values["name"], 0, floor_plan=plan)
        chosen = _spots_of(org, plan, values.get("spots", []))
        row = SpotGroup(
            organization=org,
            floor_plan=plan,
            name=values["name"],
            word=values.get("word", "group"),
        )
        _checked(row, exclude=("number",))
        _saved(row)
        Spot.objects.of(org).filter(pk__in=[s.pk for s in chosen]).update(group=row)
        record_event(actor=administrator, action=ACTIONS[SpotGroup][0], target=row)
        return plan.pk

    return organization_transaction(administrator.organization_id, apply)


def change_group(
    administrator: Any, plan_pk: int, pk: int, fields: Fields
) -> int | None:
    """Rename a group, change its word, or set the spots it holds (the list is
    the group's spots after the change: a spot in another group moves, a spot
    left out leaves)."""
    values = _group_values(fields, new=False)
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> int:
        plan = _lock(AssetInventory, org, plan_pk)
        row = _lock(SpotGroup, org, pk, floor_plan=plan)
        mine = set(
            Spot.objects.of(org)
            .select_for_update(no_key=True)
            .filter(group=row)
            .values_list("pk", flat=True)
        )
        wanted = (
            {s.pk for s in _spots_of(org, plan, values["spots"])}
            if "spots" in values
            else mine
        )
        same = (
            wanted == mine
            and values.get("name", row.name) == row.name
            and values.get("word", row.word) == row.word
        )
        if same:
            return plan.pk
        if "name" in values:
            _name_free(SpotGroup, org, values["name"], row.pk, floor_plan=plan)
        record_event(actor=administrator, action=ACTIONS[SpotGroup][1], target=row)
        row.name = values.get("name", row.name)
        row.word = values.get("word", row.word)
        _checked(row, exclude=("number",))
        _saved(row)
        Spot.objects.of(org).filter(pk__in=mine - wanted).update(group=None)
        Spot.objects.of(org).filter(pk__in=wanted - mine).update(group=row)
        return plan.pk

    return organization_transaction(administrator.organization_id, apply)


def remove_group(administrator: Any, plan_pk: int, pk: int) -> int | None:
    """Remove a group no spot is in and no price or booking line names."""

    def apply(org: Organization) -> int:
        plan = _lock(AssetInventory, org, plan_pk)
        row = _lock(SpotGroup, org, pk, floor_plan=plan)
        held = _held_group(org, row)
        if held.get("spots"):
            raise LayoutRefusedError("has-spots", {"spots": held["spots"]})
        if held:
            raise LayoutRefusedError("in-use", held)
        record_event(actor=administrator, action=ACTIONS[SpotGroup][2], target=row)
        try:
            row.delete()
        except ProtectedError, RestrictedError, IntegrityError:
            raise LayoutRefusedError("in-use") from None
        return plan.pk

    return organization_transaction(administrator.organization_id, apply)


__all__ = [
    "LayoutRefusedError",
    "SetupNotFoundError",
    "assets_place",
    "change_asset",
    "change_floor_plan",
    "change_group",
    "change_space",
    "change_spot",
    "make_asset",
    "make_floor_plan",
    "make_group",
    "make_space",
    "make_spots",
    "plan_place",
    "remove_asset",
    "remove_floor_plan",
    "remove_group",
    "remove_space",
    "remove_spot",
]
