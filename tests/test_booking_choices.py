# SPDX-License-Identifier: AGPL-3.0-only
"""what a member can choose on the booking page.

The approved made-up setups stand in for real organizations: a sauna club
(seats by day, whole evenings, several assets) and a guest bedroom whose
beds share stock with the room.
"""

import json
import re
from datetime import UTC, datetime, time, timedelta

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import Client

from randirandi.core.booking_choices import BookingChoices, booking_choices
from randirandi.core.calendar import BookableInterval, bookable_intervals
from randirandi.core.models import (
    Asset,
    AssetInventory,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    ConfigurationCategory,
    Member,
    MemberStatus,
    Organization,
    PriceBookEntry,
    PriceTier,
    Role,
    RoleCategory,
    Space,
    Status,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, ZONE, at, scenario
from tests.test_login_sessions import HOST, get, sign_in
from tests.test_login_sessions import page_settings as page_settings

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1))


def names(found: BookingChoices) -> dict[str, list[tuple[str, list[str], list[str]]]]:
    by_id = {row.id: row.name for row in found.categories}
    return {
        asset.name: [
            (
                row.name,
                [by_id[pk] for pk in row.category_ids],
                [by_id[pk] for pk in row.quantity_category_ids],
            )
            for row in asset.types
        ]
        for asset in found.assets
    }


def test_sauna_club_lists_assets_types_and_when_places_are_asked() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    found = booking_choices(member, now=NOW)
    assert [row.name for row in found.categories] == ["private"]
    sauna = next(asset for asset in found.assets if asset.name == "Sauna 1")
    assert sauna.max_quantity == 8
    assert names(found)["Sauna 1"] == [
        ("Sauna visit", ["private"], ["private"]),
        ("Private sauna", ["private"], []),
    ]
    assert [asset.name for asset in found.assets] == [
        asset.name
        for asset in Asset.objects.of(org).order_by("number")
        if asset.name in names(found)
    ]


def test_the_beds_ask_for_places_and_the_private_room_does_not() -> None:
    _, member = scenario("scenarios/four-beds.toml")
    found = booking_choices(member, now=NOW)
    assert names(found)["Sunshine"] == [
        ("Overnight bed", ["private"], ["private"]),
        ("Private bedroom", ["private"], []),
    ]
    room = next(row for row in found.assets if row.name == "Sunshine")
    assert room.max_quantity == 4


def test_each_asset_lists_its_containers_with_their_own_ceiling_and_categories() -> (
    None
):
    org, member = scenario("scenarios/sauna-palace.toml")
    work = BookingCategory.objects.create(organization=org, name="work", position=1)
    RoleCategory.objects.create(
        organization=org, role=member.role, booking_category=work
    )
    ConfigurationCategory.objects.create(
        organization=org,
        booking_configuration=BookingConfiguration.objects.of(org).get(number=1),
        booking_category=work,
    )
    entry = PriceBookEntry.objects.of(org).get(booking_configuration__number=1)
    entry.pk = None
    entry.booking_category = work
    entry.save()
    for tier in PriceTier.objects.of(org).filter(
        price_book_entry__booking_configuration__number=1
    ):
        tier.pk = None
        tier.price_book_entry = entry
        tier.save()
    found = booking_choices(member, now=NOW)
    sauna = next(asset for asset in found.assets if asset.name == "Sauna 1")
    visit, evening = (row for kind in sauna.types for row in kind.containers)
    private = BookingCategory.objects.of(org).get(name="private").pk
    assert (visit.id, visit.number, visit.max_quantity) == (
        BookingConfiguration.objects.of(org).get(number=1).pk,
        1,
        8,
    )
    assert (evening.number, evening.max_quantity) == (2, 1)
    assert visit.category_ids == (private, work.pk)
    assert evening.category_ids == (private,)
    assert (visit.period_type, visit.consumption_mode) == ("flexible", "shared")
    assert (evening.period_type, evening.consumption_mode) == ("fixed", "exclusive")
    assert (visit.interval, visit.minimum_length, visit.maximum_length) == (60, 60, 480)


def test_a_space_is_listed_with_the_booking_page_it_shows() -> None:
    org, member = scenario("scenarios/laundry.toml")
    found = booking_choices(member, now=NOW)
    (room,) = found.spaces
    assert (room.name, room.booking_page) == (
        "Laundry room",
        Space.objects.of(org).get(name="Laundry room").booking_page,
    )
    assert {asset.space_id for asset in found.assets} == {room.id}
    assert [asset.name for asset in found.assets] == [
        "Washer A",
        "Washer B",
        "Dryer",
        "Whole laundry room",
    ]
    whole = found.assets[-1].types[0].containers[0]
    assert (whole.id, whole.max_quantity) == (
        BookingConfiguration.objects.of(org).get(asset__name="Whole laundry room").pk,
        1,
    )


def test_role_decides_categories_and_what_is_offered() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    private = BookingCategory.objects.of(org).get(name="private")
    work = BookingCategory.objects.create(organization=org, name="work", position=0)
    inside = BookingCategory.objects.create(
        organization=org, name="internal", position=5
    )
    RoleCategory.objects.create(
        organization=org, role=member.role, booking_category=work
    )
    board = Role.objects.create(organization=org, name="Board")
    for category in (private, work, inside):
        RoleCategory.objects.create(
            organization=org, role=board, booking_category=category
        )
    visit = BookingConfiguration.objects.of(org).get(number=1)
    # Named on the offer and priced, but only the board can book it.
    ConfigurationCategory.objects.create(
        organization=org, booking_configuration=visit, booking_category=inside
    )
    entry = PriceBookEntry.objects.of(org).get(
        booking_configuration=visit, booking_category=private
    )
    tiers = list(entry.tiers.all())
    entry.pk = None
    entry.booking_category = inside
    entry.save()
    for tier in tiers:
        tier.pk = None
        tier.price_book_entry = entry
        tier.save()
    found = booking_choices(member, now=NOW)
    # Work comes first by position, though nothing is offered for it.
    assert [row.name for row in found.categories] == ["work", "private"]
    assert names(found)["Sauna 1"][0] == ("Sauna visit", ["private"], ["private"])
    chair = Member.objects.create(
        organization=org,
        role=board,
        email="chair@example.com",
        status=MemberStatus.ACTIVE,
        origin="member import",
    )
    found = booking_choices(chair, now=NOW)
    assert [row.name for row in found.categories] == ["work", "private", "internal"]
    assert names(found)["Sauna 1"][0] == (
        "Sauna visit",
        ["private", "internal"],
        ["private", "internal"],
    )


def test_a_type_without_a_bookable_offer_is_not_listed() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    evening = BookingConfiguration.objects.of(org).get(number=2)
    evening.status = Status.DISABLED
    evening.save()
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Sauna visit"
    ]
    evening.status = Status.ENABLED
    # Its last calendar week ended a week before.
    evening.stop_date = DAY - timedelta(days=8)
    evening.save()
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Sauna visit"
    ]
    # A future first week still lists it: the calendar shows when it begins.
    evening.stop_date = None
    evening.start_date = DAY + timedelta(weeks=8)
    evening.save()
    assert len(names(booking_choices(member, now=NOW))["Sauna 1"]) == 2
    # Without a price for the category, rule 12: never offered, never free.
    evening.start_date = None
    evening.save()
    PriceBookEntry.objects.of(org).filter(booking_configuration=evening).delete()
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Sauna visit"
    ]


def test_an_asset_none_of_whose_containers_offers_a_time_is_not_listed() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    before = set(names(booking_choices(member, now=NOW)))
    assert "Sauna 1" in before
    mine = BookingConfiguration.objects.of(org).filter(asset__name="Sauna 1")
    assert mine.count() >= 1
    # Every container of the asset ended its last calendar week before today.
    for config in mine:
        config.stop_date = DAY - timedelta(days=8)
        config.save()
    assert set(names(booking_choices(member, now=NOW))) == before - {"Sauna 1"}
    # One container still starting a period from today brings the asset back.
    revived = mine.first()
    assert revived is not None
    revived.stop_date = None
    revived.save()
    assert set(names(booking_choices(member, now=NOW))) == before


def test_an_asset_whose_only_containers_start_next_month_is_still_listed() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    mine = BookingConfiguration.objects.of(org).filter(asset__name="Sauna 1")
    assert mine.count() >= 1
    # Five weeks on, no stop date: the calendar shows when it begins.
    for config in mine:
        config.start_date = DAY + timedelta(weeks=5)
        config.stop_date = None
        config.save()
    assert "Sauna 1" in names(booking_choices(member, now=NOW))


def test_types_follow_their_position() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    BookingType.objects.of(org).filter(name="Private sauna").update(position=0)
    BookingType.objects.of(org).filter(name="Sauna visit").update(position=1)
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Private sauna",
        "Sauna visit",
    ]


def cover_twelve_hours(made: made_up.MadeUp) -> None:
    """The made-up price stops at four hours; rule 12 hides an offer it does
    not cover. Cover the whole twelve hours so the room hire is offered."""
    PriceTier.objects.create(
        organization=made.organization,
        price_book_entry=PriceBookEntry.objects.of(made.organization).get(),
        up_to=12 * 60,
        paid_per=4 * 60,
        amount=10000,
    )


def test_each_member_sees_only_their_own_organization() -> None:
    # Two organizations with the same names for everything.
    here = made_up.everything("harbourview")
    there = made_up.everything("riverside")
    cover_twelve_hours(here)
    cover_twelve_hours(there)
    for made in (here, there):
        found = booking_choices(made.member, now=NOW)
        assert [asset.id for asset in found.assets] == [made.asset.pk]
        assert [row.id for row in found.assets[0].types] == [made.booking_type.pk]
        assert [row.id for row in found.categories] == [made.private.pk]
    here.member.status = MemberStatus.DISABLED
    here.member.save()
    assert booking_choices(here.member, now=NOW) == BookingChoices((), ())


def test_a_role_without_categories_gets_nothing() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    RoleCategory.objects.of(org).filter(role=member.role).delete()
    assert booking_choices(member, now=NOW) == BookingChoices((), ())


def test_places_are_asked_per_category() -> None:
    # Sauna visit gets a whole-sauna evening, offered for work only. Places are
    # then asked for private (seats by day) and never for work (T2).
    org, member = scenario("scenarios/sauna-palace.toml")
    work = BookingCategory.objects.create(organization=org, name="work", position=0)
    RoleCategory.objects.create(
        organization=org, role=member.role, booking_category=work
    )
    visit = BookingType.objects.of(org).get(name="Sauna visit")
    evening = BookingConfiguration.objects.of(org).get(number=2)
    BookingConfiguration.objects.of(org).filter(pk=evening.pk).update(
        booking_type=visit
    )
    ConfigurationCategory.objects.of(org).filter(booking_configuration=evening).delete()
    ConfigurationCategory.objects.create(
        organization=org, booking_configuration=evening, booking_category=work
    )
    PriceBookEntry.objects.of(org).filter(booking_configuration=evening).update(
        booking_category=work
    )
    listed = names(booking_choices(member, now=NOW))["Sauna 1"]
    assert listed == [("Sauna visit", ["work", "private"], ["private"])]


def test_a_type_whose_seat_offers_are_unpriced_asks_no_places() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    visit = BookingConfiguration.objects.of(org).get(number=1)
    PriceBookEntry.objects.of(org).filter(booking_configuration=visit).delete()
    found = booking_choices(member, now=NOW)
    assert names(found)["Sauna 1"] == [("Private sauna", ["private"], [])]
    assert next(row for row in found.assets if row.name == "Sauna 1").max_quantity == 1


def test_an_invalid_offer_is_not_listed_and_stacked_containers_both_offer() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    evening = BookingConfiguration.objects.of(org).filter(number=2)
    # A stored row that bypassed validation: a fixed period with other lengths.
    evening.update(minimum_length=60)
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Sauna visit"
    ]
    # Two containers of one type back to back.
    evening.update(
        minimum_length=4 * 60,
        booking_type=BookingType.objects.of(org).get(name="Sauna visit"),
    )
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Sauna visit"
    ]
    # The visits run to 19:00, into the evening (): the shared visits and
    # the exclusive evening stack, and each offers the time they share.
    BookingConfiguration.objects.of(org).filter(number=1).update(period_end=19 * 60)
    visit, evening_offer = (
        BookingConfiguration.objects.of(org)
        .filter(number__in=(1, 2))
        .order_by("number")
    )
    assert visit.covers_the_same_time_as(evening_offer)
    assert [row[0] for row in names(booking_choices(member, now=NOW))["Sauna 1"]] == [
        "Sauna visit"
    ]
    offers = sauna_calendar(member, visit)
    assert {offer.configuration_id for offer in offers} == {visit.pk, evening_offer.pk}
    visits = [offer for offer in offers if offer.configuration_id == visit.pk]
    assert max(offer.ends_at.astimezone(ZONE).time() for offer in visits) == time(19)
    assert any(
        offer.starts_at.astimezone(ZONE).time() == time(18)
        and offer.ends_at.astimezone(ZONE).time() == time(19)
        for offer in visits
    )
    evenings = [offer for offer in offers if offer.configuration_id == evening_offer.pk]
    assert {
        (offer.starts_at.astimezone(ZONE).time(), offer.ends_at.astimezone(ZONE).time())
        for offer in evenings
    } == {(time(18), time(22))}


def sauna_calendar(
    member: Member, offer: BookingConfiguration
) -> tuple[BookableInterval, ...]:
    return bookable_intervals(
        member=member,
        asset=offer.asset,
        booking_type=offer.booking_type,
        start_date=DAY,
        end_date=DAY + timedelta(days=7),
        now=NOW,
    )


def test_the_last_start_counts_in_the_assets_own_day() -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    evening = BookingConfiguration.objects.of(org).get(number=2)
    # Monday to Friday, to the week of DAY: its last start is Friday.
    evening.stop_date = DAY + timedelta(days=6)
    evening.save()

    def listed(now: datetime) -> bool:
        types = names(booking_choices(member, now=now))["Sauna 1"]
        return any(row[0] == "Private sauna" for row in types)

    # 00:30 in Oslo is still the day before in UTC.
    friday, saturday = (
        at(DAY + timedelta(days=4), 0, 30),
        at(DAY + timedelta(days=5), 0, 30),
    )
    assert saturday.date() == DAY + timedelta(days=4)
    assert listed(friday)
    assert not listed(saturday)
    # Behind UTC: 20:00 in New York is already the next day in UTC. The
    # assets' zone counts, not the organization's, which stays in Oslo.
    Asset.objects.of(org).update(time_zone="America/New_York")
    assert Organization.objects.get(pk=org.pk).time_zone == "Europe/Oslo"
    friday_evening = datetime.combine(DAY + timedelta(days=5), time(1), UTC)
    assert listed(friday_evening)
    assert not listed(friday_evening + timedelta(days=1))


def test_a_shared_container_lists_its_floor_plans_groups_for_keep_us_together() -> None:
    org, member = scenario("scenarios/supper-club.toml")
    (plan,) = AssetInventory.objects.of(org.pk).values_list("name", flat=True)
    found = booking_choices(member, now=NOW)
    containers = [
        container
        for asset in found.assets
        for row in asset.types
        for container in row.containers
    ]
    shared = [row for row in containers if row.consumption_mode == "shared"]
    assert shared
    for container in shared:
        assert [
            (group.number, group.name, group.word, group.spots, group.plan)
            for group in container.groups
        ] == [
            (1, "Window", "table", 8, plan),
            (2, "Long", "table", 8, plan),
            (3, "Bar", "table", 4, plan),
        ]
    assert all(not row.groups for row in containers if row.consumption_mode != "shared")


def test_choices_need_their_own_transaction() -> None:
    _, member = scenario("scenarios/sauna-palace.toml")
    from django.db import transaction

    with transaction.atomic(), pytest.raises(RuntimeError):
        booking_choices(member, now=NOW)


def test_the_application_login_can_read_the_choices() -> None:
    _, member = scenario("scenarios/sauna-palace.toml")
    with application_login():
        assert booking_choices(member, now=NOW).assets


def test_the_page_gets_choices_for_the_signed_in_member_only() -> None:
    made = made_up.everything()
    cover_twelve_hours(made)
    client = Client()
    assert get(client, "/booking/choices/").status_code == 302
    sign_in(client, made.member)
    response = get(client, "/booking/choices/")
    assert response.status_code == 200
    assert {"no-store", "private"} <= {
        part.strip() for part in response["Cache-Control"].split(",")
    }
    refused = client.post(
        "/booking/choices/",
        HTTP_HOST=HOST,
        HTTP_ORIGIN="https://" + HOST,
        HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
        secure=True,
    )
    assert refused.status_code == 405
    body = json.loads(response.content)
    assert made.asset.space_id is not None
    room = Space.objects.of(made.organization).get(pk=made.asset.space_id)
    assert body == {
        "categories": [{"id": made.private.pk, "name": "private", "icon": ""}],
        "category": made.private.pk,
        "assets": [
            {
                "id": made.asset.pk,
                "name": made.asset.name,
                "description": made.asset.description,
                "max_quantity": 1,
                "space": made.asset.space_id,
                "types": [
                    {
                        "id": made.booking_type.pk,
                        "name": made.booking_type.name,
                        "description": made.booking_type.description,
                        "categories": [made.private.pk],
                        "quantity_for": [],
                        "containers": [
                            {
                                "id": made.configuration.pk,
                                "number": made.configuration.number,
                                "with_payment": True,
                                "categories": [made.private.pk],
                                "max_quantity": 1,
                                "spot_word": None,
                                "period_type": made.configuration.period_type,
                                "consumption_mode": made.configuration.consumption_mode,
                                "interval": made.configuration.interval,
                                "minimum_length": made.configuration.minimum_length,
                                "maximum_length": made.configuration.maximum_length,
                                "groups": [],
                            }
                        ],
                    }
                ],
            }
        ],
        "spaces": [
            {
                "id": room.pk,
                "name": room.name,
                "booking_page": room.booking_page,
            }
        ],
    }
    for value in (made.member.email, made.member.phone, made.administrator.email):
        assert value not in response.content.decode()
    # The same signed-in browser at another organization's address.
    made_up.everything("riverside")
    elsewhere = client.get(
        "/booking/choices/", HTTP_HOST="riverside.sandbox.example.org", secure=True
    )
    assert elsewhere.status_code != 200
    assert made.asset.name not in elsewhere.content.decode()
    # Presenting the session at another organization ends it everywhere.
    assert get(client, "/booking/choices/").status_code == 302


# --- The category the Filter starts on (part 2.995) ---------------------------


def two_categories(made: made_up.MadeUp) -> BookingCategory:
    """The standard role can also book "work", which comes after "private"."""
    work = BookingCategory.objects.create(
        organization=made.organization, name="work", position=5
    )
    RoleCategory.objects.create(
        organization=made.organization, role=made.standard, booking_category=work
    )
    return work


def choose(client: Client, value: str | None, *, token: bool = True) -> HttpResponse:
    headers = {"X-CSRFToken": client.cookies[settings.CSRF_COOKIE_NAME].value}
    response = client.post(
        "/booking/choices/category/",
        {} if value is None else {"category": value},
        HTTP_HOST=HOST,
        HTTP_ORIGIN="https://" + HOST,
        secure=True,
        headers=headers if token else None,
    )
    assert isinstance(response, HttpResponse)
    return response


def starts_on(client: Client) -> int | None:
    category: int | None = json.loads(get(client, "/booking/choices/").content)[
        "category"
    ]
    return category


def test_the_filter_starts_on_the_first_category_of_the_setup() -> None:
    org, member = scenario("fjordside.toml")
    found = booking_choices(member, now=NOW)
    assert [row.name for row in found.categories] == ["Private", "Work"]
    assert found.category == BookingCategory.objects.of(org).get(name="Private").pk
    assert member.last_booking_category_id is None


def test_a_member_without_categories_starts_on_none() -> None:
    _, member = scenario("scenarios/sauna-palace.toml")
    RoleCategory.objects.of(member.organization_id).filter(
        role_id=member.role_id
    ).delete()
    assert booking_choices(member, now=NOW).category is None


def test_the_filter_starts_on_the_category_the_member_saved() -> None:
    made = made_up.everything()
    cover_twelve_hours(made)
    work = two_categories(made)
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    assert starts_on(client) == made.private.pk
    assert choose(client, str(work.pk)).status_code == 204
    assert starts_on(client) == work.pk
    made.member.refresh_from_db()
    assert made.member.last_booking_category_id == work.pk
    # Another device is the same member record.
    elsewhere = Client(enforce_csrf_checks=True)
    sign_in(elsewhere, made.member)
    assert starts_on(elsewhere) == work.pk


def test_a_role_that_loses_the_category_falls_back_and_keeps_the_stored_one() -> None:
    made = made_up.everything()
    cover_twelve_hours(made)
    work = two_categories(made)
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    assert choose(client, str(work.pk)).status_code == 204
    RoleCategory.objects.of(made.organization).filter(
        role=made.standard, booking_category=work
    ).delete()
    assert starts_on(client) == made.private.pk
    made.member.refresh_from_db()
    assert made.member.last_booking_category_id == work.pk
    # The role gets it back: the stored choice counts again.
    RoleCategory.objects.create(
        organization=made.organization, role=made.standard, booking_category=work
    )
    assert starts_on(client) == work.pk


def test_deleting_the_category_empties_the_field_and_keeps_the_organization() -> None:
    made = made_up.everything()
    work = two_categories(made)
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        last_booking_category=work
    )
    RoleCategory.objects.of(made.organization).filter(booking_category=work).delete()
    work.delete()
    stored = Member.objects.of(made.organization).get(pk=made.member.pk)
    assert stored.last_booking_category_id is None
    assert stored.organization_id == made.organization.pk


def test_saving_a_category_the_member_cannot_book_is_refused() -> None:
    made = made_up.everything()
    cover_twelve_hours(made)
    work = two_categories(made)
    outside = BookingCategory.objects.create(
        organization=made.organization, name="internal", position=9
    )
    other = made_up.everything("riverside")
    foreign = BookingCategory.objects.of(other.organization).get()
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    assert choose(client, str(work.pk)).status_code == 204
    for pk in (outside.pk, foreign.pk, foreign.pk + 1000):
        response = choose(client, str(pk))
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not-offered"}
    made.member.refresh_from_db()
    assert made.member.last_booking_category_id == work.pk
    for bad in (None, "", "abc", "0", "-1", "1,2", "1" * 20):
        response = choose(client, bad)
        assert response.status_code == 400, bad
        assert json.loads(response.content) == {"error": "bad-request"}


def test_saving_a_category_needs_the_csrf_header_and_a_signed_in_member() -> None:
    made = made_up.everything()
    work = two_categories(made)
    client = Client(enforce_csrf_checks=True)
    nobody = client.post(
        "/booking/choices/category/", HTTP_HOST=HOST, secure=True
    ).status_code
    assert nobody in {302, 403}
    sign_in(client, made.member)
    assert choose(client, str(work.pk), token=False).status_code == 403
    assert get(client, "/booking/choices/category/").status_code == 405
    made.member.refresh_from_db()
    assert made.member.last_booking_category_id is None


def test_the_book_page_carries_the_same_choices_as_the_endpoint() -> None:
    made = made_up.everything()
    cover_twelve_hours(made)
    client = Client()
    assert get(client, "/book/").status_code == 302
    sign_in(client, made.member)
    page = get(client, "/book/")
    assert page.status_code == 200
    assert {"no-store", "private"} <= {
        part.strip() for part in page["Cache-Control"].split(",")
    }
    found = re.search(
        r'<script id="rr-config" type="application/json">(.*?)</script>',
        page.content.decode(),
        re.S,
    )
    assert found is not None
    embedded = json.loads(found.group(1))["choices"]
    assert embedded == json.loads(get(client, "/booking/choices/").content)
    assert embedded["assets"]
    # My page does not carry them.
    mine = get(client, "/me/")
    assert '"choices"' not in mine.content.decode()
