# SPDX-License-Identifier: AGPL-3.0-only
"""The space rule is one rule (W17, W21): `kept_apart` for queries over
assets and the database rule `capacity_no_double_booking` for claims must
agree. One table of cases runs through both and fails when they differ.
"""

from dataclasses import dataclass
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from randirandi.core.models import (
    Asset,
    BookingConfiguration,
    ConsumptionMode,
    Space,
    Status,
)
from randirandi.core.models.bookable import kept_apart
from randirandi.space_rule import (
    NEEDS_SPACE,
    SPACE_ALREADY_MARKED,
    space_mark_fault,
)
from tests import made_up
from tests.test_floor_plans import constraint_of


@dataclass(frozen=True)
class Claim:
    asset: str
    exclusive: bool = False
    spot: int = 1


# The assets: "washer" and "dryer" share the room, "room" is the one marked
# as covering it, "copy" and "porch" are in another space, "alone" has none.
ASSETS = {
    "washer": ("laundry", False),
    "dryer": ("laundry", False),
    "room": ("laundry", True),
    "copy": ("garden", False),
    "porch": ("garden", False),
    "alone": (None, False),
}

CASES = {
    "the same asset, the same spot": (Claim("washer"), Claim("washer")),
    "the same asset, another spot": (Claim("washer"), Claim("washer", spot=2)),
    "the same asset, exclusive and shared": (
        Claim("washer", exclusive=True),
        Claim("washer"),
    ),
    "the same asset, both exclusive": (
        Claim("washer", exclusive=True),
        Claim("washer", exclusive=True),
    ),
    "two assets of one space, neither marked": (Claim("washer"), Claim("dryer")),
    "two assets of one space, exclusive": (
        Claim("washer", exclusive=True),
        Claim("dryer", exclusive=True),
    ),
    "one marked, shared": (Claim("room"), Claim("washer")),
    "one marked, the other way round": (Claim("washer"), Claim("room")),
    "one marked, another spot": (Claim("room"), Claim("dryer", spot=2)),
    "one marked, exclusive and shared": (Claim("room", exclusive=True), Claim("dryer")),
    "the marked asset twice, another spot": (Claim("room"), Claim("room", spot=2)),
    "assets of different spaces": (Claim("washer"), Claim("copy")),
    "unmarked assets of another space": (Claim("copy"), Claim("porch")),
    "marked beside another space": (Claim("room"), Claim("porch")),
    "an asset without a space": (Claim("alone"), Claim("washer")),
    "an asset without a space, exclusive": (
        Claim("alone", exclusive=True),
        Claim("room", exclusive=True),
    ),
}


def predicted(assets: dict[str, Asset], first: Claim, second: Claim) -> bool:
    """Whether `kept_apart`, with the spot rule, says the two claims at one
    time are refused."""
    one, other = assets[first.asset], assets[second.asset]
    if (
        not Asset.objects.of(one.organization_id)
        .filter(kept_apart(one), pk=other.pk)
        .exists()
    ):
        return False
    if one.pk != other.pk:
        return True
    return first.exclusive or second.exclusive or first.spot == second.spot


@pytest.mark.django_db
def test_kept_apart_and_the_database_rule_agree() -> None:
    made = made_up.everything()
    spaces = {
        name: Space.objects.create(organization=made.organization, name=name)
        for name in ("laundry", "garden")
    }
    exclusive_container: dict[str, BookingConfiguration] = {}
    shared_container: dict[str, BookingConfiguration] = {}
    assets: dict[str, Asset] = {}
    for name, (space, marked) in ASSETS.items():
        # Disabled: only their claims matter here, and the database rule is
        # the one that refuses a claim.
        shared = made.shared(
            name,
            3,
            space=spaces[space] if space else None,
            whole_space=marked,
            status=Status.DISABLED,
        )
        shared_container[name] = shared
        assets[name] = shared.asset
        exclusive = BookingConfiguration.objects.of(made.organization).get(pk=shared.pk)
        exclusive.pk = None
        exclusive.number = None  # type: ignore[assignment]
        exclusive._state.adding = True
        exclusive.consumption_mode = ConsumptionMode.EXCLUSIVE
        exclusive.floor_plan = None
        exclusive.save()
        exclusive_container[name] = exclusive

    def hold(claim: Claim, hour: int) -> None:
        container = (exclusive_container if claim.exclusive else shared_container)[
            claim.asset
        ]
        units = (
            None
            if claim.exclusive
            else made_up.spots_of(container.floor_plan, claim.spot)  # type: ignore[arg-type]
        )
        made.block(hour, hour + 2, configuration=container, units=units)

    for label, (first, second) in CASES.items():
        # A Monday of its own for each case, from the first.
        made.day = made_up.A_DAY + timedelta(weeks=list(CASES).index(label))
        hour = 10
        hold(first, hour)
        try:
            with transaction.atomic():
                hold(second, hour)
            refused = False
        except IntegrityError as error:
            assert constraint_of(pytest.ExceptionInfo.from_exception(error)) == (
                "capacity_no_double_booking"
            ), label
            refused = True
        assert refused == predicted(assets, first, second), label
    # Every kind of answer is in the table.
    answers = {predicted(assets, *pair) for pair in CASES.values()}
    assert answers == {True, False}


@pytest.mark.django_db
def test_a_second_mark_in_one_space_is_refused_everywhere() -> None:
    made = made_up.everything()
    space = Space.objects.create(organization=made.organization, name="laundry")
    made.shared("room", 2, space=space, whole_space=True)
    other = made.shared("hall", 2, space=space, status=Status.DISABLED).asset

    with pytest.raises(ValidationError) as checked:
        other.whole_space = True
        other.save()
    assert "whole_space" in checked.value.message_dict
    with pytest.raises(IntegrityError) as stored, transaction.atomic():
        Asset.objects.of(made.organization).filter(pk=other.pk).update(whole_space=True)
    assert constraint_of(stored) == "asset_whole_space_once"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("in_space", "marked_here", "marked_elsewhere"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (True, False, True),
        (True, True, True),
    ],
)
def test_the_mark_rule_and_the_database_agree(
    in_space: bool, marked_here: bool, marked_elsewhere: bool
) -> None:
    # `space_mark_fault` and the database's own rules on the mark
    # (`asset_whole_space_once`, a mark needs a space) answer alike.
    made = made_up.everything()
    laundry = Space.objects.create(organization=made.organization, name="laundry")
    porch = Space.objects.create(organization=made.organization, name="porch")
    if marked_here:
        made.shared("room", 2, space=laundry, whole_space=True)
    if marked_elsewhere:
        made.shared("copy", 2, space=porch, whole_space=True)
    asset = made.shared(
        "hall", 2, space=laundry if in_space else None, status=Status.DISABLED
    ).asset
    marked = set(
        Asset.objects.of(made.organization)
        .filter(whole_space=True)
        .values_list("space_id", flat=True)
    )
    predicted = (
        space_mark_fault(whole_space=True, space=asset.space_id, marked_spaces=marked)
        is not None
    )
    try:
        with transaction.atomic():
            Asset.objects.of(made.organization).filter(pk=asset.pk).update(
                whole_space=True
            )
        refused = False
    except IntegrityError:
        refused = True
    assert refused == predicted


@pytest.mark.parametrize(
    ("whole_space", "space", "marked", "fault"),
    [
        (False, None, set(), None),
        (False, "room", {"room"}, None),
        (True, None, set(), NEEDS_SPACE),
        (True, "", set(), NEEDS_SPACE),
        (True, "room", set(), None),
        (True, "room", {"room"}, SPACE_ALREADY_MARKED),
        (True, "room", {"hall"}, None),
    ],
)
def test_who_may_carry_the_mark(
    whole_space: bool, space: str | None, marked: set[str], fault: str | None
) -> None:
    assert (
        space_mark_fault(whole_space=whole_space, space=space, marked_spaces=marked)
        == fault
    )
