# SPDX-License-Identifier: AGPL-3.0-only
"""What a member can choose on the booking page .

Lists the assets, booking types and booking categories the member's role can
book, ordered as administrators set them, and says when the page asks for a
quantity and which containers are booked without payment (W12). Each type
lists its containers, each with its id, the most one booking can take, the
categories it is offered for and the lengths it allows, and each asset its
space, whose booking page shows one asset or the whole space . Reserves nothing and
reads no other member's data.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from randirandi.core import booking_words
from randirandi.core.calendar import offered_categories, valid_configuration
from randirandi.core.models import (
    BookingCategory,
    BookingConfiguration,
    ConsumptionMode,
    Member,
    MemberStatus,
    RoleCategory,
    Space,
    SpotGroup,
    Status,
)


@dataclass(frozen=True)
class CategoryChoice:
    id: int
    name: str
    # One of `CATEGORY_ICONS`, or "" for none.
    icon: str = ""


@dataclass(frozen=True)
class GroupChoice:
    """A group of a shared container's floor plan, which the Filter's "keep
    us together" can ask for (W20, W22)."""

    id: int
    number: int
    name: str
    # Its own word: "table", "section", "row".
    word: str
    # How many spots it has: the most one booking can keep together.
    spots: int
    # Its floor plan's name, to tell two groups of one name apart.
    plan: str


@dataclass(frozen=True)
class ContainerChoice:
    """One container the type is offered under, and whether its bookings
    are paid: one without payment is confirmed without Vipps."""

    id: int
    number: int
    with_payment: bool
    # The role's categories it is offered for.
    category_ids: tuple[int, ...]
    # The most one booking can take: its floor plan's spots, or one.
    max_quantity: int
    period_type: str
    consumption_mode: str
    # In minutes; for a fixed period all three are its length.
    interval: int
    minimum_length: int
    maximum_length: int
    # The floor plan's word for its spots ("seat", "bed"); None without a
    # written one (`booking_words.spot_word`).
    spot_word: str | None = None
    # A shared container's floor plan's groups, by number; none otherwise.
    groups: tuple[GroupChoice, ...] = ()


@dataclass(frozen=True)
class TypeChoice:
    id: int
    name: str
    description: str
    # The role's categories this type is offered for on the asset.
    category_ids: tuple[int, ...]
    # The categories for which a shared configuration has more than one unit,
    # so the page asks for a quantity (T2).
    quantity_category_ids: tuple[int, ...]
    # The containers it is offered under, by number.
    containers: tuple[ContainerChoice, ...] = ()


@dataclass(frozen=True)
class AssetChoice:
    id: int
    name: str
    description: str
    # The most units one booking can ask for; one when nothing is shared.
    max_quantity: int
    types: tuple[TypeChoice, ...]
    space_id: int | None = None


@dataclass(frozen=True)
class SpaceChoice:
    """A space of the listed assets, and what its booking page shows: one
    asset at a time or the whole space (W15)."""

    id: int
    name: str
    booking_page: str


@dataclass(frozen=True)
class BookingChoices:
    categories: tuple[CategoryChoice, ...]
    assets: tuple[AssetChoice, ...]
    spaces: tuple[SpaceChoice, ...] = ()
    # The category the Filter starts on: the member's last choice while the
    # role can still book it, else the first of `categories`; none without any.
    category: int | None = None


NOTHING = BookingChoices(categories=(), assets=())


def _spots(config: BookingConfiguration) -> int:
    """The most places one booking can take: the spots of a shared
    configuration's floor plan; one for an exclusive one, the whole asset."""
    if config.consumption_mode == ConsumptionMode.SHARED and config.floor_plan:
        return config.floor_plan.capacity
    return 1


def _read(member: Member, moment: datetime) -> BookingChoices:
    org_id = member.organization_id
    stored = (
        Member.objects.of(org_id)
        .filter(pk=member.pk, status=MemberStatus.ACTIVE)
        .first()
    )
    if stored is None:
        return NOTHING

    def offered_from_today(config: BookingConfiguration) -> bool:
        """Whether the container still starts from today, on its asset's
        clock."""
        return config.starts_from(moment.astimezone(config.asset.zone).date())

    allowed = set(
        RoleCategory.objects.of(org_id)
        .filter(role_id=stored.role_id)
        .values_list("booking_category_id", flat=True)
    )
    categories = tuple(
        CategoryChoice(category.pk, category.name, category.icon)
        for category in BookingCategory.objects.of(org_id).filter(pk__in=allowed)
    )
    configurations = [
        config
        for config in BookingConfiguration.objects.of(org_id)
        .filter(status=Status.ENABLED)
        .select_related("asset", "booking_type", "floor_plan")
        if valid_configuration(config)
    ]
    offered = offered_categories(org_id, stored.role_id, configurations)
    plans: dict[int, list[GroupChoice]] = defaultdict(list)
    for group in (
        SpotGroup.objects.of(org_id)
        .filter(
            floor_plan_id__in={
                config.floor_plan_id
                for config in configurations
                if config.consumption_mode == ConsumptionMode.SHARED
                and config.floor_plan_id is not None
            }
        )
        .annotate(size=Count("spots"))
        .order_by("floor_plan_id", "number")
        .values_list(
            "pk", "floor_plan_id", "number", "name", "word", "size", "floor_plan__name"
        )
    ):
        pk, plan_id, number, name, word, size, plan = group
        plans[plan_id].append(GroupChoice(pk, number, name, word, size, plan))
    groups: dict[tuple[int, int], list[BookingConfiguration]] = defaultdict(list)
    for config in configurations:
        if config.pk in offered:
            groups[config.asset_id, config.booking_type_id].append(config)
    on_asset = {configs[0].asset.pk: configs[0].asset for configs in groups.values()}
    assets = []
    for asset in sorted(on_asset.values(), key=lambda row: row.number):
        types = []
        for configs in sorted(
            (
                configs
                for (asset_id, _), configs in groups.items()
                if asset_id == asset.pk
            ),
            key=lambda group: (
                group[0].booking_type.position,
                group[0].booking_type_id,
            ),
        ):
            booking_type = configs[0].booking_type
            configs = [config for config in configs if offered_from_today(config)]
            if not configs:
                continue
            offered_ids = {pk for config in configs for pk in offered[config.pk]}
            shared_ids = {
                pk
                for config in configs
                if _spots(config) > 1
                for pk in offered[config.pk]
            }
            types.append(
                TypeChoice(
                    booking_type.pk,
                    booking_type.name,
                    booking_type.description,
                    tuple(row.id for row in categories if row.id in offered_ids),
                    tuple(row.id for row in categories if row.id in shared_ids),
                    tuple(
                        ContainerChoice(
                            config.pk,
                            config.number,
                            config.with_payment,
                            tuple(
                                row.id
                                for row in categories
                                if row.id in offered[config.pk]
                            ),
                            _spots(config),
                            config.period_type,
                            config.consumption_mode,
                            config.interval,
                            config.minimum_length,
                            config.maximum_length,
                            booking_words.spot_word(
                                config.floor_plan.unit if config.floor_plan else None
                            ),
                            tuple(plans[config.floor_plan_id])
                            if config.consumption_mode == ConsumptionMode.SHARED
                            and config.floor_plan_id is not None
                            else (),
                        )
                        for config in sorted(configs, key=lambda row: row.number)
                    ),
                )
            )
        if not types:
            continue
        max_quantity = max(
            _spots(config)
            for (asset_id, _), configs in groups.items()
            if asset_id == asset.pk
            for config in configs
            if offered_from_today(config)
        )
        assets.append(
            AssetChoice(
                asset.pk,
                asset.name,
                asset.description,
                max_quantity,
                tuple(types),
                asset.space_id,
            )
        )
    spaces = tuple(
        SpaceChoice(space.pk, space.name, space.booking_page)
        for space in Space.objects.of(org_id)
        .filter(pk__in={asset.space_id for asset in assets})
        .order_by("number")
    )
    first = next(
        (row.id for row in categories if row.id == stored.last_booking_category_id),
        categories[0].id if categories else None,
    )
    return BookingChoices(
        categories=categories, assets=tuple(assets), spaces=spaces, category=first
    )


def booking_choices(member: Member, *, now: datetime | None = None) -> BookingChoices:
    """The member's assets, booking types and categories, in their positions.

    Requires its own transaction: all reads share one REPEATABLE READ, READ
    ONLY snapshot. An inactive or foreign member gets nothing. Whether an
    offered time still fits the availability calendar is the calendar's
    question.
    """
    moment = now if now is not None else timezone.now()
    if timezone.is_naive(moment):
        raise ValueError("The current moment must include a time zone.")
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError(
            "The booking choices require their own read-only transaction."
        )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _read(member, moment)


def choose_category(member: Member, category_id: int) -> bool:
    """Keep the category the member chose in the Filter, when the member's
    role can book it. False, and nothing saved, for any other.

    One update of the member's own row and one column, which checks the role
    in the same statement; no lock. The role's link protects the category
    from deletion, so a category the update finds is still there. Not
    audited: it is the member's own preference.
    """
    offering = (
        RoleCategory.objects.of(member.organization_id)
        .filter(booking_category_id=category_id)
        .values("role_id")
    )
    saved = (
        Member.objects.of(member.organization_id)
        .filter(pk=member.pk, status=MemberStatus.ACTIVE, role_id__in=offering)
        .update(last_booking_category=category_id)
    )
    return saved == 1
