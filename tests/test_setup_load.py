# SPDX-License-Identifier: AGPL-3.0-only
"""Loading a setup into the database, and exporting it again
.
"""

from dataclasses import replace
from datetime import date
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command

from randirandi import setup_file
from randirandi.core import setup as setups
from randirandi.core import validator
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    Organization,
    PeriodType,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Space,
    Status,
    TiersGoBy,
)
from randirandi.core.models.base import DAY
from randirandi.core.models.bookable import share_time
from randirandi.core.models.prices import what_the_tiers_leave_out
from randirandi.setup_file import WEEKDAYS, Setup, SetupError, parse, read
from tests import made_up
from tests.test_setup_file import FILES, FORMAT_THREE, SETUPS, SMALLEST

EXAMPLE = SETUPS / "example.toml"
TODAY = date(2027, 3, 1)


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["space", "capacity", "unit", "mode"])
def test_an_edited_format_one_setup_exports_what_it_now_holds(change: str) -> None:
    # Format 1 has no room for these; the export, always format 3, has.
    organization = setups.load(read(EXAMPLE), today=TODAY)
    if change == "space":
        Space.objects.create(organization=organization, name="Equipment room")
    elif change in {"capacity", "unit"}:
        floor_plan = AssetInventory.objects.of(organization).first()
        assert floor_plan is not None
        if change == "capacity":
            made_up.spots(floor_plan, 4)
        else:
            floor_plan.unit = "beds"
            floor_plan.save()
    else:
        for configuration in BookingConfiguration.objects.of(organization):
            BookingConfiguration.objects.of(organization).filter(
                pk=configuration.pk
            ).update(
                consumption_mode="shared",
                floor_plan=made_up.floor_plan_of(configuration.asset),
            )
        # Shared, a container may be priced by the spot.
        PriceBookEntry.objects.of(organization).update(
            billing_basis="per_capacity_unit"
        )
    exported = setups.export(organization)
    assert exported.format == setup_file.FORMAT
    assert exported != in_order(read(EXAMPLE))
    copy = replace(
        exported, organization=replace(exported.organization, subdomain="copy")
    )
    assert (
        setups.export(setups.load(parse(setup_file.write(copy)), today=TODAY)) == copy
    )


# A second configuration for the smallest setup, on the same asset and
# booking type, inside the hours of the first.
ONE_MORE = """
[[booking_configurations]]
number = 2
asset = "Clubhouse"
booking_type = "Clubhouse hire"
period_type = "flexible"
period_start = "12:00"
period_end = "16:00"
end_day = 0
period_start_weekdays = ["sun"]
interval = "1 h"
minimum_length = "1 h"
maximum_length = "4 h"
status = "enabled"
price_book = "Prices"
booking_categories = ["private"]
"""

# The smallest setup with two floor plans on its asset and its container
# shared on the first: the second container of `ONE_MORE`, shared on the other
# floor plan, covers the same time without being able to share its spots.
TWO_FLOOR_PLANS: list[tuple[str, str]] = [
    ("format = 1", "format = 3"),
    (
        'name = "Clubhouse"\n',
        'name = "Clubhouse"\n\n'
        '[[assets.floor_plans]]\nname = "Seated"\nspot_word = "seat"\nspots = 12\n\n'
        '[[assets.floor_plans]]\nname = "Standing"\nspot_word = "place"\n'
        "spots = 40\n",
    ),
    (
        'price_book = "Prices"\nbooking_categories = ["private"]\n',
        'price_book = "Prices"\nbooking_categories = ["private"]\n'
        'consumption_mode = "shared"\nfloor_plan = "Seated"\n',
    ),
    (
        'tiers_go_by = "length"\n',
        'tiers_go_by = "length"\nbilling_basis = "per_capacity_unit"\n',
    ),
]
STANDING = ONE_MORE.replace(
    'booking_categories = ["private"]',
    'booking_categories = ["private"]\nconsumption_mode = "shared"\n'
    'floor_plan = "Standing"',
)

# Changes to the smallest setup that the format allows and a rule refuses,
# each with the fault it must be refused with.
REFUSED: list[tuple[str, list[tuple[str, str]], str]] = [
    (
        "a subdomain no organization may take",
        [('subdomain = "lakeside"', 'subdomain = "www"')],
        "organization.subdomain: No organization may take this subdomain.",
    ),
    (
        "a period that ends before it starts",
        [('period_end = "18:00"', 'period_end = "09:00"')],
        "booking_configurations[1].period_end: The period ends after it starts.",
    ),
    (
        "a period that is not a whole number of intervals",
        [('interval = "30 min"', 'interval = "3 h"')],
        "booking_configurations[1].interval: The period is a whole number of "
        "intervals.",
    ),
    (
        "a minimum above the maximum",
        [('minimum_length = "1 h"', 'minimum_length = "9 h"')],
        "booking_configurations[1].minimum_length: It is at most the maximum length.",
    ),
    (
        "a maximum longer than the period",
        [('maximum_length = "8 h"', 'maximum_length = "9 h"')],
        "booking_configurations[1].maximum_length: It is at most the period's length.",
    ),
    (
        "a fixed period longer than the time to the next weekday it starts on",
        [
            ('period_type = "flexible"', 'period_type = "fixed"'),
            ("end_day = 0", "end_day = 2"),
            ('interval = "30 min"\n', ""),
            ('minimum_length = "1 h"\n', ""),
            ('maximum_length = "8 h"\n', ""),
        ],
        "booking_configurations[1].period_start_weekdays: The period is longer "
        "than the time to the next weekday it starts on.",
    ),
    (
        "two shared configurations on two floor plans at the same time",
        [*TWO_FLOOR_PLANS, ("\n[[price_books]]", STANDING + "\n[[price_books]]")],
        "booking_configurations[2].status: It covers the same time as "
        "container 001 on Clubhouse; the two book different floor plans of the "
        "asset.",
    ),
    (
        "price tiers that stop short of the maximum length",
        [('up_to = "8 h"', 'up_to = "4 h"')],
        "price_books[1].entries[1].tiers: The price tiers reach 4 h, and the "
        "configuration can be booked for 8 h.",
    ),
    (
        "price tiers by time of day that stop before the period ends",
        [
            (
                'tiers_go_by = "length"\ntiers = [{ up_to = "8 h"',
                'tiers_go_by = "time of day"\ntiers = [{ up_to = "17:00"',
            )
        ],
        "price_books[1].entries[1].tiers: The price tiers reach 17:00, and the "
        "period ends at 18:00.",
    ),
    (
        "price tiers by time of day for a period that ends on a later day",
        [
            # Available every night too, so that the period is covered.
            (
                'weekdays = ["sat", "sun"], start = "09:00"',
                'weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], '
                'start = "00:00"',
            ),
            ('period_end = "18:00"', 'period_end = "02:00"'),
            ("end_day = 0", "end_day = 1"),
            (
                'tiers_go_by = "length"\ntiers = [{ up_to = "8 h"',
                'tiers_go_by = "time of day"\ntiers = [{ up_to = "23:00"',
            ),
        ],
        "price_books[1].entries[1].tiers: Price tiers by time of day are not "
        "worked out for a period that ends on a later day.",
    ),
]


def changed(changes: list[tuple[str, str]]) -> Setup:
    text = SMALLEST
    for old, new in changes:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return parse(text)


def in_order(setup: Setup, today: date = TODAY) -> Setup:
    """The setup as an export gives it: in format 3, what has no order of its
    own put in one order, and a calendar without a start date has the day of
    the load.
    """
    position = {item.name: place for place, item in enumerate(setup.booking_categories)}
    number = {asset.name: asset.number for asset in setup.assets}

    def names(chosen: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(chosen, key=position.__getitem__))

    calendars = [
        replace(
            calendar,
            start_date=calendar.start_date or today,
            spans=tuple(
                sorted(
                    calendar.spans,
                    key=lambda span: (WEEKDAYS.index(span.weekday), span.start),
                )
            ),
        )
        for calendar in setup.availability_calendars
    ]
    configurations = [
        replace(
            item,
            booking_categories=names(item.booking_categories),
            period_start_weekdays=tuple(
                sorted(item.period_start_weekdays, key=WEEKDAYS.index)
            ),
        )
        for item in setup.booking_configurations
    ]
    price_books = [
        replace(
            price_book,
            entries=tuple(
                sorted(
                    (
                        replace(
                            entry,
                            tiers=tuple(sorted(entry.tiers, key=lambda t: t.up_to)),
                        )
                        for entry in price_book.entries
                    ),
                    key=lambda entry: (
                        entry.booking_configuration,
                        position[entry.booking_category],
                    ),
                )
            ),
        )
        for price_book in setup.price_books
    ]
    return replace(
        setup,
        format=setup_file.FORMAT,
        assets=tuple(sorted(setup.assets, key=lambda asset: asset.number)),
        availability_calendars=tuple(
            sorted(
                calendars,
                key=lambda calendar: (
                    number[calendar.asset],
                    calendar.start_date or today,
                ),
            )
        ),
        roles=tuple(
            replace(role, booking_categories=names(role.booking_categories))
            for role in setup.roles
        ),
        booking_configurations=tuple(
            sorted(configurations, key=lambda item: item.number)
        ),
        price_books=tuple(sorted(price_books, key=lambda book: book.number)),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("file", FILES, ids=lambda file: file.name)
def test_a_setup_file_loads_and_an_export_gives_the_same_setup_back(
    file: Path,
) -> None:
    written = read(file)

    organization = setups.load(written, today=TODAY)
    exported = setups.export(organization)

    assert exported == in_order(written)
    # As a file again, it is read back as the same setup.
    assert in_order(parse(setup_file.write(exported))) == exported
    # And the export loads, as another organization, and exports the same.
    other = replace(
        exported, organization=replace(exported.organization, subdomain="second")
    )
    assert setups.export(setups.load(other, today=TODAY)) == other


@pytest.mark.django_db
def test_a_zone_and_a_currency_of_their_own_load_export_and_write_back() -> None:
    written = in_order(read(EXAMPLE))
    guest, function, laundry = written.assets
    assert not (guest.space or laundry.space)
    own = replace(
        written,
        assets=(replace(guest, time_zone="America/New_York"), function, laundry),
        price_books=(
            replace(written.price_books[0], currency="EUR"),
            *written.price_books[1:],
        ),
    )
    text = setup_file.write(own)
    assert text.count('time_zone = "America/New_York"') == 1
    assert text.count('currency = "EUR"') == 1
    assert parse(text) == own

    organization = setups.load(own, today=TODAY)

    assert Asset.objects.of(organization).get(number=1).time_zone == "America/New_York"
    assert Asset.objects.of(organization).get(number=2).time_zone == "Europe/Oslo"
    assert PriceBook.objects.of(organization).get(number=1).currency == "EUR"
    assert PriceBook.objects.of(organization).get(number=2).currency == "NOK"
    exported = setups.export(organization)
    assert exported == own
    assert in_order(parse(setup_file.write(exported))) == exported


@pytest.mark.django_db
def test_a_zone_or_a_currency_left_out_or_the_organizations_own_is_not_written() -> (
    None
):
    written = in_order(read(EXAMPLE))
    same = replace(
        written,
        assets=tuple(
            replace(asset, time_zone=written.organization.time_zone)
            for asset in written.assets
        ),
        price_books=tuple(
            replace(book, currency=written.organization.currency)
            for book in written.price_books
        ),
    )
    text = setup_file.write(same)
    assert text == setup_file.write(written)
    assert text.count("time_zone") == 1 and text.count("currency") == 1
    organization = setups.load(same, today=TODAY)
    assert {asset.time_zone for asset in Asset.objects.of(organization)} == {
        "Europe/Oslo"
    }
    assert setups.export(organization) == written


LABELS = (
    "\n[labels]\n"
    '"place.book" = "Reserve"\n'
    '"stack.needs" = "At {time} {length} is needed"\n'
)


@pytest.mark.django_db
def test_labels_are_loaded_and_exported_and_round_trip() -> None:
    written = parse(FORMAT_THREE + LABELS)

    organization = setups.load(written, today=TODAY)

    assert organization.labels == {
        "place.book": "Reserve",
        "stack.needs": "At {time} {length} is needed",
    }
    exported = setups.export(organization)
    assert exported.labels == written.labels
    # As a file, read back as the same setup, and loaded again as another.
    assert parse(setup_file.write(exported)).labels == written.labels
    other = replace(
        exported, organization=replace(exported.organization, subdomain="second")
    )
    assert setups.export(setups.load(other, today=TODAY)).labels == written.labels


@pytest.mark.django_db
def test_an_export_writes_only_the_keys_that_are_set() -> None:
    organization = setups.load(read(EXAMPLE), today=TODAY)
    assert organization.labels == {}
    assert setups.export(organization).labels == ()
    assert "[labels]" not in setup_file.write(setups.export(organization))

    organization.labels = {"place.me": "My bookings"}
    organization.save()

    assert setups.export(organization).labels == (("place.me", "My bookings"),)


@pytest.mark.django_db
def test_a_label_the_set_does_not_allow_is_not_loaded() -> None:
    written = replace(parse(FORMAT_THREE), labels=(("no.such", "x"),))

    with pytest.raises(SetupError) as raised:
        setups.load(written, today=TODAY)

    assert "labels.no.such" in str(raised.value)
    assert not Organization.objects.filter(subdomain="lakeside").exists()


@pytest.mark.django_db
def test_the_scenario_with_labels_loads_them() -> None:
    organization = setups.load(
        read(SETUPS / "scenarios" / "four-beds.toml"), today=TODAY
    )

    assert organization.labels == {
        "place.book": "Reserve",
        "button.book": "Reserve",
        "cart.title": "Your reservations",
        "outcome.booked": "You are booked in",
    }


@pytest.mark.django_db
def test_the_made_up_organization_is_loaded_in_full() -> None:
    organization = setups.load(read(EXAMPLE), today=TODAY)

    assert Asset.objects.of(organization).count() == 3
    assert BookingConfiguration.objects.of(organization).count() == 5
    assert PriceBook.objects.of(organization).count() == 2
    assert PriceBookEntry.objects.of(organization).count() == 10
    assert PriceTier.objects.of(organization).count() == 12
    assert organization.standard_role is not None
    assert organization.standard_role.name == "Standard"
    # The order of the file is the place in the lists people choose from.
    assert [item.name for item in BookingType.objects.of(organization)] == [
        "Overnight stay",
        "Room hire",
        "Laundry",
    ]
    assert [item.name for item in BookingCategory.objects.of(organization)] == [
        "private",
        "work",
        "internal",
    ]
    # A fixed period: the system sets the three lengths to the period's.
    night = BookingConfiguration.objects.of(organization).get(number=1)
    assert night.period_type == PeriodType.FIXED
    assert night.interval == night.minimum_length == night.maximum_length == 20 * 60


@pytest.mark.django_db
def test_a_calendar_without_a_start_date_applies_from_the_day_of_the_load() -> None:
    organization = setups.load(read(EXAMPLE), today=TODAY)

    starts = {
        calendar.asset.name: calendar.start_date
        for calendar in AvailabilityCalendar.objects.of(organization)
    }

    assert starts == {
        "Guest room": TODAY,
        "Function room": TODAY,
        "Laundry room": date(2027, 1, 1),
    }


@pytest.mark.django_db
def test_the_numbers_of_the_file_are_kept_and_counting_goes_on_above_them() -> None:
    written = read(EXAMPLE)
    guest_room, function_room, laundry_room = written.assets
    # Out of order in the file, and with a gap.
    written = replace(
        written,
        assets=(replace(laundry_room, number=7), function_room, guest_room),
        booking_configurations=written.booking_configurations[::-1],
        price_books=written.price_books[::-1],
    )

    organization = setups.load(written, today=TODAY)

    assert setups.export(organization) == in_order(written)
    numbers = {asset.name: asset.number for asset in Asset.objects.of(organization)}
    assert numbers == {"Guest room": 1, "Function room": 2, "Laundry room": 7}
    sauna = Asset.objects.create(organization=organization, name="Sauna")
    assert sauna.number == 8
    organization.refresh_from_db()
    assert organization.last_booking_configuration_number == 5
    assert organization.last_price_book_number == 2


@pytest.mark.django_db
def test_the_smallest_setup_loads() -> None:
    organization = setups.load(parse(SMALLEST), today=TODAY)

    tier = PriceTier.objects.of(organization).get()
    assert (tier.up_to, tier.paid_per, tier.amount) == (8 * 60, 2 * 60, 14950)


@pytest.mark.django_db
def test_a_minimum_length_left_out_is_loaded_as_one_interval() -> None:
    organization = setups.load(changed([('minimum_length = "1 h"\n', "")]), today=TODAY)

    configuration = BookingConfiguration.objects.of(organization).get()
    assert (configuration.interval, configuration.minimum_length) == (30, 30)
    # The export writes what the system set, and is read back the same.
    exported = setups.export(organization)
    assert exported.booking_configurations[0].minimum_length == 30
    assert parse(setup_file.write(exported)) == exported


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("changes", "fault"),
    [(changes, fault) for _, changes, fault in REFUSED],
    ids=[name for name, _, _ in REFUSED],
)
def test_a_setup_that_breaks_a_rule_is_refused_with_its_place(
    changes: list[tuple[str, str]], fault: str
) -> None:
    with pytest.raises(SetupError) as refused:
        setups.load(changed(changes), today=TODAY)

    assert fault in refused.value.faults
    # Not loaded in part: nothing of it is there.
    assert not Organization.objects.exists()
    assert not Asset.across_organizations.exists()
    assert not BookingCategory.across_organizations.exists()


@pytest.mark.django_db
def test_everything_wrong_with_a_setup_is_said_in_one_go() -> None:
    written = changed([('interval = "30 min"', 'interval = "3 h"')])
    written = replace(
        written, organization=replace(written.organization, subdomain="app")
    )
    written = replace(
        written,
        booking_configurations=(
            replace(written.booking_configurations[0], maximum_length=9 * 60),
        ),
    )

    with pytest.raises(SetupError) as refused:
        setups.load(written, today=TODAY)

    # The organization comes first, and nothing can be made without it.
    assert refused.value.faults == [
        "organization.subdomain: No organization may take this subdomain."
    ]

    written = replace(
        written, organization=replace(written.organization, subdomain="lakeside")
    )
    with pytest.raises(SetupError) as refused:
        setups.load(written, today=TODAY)

    assert set(refused.value.faults) == {
        "booking_configurations[1].interval: The period is a whole number of "
        "intervals.",
        "booking_configurations[1].minimum_length: It is a whole number of intervals.",
        "booking_configurations[1].maximum_length: It is at most the period's length.",
    }


def test_the_reader_stops_a_bad_subdomain_before_any_load() -> None:
    with pytest.raises(SetupError):
        changed([('subdomain = "lakeside"', 'subdomain = "bad--end-"')])


@pytest.mark.django_db
def test_two_exclusive_configurations_on_the_same_time_load() -> None:
    organization = setups.load(
        changed([("\n[[price_books]]", ONE_MORE + "\n[[price_books]]")]), today=TODAY
    )

    statuses = {
        item.number: item.status
        for item in BookingConfiguration.objects.of(organization)
    }
    assert statuses == {1: Status.ENABLED, 2: Status.ENABLED}
    assert validator.clashes(organization, now=made_up.at(TODAY, 0)) == []


@pytest.mark.django_db
def test_two_configurations_on_two_floor_plans_load_when_one_is_disabled() -> None:
    one_more = STANDING.replace('status = "enabled"', 'status = "disabled"')

    organization = setups.load(
        changed(
            [*TWO_FLOOR_PLANS, ("\n[[price_books]]", one_more + "\n[[price_books]]")]
        ),
        today=TODAY,
    )

    statuses = {
        item.number: item.status
        for item in BookingConfiguration.objects.of(organization)
    }
    assert statuses == {1: Status.ENABLED, 2: Status.DISABLED}
    # It cannot be switched on while the first covers its time.
    second = BookingConfiguration.objects.of(organization).get(number=2)
    second.status = Status.ENABLED
    with pytest.raises(ValidationError) as refused:
        second.save()
    assert "status" in refused.value.message_dict


@pytest.mark.django_db
def test_a_setup_file_makes_a_new_organization_and_never_changes_one() -> None:
    setups.load(parse(SMALLEST), today=TODAY)

    with pytest.raises(SetupError) as refused:
        setups.load(parse(SMALLEST), today=TODAY)

    assert refused.value.faults == [
        "organization.subdomain: an organization with this subdomain exists "
        "already. A setup file makes a new organization."
    ]
    assert Organization.objects.count() == 1


@pytest.mark.django_db
def test_a_check_goes_through_every_rule_and_keeps_nothing() -> None:
    setups.check(read(EXAMPLE), today=TODAY)

    assert not Organization.objects.exists()
    assert not Asset.across_organizations.exists()

    with pytest.raises(SetupError):
        setups.check(changed([('up_to = "8 h"', 'up_to = "4 h"')]), today=TODAY)


@pytest.mark.django_db
def test_a_price_is_only_for_a_booking_category_the_configuration_applies_to() -> None:
    ours = made_up.everything()
    work = BookingCategory.objects.create(organization=ours.organization, name="work")
    entry = PriceBookEntry(
        organization=ours.organization,
        price_book=ours.price_book,
        booking_configuration=ours.configuration,
        booking_category=work,
        tiers_go_by=TiersGoBy.LENGTH,
    )

    with pytest.raises(ValidationError) as refused:
        entry.full_clean()

    assert refused.value.message_dict == {
        "booking_category": [
            "The configuration does not apply to this booking category."
        ]
    }


@pytest.mark.django_db
def test_the_price_tiers_reach_the_maximum_length_or_the_end_of_the_period() -> None:
    ours = made_up.everything()
    configuration = ours.configuration  # 10:00 to 22:00, at most 12 hours.

    def left_out(tiers_go_by: TiersGoBy, *up_to: int) -> str | None:
        return what_the_tiers_leave_out(configuration, tiers_go_by, up_to)

    assert left_out(TiersGoBy.LENGTH, 4 * 60, 12 * 60) is None
    assert left_out(TiersGoBy.LENGTH, 4 * 60, 8 * 60) is not None
    assert left_out(TiersGoBy.TIME_OF_DAY, 17 * 60, 22 * 60) is None
    assert left_out(TiersGoBy.TIME_OF_DAY, 17 * 60) is not None
    assert left_out(TiersGoBy.LENGTH) == "It has no price tier."
    # The made-up organization's own entry reaches 4 of the 12 hours.
    entry = PriceBookEntry.objects.of(ours.organization).get()
    assert entry.what_its_tiers_leave_out() == (
        "The price tiers reach 4 h, and the configuration can be booked for 12 h."
    )


def test_weekly_stretches_share_time_also_around_the_end_of_the_week() -> None:
    monday_morning = [(8 * 60, 12 * 60)]
    sunday_night = [(6 * DAY + 22 * 60, 6 * DAY + 34 * 60)]  # to Monday 10:00
    saturday = [(5 * DAY + 10 * 60, 5 * DAY + 22 * 60)]

    assert share_time(sunday_night, monday_morning)
    assert share_time(monday_morning, sunday_night)
    assert not share_time(saturday, monday_morning)
    assert not share_time(saturday, sunday_night)
    # One ends where the other starts: no time is shared.
    assert not share_time([(8 * 60, 10 * 60)], [(10 * 60, 12 * 60)])


@pytest.mark.django_db
def test_configurations_cover_the_same_time_only_while_their_weeks_overlap() -> None:
    organization = setups.load(read(EXAMPLE), today=TODAY)
    configurations = BookingConfiguration.objects.of(organization)
    until_may, from_june = configurations.get(number=1), configurations.get(number=2)

    # The same night every day of the week, one to week 21 of 2027, which
    # ends on Sunday 30 May, and one from week 22.
    assert share_time(until_may.stretches(), from_june.stretches())
    assert not until_may.covers_the_same_time_as(from_june)

    from_june.start_date = date(2027, 5, 24)
    assert until_may.covers_the_same_time_as(from_june)
    from_june.start_date = None
    assert until_may.covers_the_same_time_as(from_june)
    assert from_june.covers_the_same_time_as(until_may)


@pytest.mark.django_db
@pytest.mark.parametrize("morning_hour", [6, 8])
@pytest.mark.parametrize("night_number", [1, 2])
def test_setup_checks_overnight_overlap_and_touching_boundaries(
    morning_hour: int, night_number: int
) -> None:
    setup = parse(SMALLEST)
    base = setup.booking_configurations[0]
    night = replace(
        base,
        number=night_number,
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        period_start_weekdays=("sun",),
        # Week 53 of 2026, which ends on Sunday 3 January 2027.
        start_date=date(2026, 12, 28),
        stop_date=date(2027, 1, 3),
    )
    morning_length = (10 - morning_hour) * 60
    morning = replace(
        base,
        number=3 - night_number,
        period_start=morning_hour * 60,
        period_end=10 * 60,
        maximum_length=morning_length,
        period_start_weekdays=("mon",),
        # Week 1 of 2027.
        start_date=date(2027, 1, 4),
        stop_date=date(2027, 1, 10),
    )
    entry = setup.price_books[0].entries[0]
    price_book = replace(
        setup.price_books[0],
        entries=(
            replace(entry, booking_configuration=night.number),
            replace(
                entry,
                booking_configuration=morning.number,
                tiers=(replace(entry.tiers[0], up_to=morning_length),),
            ),
        ),
    )
    # The validator checks only date periods that end after the load.
    before = date(2026, 12, 1)
    # Available around the clock, so that only the overlap is in question.
    around_the_clock = replace(
        setup.availability_calendars[0],
        start_date=before,
        spans=tuple(setup_file.AvailableSpan(day, 0, DAY) for day in WEEKDAYS),
    )
    # Shared on the asset's two floor plans, whose spots cannot be compared.
    night = replace(night, consumption_mode="shared", floor_plan="Seated")
    morning = replace(morning, consumption_mode="shared", floor_plan="Standing")
    price_book = replace(
        price_book,
        entries=tuple(
            replace(entry, billing_basis="per_capacity_unit")
            for entry in price_book.entries
        ),
    )
    clubhouse = replace(
        setup.assets[0],
        floor_plans=(
            setup_file.FloorPlan(name="Seated", spot_word="seat", spots=("",) * 12),
            setup_file.FloorPlan(name="Standing", spot_word="place", spots=("",) * 40),
        ),
    )
    setup = replace(
        setup,
        format=3,
        assets=(clubhouse,),
        availability_calendars=(around_the_clock,),
        booking_configurations=(night, morning),
        price_books=(price_book,),
    )
    # Exercise the file reader as well as model validation and atomic loading.
    setup = parse(setup_file.write(setup))
    if morning_hour == 6:
        with pytest.raises(SetupError, match="covers the same time as container"):
            setups.load(setup, today=before)
        assert not Organization.objects.exists()
        assert not BookingConfiguration.across_organizations.exists()
    else:
        organization = setups.load(setup, today=before)
        assert BookingConfiguration.objects.of(organization).count() == 2
        assert in_order(setups.export(organization)) == in_order(setup)


@pytest.mark.django_db
def test_the_commands_load_check_and_export(tmp_path: Path) -> None:
    out = StringIO()

    call_command("load_setup", str(EXAMPLE), "--check", stdout=out)
    assert "passes every check" in out.getvalue()
    assert "Nothing is loaded." in out.getvalue()
    assert not Organization.objects.exists()

    out = StringIO()
    call_command("load_setup", str(EXAMPLE), stdout=out)
    assert out.getvalue().strip() == (
        "Loaded the organization harbourview: 3 assets, 5 booking "
        "configurations and 2 price books."
    )
    organization = Organization.objects.get(subdomain="harbourview")

    out = StringIO()
    call_command("export_setup", "harbourview", stdout=out)
    assert in_order(parse(out.getvalue())) == setups.export(organization)
    # The export is a setup file: it passes the check as it is, once the
    # organization it came from is out of the way.
    exported = tmp_path / "harbourview.toml"
    exported.write_text(out.getvalue().replace('"harbourview"', '"seaview"'))
    call_command("load_setup", str(exported), "--check", stdout=StringIO())


@pytest.mark.django_db
def test_a_setup_is_loaded_from_standard_input_when_the_file_is_a_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The image carries no client's setup; an operator pipes one in.
    monkeypatch.setattr("sys.stdin", StringIO(EXAMPLE.read_text(encoding="utf-8")))
    out = StringIO()

    call_command("load_setup", "-", stdout=out)

    assert out.getvalue().strip().startswith("Loaded the organization harbourview:")
    assert Organization.objects.filter(subdomain="harbourview").exists()


@pytest.mark.django_db
def test_standard_input_is_checked_or_refused_by_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(EXAMPLE.read_text(encoding="utf-8")))
    out = StringIO()
    call_command("load_setup", "-", "--check", stdout=out)
    assert "standard input passes every check" in out.getvalue()
    assert "Nothing is loaded." in out.getvalue()
    assert not Organization.objects.exists()

    monkeypatch.setattr("sys.stdin", StringIO(""))
    with pytest.raises(CommandError) as refused:
        call_command("load_setup", "-", stdout=StringIO())
    assert str(refused.value).startswith("standard input is not loaded:")
    assert not Organization.objects.exists()

    monkeypatch.setattr(
        "sys.stdin", TextIOWrapper(BytesIO(b"\xff\xfe"), encoding="utf-8")
    )
    with pytest.raises(CommandError) as refused:
        call_command("load_setup", "-", stdout=StringIO())
    assert str(refused.value) == "standard input is not UTF-8 text."


@pytest.mark.django_db
def test_the_commands_say_what_is_wrong(tmp_path: Path) -> None:
    short = tmp_path / "lakeside.toml"
    short.write_text(SMALLEST.replace('up_to = "8 h"', 'up_to = "4 h"'))

    with pytest.raises(CommandError) as refused:
        call_command("load_setup", str(short), stdout=StringIO())
    assert "lakeside.toml is not loaded:" in str(refused.value)
    assert "price_books[1].entries[1].tiers: The price tiers reach 4 h" in str(
        refused.value
    )
    assert not Organization.objects.exists()

    unknown = tmp_path / "kind.toml"
    unknown.write_text(SMALLEST.replace("format = 1", "format = 1\nmembers = 3"))
    with pytest.raises(CommandError) as refused:
        call_command("load_setup", str(unknown), stdout=StringIO())
    assert "members: is not part of the format." in str(refused.value)

    with pytest.raises(CommandError) as refused:
        call_command("load_setup", str(tmp_path / "nothing.toml"), stdout=StringIO())
    assert "cannot be read" in str(refused.value)

    with pytest.raises(CommandError) as refused:
        call_command("export_setup", "nobody", stdout=StringIO())
    assert "There is no organization with the subdomain nobody." in str(refused.value)
