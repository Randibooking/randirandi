# SPDX-License-Identifier: AGPL-3.0-only
"""The member's own bookings, listed ."""

import json
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test import Client as Browser
from django.test.utils import CaptureQueriesContext

from randirandi.core import booking_outcome
from randirandi.core.models import (
    BookingState,
    Member,
    MemberStatus,
    Order,
    Origin,
    TakenTime,
)
from randirandi.core.ordering import create_order
from tests import made_up
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, sign_in
from tests.test_ordering import NOW, request
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_second_factor import clocks as clocks

pytestmark = pytest.mark.django_db(transaction=True)

PATH = "/booking/bookings/"
# One request a day, 10:00 to 12:00.


def day(made: Example, days: int, **changes: Any) -> Any:
    changes.setdefault("starts_at", at(DAY + timedelta(days=days), 10))
    changes.setdefault("ends_at", at(DAY + timedelta(days=days), 12))
    return request(made, **changes)


def order_of(made: Example, *days: int, member: Member | None = None) -> Order:
    return create_order(
        member=member or made.member,
        requests=[day(made, offset) for offset in days],
        now=min(NOW, at(DAY + timedelta(days=min(days) - 1))),
        accepted_version=made_up.accepted(member or made.member),
    )


def settle(order: Order, state: str = BookingState.CONFIRMED) -> None:
    TakenTime.objects.of(order.organization_id).filter(booking__order=order).update(
        state=state
    )
    Order.objects.of(order.organization_id).filter(pk=order.pk).update(
        hold_expires_at=None
    )


def other(made: Example) -> Member:
    return Member.objects.create(
        organization=made.org,
        role=made.member.role,
        email="zebedee.quill@example.com",
        first_name="Zebedee",
        last_name="Quill",
        phone="+4799887766",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )


def signed_in(made: Example) -> Browser:
    browser = Browser()
    assert sign_in(browser, made.member).status_code == 302
    return browser


def test_coming_and_earlier_hold_the_members_bookings_with_their_order(
    made: Example,
) -> None:
    past, coming = order_of(made, -3), order_of(made, 2)
    settle(past)
    settle(coming)
    found = booking_outcome.bookings(made.org, made.member, now=at(DAY))
    assert [item["order"] for item in found["earlier"]] == [
        {"id": str(past.public_id), "reference": past.reference, "conditions": True}
    ]
    assert [item["order"] for item in found["coming"]] == [
        {
            "id": str(coming.public_id),
            "reference": coming.reference,
            "conditions": True,
        }
    ]
    read = booking_outcome.read(made.org, made.member, coming.public_id)
    assert read is not None
    item = found["coming"][0]
    # The read adds a confirmed booking's Google Calendar address (step 5).
    google = read["bookings"][0].pop("google")
    assert google.startswith("https://calendar.google.com/")
    assert item == {**read["bookings"][0], "order": item["order"]}
    assert item["state"] == "confirmed"


def test_a_stay_of_three_nights_is_three_items_in_start_order(made: Example) -> None:
    stay = order_of(made, 3, 1, 2)
    settle(stay)
    found = booking_outcome.bookings(made.org, made.member, now=at(DAY))
    assert found["earlier"] == []
    assert [item["start"] for item in found["coming"]] == [
        at(DAY + timedelta(days=n), 10).astimezone(made_up_zone(made)).isoformat()
        for n in (1, 2, 3)
    ]
    assert {item["order"]["reference"] for item in found["coming"]} == {stay.reference}


def made_up_zone(made: Example) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(made.org.time_zone)


def test_a_booking_ending_exactly_now_is_earlier(made: Example) -> None:
    settle(order_of(made, 0))
    for now, side in (
        (at(DAY, 12), "earlier"),
        (at(DAY, 12) - timedelta(seconds=1), "coming"),
    ):
        found = booking_outcome.bookings(made.org, made.member, now=now)
        assert len(found[side]) == 1
        assert len(found["earlier" if side == "coming" else "coming"]) == 0


def test_earlier_runs_latest_start_first(made: Example) -> None:
    settle(order_of(made, -5, -2, -4))
    found = booking_outcome.bookings(made.org, made.member, now=at(DAY))
    starts = [item["start"] for item in found["earlier"]]
    assert starts == sorted(starts, reverse=True) and len(starts) == 3


def test_a_booking_names_its_floor_plans_word_for_spots(made: Example) -> None:
    made.capacity(4)
    plan = made_up.floor_plan_of(made.asset)
    plan.unit = "bed"
    plan.save()
    shared = create_order(
        member=made.member,
        requests=[day(made, 1, quantity=2)],
        now=min(NOW, at(DAY)),
        accepted_version=made_up.accepted(made.member),
    )
    settle(shared)

    found = booking_outcome.bookings(made.org, made.member, now=at(DAY))

    assert [(item["places"], item["spot_word"]) for item in found["coming"]] == [
        (2, "bed")
    ]


def test_a_cancelled_booking_is_listed_as_cancelled(made: Example) -> None:
    settle(order_of(made, 1), BookingState.CANCELLED)
    found = booking_outcome.bookings(made.org, made.member, now=at(DAY))
    assert [item["state"] for item in found["coming"]] == ["cancelled"]


def test_held_and_not_completed_orders_are_not_listed(made: Example) -> None:
    order_of(made, 1)  # held
    expired = order_of(made, 2)
    Order.objects.of(made.org).filter(pk=expired.pk).update(
        hold_expires_at=NOW - timedelta(minutes=1)
    )
    TakenTime.objects.of(made.org).filter(booking__order=expired).update(
        state=BookingState.EXPIRED
    )
    assert booking_outcome.bookings(made.org, made.member, now=at(DAY)) == {
        "coming": [],
        "earlier": [],
    }


def test_another_members_bookings_never_appear(made: Example) -> None:
    stranger = other(made)
    theirs = create_order(
        member=stranger,
        requests=[
            day(
                made,
                1,
                starts_at=at(DAY + timedelta(days=1), 12),
                ends_at=at(DAY + timedelta(days=1), 14),
            )
        ],
        now=NOW,
        accepted_version=made_up.accepted(stranger),
    )
    settle(theirs)
    mine = order_of(made, 1)
    settle(mine)
    response = signed_in(made).get(PATH, HTTP_HOST=HOST, secure=True)
    assert response.status_code == 200
    text = response.content.decode()
    body = json.loads(text)
    assert [item["order"]["id"] for item in body["coming"]] == [str(mine.public_id)]
    for private in (
        "zebedee.quill@example.com",
        "Zebedee",
        "Quill",
        "4799887766",
        str(theirs.public_id),
    ):
        assert private not in text


def test_a_member_of_another_organization_with_the_same_email_sees_their_own(
    made: Example,
) -> None:
    settle(order_of(made, 1))
    elsewhere = made_up.organization("elsewhere")
    twin = Member.objects.create(
        organization=elsewhere,
        role=made.member.role.__class__.objects.create(
            organization=elsewhere, name="Member"
        ),
        email=made.member.email,
        first_name="Test",
        last_name="Member",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    assert booking_outcome.bookings(elsewhere, twin, now=at(DAY)) == {
        "coming": [],
        "earlier": [],
    }
    assert (
        len(booking_outcome.bookings(made.org, made.member, now=at(DAY))["coming"]) == 1
    )


def test_signed_out_goes_to_login_and_post_is_405_and_nothing_is_cached(
    made: Example,
) -> None:
    # The login gate answers before the view does: a redirect to login.
    assert get(Browser(), PATH).status_code == 302
    browser = signed_in(made)
    assert browser.post(PATH, HTTP_HOST=HOST, secure=True).status_code == 405
    response = get(browser, PATH)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert json.loads(response.content) == {"coming": [], "earlier": []}


def queries(made: Example) -> int:
    with CaptureQueriesContext(connection) as captured:
        booking_outcome.bookings(made.org, made.member, now=at(DAY))
    return len(captured)


def test_the_number_of_queries_does_not_grow_with_the_orders(made: Example) -> None:
    settle(order_of(made, 1))
    one = queries(made)
    # Five orders, one of them a stay of three nights.
    for days in ((2, 3, 4), (5,), (6,), (7,)):
        settle(order_of(made, *days))
    found = booking_outcome.bookings(made.org, made.member, now=at(DAY))["coming"]
    assert len(found) == 7
    assert len({item["order"]["id"] for item in found}) == 5
    assert queries(made) == one
