# SPDX-License-Identifier: AGPL-3.0-only
"""at most N bookings a day per member, at most M months ahead
."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from randirandi import setup_file
from randirandi.core import (
    booking_cart,
    booking_checkout,
    booking_limits,
    booking_views,
    booking_words,
    ordering,
    payment_recovery,
)
from randirandi.core import setup as setups
from randirandi.core.booking_cart import CartRow, Refusal
from randirandi.core.booking_quote import Choice, quote
from randirandi.core.member_calendar import member_calendar
from randirandi.core.models import (
    Booking,
    BookingState,
    Member,
    MemberStatus,
    Order,
    Organization,
    Origin,
    PriceTier,
    TakenTime,
)
from randirandi.core.ordering import (
    BookingRequest,
    OrderOverDayLimitError,
    OrderTooFarAheadError,
    create_order,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_setup_file import SMALLEST

pytestmark = pytest.mark.django_db(transaction=True)
NOW = at(DAY - timedelta(days=1), 9)


def limits(made: Example, day: int | None = None, ahead: int | None = None) -> None:
    Organization.objects.filter(pk=made.org.pk).update(
        max_bookings_a_day=day, book_ahead_months=ahead
    )


def asked(made: Example, *days: int, hour: int = 10) -> list[BookingRequest]:
    """One booking an hour long on each of the days after DAY."""
    return [
        BookingRequest(
            made.config.pk,
            made.category.pk,
            at(DAY + timedelta(days=day), hour),
            at(DAY + timedelta(days=day), hour + 1),
        )
        for day in days
    ]


def order(made: Example, *days: int, now: datetime = NOW) -> Order:
    return create_order(
        member=made.member,
        requests=asked(made, *days),
        now=now,
        accepted_version=made_up.accepted(made.member),
    )


def counted(made: Example, now: datetime = NOW) -> int:
    made.org.refresh_from_db()
    return booking_limits.made_today(made.member, made.org, now)


def test_the_sixth_booking_of_the_day_is_refused_whole_and_the_fifth_accepted(
    made: Example,
) -> None:
    limits(made, day=5)
    with pytest.raises(OrderOverDayLimitError) as refused:
        order(made, 0, 1, 2, 3, 4, 5)
    assert (refused.value.left, refused.value.limit) == (5, 5)
    assert not Order.objects.of(made.org).exists()
    assert not Booking.objects.of(made.org).exists()
    assert not TakenTime.objects.of(made.org).exists()
    made.org.refresh_from_db()
    assert made.org.last_order_number == 0
    assert order(made, 0, 1, 2, 3, 4).bookings.count() == 5
    with pytest.raises(OrderOverDayLimitError) as none_left:
        order(made, 5)
    assert (none_left.value.left, none_left.value.limit) == (0, 5)


def test_an_order_of_three_after_three_today_is_refused_with_two_left(
    made: Example,
) -> None:
    limits(made, day=5)
    order(made, 0, 1, 2)
    with pytest.raises(OrderOverDayLimitError) as refused:
        order(made, 3, 4, 5)
    assert (refused.value.left, refused.value.limit) == (2, 5)
    assert Booking.objects.of(made.org).count() == 3
    assert order(made, 3, 4).bookings.count() == 2


def test_a_booking_counts_once_whatever_it_is_paid_in_lines(made: Example) -> None:
    limits(made, day=1)
    made.configure(maximum_length=240)
    long = BookingRequest(made.config.pk, made.category.pk, at(DAY, 10), at(DAY, 14))
    placed = create_order(
        member=made.member,
        requests=[long],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )
    assert placed.bookings.count() == 1
    assert booking_limits.made_today(made.member, made.org, NOW) == 1


def test_a_released_hold_is_not_counted_and_a_cancelled_paid_booking_is(
    made: Example,
) -> None:
    limits(made, day=2)
    released = order(made, 0)
    payment_recovery.release(made.org, released.pk)
    assert TakenTime.objects.of(made.org).get().state == BookingState.EXPIRED
    assert counted(made) == 0
    # Two more fit: the released one does not count.
    paid = order(made, 1)
    unpaid = order(made, 2)
    assert counted(made) == 2
    with pytest.raises(OrderOverDayLimitError):
        order(made, 3)
    # Paid, then cancelled by an administrator: it was confirmed, so it counts.
    Order.objects.of(made.org).filter(pk=paid.pk).update(hold_expires_at=None)
    TakenTime.objects.of(made.org).filter(booking__order=paid).update(
        state=BookingState.CANCELLED
    )
    # Cancelled before it was paid (the hold still set): it does not count.
    TakenTime.objects.of(made.org).filter(booking__order=unpaid).update(
        state=BookingState.CANCELLED
    )
    assert counted(made) == 1
    # Payment (or being free) clears the hold: the order was confirmed.
    Order.objects.of(made.org).filter(pk=unpaid.pk).update(hold_expires_at=None)
    assert counted(made) == 2


def test_a_hold_past_its_time_counts_until_it_is_expired(made: Example) -> None:
    limits(made, day=1)
    order(made, 0)
    assert counted(made) == 1
    # Still held in the database, past its ten minutes: a capture may be in
    # flight and confirm it, so it counts until `expire_holds` marks it.
    later = NOW + timedelta(minutes=11)
    assert booking_limits.made_today(made.member, made.org, later) == 1
    with pytest.raises(OrderOverDayLimitError):
        order(made, 1, now=later)
    payment_recovery.expire_holds(now=later)
    assert booking_limits.made_today(made.member, made.org, later) == 0
    assert order(made, 1, now=later).bookings.count() == 1


def test_a_free_booking_counts(made: Example) -> None:
    limits(made, day=1)
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(amount=0)
    placed = order(made, 0)
    assert placed.total == 0 and placed.hold_expires_at is None
    assert counted(made) == 1
    with pytest.raises(OrderOverDayLimitError):
        order(made, 1)


def test_the_day_is_the_organizations_not_utc(made: Example) -> None:
    # Oslo is UTC+1 in March: 22:59 UTC and 23:00 UTC on 28 February are
    # 23:59 on the 28th and 00:00 on 1 March there, though one UTC date.
    limits(made, day=1)
    before_midnight = at(DAY - timedelta(days=1), 23, 59)
    after_midnight = at(DAY, 0, 0)
    assert before_midnight.date() == after_midnight.date()
    order(made, 1, now=before_midnight)
    # Yesterday's booking is not counted today.
    assert booking_limits.made_today(made.member, made.org, after_midnight) == 0
    order(made, 2, now=after_midnight)
    with pytest.raises(OrderOverDayLimitError) as refused:
        order(made, 3, now=at(DAY, 0, 1))
    assert refused.value.left == 0
    # And the earlier moment still sees only its own day's one booking.
    assert booking_limits.made_today(made.member, made.org, before_midnight) == 1


def test_another_members_bookings_are_not_counted(made: Example) -> None:
    limits(made, day=1)
    order(made, 0)
    other = Member.objects.create(
        organization=made.org,
        role=made.member.role,
        email="other@example.com",
        first_name="Other",
        last_name="Member",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    placed = create_order(
        member=other,
        requests=asked(made, 1),
        now=NOW,
        accepted_version=made_up.accepted(other),
    )
    assert placed.member_id == other.pk


def test_two_orders_of_one_member_made_together_cannot_both_pass(
    made: Example,
) -> None:
    limits(made, day=1)
    ready = Barrier(2)

    def run(day: int) -> str:
        try:
            ready.wait(timeout=15)
            order(made, day)
            return "made"
        except OrderOverDayLimitError:
            return "refused"
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(run, [0, 1]))
        assert results == ["made", "refused"]
        assert Order.objects.of(made.org).count() == 1
        assert Booking.objects.of(made.org).count() == 1


def test_no_limit_when_unset(made: Example) -> None:
    limits(made)
    assert order(made, 0, 1, 2, 3, 4, 5, 6).bookings.count() == 7
    assert order(made, 400).bookings.count() == 1
    assert booking_limits.left_today(made.member, made.org, NOW) is None
    assert booking_limits.starts_before(made.org, NOW) is None


@pytest.mark.parametrize(
    ("today", "months", "last"),
    [
        (date(2027, 8, 31), 6, date(2028, 2, 29)),
        (date(2026, 8, 31), 6, date(2027, 2, 28)),
        (date(2027, 3, 1), 6, date(2027, 9, 1)),
        (date(2026, 10, 22), 6, date(2027, 4, 22)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),
        (date(2027, 1, 31), 1, date(2027, 2, 28)),
        (date(2027, 12, 15), 12, date(2028, 12, 15)),
    ],
)
def test_the_last_date_is_the_months_on_a_missing_day_being_the_months_last(
    today: date, months: int, last: date
) -> None:
    assert booking_limits.last_date(today, months) == last


def test_the_last_date_is_included_whole_and_the_next_days_first_start_is_refused(
    made: Example,
) -> None:
    limits(made, ahead=6)
    made.configure(period_start=0, period_end=0, end_day=1, maximum_length=240)
    # Today 1 March 2027 in Oslo; the last date is 1 September.
    now = at(DAY, 9)
    last = date(2027, 9, 1)
    late = BookingRequest(
        made.config.pk,
        made.category.pk,
        at(last, 23),
        at(last + timedelta(days=1), 0),
    )
    assert (
        create_order(
            member=made.member,
            requests=[late],
            now=now,
            accepted_version=made_up.accepted(made.member),
        ).bookings.count()
        == 1
    )
    first = last + timedelta(days=1)
    next_day = replace(late, starts_at=at(first, 0), ends_at=at(first, 1))
    with pytest.raises(OrderTooFarAheadError):
        create_order(
            member=made.member,
            requests=[next_day],
            now=now,
            accepted_version=made_up.accepted(made.member),
        )
    assert Order.objects.of(made.org).count() == 1
    # The boundary is Oslo's midnight, not UTC's.
    made.org.refresh_from_db()
    assert booking_limits.starts_before(made.org, now) == at(first, 0)


def test_the_calendar_shows_time_after_the_last_date_as_closed(made: Example) -> None:
    limits(made, ahead=6)
    now = at(DAY, 9)
    last, after = date(2027, 9, 1), date(2027, 9, 2)
    # A booking by someone else on the first day beyond: never shown as taken.
    made.claim(at(after, 10), at(after, 11))
    found = member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        start_date=last,
        end_date=after + timedelta(days=1),
        now=now,
    )
    (container,) = found.containers
    assert [period.day for period in container.periods] == [last]
    (period,) = container.periods
    assert period.taken == () and len(period.starts) > 0
    assert period.closed_rows == ()
    # Only the beyond day asked: no container at all.
    beyond = member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        start_date=after,
        end_date=after + timedelta(days=1),
        now=now,
    )
    assert beyond.containers == ()
    # Without a limit it is there, taken.
    limits(made)
    open_ = member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        start_date=after,
        end_date=after + timedelta(days=1),
        now=now,
    )
    assert open_.containers and open_.containers[0].periods[0].taken


def test_the_last_dates_late_start_is_offered_and_the_next_days_first_is_not(
    made: Example,
) -> None:
    limits(made, ahead=6)
    made.configure(period_start=0, period_end=0, end_day=1, maximum_length=240)
    now = at(DAY, 9)
    last, after = date(2027, 9, 1), date(2027, 9, 2)
    on_last = made.offers(last, now=now)
    assert max(offer.starts_at for offer in on_last) == at(last, 23)
    assert made.offers(after, now=now) == ()
    found = member_calendar(
        member=made.member,
        asset_id=made.asset.pk,
        start_date=last,
        end_date=after,
        now=now,
    )
    (period,) = found.containers[0].periods
    late = [period.rows[start.row] for start in period.starts]
    assert at(last, 23) in late


def test_the_quote_refuses_a_start_too_far_ahead_as_it_refuses_a_past_one(
    made: Example,
) -> None:
    limits(made, ahead=6)
    now = at(DAY, 9)

    def ask(day: date, hour: int) -> Any:
        return quote(
            member=made.member,
            configuration_id=made.config.pk,
            booking_category_id=made.category.pk,
            quantity=1,
            starts_at=at(day, hour),
            ends_at=at(day, hour + 1),
            now=now,
        )

    assert ask(date(2027, 9, 1), 13).reason is None
    assert ask(date(2027, 9, 2), 10).reason == booking_words.TOO_FAR_AHEAD
    assert ask(DAY - timedelta(days=1), 10).reason == booking_words.STARTED
    limits(made)
    assert ask(date(2027, 9, 2), 10).reason is None


def choice(made: Example, day: int) -> Choice:
    return Choice(
        made.config.pk,
        made.category.pk,
        1,
        at(DAY + timedelta(days=day), 10),
        at(DAY + timedelta(days=day), 11),
    )


def test_the_cart_refuses_early_when_its_lines_and_todays_count_reach_the_limit(
    made: Example,
) -> None:
    limits(made, day=2)
    session = SessionStore()
    order(made, 10)
    assert isinstance(
        booking_cart.add(session, made.member, choice(made, 0), 100, NOW), CartRow
    )
    refused = booking_cart.add(session, made.member, choice(made, 1), 100, NOW)
    assert refused == Refusal("day-limit", left=0, limit=2, in_cart=True)
    assert len(booking_cart.rows(session, made.member)) == 1
    limits(made, day=3)
    assert isinstance(
        booking_cart.add(session, made.member, choice(made, 1), 100, NOW), CartRow
    )


def test_the_cart_names_itself_when_its_lines_leave_none_and_not_a_used_day(
    made: Example,
) -> None:
    # Seen rendered, 2026-11-16: five nights in the cart and none ordered, the
    # sixth said "Du har brukt alle 5 bookingene du kan gjøre i dag. Du kan
    # booke igjen i morgen.", while the cart could still be ordered today.
    limits(made, day=2)
    session = SessionStore()
    for day in range(2):
        assert isinstance(
            booking_cart.add(session, made.member, choice(made, day), 100, NOW), CartRow
        )
    refused = booking_cart.add(session, made.member, choice(made, 2), 100, NOW)
    assert refused == Refusal("day-limit", left=0, limit=2, in_cart=True)
    # A limit of 1: the one line in the cart leaves none.
    limits(made, day=1)
    one = SessionStore()
    assert isinstance(
        booking_cart.add(one, made.member, choice(made, 0), 100, NOW), CartRow
    )
    alone = booking_cart.add(one, made.member, choice(made, 1), 100, NOW)
    assert alone == Refusal("day-limit", left=0, limit=1, in_cart=True)
    limits(made, day=2)
    # Two ordered today and an empty cart: the day is used.
    order(made, 10, 11)
    used = booking_cart.add(SessionStore(), made.member, choice(made, 3), 100, NOW)
    assert used == Refusal("day-limit", left=0, limit=2)


def test_checkout_refuses_the_whole_order_and_says_how_many_are_left(
    made: Example,
) -> None:
    session = SessionStore()
    for day in range(3):
        assert isinstance(
            booking_cart.add(session, made.member, choice(made, day), 100, NOW), CartRow
        )
    earlier = order(made, 20)
    # Paid, so no order is held and the member may order again.
    Order.objects.of(made.org).filter(pk=earlier.pk).update(hold_expires_at=None)
    TakenTime.objects.of(made.org).update(state=BookingState.CONFIRMED)
    limits(made, day=3)
    refused = booking_checkout.order(
        session, made.member, 300, NOW, accepted_version=made_up.accepted(made.member)
    )
    assert refused == Refusal("day-limit", left=2, limit=3)
    assert Order.objects.of(made.org).count() == 1
    assert len(booking_cart.rows(session, made.member)) == 3


@pytest.mark.parametrize(
    ("language", "limit", "left", "text"),
    [
        (
            "en",
            5,
            0,
            "You have used all 5 bookings you can make today. "
            "You can book again tomorrow.",
        ),
        (
            "en",
            1,
            0,
            "You have used the one booking you can make today. "
            "You can book again tomorrow.",
        ),
        ("en", 5, 2, "You can make 5 bookings a day. You have 2 left today."),
        ("en", 1, 1, "You can make 1 booking a day. You have 1 left today."),
        (
            "nb",
            5,
            0,
            "Du har brukt alle 5 bookingene du kan gjøre i dag. "
            "Du kan booke igjen i morgen.",
        ),
        (
            "nb",
            1,
            0,
            "Du har brukt den ene bookingen du kan gjøre i dag. "
            "Du kan booke igjen i morgen.",
        ),
        ("nb", 5, 2, "Du kan gjøre 5 bookinger per dag. Du har 2 igjen i dag."),
        ("nb", 1, 1, "Du kan gjøre 1 booking per dag. Du har 1 igjen i dag."),
    ],
)
def test_the_day_limit_text_says_the_limit_and_whether_any_are_left_today(
    language: str, limit: int, left: int, text: str
) -> None:
    # T3, 2026-11-15: "Det blir for mange på én dag" read as the chosen date
    # being full; the text says the limit is on the bookings a member makes.
    org = Organization(
        name="Fjord & Co", contact_email="post@fjord.example", language=language
    )
    assert booking_words.text("day-limit", org=org, limit=limit, left=left) == text


@pytest.mark.parametrize(
    ("language", "limit", "text"),
    [
        (
            "en",
            5,
            "You can make 5 bookings a day. "
            "With what is in your cart, you have none left today. "
            "Remove something from your cart to add this.",
        ),
        (
            "en",
            1,
            "You can make 1 booking a day. "
            "With what is in your cart, you have none left today. "
            "Remove something from your cart to add this.",
        ),
        (
            "nb",
            5,
            "Du kan gjøre 5 bookinger per dag. "
            "Med det som ligger i bestillingen din, har du ingen igjen i dag. "
            "Fjern noe fra bestillingen for å legge til dette.",
        ),
        (
            "nb",
            1,
            "Du kan gjøre 1 booking per dag. "
            "Med det som ligger i bestillingen din, har du ingen igjen i dag. "
            "Fjern noe fra bestillingen for å legge til dette.",
        ),
    ],
)
def test_the_day_limit_text_names_the_cart_when_the_cart_leaves_none(
    language: str, limit: int, text: str
) -> None:
    org = Organization(
        name="Fjord & Co", contact_email="post@fjord.example", language=language
    )
    words = booking_words.text("day-limit", org=org, limit=limit, left=0, in_cart=True)
    assert words == text
    # Some left: the cart is not named, whatever it holds.
    assert booking_words.text(
        "day-limit", org=org, limit=limit, left=1, in_cart=True
    ) == booking_words.text("day-limit", org=org, limit=limit, left=1)


def test_the_day_limit_text_is_the_organizations_own_when_it_has_one() -> None:
    org = Organization(
        name="Fjord & Co",
        contact_email="post@fjord.example",
        language="en",
        labels={"day-limit.used.other": "{count} a day; none left."},
    )
    text = booking_words.text("day-limit", org=org, limit=4, left=0)
    assert text == "4 a day; none left."
    with pytest.raises(KeyError):
        booking_words.text("day-limit", org=org, left=0)


def test_a_refused_add_or_order_sends_the_limit_with_the_text(made: Example) -> None:
    made.org.language = "nb"
    body = json.loads(
        booking_views._refused(Refusal("day-limit", left=0, limit=5), made.org).content
    )
    assert body == {
        "reason": "day-limit",
        "text": "Du har brukt alle 5 bookingene du kan gjøre i dag. "
        "Du kan booke igjen i morgen.",
        "left": 0,
        "limit": 5,
    }
    cart = json.loads(
        booking_views._refused(
            Refusal("day-limit", left=0, limit=5, in_cart=True), made.org
        ).content
    )
    assert cart["text"] == (
        "Du kan gjøre 5 bookinger per dag. "
        "Med det som ligger i bestillingen din, har du ingen igjen i dag. "
        "Fjern noe fra bestillingen for å legge til dette."
    )


def test_checkout_turns_a_start_too_far_ahead_into_a_refusal_the_cart_shows(
    made: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = SessionStore()
    assert isinstance(
        booking_cart.add(session, made.member, choice(made, 0), 100, NOW), CartRow
    )

    def too_far(**_: Any) -> Order:
        raise OrderTooFarAheadError("A booking starts too far ahead.")

    monkeypatch.setattr(ordering, "create_order", too_far)
    assert booking_checkout.order(
        session, made.member, 100, NOW, accepted_version=made_up.accepted(made.member)
    ) == Refusal("too-far-ahead")
    monkeypatch.undo()
    # And the cart marks the booking when the limit is set under it.
    limits(made, ahead=1)
    cart = booking_cart.look(session, made.member, at(DAY - timedelta(days=60)))
    assert [item.state for item in cart.items] == ["too-far-ahead"]
    assert not cart.ready


def test_a_limit_of_zero_is_refused_by_the_database(made: Example) -> None:
    for field in ("max_bookings_a_day", "book_ahead_months"):
        with pytest.raises(IntegrityError), transaction.atomic():
            Organization.objects.filter(pk=made.org.pk).update(**{field: 0})
    made.org.max_bookings_a_day = 0
    with pytest.raises(ValidationError):
        made.org.full_clean()


@pytest.fixture
def smallest() -> Iterator[str]:
    yield SMALLEST.replace(
        "booking_retention_months = 36\n",
        "booking_retention_months = 36\n"
        "max_bookings_a_day = 5\nbook_ahead_months = 6\n",
        1,
    )


def test_the_setup_file_reads_and_writes_the_limits(smallest: str) -> None:
    read = setup_file.parse(smallest)
    assert (
        read.organization.max_bookings_a_day,
        read.organization.book_ahead_months,
    ) == (
        5,
        6,
    )
    assert setup_file.parse(setup_file.write(read)) == read
    plain = setup_file.parse(SMALLEST)
    assert plain.organization.max_bookings_a_day is None
    assert "max_bookings_a_day" not in setup_file.write(plain)


def test_the_limits_survive_load_and_export(smallest: str) -> None:
    read = setup_file.parse(smallest)
    organization = setups.load(read, today=DAY)
    assert (organization.max_bookings_a_day, organization.book_ahead_months) == (5, 6)
    exported = setups.export(organization)
    assert exported.organization.max_bookings_a_day == 5
    assert exported.organization.book_ahead_months == 6
    assert setup_file.parse(setup_file.write(exported)) == exported


@pytest.mark.parametrize("key", ["max_bookings_a_day", "book_ahead_months"])
def test_a_limit_below_one_is_refused_in_the_setup_file(key: str) -> None:
    text = SMALLEST.replace(
        "booking_retention_months = 36\n",
        f"booking_retention_months = 36\n{key} = 0\n",
        1,
    )
    with pytest.raises(setup_file.SetupError, match=f"organization.{key}"):
        setup_file.parse(text)


def test_the_day_boundary_on_a_daylight_saving_change_date(made: Example) -> None:
    # Oslo springs forward on 28 March 2027: that day is 23 hours long, from
    # 23:00 UTC on the 27th to 22:00 UTC on the 28th.
    limits(made, day=1)
    change = date(2027, 3, 28)
    order(made, 40, now=at(change, 0, 30))
    assert booking_limits.made_today(made.member, made.org, at(change, 23, 30)) == 1
    with pytest.raises(OrderOverDayLimitError):
        order(made, 41, now=at(change, 23, 30))
    next_day = at(change + timedelta(days=1), 0, 0)
    assert next_day - at(change, 0, 0) == timedelta(hours=23)
    assert booking_limits.made_today(made.member, made.org, next_day) == 0
    assert order(made, 41, now=next_day).bookings.count() == 1
    # And an order just before the change's midnight is the day before.
    before = at(change, 0, 0) - timedelta(minutes=1)
    assert booking_limits.made_today(made.member, made.org, before) == 0


def test_a_period_starting_on_the_last_date_is_allowed_whatever_its_end(
    made: Example,
) -> None:
    limits(made, ahead=6)
    # A night from 17:00 to 08:00 the next day. Today is 1 March 2027 in Oslo:
    # the last date is 1 September: the night of 1 September ends after it but
    # starts on it, so it is allowed; the night of 2 September is refused.
    made.configure(
        period_type="fixed", period_start=17 * 60, period_end=8 * 60, end_day=1
    )
    now = at(DAY, 9)
    inside, across = date(2027, 9, 1), date(2027, 9, 2)

    def night(day: date) -> BookingRequest:
        return BookingRequest(
            made.config.pk,
            made.category.pk,
            at(day, 17),
            at(day + timedelta(days=1), 8),
        )

    def days(day: date) -> list[date]:
        found = member_calendar(
            member=made.member,
            asset_id=made.asset.pk,
            start_date=day,
            end_date=day + timedelta(days=1),
            now=now,
        )
        return [
            period.day
            for container in found.containers
            for period in container.periods
            if period.starts and period.day >= day
        ]

    assert days(inside) == [inside]
    assert days(across) == []

    def ask(wanted: BookingRequest) -> Any:
        return quote(
            member=made.member,
            configuration_id=wanted.configuration_id,
            booking_category_id=wanted.booking_category_id,
            quantity=1,
            starts_at=wanted.starts_at,
            ends_at=wanted.ends_at,
            now=now,
        )

    assert ask(night(inside)).reason is None
    assert ask(night(across)).reason == booking_words.TOO_FAR_AHEAD
    with pytest.raises(OrderTooFarAheadError):
        create_order(
            member=made.member,
            requests=[night(across)],
            now=now,
            accepted_version=made_up.accepted(made.member),
        )
    assert not Order.objects.of(made.org).exists()
    assert (
        create_order(
            member=made.member,
            requests=[night(inside)],
            now=now,
            accepted_version=made_up.accepted(made.member),
        ).bookings.count()
        == 1
    )
