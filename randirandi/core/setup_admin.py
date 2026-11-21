# SPDX-License-Identifier: AGPL-3.0-only
"""Booking types, booking categories and roles, as an administrator sets them
.

Every change runs in `organization_transaction`: the organization's row is
locked, so two administrators changing the lists, or the same role, take turns
and the second works on what the first left. A change writes its audit event
first , in the same transaction; a removal's event is written before
the delete. The rules are the model's own: a row is checked with `full_clean`
as the setup file's loader checks it, and what the setup file would refuse
(a role with no booking category) is refused here, so an export made after any
change on the screens reads and loads again.
"""

from collections.abc import Callable, Mapping
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.models import Count
from django.db.models.deletion import ProtectedError

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import gained_admin_access
from randirandi.core.models import (
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    ConfigurationCategory,
    Member,
    MemberStatus,
    Organization,
    PriceBookEntry,
    Role,
    RoleCategory,
)
from randirandi.limits import CATEGORY_ICONS, name_fault, same_name

type Item = BookingType | BookingCategory
type ItemModel = type[BookingType] | type[BookingCategory]
type Fields = Mapping[str, object]

# What the screens call each list; the address of its calls.
LISTS: dict[str, ItemModel] = {
    "booking-types": BookingType,
    "booking-categories": BookingCategory,
}
ACTIONS = {
    BookingType: (
        AuditAction.MADE_BOOKING_TYPE,
        AuditAction.CHANGED_BOOKING_TYPE,
        AuditAction.REMOVED_BOOKING_TYPE,
    ),
    BookingCategory: (
        AuditAction.MADE_BOOKING_CATEGORY,
        AuditAction.CHANGED_BOOKING_CATEGORY,
        AuditAction.REMOVED_BOOKING_CATEGORY,
    ),
    Role: (
        AuditAction.MADE_ROLE,
        AuditAction.CHANGED_ROLE,
        AuditAction.REMOVED_ROLE,
    ),
}
DIRECTIONS = ("up", "down")


class SetupRefusedError(Exception):
    """The model forbids the change. `code` is the refusal's fixed word; `used`
    says what holds a row that cannot be removed; `reason` is the model's own
    sentence where the refusal comes from a model rule."""

    def __init__(
        self,
        code: str,
        used: dict[str, int] | None = None,
        reason: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.used = used or {}
        # More the answer carries (7.99: the numbers a retention save names).
        self.details = details or {}
        # The model's own words for the refusal, where it has them (7.97).
        self.reason = reason


class SetupNotFoundError(Exception):
    """The row is not one of the organization's."""


def organization_transaction[T](
    organization_id: int, action: Callable[[Organization], T]
) -> T:
    """Run `action` with the organization's row locked, in its own transaction.

    Every change here takes that lock first, so two administrators take turns
    and the second reads what the first left (READ COMMITTED, each statement
    sees the latest commit): there is no snapshot to go stale and nothing to
    retry. The imports and checkouts take the same lock.
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("A setup change needs its own transaction.")
    with transaction.atomic():
        organization = Organization.objects.select_for_update(no_key=True).get(
            pk=organization_id
        )
        return action(organization)


# --- What the places show -----------------------------------------------------


def _used_by_type(org: Organization) -> dict[int, dict[str, int]]:
    containers = dict(
        BookingConfiguration.objects.of(org)
        .values_list("booking_type")
        .annotate(n=Count("pk"))
    )
    bookings = dict(
        Booking.objects.of(org).values_list("booking_type").annotate(n=Count("pk"))
    )
    return {
        pk: {"containers": containers.get(pk, 0), "bookings": bookings.get(pk, 0)}
        for pk in BookingType.objects.of(org).values_list("pk", flat=True)
    }


def _used_by_category(org: Organization) -> dict[int, dict[str, int]]:
    def counts(model: type[models.Model]) -> dict[int, int]:
        rows = model.objects.of(org).values_list("booking_category")  # type: ignore[attr-defined]
        return dict(rows.annotate(n=Count("pk")))

    containers = counts(ConfigurationCategory)
    roles = counts(RoleCategory)
    prices = counts(PriceBookEntry)
    bookings = counts(Booking)
    return {
        pk: {
            "roles": roles.get(pk, 0),
            "containers": containers.get(pk, 0),
            "prices": prices.get(pk, 0),
            "bookings": bookings.get(pk, 0),
        }
        for pk in BookingCategory.objects.of(org).values_list("pk", flat=True)
    }


def types_place(org: Organization) -> dict[str, object]:
    """Both lists in the order members see them, with what holds each row (a
    row with anything held is not removable) and, for a category, the roles
    that book it and its icon."""
    held = _used_by_type(org)
    held_category = _used_by_category(org)
    roles: dict[int, list[str]] = {}
    for category_id, name in (
        RoleCategory.objects.of(org)
        .order_by("role_id")
        .values_list("booking_category", "role__name")
    ):
        roles.setdefault(category_id, []).append(name)
    return {
        "booking_types": [
            {
                "id": row.pk,
                "name": row.name,
                "description": row.description,
                "containers": held[row.pk]["containers"],
                "bookings": held[row.pk]["bookings"],
                "removable": not any(held[row.pk].values()),
            }
            for row in BookingType.objects.of(org)
        ],
        "booking_categories": [
            {
                "id": row.pk,
                "name": row.name,
                "icon": row.icon,
                "roles": roles.get(row.pk, []),
                "containers": held_category[row.pk]["containers"],
                "prices": held_category[row.pk]["prices"],
                "bookings": held_category[row.pk]["bookings"],
                "removable": not any(held_category[row.pk].values()),
            }
            for row in BookingCategory.objects.of(org)
        ],
        "icons": list(CATEGORY_ICONS),
    }


def roles_place(org: Organization) -> dict[str, object]:
    """The roles with their booking categories (ids, in the categories' order),
    admin access, whether each is the standard role and how many members have
    it; and the categories a role may be given."""
    members = dict(Member.objects.of(org).values_list("role").annotate(n=Count("pk")))
    given: dict[int, list[int]] = {}
    for role_id, category_id in RoleCategory.objects.of(org).values_list(
        "role", "booking_category"
    ):
        given.setdefault(role_id, []).append(category_id)
    order = {
        pk: place
        for place, pk in enumerate(
            BookingCategory.objects.of(org).values_list("pk", flat=True)
        )
    }
    return {
        "roles": [
            {
                "id": role.pk,
                "name": role.name,
                "categories": sorted(given.get(role.pk, []), key=order.__getitem__),
                "admin_access": role.admin_access,
                "standard": role.pk == org.standard_role_id,
                "members": members.get(role.pk, 0),
            }
            for role in Role.objects.of(org).order_by("pk")
        ],
        "booking_categories": [
            {"id": row.pk, "name": row.name} for row in BookingCategory.objects.of(org)
        ],
    }


# --- The rules ----------------------------------------------------------------


def _name(value: object) -> str:
    """A name as the setup file keeps it (`limits.name_fault`, the one rule the
    file reader, these screens and the column share), after trimming."""
    if not isinstance(value, str):
        raise ValueError("A name is text.")
    name = value.strip()
    if name_fault(name):
        raise SetupRefusedError("invalid-name")
    return name


def _taken(model: type[models.Model], org: Organization, name: str, own: int) -> None:
    """Names are unique in a list, whatever their case (`limits.same_name`)."""
    others = model.objects.of(org).exclude(pk=own).values_list("name", flat=True)  # type: ignore[attr-defined]
    if any(same_name(name, other) for other in others):
        raise SetupRefusedError("name-taken")


def _checked(row: models.Model) -> None:
    """The model's own validation, as the setup file's loader applies it."""
    try:
        row.full_clean()
    except ValidationError as error:
        fields = set(getattr(error, "error_dict", {}))
        if "name" in fields:
            raise SetupRefusedError(
                "name-taken" if _is_unique(error) else "invalid-name"
            ) from None
        if "icon" in fields:
            raise SetupRefusedError("unknown-icon") from None
        raise SetupRefusedError("invalid") from None


def _is_unique(error: ValidationError) -> bool:
    return any(e.code == "unique" for e in error.error_dict.get("name", []))


def _only(fields: Fields, allowed: set[str], *, required: set[str]) -> None:
    if set(fields) - allowed or required - set(fields):
        raise ValueError("The body is wrong.")


def _fields_of(model: ItemModel, fields: Fields, *, new: bool) -> dict[str, Any]:
    """The values a body gives for a booking type or category, checked."""
    if model is BookingType:
        _only(fields, {"name", "description"}, required={"name"} if new else set())
        found: dict[str, Any] = {}
        if "name" in fields:
            found["name"] = _name(fields["name"])
        if "description" in fields:
            description = fields["description"]
            if not isinstance(description, str):
                raise ValueError("A description is text.")
            found["description"] = description.strip()
        return found
    _only(fields, {"name", "icon"}, required={"name"} if new else set())
    found = {}
    if "name" in fields:
        found["name"] = _name(fields["name"])
    if "icon" in fields:
        icon = fields["icon"]
        if not isinstance(icon, str):
            raise ValueError("An icon is text.")
        if icon and icon not in CATEGORY_ICONS:
            raise SetupRefusedError("unknown-icon")
        found["icon"] = icon
    return found


def _renumber(model: ItemModel, org: Organization) -> None:
    """Positions 1, 2, 3 ... in the order the list has; what the setup file's
    loader gives them."""
    manager: Any = model.objects
    rows: list[Any] = list(
        manager.of(org).select_for_update().order_by("position", "pk")
    )
    changed = []
    for place, row in enumerate(rows, start=1):
        if row.position != place:
            row.position = place
            changed.append(row)
    if changed:
        manager.of(org).bulk_update(changed, ["position"])


def _item(model: ItemModel, org: Organization, pk: int) -> Item:
    row = model.objects.of(org).select_for_update().filter(pk=pk).first()
    if row is None:
        raise SetupNotFoundError
    return row


# --- Booking types and booking categories ---------------------------------------


def make_item(administrator: Member, model: ItemModel, fields: Fields) -> None:
    """Add a booking type or category at the end of its list."""
    values = _fields_of(model, fields, new=True)

    def apply(org: Organization) -> None:
        _taken(model, org, values["name"], 0)
        last = model.objects.of(org).aggregate(last=models.Max("position"))["last"]
        row = model(organization=org, position=(last or 0) + 1, **values)
        _checked(row)
        try:
            row.save()
        except IntegrityError:
            raise SetupRefusedError("invalid") from None
        _renumber(model, org)
        record_event(actor=administrator, action=ACTIONS[model][0], target=row)

    organization_transaction(administrator.organization_id, apply)


def change_item(
    administrator: Member, model: ItemModel, pk: int, fields: Fields
) -> None:
    """Rename a booking type or category, change a type's description or a
    category's icon. A change that changes nothing writes nothing."""
    values = _fields_of(model, fields, new=False)
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> None:
        row = _item(model, org, pk)
        if all(getattr(row, key) == value for key, value in values.items()):
            return
        if "name" in values:
            _taken(model, org, values["name"], row.pk)
        record_event(actor=administrator, action=ACTIONS[model][1], target=row)
        for key, value in values.items():
            setattr(row, key, value)
        _checked(row)
        try:
            row.save()
        except IntegrityError:
            raise SetupRefusedError("invalid") from None

    organization_transaction(administrator.organization_id, apply)


def move_item(administrator: Member, model: ItemModel, pk: int, direction: str) -> None:
    """Move a row one place up or down, as the list is now. At the end of the
    list nothing moves. Two administrators moving at once take turns, and the
    list is renumbered from 1 each time, so it is one valid order."""
    if direction not in DIRECTIONS:
        raise ValueError("The direction is up or down.")

    def apply(org: Organization) -> None:
        _renumber(model, org)
        manager: Any = model.objects
        rows: list[Any] = list(manager.of(org).order_by("position", "pk"))
        at = next((i for i, row in enumerate(rows) if row.pk == pk), None)
        if at is None:
            raise SetupNotFoundError
        to = at - 1 if direction == "up" else at + 1
        if not 0 <= to < len(rows):
            return
        record_event(actor=administrator, action=ACTIONS[model][1], target=rows[at])
        one, other = rows[at], rows[to]
        one.position, other.position = other.position, one.position
        manager.of(org).bulk_update([one, other], ["position"])

    organization_transaction(administrator.organization_id, apply)


def remove_item(administrator: Member, model: ItemModel, pk: int) -> None:
    """Remove a booking type or category nothing names: no container, role,
    price or booking (history keeps both)."""

    def apply(org: Organization) -> None:
        row = _item(model, org, pk)
        held = (_used_by_type(org) if model is BookingType else _used_by_category(org))[
            pk
        ]
        if any(held.values()):
            raise SetupRefusedError("in-use", {k: v for k, v in held.items() if v})
        record_event(actor=administrator, action=ACTIONS[model][2], target=row)
        try:
            row.delete()
        except ProtectedError:
            raise SetupRefusedError("in-use") from None
        _renumber(model, org)

    organization_transaction(administrator.organization_id, apply)


# --- Roles ----------------------------------------------------------------------


def _role_values(fields: Fields, *, new: bool) -> dict[str, Any]:
    _only(
        fields,
        {"name", "categories", "admin_access", "standard"},
        required={"name", "categories"} if new else set(),
    )
    found: dict[str, Any] = {}
    if "name" in fields:
        found["name"] = _name(fields["name"])
    for key in ("admin_access", "standard"):
        if key in fields:
            if type(fields[key]) is not bool:
                raise ValueError("Yes or no is true or false.")
            found[key] = fields[key]
    if "categories" in fields:
        given = fields["categories"]
        if not isinstance(given, list) or any(type(x) is not int for x in given):
            raise ValueError("Categories are a list of ids.")
        found["categories"] = given
    return found


def _categories_of(org: Organization, ids: list[int]) -> list[BookingCategory]:
    if not ids:
        raise SetupRefusedError("no-category")
    chosen = list(
        BookingCategory.objects.of(org).select_for_update().filter(pk__in=set(ids))
    )
    if len(chosen) != len(set(ids)):
        raise SetupRefusedError("unknown-category")
    return chosen


def _gain_admin_access(org: Organization, administrator: Member, role: Role) -> None:
    """Every member of the role comes to have admin access: one "gave admin
    access" event each, as a correction of one member writes (7.993)."""
    for member in Member.objects.of(org).filter(role=role).order_by("pk"):
        gained_admin_access(org, administrator, member)


def _keeps_an_administrator(org: Organization, role: Role) -> bool:
    """Whether an active administrator is left when `role` gives no admin
    access: the model's rule for a correction (`member_data._change_role`)."""
    if (
        not Member.objects.of(org)
        .filter(role=role, status=MemberStatus.ACTIVE)
        .exists()
    ):
        return True
    return (
        Member.objects.of(org)
        .filter(status=MemberStatus.ACTIVE, role__admin_access=True)
        .exclude(role=role)
        .exists()
    )


def make_role(administrator: Member, fields: Fields) -> None:
    """Add a role. It has no members yet, so nobody gains admin access."""
    values = _role_values(fields, new=True)

    def apply(org: Organization) -> None:
        admin = values.get("admin_access", False)
        standard = values.get("standard", False)
        if admin and standard:
            raise SetupRefusedError("standard-admin")
        _taken(Role, org, values["name"], 0)
        chosen = _categories_of(org, values["categories"])
        role = Role(organization=org, name=values["name"], admin_access=admin)
        _checked(role)
        try:
            role.save()
        except IntegrityError:
            raise SetupRefusedError("invalid") from None
        for category in chosen:
            RoleCategory.objects.create(
                organization=org, role=role, booking_category=category
            )
        if standard:
            org.standard_role = role
            org.save(update_fields=["standard_role"])
        record_event(actor=administrator, action=ACTIONS[Role][0], target=role)

    organization_transaction(administrator.organization_id, apply)


def change_role(administrator: Member, pk: int, fields: Fields) -> None:
    """Rename a role, change its booking categories, its admin access or which
    role is the standard one. The model's rules: the standard role never gives
    admin access (a member import refuses it) and stays set; taking admin
    access from a role leaves an active administrator; giving it writes one
    "gave admin access" event for each member of the role."""
    values = _role_values(fields, new=False)
    if not values:
        raise ValueError("The body is wrong.")

    def apply(org: Organization) -> None:
        role = Role.objects.of(org).select_for_update().filter(pk=pk).first()
        if role is None:
            raise SetupNotFoundError
        was_standard = role.pk == org.standard_role_id
        admin = values.get("admin_access", role.admin_access)
        standard = values.get("standard", was_standard)
        if admin and standard:
            raise SetupRefusedError("standard-admin")
        if was_standard and not standard:
            raise SetupRefusedError("standard-needed")
        if "name" in values:
            _taken(Role, org, values["name"], role.pk)
        current = set(
            RoleCategory.objects.of(org)
            .filter(role=role)
            .values_list("booking_category", flat=True)
        )
        wanted = current
        chosen: list[BookingCategory] = []
        if "categories" in values:
            chosen = _categories_of(org, values["categories"])
            wanted = {c.pk for c in chosen}
        elif not current:
            # A role with none cannot be read back from the setup file.
            raise SetupRefusedError("no-category")
        if role.admin_access and not admin and not _keeps_an_administrator(org, role):
            raise SetupRefusedError("last-administrator")
        gains = admin and not role.admin_access
        same = (
            values.get("name", role.name) == role.name
            and admin == role.admin_access
            and standard == was_standard
            and wanted == current
        )
        if same:
            return
        record_event(actor=administrator, action=ACTIONS[Role][1], target=role)
        role.name = values.get("name", role.name)
        role.admin_access = admin
        _checked(role)
        try:
            role.save()
        except IntegrityError:
            raise SetupRefusedError("invalid") from None
        if wanted != current:
            RoleCategory.objects.of(org).filter(role=role).exclude(
                booking_category__in=wanted
            ).delete()
            for category in chosen:
                if category.pk not in current:
                    RoleCategory.objects.create(
                        organization=org, role=role, booking_category=category
                    )
        if standard and not was_standard:
            org.standard_role = role
            org.save(update_fields=["standard_role"])
        if gains:
            _gain_admin_access(org, administrator, role)

    organization_transaction(administrator.organization_id, apply)


def remove_role(administrator: Member, pk: int) -> None:
    """Remove a role no member has and that is not the standard role."""

    def apply(org: Organization) -> None:
        role = Role.objects.of(org).select_for_update().filter(pk=pk).first()
        if role is None:
            raise SetupNotFoundError
        if role.pk == org.standard_role_id:
            raise SetupRefusedError("is-standard")
        count = Member.objects.of(org).filter(role=role).count()
        if count:
            raise SetupRefusedError("has-members", {"members": count})
        record_event(actor=administrator, action=ACTIONS[Role][2], target=role)
        try:
            role.delete()
        except ProtectedError:
            raise SetupRefusedError("has-members") from None

    organization_transaction(administrator.organization_id, apply)
