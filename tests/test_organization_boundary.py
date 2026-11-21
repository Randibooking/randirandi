# SPDX-License-Identifier: AGPL-3.0-only
"""Every object belongs to one organization, and nothing crosses between two
(docs/data-model.md, check 6; controls C12 and C13).

Tried over every object: a query has to name its organization, a query for
one organization returns none of another's rows, and the database refuses
every link from one organization's row to another's.
"""

from datetime import date, timedelta

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction

from randirandi.core.models import (
    AssetInventory,
    Authenticator,
    AvailabilityCalendar,
    AvailableSpan,
    BookingConfiguration,
    CapacityAllocation,
    CategoryHoursSpan,
    LedgerEntry,
    Order,
    Organization,
    OrganizationNotNamedError,
    Owned,
    Payment,
    Payout,
    PriceTier,
    SameOrganization,
    Spot,
    SpotGroup,
)
from tests import made_up

OWNED: list[type[Owned]] = [
    model
    for model in apps.get_app_config("core").get_models()
    if issubclass(model, Owned)
]


def links_of(model: type[Owned]) -> list[models.ForeignKey[Owned, Owned]]:
    """The model's links to other objects of the organization."""
    return [
        field
        for field in model._meta.get_fields()
        if isinstance(field, models.ForeignKey)
        and isinstance(field.related_model, type)
        and issubclass(field.related_model, Owned)
    ]


LINKS = [(model, link.name) for model in OWNED for link in links_of(model)]

# Two rows that would land on top of their row of the same kind when moved,
# and be refused by another rule first. They are given a value of their own.
OUT_OF_THE_WAY: dict[tuple[type[Owned], str], dict[str, int | str]] = {
    (AvailableSpan, "availability_calendar"): {"start_time": 7 * 60},
    (CategoryHoursSpan, "configuration_category"): {"start_time": 7 * 60},
    (PriceTier, "price_book_entry"): {"up_to": 12 * 60},
    (LedgerEntry, "ledger"): {"psp_reference": "out-of-the-way"},
    (Payout, "ledger"): {"psp_reference": "out-of-the-way"},
    (Spot, "floor_plan"): {"number": 99, "name": "Out of the way"},
    (SpotGroup, "floor_plan"): {"number": 99, "name": "Out of the way"},
}


def everything(
    subdomain: str = "harbourview", day: date = made_up.A_DAY
) -> made_up.MadeUp:
    return made_up.with_event(
        made_up.with_payout(
            made_up.with_trouble(
                made_up.with_closure(
                    made_up.with_category_hours(made_up.everything(subdomain, day))
                )
            )
        )
    )


def name_of(value: object) -> str:
    return value.__name__ if isinstance(value, type) else str(value)


def test_the_organization_has_forty_six_objects_and_three_link_tables() -> None:
    # The count was 40 before the event's five , was 45 after
    # them and is 46 with the booking conditions ;
    # the organization is one of them, and the three tables on top hold its
    # many-to-many links.
    assert len(OWNED) + 1 == 46 + 3


@pytest.mark.parametrize("model", OWNED, ids=name_of)
def test_a_query_that_names_no_organization_fails(model: type[Owned]) -> None:
    for query in (
        model.objects.all,
        model.objects.count,
        lambda: model.objects.filter(pk=1),
        lambda: model.objects.get(pk=1),
    ):
        with pytest.raises(OrganizationNotNamedError):
            query()


@pytest.mark.parametrize("model", OWNED, ids=name_of)
def test_a_new_row_needs_its_organization(model: type[Owned]) -> None:
    with pytest.raises(OrganizationNotNamedError):
        model.objects.create()


@pytest.mark.parametrize("model", OWNED, ids=name_of)
def test_django_reads_through_the_manager_that_is_named_as_crossing(
    model: type[Owned],
) -> None:
    # Following a link and checking uniqueness are Django's own reads. They
    # start from a row that is already inside one organization.
    assert model._default_manager.name == "across_organizations"
    assert type(model._default_manager) is models.Manager


@pytest.mark.parametrize("model", OWNED, ids=name_of)
def test_every_link_of_every_object_is_kept_inside_the_organization(
    model: type[Owned],
) -> None:
    kept = {
        constraint.link
        for constraint in model._meta.constraints
        if isinstance(constraint, SameOrganization)
    }
    assert kept == {link.name for link in links_of(model)}
    # What other tables point at: the organization and the id together.
    assert any(
        isinstance(constraint, models.UniqueConstraint)
        and tuple(constraint.fields) == ("organization", "id")
        for constraint in model._meta.constraints
    )
    # No many-to-many link goes through a table Django made by itself.
    for many in model._meta.many_to_many:
        through = many.remote_field.through
        assert through is not None and issubclass(through, Owned)


@pytest.mark.django_db
def test_the_database_holds_a_foreign_key_for_every_link() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'f' AND conname LIKE %s",
            ["%\\_same\\_org"],
        )
        in_the_database = {name for (name,) in cursor.fetchall()}

    wanted = {f"{model._meta.model_name}_{link}_same_org" for model, link in LINKS}
    wanted.add("organization_standard_role_same_org")
    assert in_the_database == wanted
    assert len(wanted) == 77


@pytest.mark.django_db
@pytest.mark.parametrize("model", OWNED, ids=name_of)
def test_a_query_for_one_organization_returns_none_of_anothers_rows(
    model: type[Owned],
) -> None:
    ours = everything("harbourview")
    theirs = everything("lakeside", made_up.A_DAY + timedelta(days=7))

    found = list(model.objects.of(ours.organization))

    assert ours.rows[model] in found
    assert theirs.rows[model] not in found
    assert {row.organization_id for row in found} == {ours.organization.pk}
    # By id works as well as by object.
    assert list(model.objects.of(ours.organization.pk)) == found


@pytest.mark.django_db
@pytest.mark.parametrize(("model", "link"), LINKS, ids=name_of)
def test_the_database_refuses_a_link_into_another_organization(
    model: type[Owned], link: str
) -> None:
    ours = everything("harbourview")
    theirs = everything("lakeside", made_up.A_DAY + timedelta(days=7))
    target = model._meta.get_field(link).related_model
    assert isinstance(target, type) and issubclass(target, Owned)
    # A payment's order is its own alone, so the try goes to an order that
    # has no payment yet. Otherwise that rule would refuse it first. The
    # same holds for a booking's and a block's taken time.
    theirs_row: Owned
    if (model, link) == (Payment, "order"):
        theirs_row = theirs.spare_order
    elif (model, link) == (Authenticator, "member"):
        # Use an unclaimed member so uniqueness does not mask the tenant FK.
        theirs_row = theirs.administrator
    elif link == "taken_time":
        theirs_row = theirs.spare_taken_time
    else:
        theirs_row = theirs.rows[target]
    source = ours.rows[model]
    if model is CapacityAllocation and link != "asset":
        # Only a shared claim names a spot, on a floor plan; its second one,
        # because their spare taken time holds its own first.
        shared = ours.shared("Benches", 2)
        assert shared.floor_plan is not None
        units = made_up.spots_of(shared.floor_plan, 1, 2)
        booked = ours.book(10, 12, configuration=shared, units=units)
        source = booked.taken_time.allocations.get(ordinal=2)
    row = model.across_organizations.filter(pk=source.pk)
    row.update(**OUT_OF_THE_WAY.get((model, link), {}))
    if (model, link) == (BookingConfiguration, "floor_plan"):
        # Only a shared container names a floor plan.
        BookingConfiguration.across_organizations.filter(pk=source.pk).update(
            consumption_mode="shared", floor_plan=made_up.floor_plan_of(ours.asset)
        )

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        row.update(**{link: theirs_row})

    cause = refused.value.__cause__
    assert cause is not None
    constraint = cause.diag.constraint_name  # type: ignore[attr-defined]
    protected_witnesses: dict[tuple[type[Owned], str], str] = {
        # A spot's group is on the spot's floor plan: that rule comes first.
        (Spot, "group"): "spot_group_on_its_floor_plan",
    }
    expected = protected_witnesses.get(
        (model, link), f"{model._meta.model_name}_{link}_same_org"
    )
    # Two rules that both span the organization refuse these. PostgreSQL
    # fires them in the order of its own trigger names, not of ours.
    either: dict[tuple[type[Owned], str], str] = {
        (Spot, "floor_plan"): "spot_capacity_bound",
        (Spot, "group"): "spot_group_same_org",
        (AssetInventory, "asset"): "floor_plan_counts_its_spots",
        (BookingConfiguration, "floor_plan"): "configuration_floor_plan_of_its_asset",
        (CapacityAllocation, "taken_time"): "allocation_claim_header",
        (CapacityAllocation, "asset"): "allocation_claim_header",
        (CapacityAllocation, "inventory"): "allocation_spot_on_its_floor_plan",
        (CapacityAllocation, "spot"): "allocation_spot_on_its_floor_plan",
        # The text and the moment come together; the text alone is refused by
        # that rule as well as by the organization's.
        (Order, "conditions"): "order_conditions_both_or_neither",
    }
    assert constraint in {expected, either.get((model, link), expected)}


@pytest.mark.django_db
def test_an_organizations_standard_role_is_one_of_its_own() -> None:
    ours = everything("harbourview")
    theirs = everything("lakeside", made_up.A_DAY + timedelta(days=7))

    with pytest.raises(IntegrityError), transaction.atomic():
        Organization.objects.filter(pk=ours.organization.pk).update(
            standard_role=theirs.standard
        )

    ours.organization.standard_role = theirs.standard
    with pytest.raises(ValidationError) as refused:
        ours.organization.full_clean()
    assert "standard_role" in refused.value.message_dict


@pytest.mark.django_db
def test_validation_refuses_a_link_into_another_organization_before_the_database() -> (
    None
):
    ours = everything("harbourview")
    theirs = everything("lakeside", made_up.A_DAY + timedelta(days=7))
    calendar = AvailabilityCalendar(
        organization=ours.organization,
        asset=theirs.asset,
        start_date=made_up.A_DAY + timedelta(days=30),
    )

    with pytest.raises(ValidationError) as refused:
        calendar.full_clean()

    assert refused.value.message_dict == {
        "asset": ["It belongs to another organization."]
    }


@pytest.mark.django_db
def test_a_row_cannot_be_moved_to_another_organization_with_what_points_to_it() -> None:
    ours = everything("harbourview")
    theirs = everything("lakeside", made_up.A_DAY + timedelta(days=7))

    with pytest.raises(IntegrityError), transaction.atomic():
        type(ours.asset).across_organizations.filter(pk=ours.asset.pk).update(
            organization=theirs.organization
        )
