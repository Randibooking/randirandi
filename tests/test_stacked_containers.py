# SPDX-License-Identifier: AGPL-3.0-only
"""Containers stacked on one asset and across a space .

Containers may cover the same time on one asset, and across a space under the
whole-space mark. A booking takes the asset (exclusive) or its spots (shared),
whichever container sold it, and every container shows that time as taken. Only
two shared containers on one asset that book different floor plans are refused.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from randirandi.core import setup
from randirandi.core.models import Asset, BookingConfiguration, Member, Order, Status
from randirandi.core.ordering import OrderUnavailableError, create_order
from randirandi.setup_file import parse
from tests import made_up
from tests.capacity_app import application_login
from tests.test_booking_scenarios import NOW, Journey, loaded
from tests.test_calendar import DAY, ZONE, Example, at, offer_on
from tests.test_calendar import made as made
from tests.test_ordering import race, request
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
SATURDAY = DAY + timedelta(days=5)


@contextmanager
def loaded_text(text: str) -> Iterator[Journey]:
    """A scenario file's text, edited by the test, loaded as `loaded` does."""
    org = setup.load(parse(text), today=DAY)
    assert org.standard_role_id is not None
    member = Member.objects.create(
        organization=org,
        role_id=org.standard_role_id,
        email="scenario@example.com",
        status="active",
        origin="member import",
    )
    with application_login():
        yield Journey(org, member)


def scenario_text(name: str) -> str:
    return (SETUPS / "scenarios" / f"{name}.toml").read_text()


def replaced(text: str, old: str, new: str) -> str:
    """The text with its first `old` replaced by `new`."""
    assert old in text, old
    return text.replace(old, new, 1)


def starts(journey: Journey, number: int, day: date = DAY, **fields: Any) -> list[int]:
    """The local hours a container offers its bookings to start at on a day,
    for a booking of the given length (an hour by default)."""
    wanted = journey.request(number, at(day, 10), at(day, 11), **fields)
    return sorted(
        {
            offer.starts_at.astimezone(ZONE).hour
            for offer in journey.offers(wanted)
            if offer.configuration_id == wanted.configuration_id
        }
    )


def windows(journey: Journey, number: int, day: date = DAY) -> set[tuple[time, time]]:
    """The local start and end of every offer of a container on a day."""
    wanted = journey.request(number, at(day, 10), at(day, 11))
    return {
        (
            offer.starts_at.astimezone(ZONE).time(),
            offer.ends_at.astimezone(ZONE).time(),
        )
        for offer in journey.offers(wanted)
        if offer.configuration_id == wanted.configuration_id
    }


def test_a_brewing_session_and_a_tapping_hour_take_each_others_time() -> None:
    with loaded("scenarios/brew-lab.toml") as brewery:
        # Brewing, 5 hours, starts from 10:00 to 17:00; tapping hours from 10:00.
        assert starts(brewery, 1) == list(range(10, 18))
        assert starts(brewery, 2) == list(range(10, 22))
        brewing = brewery.request(1, at(DAY, 11), at(DAY, 16))
        brewery.buy(brewing, amount=150000)
        # The tapping answer shows 11:00 to 16:00 taken.
        assert starts(brewery, 2) == [10, 16, 17, 18, 19, 20, 21]
        assert starts(brewery, 1) == [16, 17]
        brewery.refuse(brewery.request(2, at(DAY, 12), at(DAY, 13)))
        # A tapping hour at 18:00 leaves no brewing start that day.
        tapping = brewery.request(2, at(DAY, 18), at(DAY, 19))
        brewery.buy(tapping, amount=30000)
        assert starts(brewery, 1) == []
        assert starts(brewery, 1, DAY + timedelta(days=1)) == list(range(10, 18))


def test_seats_and_the_whole_sauna_take_each_others_time() -> None:
    text = scenario_text("sauna-palace")
    # The visits run through the evening, and the private sauna by the hour.
    text = replaced(
        text,
        'period_start = "10:00"\nperiod_end = "18:00"',
        'period_start = "10:00"\nperiod_end = "22:00"',
    )
    text = replaced(
        text,
        'period_type = "fixed"\nperiod_start = "18:00"\nperiod_end = "22:00"\n'
        'end_day = 0\nperiod_start_weekdays = ["mon", "tue", "wed", "thu", "fri"]\n',
        'period_type = "flexible"\nperiod_start = "18:00"\nperiod_end = "22:00"\n'
        'end_day = 0\nperiod_start_weekdays = ["mon", "tue", "wed", "thu", "fri"]\n'
        'interval = "1 h"\nminimum_length = "1 h"\nmaximum_length = "4 h"\n',
    )
    with loaded_text(text) as sauna:
        assert 19 in starts(sauna, 2)
        two_seats = sauna.request(1, at(DAY, 19), at(DAY, 20), quantity=2)
        create_order(
            member=sauna.member,
            requests=[two_seats],
            now=NOW,
            accepted_version=made_up.accepted(sauna.member),
        )
        # The private sauna is not offered at 19:00 and is refused there.
        assert 19 not in starts(sauna, 2)
        assert {18, 20, 21} <= set(starts(sauna, 2))
        sauna.refuse(sauna.request(2, at(DAY, 19), at(DAY, 20)))
        # The other six seats are still offered.
        assert sauna.available(replace(two_seats, quantity=6))
        assert not sauna.available(replace(two_seats, quantity=7))
        # The private sauna sold at 20:00 takes all 8 spots.
        private = sauna.request(2, at(DAY, 20), at(DAY, 21))
        create_order(
            member=sauna.member,
            requests=[private],
            now=NOW,
            accepted_version=made_up.accepted(sauna.member),
        )
        assert 20 not in starts(sauna, 1)
        sauna.refuse(sauna.request(1, at(DAY, 20), at(DAY, 21)))
        assert 21 in starts(sauna, 1)


def test_two_shared_containers_on_one_floor_plan_share_its_spots() -> None:
    text = scenario_text("sauna-palace")
    # A second shared container on Sauna 1 and its only floor plan.
    second = (
        '[[booking_configurations]]\nnumber = 8\nasset = "Sauna 1"\n'
        'booking_type = "Sauna visit"\nperiod_type = "flexible"\n'
        'period_start = "10:00"\nperiod_end = "18:00"\nend_day = 0\n'
        'period_start_weekdays = ["mon", "tue", "wed", "thu", "fri"]\n'
        'interval = "1 h"\nminimum_length = "1 h"\nmaximum_length = "8 h"\n'
        'status = "enabled"\nprice_book = "Prices"\n'
        'booking_categories = ["private"]\nconsumption_mode = "shared"\n\n'
    )
    price = (
        "\n[[price_books.entries]]\nbooking_configuration = 8\n"
        'booking_category = "private"\ntiers_go_by = "length"\n'
        'billing_basis = "per_capacity_unit"\n'
        'tiers = [\n  { up_to = "8 h", paid_per = "1 h", amount = 50 },\n]\n'
    )
    text = replaced(text, "[[price_books]]", second + "[[price_books]]") + price
    with loaded_text(text) as sauna:
        first = replace(sauna.request(1, at(DAY, 10), at(DAY, 11)), spots=(5,))
        create_order(
            member=sauna.member,
            requests=[first],
            now=NOW,
            accepted_version=made_up.accepted(sauna.member),
        )
        other = replace(
            first,
            configuration_id=sauna.request(
                8, at(DAY, 10), at(DAY, 11)
            ).configuration_id,
        )
        # Spot 5 sold in the first container is taken in the second.
        with pytest.raises(OrderUnavailableError, match="picked spot is not free"):
            create_order(
                member=sauna.member,
                requests=[other],
                now=NOW,
                accepted_version=made_up.accepted(sauna.member),
            )
        assert sauna.available(replace(other, spots=(), quantity=7))
        assert not sauna.available(replace(other, spots=(), quantity=8))
        create_order(
            member=sauna.member,
            requests=[replace(other, spots=(6,))],
            now=NOW,
            accepted_version=made_up.accepted(sauna.member),
        )
        assert sauna.available(replace(first, spots=(), quantity=6))
        assert not sauna.available(replace(first, spots=(), quantity=7))
        assert Order.objects.of(sauna.org).count() == 2


def test_two_shared_containers_on_two_floor_plans_are_refused_and_listed() -> None:
    text = scenario_text("riverside-hall")
    # A standing container on the Friday of the seated concert, disabled so
    # that it loads, with the price it would need.
    standing = (
        '[[booking_configurations]]\nnumber = 5\nasset = "Riverside Hall"\n'
        'booking_type = "Club night"\nperiod_type = "fixed"\n'
        'period_start = "20:00"\nperiod_end = "22:00"\nend_day = 0\n'
        'period_start_weekdays = ["fri"]\nstatus = "disabled"\n'
        'price_book = "Prices"\nbooking_categories = ["private"]\n'
        'consumption_mode = "shared"\nfloor_plan = "Standing"\n\n'
    )
    price = (
        "\n[[price_books.entries]]\nbooking_configuration = 5\n"
        'booking_category = "private"\ntiers_go_by = "length"\n'
        'billing_basis = "per_capacity_unit"\n'
        'tiers = [\n  { up_to = "2 h", paid_per = "2 h", amount = 120 },\n]\n'
    )
    text = replaced(text, "[[price_books]]", standing + "[[price_books]]") + price
    friday = DAY + timedelta(days=4)
    with loaded_text(text) as hall:
        assert windows(hall, 2, friday) == {(time(19), time(22))}
        club = BookingConfiguration.objects.of(hall.org).get(number=5)
        club.status = Status.ENABLED
        with pytest.raises(ValidationError) as refused:
            club.save(now=NOW)
        assert refused.value.message_dict == {
            "status": [
                "It covers the same time as container 002 on Riverside Hall; the "
                "two book different floor plans of the asset."
            ]
        }
        # Written past the validator, neither offers the time they share.
        BookingConfiguration.objects.of(hall.org).filter(pk=club.pk).update(
            status=Status.ENABLED
        )
        assert windows(hall, 2, friday) == set()
        assert windows(hall, 5, friday) == set()
        output = StringIO()
        call_command("list_clashes", "riverside-hall", stdout=output)
        assert output.getvalue() == (
            "Container 002 on Riverside Hall and container 005 on Riverside Hall "
            "book different floor plans of one asset at the same time, so neither "
            "offers the time they share.\n"
        )


def test_a_booking_of_30_minutes_steps_takes_the_rows_of_a_60_minute_grid() -> None:
    text = scenario_text("brew-lab")
    # Tapping every half hour, brewing by the hour from one to five hours.
    text = replaced(
        text,
        'interval = "1 h"\nminimum_length = "1 h"\nmaximum_length = "1 h"',
        'interval = "30 min"\nminimum_length = "1 h"\nmaximum_length = "1 h"',
    )
    text = replaced(
        text,
        'interval = "1 h"\nminimum_length = "5 h"\nmaximum_length = "5 h"',
        'interval = "1 h"\nminimum_length = "1 h"\nmaximum_length = "5 h"',
    )
    with loaded_text(text) as brewery:
        assert (time(10, 30), time(11, 30)) in windows(brewery, 2)
        assert (time(10), time(11)) in windows(brewery, 1)
        tapping = brewery.request(2, at(DAY, 10, 30), at(DAY, 11, 30))
        create_order(
            member=brewery.member,
            requests=[tapping],
            now=NOW,
            accepted_version=made_up.accepted(brewery.member),
        )
        offered = windows(brewery, 1)
        # The booking takes the 10:00 and the 11:00 row; 12:00 is free.
        assert (time(10), time(11)) not in offered
        assert (time(11), time(12)) not in offered
        assert (time(12), time(13)) in offered
        assert (time(9), time(10)) not in offered


def test_two_orders_at_once_on_stacked_containers_are_one_order_and_one_refusal(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second order waits on the organization row and retries (40001), and
    fresh, finds the time taken, as in `test_ordering`."""
    # `offer_on` gives a new asset its own availability, so the one-hour offer
    # is made there and moved; that the validator saves two containers on
    # one asset at one time is `test_validator`'s and the brewery's above.
    tapping = offer_on(
        made,
        Asset.objects.create(organization=made.org, name="Spare"),
        maximum_length=60,
    )
    BookingConfiguration.objects.of(made.org).filter(pk=tapping.config.pk).update(
        asset=made.asset
    )
    brewing = request(made, starts_at=at(hour=11), ends_at=at(hour=13))
    one_hour = request(tapping, starts_at=at(hour=12), ends_at=at(hour=13))
    (first, second), states = race(made, brewing, one_hour, monkeypatch)
    assert isinstance(first, Order)
    assert isinstance(second, OrderUnavailableError)
    assert states == ["40001"]
    assert Order.objects.of(made.org).count() == 1


def test_a_room_booked_first_takes_the_whole_house() -> None:
    with loaded("scenarios/old-mill.toml") as mill:
        kitchen = mill.request(2, at(SATURDAY, 12), at(SATURDAY, 13))
        mill.buy(kitchen, amount=10000)
        # The Whole house, fixed 10:00 to 22:00, is not offered; the Hall is.
        house = mill.request(3, at(SATURDAY, 10), at(SATURDAY, 22))
        mill.refuse(house)
        assert windows(mill, 3, SATURDAY) == set()
        assert (time(12), time(13)) in windows(mill, 1, SATURDAY)


def test_the_whole_house_booked_first_takes_its_rooms() -> None:
    with loaded("scenarios/old-mill.toml") as mill:
        house = mill.request(3, at(SATURDAY, 10), at(SATURDAY, 22))
        mill.buy(house, amount=250000)
        # The Hall and the Kitchen show taken all day.
        assert windows(mill, 1, SATURDAY) == set()
        assert windows(mill, 2, SATURDAY) == set()
        mill.refuse(mill.request(1, at(SATURDAY, 12), at(SATURDAY, 13)))
        mill.refuse(mill.request(2, at(SATURDAY, 12), at(SATURDAY, 13)))
        # A weekday has no house hire, and the rooms stay free.
        assert (time(12), time(13)) in windows(mill, 1, DAY)


def test_one_order_with_two_lines_on_stacked_containers_adds_the_claims_up() -> None:
    with loaded("scenarios/brew-lab.toml") as brewery:
        brewing = brewery.request(1, at(DAY, 11), at(DAY, 16))
        overlapping = brewery.request(2, at(DAY, 12), at(DAY, 13))
        assert brewery.available(brewing) and brewery.available(overlapping)
        before = brewery.stored()
        with pytest.raises(OrderUnavailableError):
            create_order(
                member=brewery.member,
                requests=[brewing, overlapping],
                now=NOW,
                accepted_version=made_up.accepted(brewery.member),
            )
        assert brewery.stored() == before
        # Back to back in one cart is one order, two bookings.
        later = brewery.request(2, at(DAY, 18), at(DAY, 19))
        order = brewery.buy(brewing, later, amount=180000)
        assert order.bookings.count() == 2
