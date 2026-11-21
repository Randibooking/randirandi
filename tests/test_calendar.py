# SPDX-License-Identifier: AGPL-3.0-only
"""Executable availability rules, independent of customer-specific names."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from randirandi import setup_file
from randirandi.core import calendar, setup
from randirandi.core.calendar import (
    BookableInterval,
    CalendarLimitError,
    bookable_intervals,
)
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AvailabilityCalendar,
    AvailableSpan,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    ConfigurationCategory,
    Member,
    MemberStatus,
    Organization,
    Origin,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Role,
    RoleCategory,
    Space,
    TakenTime,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2027, 3, 1)
ZONE = ZoneInfo("Europe/Oslo")


def at(day: date = DAY, hour: int = 0, minute: int = 0, *, fold: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, fold=fold), ZONE).astimezone(UTC)


@dataclass
class Example:
    org: Organization
    member: Member
    asset: Asset
    kind: BookingType
    config: BookingConfiguration
    availability: AvailabilityCalendar
    category: BookingCategory
    price: PriceBookEntry

    def offers(
        self, day: date = DAY, *, quantity: int = 1, **changes: Any
    ) -> tuple[BookableInterval, ...]:
        parameters: dict[str, Any] = {
            "member": self.member,
            "asset": self.asset,
            "booking_type": self.kind,
            "start_date": day,
            "end_date": day + timedelta(days=1),
            "quantity": quantity,
            "now": at(day - timedelta(days=1)),
        }
        parameters.update(changes)
        return bookable_intervals(**parameters)

    def available(
        self, spans: list[tuple[int, int]], *, weekday: int | None = None
    ) -> None:
        rows = AvailableSpan.objects.of(self.org).filter(
            availability_calendar=self.availability
        )
        if weekday is not None:
            rows = rows.filter(weekday=weekday)
        rows.delete()
        for day in range(7) if weekday is None else [weekday]:
            for first, last in spans:
                AvailableSpan.objects.create(
                    organization=self.org,
                    availability_calendar=self.availability,
                    weekday=day,
                    start_time=first,
                    end_time=last,
                )

    def configure(self, **changes: Any) -> None:
        if "consumption_mode" in changes and "floor_plan" not in changes:
            # A shared container books its asset's floor plan.
            changes["floor_plan_id"] = (
                made_up.floor_plan_of(self.asset).pk
                if changes["consumption_mode"] == "shared"
                else None
            )
        for name, value in changes.items():
            setattr(self.config, name, value)
        self.config.full_clean()
        self.config.save()
        PriceTier.objects.of(self.org).filter(price_book_entry=self.price).update(
            up_to=self.config.maximum_length
        )

    def capacity(self, amount: int, mode: str = "shared") -> None:
        made_up.spots(made_up.floor_plan_of(self.asset), amount)
        self.configure(consumption_mode=mode)

    def claim(
        self,
        start: datetime,
        end: datetime,
        *,
        asset: Asset | None = None,
        mode: str = "exclusive",
        units: tuple[int, ...] = (),
        state: str = "confirmed",
    ) -> TakenTime:
        on = asset or self.asset
        return TakenTime.objects.take(
            organization=self.org,
            asset=on,
            starts_at=start,
            ends_at=end,
            mode=mode,
            quantity=len(units) if mode == "shared" else 1,
            state=state,
            units=made_up.spots_of(made_up.floor_plan_of(on), *units)
            if mode == "shared"
            else None,
        )


@pytest.fixture
def made() -> Example:
    org = made_up.organization()
    role = Role.objects.create(organization=org, name="Member")
    category = BookingCategory.objects.create(organization=org, name="Private")
    RoleCategory.objects.create(organization=org, role=role, booking_category=category)
    member = Member.objects.create(
        organization=org,
        role=role,
        email="test@example.com",
        first_name="Test",
        last_name="Member",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    asset = Asset.objects.create(organization=org, name="Pool")
    kind = BookingType.objects.create(organization=org, name="Visit")
    book = PriceBook.objects.create(organization=org, name="Prices")
    config = BookingConfiguration.objects.create(
        organization=org,
        asset=asset,
        booking_type=kind,
        period_type="flexible",
        period_start=600,
        period_end=840,
        end_day=0,
        period_start_weekdays=list(range(7)),
        interval=60,
        minimum_length=60,
        maximum_length=240,
        status="enabled",
        price_book=book,
    )
    ConfigurationCategory.objects.create(
        organization=org, booking_configuration=config, booking_category=category
    )
    entry = PriceBookEntry.objects.create(
        organization=org,
        price_book=book,
        booking_configuration=config,
        booking_category=category,
        tiers_go_by="length",
    )
    PriceTier.objects.create(
        organization=org, price_book_entry=entry, up_to=240, paid_per=60, amount=100
    )
    availability = AvailabilityCalendar.objects.create(
        organization=org, asset=asset, start_date=date(2020, 1, 1)
    )
    result = Example(org, member, asset, kind, config, availability, category, entry)
    result.available([(0, 1440)])
    return result


def times(offers: tuple[BookableInterval, ...]) -> set[tuple[datetime, datetime]]:
    return {(offer.starts_at, offer.ends_at) for offer in offers}


def test_flexible_grid_returns_every_start_and_length_with_metadata(
    made: Example,
) -> None:
    offers = made.offers()
    assert times(offers) == {
        (at(hour=a), at(hour=b)) for a in range(10, 14) for b in range(a + 1, 15)
    }
    assert len(offers) == 10
    assert all(
        o.category_ids == (made.category.pk,)
        and o.configuration_id == made.config.pk
        and o.consumption_mode == "exclusive"
        and o.quantity == 1
        for o in offers
    )
    assert all(o.starts_at.tzinfo is UTC for o in offers)
    assert TakenTime.objects.of(made.org).count() == 0


def test_availability_intersection_keeps_original_grid(made: Example) -> None:
    made.available([(10 * 60 + 30, 13 * 60 + 30)])
    assert times(made.offers()) == {
        (at(hour=11), at(hour=12)),
        (at(hour=11), at(hour=13)),
        (at(hour=12), at(hour=13)),
    }


@pytest.mark.parametrize(
    ("entered", "minimum", "lone_hour"), [(None, 60, True), (120, 120, False)]
)
def test_a_minimum_left_out_makes_no_gap_and_a_longer_one_leaves_its_gaps(
    made: Example, entered: int | None, minimum: int, lone_hour: bool
) -> None:
    # Left out, as a setup file or a form leaves it, or two intervals.
    fields: dict[str, Any] = {"minimum_length": entered}
    for field, value in fields.items():
        setattr(made.config, field, value)
    made.config.full_clean(exclude=["number"])
    made.config.save()
    assert made.config.minimum_length == minimum
    # Available 10:00 to 11:00 and 12:00 to 14:00: the hour from 10:00 is
    # bookable only when the minimum is one interval. The gap a longer
    # minimum leaves is accepted.
    made.available([(600, 660), (720, 840)])

    offers = times(made.offers())

    assert ((at(hour=10), at(hour=11)) in offers) is lone_hour
    assert (at(hour=12), at(hour=14)) in offers


def test_contiguous_spans_merge_but_a_gap_cannot_be_booked_across(
    made: Example,
) -> None:
    made.available([(600, 660), (660, 720), (750, 840)])
    offers = times(made.offers())
    assert (at(hour=10), at(hour=12)) in offers
    assert (at(hour=11), at(hour=13)) not in offers
    assert (at(hour=13), at(hour=14)) in offers


@pytest.mark.parametrize("missing", ["all", "weekday", "not_started"])
def test_missing_applicable_availability_gives_no_offers(
    made: Example, missing: str
) -> None:
    if missing == "all":
        made.availability.delete()
    elif missing == "weekday":
        made.available([], weekday=DAY.weekday())
    else:
        made.availability.start_date = DAY + timedelta(days=1)
        made.availability.save()
    assert made.offers() == ()


@pytest.mark.parametrize("fits", [False, True])
def test_fixed_night_needs_all_time_on_both_days(made: Example, fits: bool) -> None:
    made.configure(
        period_type="fixed", period_start=17 * 60, period_end=8 * 60, end_day=1
    )
    made.available([(17 * 60, 1440)], weekday=0)
    made.available([(0, (8 if fits else 7) * 60)], weekday=1)
    result = made.offers()
    assert times(result) == (
        {(at(hour=17), at(DAY + timedelta(days=1), 8))} if fits else set()
    )


def test_new_calendar_replaces_old_on_each_occupied_day(made: Example) -> None:
    made.configure(
        period_type="fixed", period_start=17 * 60, period_end=8 * 60, end_day=1
    )
    new = AvailabilityCalendar.objects.create(
        organization=made.org, asset=made.asset, start_date=DAY + timedelta(days=1)
    )
    assert made.offers() == ()
    AvailableSpan.objects.create(
        organization=made.org,
        availability_calendar=new,
        weekday=1,
        start_time=0,
        end_time=480,
    )
    assert len(made.offers()) == 1
    assert made.offers(DAY + timedelta(days=1)) == ()


def test_future_calendar_does_not_replace_current_early(made: Example) -> None:
    AvailabilityCalendar.objects.create(
        organization=made.org, asset=made.asset, start_date=DAY + timedelta(days=1)
    )
    assert len(made.offers()) == 10


def test_fixed_multiple_days_are_not_cut_at_midnight(made: Example) -> None:
    made.configure(
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=3,
        period_start_weekdays=[0],
    )
    assert times(made.offers()) == {(at(hour=17), at(DAY + timedelta(days=3), 8))}
    made.available([], weekday=2)
    assert made.offers() == ()


def test_calendar_weeks_include_last_start_and_keep_next_day_occupancy(
    made: Example,
) -> None:
    # Week 53 of 2026, from Monday 28 December to Sunday 3 January.
    sunday = date(2027, 1, 3)
    made.configure(
        period_type="fixed",
        period_start=17 * 60,
        period_end=8 * 60,
        end_day=1,
        start_date=sunday - timedelta(days=6),
        stop_date=sunday,
        period_start_weekdays=[6],
    )
    assert times(made.offers(sunday)) == {
        (at(sunday, 17), at(sunday + timedelta(days=1), 8))
    }
    assert made.offers(sunday - timedelta(days=1)) == ()
    assert made.offers(sunday + timedelta(days=1)) == ()
    assert made.offers(sunday - timedelta(days=7)) == ()
    assert made.offers(sunday + timedelta(days=7)) == ()


def test_flexible_after_midnight_starts_use_the_prior_dated_period(
    made: Example,
) -> None:
    sunday = DAY - timedelta(days=1)
    made.configure(
        period_start=22 * 60,
        period_end=2 * 60,
        end_day=1,
        start_date=sunday - timedelta(days=6),
        stop_date=sunday,
        period_start_weekdays=[6],
    )
    assert times(made.offers()) == {
        (at(hour=0), at(hour=1)),
        (at(hour=0), at(hour=2)),
        (at(hour=1), at(hour=2)),
    }


def test_now_filters_exact_moments_without_extra_notice_policy(made: Example) -> None:
    assert len(made.offers(now=at(hour=12))) == 3
    assert len(made.offers(now=at(hour=12) + timedelta(microseconds=1))) == 1


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "wrong_type",
        "no_role_category",
        "no_config_category",
        "revoked",
        "deleted_member",
    ],
)
def test_permissions_and_current_stored_state_are_required(
    made: Example, change: str
) -> None:
    if change == "disabled":
        BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
            status="disabled"
        )
    elif change == "wrong_type":
        made.kind = BookingType.objects.create(organization=made.org, name="Other")
    elif change == "no_role_category":
        RoleCategory.objects.of(made.org).delete()
    elif change == "no_config_category":
        ConfigurationCategory.objects.of(made.org).delete()
    elif change == "revoked":
        Member.objects.of(made.org).filter(pk=made.member.pk).update(status="disabled")
    else:
        Member.objects.of(made.org).filter(pk=made.member.pk).delete()
    assert made.offers() == ()


@pytest.mark.parametrize(
    "change",
    [
        "missing_entry",
        "no_tiers",
        "short_tiers",
        "wrong_book",
        "unknown_mode",
        "overnight_clock_tiers",
    ],
)
def test_a_category_needs_a_complete_current_price(made: Example, change: str) -> None:
    if change == "missing_entry":
        made.price.delete()
    elif change == "no_tiers":
        PriceTier.objects.of(made.org).delete()
    elif change == "short_tiers":
        PriceTier.objects.of(made.org).update(up_to=120)
    elif change == "wrong_book":
        book = PriceBook.objects.create(organization=made.org, name="Other")
        made.price.price_book = book
        made.price.save()
    elif change == "unknown_mode":
        made.price.tiers_go_by = "unknown"
        made.price.save()
    else:
        made.configure(period_start=22 * 60, period_end=2 * 60, end_day=1)
        made.price.tiers_go_by = "time of day"
        made.price.save()
    assert made.offers() == ()


def test_explicit_zero_price_is_available(made: Example) -> None:
    PriceTier.objects.of(made.org).update(amount=0)
    assert len(made.offers()) == 10


def _book(made: Example) -> PriceBook:
    """The example container's price book; it is with payment."""
    book = made.config.price_book
    assert book is not None
    return book


def test_only_allowed_priced_categories_are_returned(made: Example) -> None:
    extra = BookingCategory.objects.create(organization=made.org, name="Internal")
    ConfigurationCategory.objects.create(
        organization=made.org, booking_configuration=made.config, booking_category=extra
    )
    entry = PriceBookEntry.objects.create(
        organization=made.org,
        price_book=_book(made),
        booking_configuration=made.config,
        booking_category=extra,
        tiers_go_by="length",
    )
    PriceTier.objects.create(
        organization=made.org, price_book_entry=entry, up_to=240, paid_per=60, amount=0
    )
    assert made.offers()[0].category_ids == (made.category.pk,)
    RoleCategory.objects.create(
        organization=made.org, role=made.member.role, booking_category=extra
    )
    assert set(made.offers()[0].category_ids) == {made.category.pk, extra.pk}
    PriceTier.objects.of(made.org).filter(price_book_entry=entry).delete()
    assert made.offers()[0].category_ids == (made.category.pk,)


@pytest.mark.parametrize(
    "mode,state",
    [
        ("exclusive", "held"),
        ("exclusive", "confirmed"),
        ("exclusive", "blocked"),
        ("shared", "held"),
        ("shared", "confirmed"),
        ("shared", "blocked"),
    ],
)
def test_every_holding_state_consumes_capacity(
    made: Example, mode: str, state: str
) -> None:
    made.capacity(2)
    made.claim(
        at(hour=11),
        at(hour=12),
        mode=mode,
        state=state,
        units=(1,) if mode == "shared" else (),
    )
    intervals = times(made.offers(quantity=2))
    assert (at(hour=10), at(hour=11)) in intervals
    assert (at(hour=12), at(hour=14)) in intervals
    assert (at(hour=10), at(hour=12)) not in intervals
    if mode == "shared":
        assert (at(hour=10), at(hour=14)) in times(made.offers())


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_released_states_free_capacity(made: Example, state: str) -> None:
    claim = made.claim(at(hour=10), at(hour=14))
    assert made.offers() == ()
    claim.state = state
    claim.save(update_fields=["state"])
    assert len(made.offers()) == 10


def test_quantity_must_fit_at_every_instant_not_average_or_total(made: Example) -> None:
    made.capacity(4)
    made.claim(at(hour=10), at(hour=11), mode="shared", units=(1, 2))
    made.claim(at(hour=11), at(hour=12), mode="shared", units=(1, 2))
    assert (at(hour=10), at(hour=12)) in times(made.offers(quantity=2))
    assert (at(hour=10), at(hour=12)) not in times(made.offers(quantity=3))
    made.claim(
        at(hour=10, minute=30), at(hour=10, minute=31), mode="shared", units=(3,)
    )
    assert (at(hour=10), at(hour=12)) not in times(made.offers(quantity=2))


def test_a_booking_keeps_its_spots_so_a_longer_one_needs_one_spot_free(
    made: Example,
) -> None:
    made.capacity(2)
    made.claim(at(hour=10), at(hour=11), mode="shared", units=(1,))
    made.claim(at(hour=11), at(hour=12), mode="shared", units=(2,))
    # Each spot is free for one of the two hours, none for both.
    assert (at(hour=10), at(hour=12)) not in times(made.offers())
    assert (at(hour=10), at(hour=11)) in times(made.offers())
    assert (at(hour=11), at(hour=12)) in times(made.offers())
    assert all(offer.starts_at >= at(hour=12) for offer in made.offers(quantity=2))


def test_a_taken_spot_leaves_the_others_and_an_exclusive_one_needs_all_free(
    made: Example,
) -> None:
    made.capacity(4)
    made.claim(at(hour=10), at(hour=14), mode="shared", units=(4,))
    assert len(made.offers(quantity=3)) == 10
    assert made.offers(quantity=4) == ()
    made.configure(consumption_mode="exclusive")
    assert made.offers() == ()


def test_a_marked_asset_competes_with_its_space_but_siblings_do_not(
    made: Example,
) -> None:
    room = Space.objects.create(organization=made.org, name="Laundry")
    made.asset.space = room
    made.asset.save()
    whole = Asset.objects.create(
        organization=made.org, name="Whole laundry", space=room, whole_space=True
    )
    other = Asset.objects.create(
        organization=made.org, name="Other machine", space=room
    )
    made.claim(at(hour=10), at(hour=11), asset=other)
    assert len(made.offers()) == 10
    held = made.claim(at(hour=14), at(hour=15), asset=whole)
    assert len(made.offers()) == 10  # Touching only.
    held.revise(
        starts_at=at(hour=13), ends_at=at(hour=15), mode="exclusive", quantity=1
    )
    assert len(made.offers()) == 6


def offer_on(made: Example, asset: Asset, **changes: Any) -> Example:
    config = BookingConfiguration(
        organization=made.org,
        asset=asset,
        booking_type=made.kind,
        status="enabled",
        period_type=made.config.period_type,
        period_start=made.config.period_start,
        period_end=made.config.period_end,
        end_day=made.config.end_day,
        period_start_weekdays=made.config.period_start_weekdays,
        interval=made.config.interval,
        minimum_length=made.config.minimum_length,
        maximum_length=made.config.maximum_length,
        consumption_mode=made.config.consumption_mode,
        price_book_id=made.config.price_book_id,
    )
    for name, value in changes.items():
        setattr(config, name, value)
    if config.consumption_mode == "shared" and "floor_plan" not in changes:
        config.floor_plan_id = made_up.floor_plan_of(asset).pk
    config.save()
    ConfigurationCategory.objects.create(
        organization=made.org,
        booking_configuration=config,
        booking_category=made.category,
    )
    entry = PriceBookEntry.objects.create(
        organization=made.org,
        price_book=_book(made),
        booking_configuration=config,
        booking_category=made.category,
        tiers_go_by="length",
    )
    PriceTier.objects.create(
        organization=made.org,
        price_book_entry=entry,
        up_to=config.maximum_length,
        paid_per=60,
        amount=100,
    )
    availability = AvailabilityCalendar.objects.create(
        organization=made.org, asset=asset, start_date=date(2020, 1, 1)
    )
    result = Example(
        made.org,
        made.member,
        asset,
        made.kind,
        config,
        availability,
        made.category,
        entry,
    )
    result.available([(0, 1440)])
    return result


def test_any_offer_on_a_marked_asset_sees_every_asset_of_its_space(
    made: Example,
) -> None:
    room = Space.objects.create(organization=made.org, name="Room")
    made.asset.space = room
    made.asset.save()
    whole = Asset(organization=made.org, name="Entire room", space=room)
    whole.whole_space = True
    whole.save(floor_plan=AssetInventory(capacity=8, unit="seat"))
    # One container in the space at a time under the mark .
    made.configure(status="disabled")
    offered = offer_on(made, whole)
    claim = made.claim(at(hour=10), at(hour=14))
    assert offered.offers() == ()
    # Its places too: a claim on a marked asset covers its space (W17).
    offered.configure(consumption_mode="shared")
    assert offered.offers() == ()
    claim.state = "cancelled"
    claim.save(update_fields=["state"])
    assert len(offered.offers(quantity=8)) == 10


def test_other_rooms_and_assets_without_a_space_do_not_take_this_time(
    made: Example,
) -> None:
    other = Asset.objects.create(organization=made.org, name="Other loose pool")
    made.claim(at(hour=10), at(hour=14), asset=other)
    room = Space.objects.create(organization=made.org, name="Another room")
    whole = Asset.objects.create(
        organization=made.org, name="Entire other room", space=room, whole_space=True
    )
    made.claim(at(hour=10), at(hour=14), asset=whole)
    assert len(made.offers()) == 10


@pytest.mark.parametrize("target", ["asset", "booking_type", "member"])
def test_foreign_organization_objects_return_no_offers(
    made: Example, target: str
) -> None:
    other = made_up.everything("foreign")
    value = {
        "asset": other.asset,
        "booking_type": other.booking_type,
        "member": other.member,
    }[target]
    changes: dict[str, Any] = {target: value}
    assert made.offers(**changes) == ()


def test_changed_role_is_reloaded_instead_of_trusting_caller(made: Example) -> None:
    role = Role.objects.create(organization=made.org, name="No permissions")
    Member.objects.of(made.org).filter(pk=made.member.pk).update(role=role)
    assert made.member.role_id != role.pk
    assert made.offers() == ()


def test_current_capacity_is_reloaded_after_a_reduction(made: Example) -> None:
    made.capacity(4)
    assert len(made.offers(quantity=4)) == 10
    held = made_up.floor_plan_of(made.asset)
    made_up.fewer_spots(made_up.floor_plan_of(made.asset), 2)
    assert held.capacity == 4
    assert made.offers(quantity=4) == ()
    assert len(made.offers(quantity=2)) == 10


@pytest.mark.parametrize(
    "day,elapsed_hours", [(date(2027, 3, 27), 14), (date(2027, 10, 30), 16)]
)
def test_fixed_nights_keep_local_endpoints_across_clock_changes(
    made: Example, day: date, elapsed_hours: int
) -> None:
    made.configure(
        period_type="fixed", period_start=17 * 60, period_end=8 * 60, end_day=1
    )
    (offer,) = made.offers(day)
    assert offer.starts_at == at(day, 17)
    assert offer.ends_at == at(day + timedelta(days=1), 8)
    assert offer.ends_at - offer.starts_at == timedelta(hours=elapsed_hours)


def test_spring_gap_skips_nonexistent_starts_and_uses_elapsed_lengths(
    made: Example,
) -> None:
    day = date(2027, 3, 28)
    made.configure(period_start=60, period_end=240, maximum_length=120)
    offered = times(made.offers(day))
    assert (at(day, 1), at(day, 3)) in offered
    assert (at(day, 1), at(day, 4)) in offered
    assert all(start.astimezone(ZONE).hour != 2 for start, _ in offered)
    assert all(
        end - start in (timedelta(hours=1), timedelta(hours=2))
        for start, end in offered
    )


def test_fall_fold_returns_both_starts_as_distinct_instants(made: Example) -> None:
    day = date(2027, 10, 31)
    made.configure(period_start=120, period_end=240, maximum_length=60)
    offers = made.offers(day)
    repeated = [offer for offer in offers if offer.starts_at.astimezone(ZONE).hour == 2]
    assert len(repeated) == 2
    assert {offer.starts_at.astimezone(ZONE).fold for offer in repeated} == {0, 1}
    assert all(
        offer.ends_at - offer.starts_at == timedelta(hours=1) for offer in offers
    )


def test_short_window_inside_repeated_hour_does_not_open_the_gap(made: Example) -> None:
    day = date(2027, 10, 31)
    made.configure(
        period_start=120,
        period_end=240,
        interval=15,
        minimum_length=15,
        maximum_length=60,
    )
    made.available([(150, 165)])
    offers = made.offers(day)
    assert times(offers) == {
        (at(day, 2, 30, fold=fold), at(day, 2, 45, fold=fold)) for fold in (0, 1)
    }


@pytest.mark.parametrize("day", [date(2027, 3, 28), date(2027, 10, 31)])
def test_fixed_nonexistent_or_ambiguous_endpoint_is_not_guessed(
    made: Example, day: date
) -> None:
    made.configure(period_type="fixed", period_start=150, period_end=240)
    assert made.offers(day) == ()


def test_capacity_compares_exact_instants_within_a_repeated_hour(made: Example) -> None:
    day = date(2027, 10, 31)
    made.configure(period_start=120, period_end=240, maximum_length=60)
    made.claim(at(day, 2, fold=0), at(day, 2, fold=1))
    offered = times(made.offers(day))
    assert (at(day, 2, fold=0), at(day, 2, fold=1)) not in offered
    assert (at(day, 2, fold=1), at(day, 3)) in offered


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_quantity_is_a_positive_integer(made: Example, quantity: Any) -> None:
    with pytest.raises(ValueError, match="positive whole"):
        made.offers(quantity=quantity)


@pytest.mark.parametrize("length", [0, -1, 32])
def test_range_is_bounded_and_not_silently_truncated(
    made: Example, length: int
) -> None:
    with pytest.raises(CalendarLimitError, match="one and 31"):
        made.offers(end_date=DAY + timedelta(days=length))


def test_range_uses_dates_and_now_needs_a_timezone(made: Example) -> None:
    with pytest.raises(ValueError, match="local dates"):
        made.offers(start_date=at())
    with pytest.raises(ValueError, match="time zone"):
        made.offers(now=datetime(2027, 3, 1))  # noqa: DTZ001 -- deliberately invalid
    with pytest.raises(ValueError, match="supported calendar range"):
        made.offers(date.max - timedelta(days=1))


@pytest.mark.parametrize("limit", ["MAX_RESULTS", "MAX_CANDIDATES"])
def test_expansion_limits_fail_instead_of_returning_a_partial_calendar(
    made: Example, limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar, limit, 2)
    with pytest.raises(CalendarLimitError, match="fewer days"):
        made.offers()
    assert not connection.in_atomic_block
    # READ ONLY was transaction-local; normal writes still work afterwards.
    Member.objects.of(made.org).filter(pk=made.member.pk).update(
        first_name="Still writable"
    )


def test_quantity_incompatible_with_mode_returns_no_offers_before_expansion(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar, "MAX_CANDIDATES", 1)
    assert made.offers(quantity=2) == ()
    made.capacity(4)
    assert made.offers(quantity=5) == ()


def test_calendar_refuses_an_ambient_write_transaction(made: Example) -> None:
    with transaction.atomic(), pytest.raises(RuntimeError, match="own read-only"):
        made.offers()


@pytest.mark.parametrize("pending_write", [False, True])
def test_calendar_refuses_manual_transactions_without_changing_them(
    made: Example, pending_write: bool
) -> None:
    transaction.set_autocommit(False)
    try:
        if pending_write:
            Member.objects.of(made.org).filter(pk=made.member.pk).update(
                first_name="Uncommitted"
            )
        with pytest.raises(RuntimeError, match="own read-only"):
            made.offers()
        assert not connection.get_autocommit()
        if pending_write:
            assert (
                Member.objects.of(made.org).get(pk=made.member.pk).first_name
                == "Uncommitted"
            )
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)
    assert Member.objects.of(made.org).get(pk=made.member.pk).first_name == "Test"
    assert len(made.offers()) == 10


@pytest.mark.parametrize("weekdays", [[0, 1], [0, 6]])
def test_own_overlapping_periods_are_not_offered_when_validation_is_bypassed(
    made: Example, weekdays: list[int]
) -> None:
    made.configure(
        period_type="fixed",
        period_start=600,
        period_end=840,
        end_day=1,
        period_start_weekdays=[0],
    )
    BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
        period_start_weekdays=weekdays
    )
    made.config.refresh_from_db()
    with pytest.raises(ValidationError, match="longer than the time"):
        made.config.full_clean()
    assert made.offers(end_date=DAY + timedelta(days=2)) == ()


def test_exclusive_containers_on_one_asset_both_offer_the_time_they_share(
    made: Example,
) -> None:
    clone = offer_on(
        made,
        Asset.objects.create(organization=made.org, name="Spare"),
        period_start=720,
        maximum_length=120,
    )
    BookingConfiguration.objects.of(made.org).filter(pk=clone.config.pk).update(
        asset=made.asset
    )
    # The clone covers 12:00 to 14:00, inside the configuration's 10:00 to
    # 14:00 (): both offer it, and the first booking takes it.
    offers = made.offers()
    assert {
        (offer.starts_at, offer.ends_at)
        for offer in offers
        if offer.configuration_id == clone.config.pk
    } == {
        (at(hour=12), at(hour=13)),
        (at(hour=12), at(hour=14)),
        (at(hour=13), at(hour=14)),
    }
    assert (at(hour=12), at(hour=14)) in times(
        tuple(offer for offer in offers if offer.configuration_id == made.config.pk)
    )


def test_configurations_that_bypass_the_validator_offer_neither_where_they_clash(
    made: Example,
) -> None:
    standing = AssetInventory.objects.create(
        organization=made.org, asset=made.asset, name="Standing"
    )
    BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
        consumption_mode="shared", floor_plan=made_up.floor_plan_of(made.asset)
    )
    made.config.refresh_from_db()
    clone = offer_on(
        made,
        Asset.objects.create(organization=made.org, name="Spare"),
        period_start=720,
        maximum_length=120,
    )
    # Shared on the other floor plan of the configuration's asset: their spots
    # cannot be compared, and the validator would have refused it.
    BookingConfiguration.objects.of(made.org).filter(pk=clone.config.pk).update(
        asset=made.asset, floor_plan=standing
    )
    # The clone covers 12:00 to 14:00, inside the configuration's 10:00 to
    # 14:00: neither offers that time, and the configuration keeps the rest.
    assert times(made.offers()) == {
        (at(hour=10), at(hour=11)),
        (at(hour=10), at(hour=12)),
        (at(hour=11), at(hour=12)),
    }
    assert {offer.configuration_id for offer in made.offers()} == {made.config.pk}


def test_one_snapshot_despite_a_concurrent_revocation_and_configuration_edit(
    made: Example,
) -> None:
    read_started, changed = Event(), Event()

    def reader() -> tuple[BookableInterval, ...]:
        paused = False

        def after_first_read(
            execute: Any, sql: str, params: Any, many: bool, context: Any
        ) -> Any:
            nonlocal paused
            result = execute(sql, params, many, context)
            if not paused and 'FROM "core_member"' in sql:
                paused = True
                read_started.set()
                assert changed.wait(timeout=10)
            return result

        try:
            with connection.execute_wrapper(after_first_read):
                return made.offers()
        finally:
            connection.close()

    def writer() -> None:
        try:
            assert read_started.wait(timeout=10)
            with transaction.atomic():
                Member.objects.of(made.org).filter(pk=made.member.pk).update(
                    status="disabled"
                )
                AvailableSpan.objects.of(made.org).delete()
            changed.set()
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(reader)
        second = executor.submit(writer)
        assert len(first.result(timeout=20)) == 10
        second.result(timeout=20)
        assert made.offers() == ()


def test_actual_application_login_reads_without_reserving_or_leaving_read_only(
    made: Example,
) -> None:
    with application_login():
        assert len(made.offers()) == 10
        made.claim(at(hour=10), at(hour=14))
        assert made.offers() == ()


def scenario(filename: str) -> tuple[Organization, Member]:
    org = setup.load(setup_file.read(SETUPS / filename), today=DAY)
    assert org.standard_role_id is not None
    member = Member.objects.create(
        organization=org,
        role_id=org.standard_role_id,
        email="scenario@example.com",
        status="active",
        origin="member import",
    )
    return org, member


def offers_for(
    org: Organization,
    member: Member,
    asset_name: str,
    kind_name: str,
    *,
    day: date = DAY,
    quantity: int = 1,
) -> tuple[BookableInterval, ...]:
    return bookable_intervals(
        member=member,
        asset=Asset.objects.of(org).get(name=asset_name),
        booking_type=BookingType.objects.of(org).get(name=kind_name),
        quantity=quantity,
        start_date=day,
        end_date=day + timedelta(days=1),
        now=at(day - timedelta(days=1)),
    )


def test_sauna_fixture_has_shared_daytime_and_exclusive_evening(made: Example) -> None:
    org, member = scenario("scenarios/sauna-palace.toml")
    daytime = offers_for(org, member, "Sauna 1", "Sauna visit", quantity=3)
    assert (at(hour=17), at(hour=18)) in times(daytime)
    assert (at(hour=17), at(hour=19)) not in times(daytime)
    assert (at(hour=18), at(hour=22)) in times(
        offers_for(org, member, "Sauna 1", "Private sauna")
    )
    assert offers_for(org, member, "Sauna 1", "Private sauna", quantity=2) == ()
    asset = Asset.objects.of(org).get(name="Sauna 1")
    TakenTime.objects.take(
        organization=org,
        asset=asset,
        starts_at=at(hour=12),
        ends_at=at(hour=14),
        state="confirmed",
        mode="shared",
        quantity=6,
        units=made_up.spots_of(made_up.floor_plan_of(asset), *range(1, 7)),
    )
    assert (at(hour=12), at(hour=14)) not in times(
        offers_for(org, member, "Sauna 1", "Sauna visit", quantity=3)
    )
    assert (at(hour=12), at(hour=14)) in times(
        offers_for(org, member, "Sauna 2", "Sauna visit", quantity=3)
    )
    assert (at(hour=12), at(hour=14)) in times(
        offers_for(org, member, "Gym", "Gym visit", quantity=3)
    )


def test_four_beds_take_spots_and_the_whole_room_needs_them_all() -> None:
    org, member = scenario("scenarios/four-beds.toml")
    room = Asset.objects.of(org).get(name="Sunshine")
    first_bed = made_up.spots_of(made_up.floor_plan_of(room), 1)
    # Bed 1 is taken every night from Monday to Friday.
    for days in range(5):
        day = DAY + timedelta(days=days)
        TakenTime.objects.take(
            organization=org,
            asset=room,
            starts_at=at(day, 17),
            ends_at=at(day + timedelta(days=1), 8),
            mode="shared",
            quantity=1,
            state="confirmed",
            units=first_bed,
        )
    assert len(offers_for(org, member, "Sunshine", "Overnight bed", quantity=3)) == 1
    assert offers_for(org, member, "Sunshine", "Overnight bed", quantity=4) == ()
    friday, saturday = (DAY + timedelta(days=days) for days in (4, 5))
    assert offers_for(org, member, "Sunshine", "Private bedroom", day=friday) == ()
    assert (
        len(offers_for(org, member, "Sunshine", "Private bedroom", day=saturday)) == 1
    )


def test_a_weekend_container_offers_the_whole_day_under_an_open_calendar() -> None:
    # Available around the clock: a weekend open house is an event, which the
    # setup file does not hold.
    org, member = scenario("fjordside.toml")
    saturday = DAY + timedelta(days=5)
    offered = offers_for(org, member, "Hall", "Hall hire", day=saturday)
    assert (at(saturday, 10), at(saturday, 22)) in times(offered)
    assert all(
        offer.starts_at >= at(saturday, 10) and offer.ends_at <= at(saturday, 22)
        for offer in offered
    )
