# SPDX-License-Identifier: AGPL-3.0-only
"""Bringing in the bookings of an earlier calendar, with their blocks and
events (`old_booking_import`): settled with nothing to pay, past the member's
own limits but within the container's rules, safe to run again, and checked
before saving."""

import json
from datetime import UTC, date, datetime
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from randirandi.core import old_booking_import
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    AuditEvent,
    Block,
    Booking,
    BookingCategory,
    BookingLine,
    BookingState,
    ConfigurationCategory,
    Event,
    EventDay,
    Order,
    Organization,
    Payment,
    PriceBookEntry,
    PriceTier,
    RoleCategory,
    TakenTime,
    TiersGoBy,
)
from tests import made_up

pytestmark = pytest.mark.django_db(transaction=True)

# Before every day the tests use. The made-up container takes Mondays, 10:00
# to 22:00 on the organization's clock (Oslo), by the hour; its own booking
# holds 1 March 11:00 to 13:00.
NOW = datetime(2027, 2, 1, 12, 0, tzinfo=UTC)
MARCH_8, MARCH_15, MARCH_22 = date(2027, 3, 8), date(2027, 3, 15), date(2027, 3, 22)


@pytest.fixture
def made() -> made_up.MadeUp:
    made = made_up.everything()
    # The made-up prices stop at 4 hours; its container takes up to 12.
    PriceTier.objects.create(
        organization=made.organization,
        price_book_entry=PriceBookEntry.objects.of(made.organization).get(
            booking_category=made.private
        ),
        up_to=12 * 60,
        paid_per=4 * 60,
        amount=10000,
    )
    return made


def piece(day: date, start: int, end: int, category: str = "private") -> dict[str, Any]:
    return {
        "configuration": 1,
        "category": category,
        "starts": f"{day.isoformat()}T{start:02d}:00",
        "ends": f"{day.isoformat()}T{end:02d}:00",
    }


def booking(key: str, holder: str, *pieces: dict[str, Any]) -> dict[str, Any]:
    return {"key": key, "holder": holder, "pieces": list(pieces)}


def document(**parts: Any) -> old_booking_import.Document:
    return old_booking_import.read({"format": 1, **parts})


def results(outcomes: list[old_booking_import.Outcome]) -> dict[str, str]:
    return {outcome.key: outcome.result for outcome in outcomes}


def bring_in(made: made_up.MadeUp, **parts: Any) -> list[old_booking_import.Outcome]:
    return old_booking_import.bring_in(
        made.organization, made.administrator, document(**parts), now=NOW
    )


def check(made: made_up.MadeUp, **parts: Any) -> list[old_booking_import.Outcome]:
    return old_booking_import.check(
        made.organization, made.administrator, document(**parts), now=NOW
    )


def brought_in() -> Any:
    """The bookings of the orders the audit log records as brought in."""
    orders = AuditEvent.across_organizations.filter(
        action=AuditAction.BROUGHT_IN_BOOKING
    ).values_list("object_id", flat=True)
    return Booking.across_organizations.filter(order_id__in=list(orders))


def counts() -> tuple[int, int, int, int, int, int]:
    return (
        Order.across_organizations.count(),
        Booking.across_organizations.count(),
        BookingLine.across_organizations.count(),
        TakenTime.across_organizations.count(),
        Event.across_organizations.count(),
        AuditEvent.across_organizations.count(),
    )


def test_a_booking_comes_in_settled_confirmed_and_audited(made: made_up.MadeUp) -> None:
    payments = Payment.across_organizations.count()
    outcomes = bring_in(
        made, bookings=[booking("B1", made.member.email, piece(MARCH_8, 10, 15))]
    )

    assert results(outcomes) == {"B1": "brought in"}
    brought = brought_in().get()
    order = brought.order
    assert outcomes[0].detail == (order.reference,)
    # Nothing to pay, no payment and no confirmation email: settled before.
    assert order.total == 0
    assert order.hold_expires_at is None
    assert order.confirmation_due_at is None
    assert order.conditions_id is None
    assert Payment.across_organizations.count() == payments
    assert brought.taken_time.state == BookingState.CONFIRMED
    assert brought.booking_configuration == made.configuration
    assert brought.booking_category_name == "private"
    local = made.asset.zone
    assert brought.taken_time.starts_at == datetime(2027, 3, 8, 10, tzinfo=local)
    assert brought.taken_time.ends_at == datetime(2027, 3, 8, 15, tzinfo=local)
    # The price list's tiers, at amount 0: a cancel refunds nothing.
    lines = list(BookingLine.across_organizations.filter(booking=brought))
    assert lines and all(line.amount == 0 for line in lines)
    event = AuditEvent.across_organizations.get(action=AuditAction.BROUGHT_IN_BOOKING)
    assert (event.administrator_id, event.object_kind, event.object_id) == (
        made.administrator.pk,
        "order",
        order.pk,
    )


def test_a_booking_without_a_reminder_is_reminded_already(made: made_up.MadeUp) -> None:
    near = {**booking("B1", made.member.email, piece(MARCH_8, 10, 12)), "remind": False}
    later = booking("B2", made.member.email, piece(MARCH_15, 10, 12))
    assert results(bring_in(made, bookings=[near, later])) == {
        "B1": "brought in",
        "B2": "brought in",
    }
    brought = brought_in()
    reminded = {b.taken_time.starts_at.date(): b.reminded_at for b in brought}
    assert reminded == {MARCH_8: NOW, MARCH_15: None}


def test_run_again_an_entry_is_in_already_and_nothing_is_added(
    made: made_up.MadeUp,
) -> None:
    entry = booking(
        "B1", made.member.email, piece(MARCH_8, 10, 12), piece(MARCH_15, 10, 12)
    )
    assert results(bring_in(made, bookings=[entry])) == {"B1": "brought in"}
    before = counts()
    assert results(bring_in(made, bookings=[entry])) == {"B1": "in already"}
    assert results(check(made, bookings=[entry])) == {"B1": "in already"}
    assert counts() == before


def test_the_members_own_limits_do_not_apply(made: made_up.MadeUp) -> None:
    organization = made.organization
    # An update, not a save: a saved stale copy would set the order counter back.
    Organization.objects.filter(pk=organization.pk).update(
        book_ahead_months=1, max_bookings_a_day=1
    )
    # An internal booking: a category the board's role books, the member's not.
    internal = BookingCategory.objects.create(
        organization=organization, name="internal"
    )
    ConfigurationCategory.objects.create(
        organization=organization,
        booking_configuration=made.configuration,
        booking_category=internal,
    )
    RoleCategory.objects.create(
        organization=organization,
        role=made.administrator.role,
        booking_category=internal,
    )
    entry = made_up_price(made, internal)
    far = date(2027, 9, 6)
    outcomes = bring_in(
        made,
        bookings=[
            booking("B1", made.member.email, piece(far, 10, 12), piece(far, 13, 14)),
            booking("B2", made.member.email, piece(MARCH_8, 10, 12, "internal")),
        ],
    )
    assert entry
    assert results(outcomes) == {"B1": "brought in", "B2": "brought in"}


def made_up_price(made: made_up.MadeUp, category: BookingCategory) -> PriceBookEntry:
    """A price at 0 for the category in the made-up container's price book."""
    entry = PriceBookEntry.objects.create(
        organization=made.organization,
        price_book=made.price_book,
        booking_configuration=made.configuration,
        booking_category=category,
        tiers_go_by=TiersGoBy.LENGTH,
    )
    PriceTier.objects.create(
        organization=made.organization,
        price_book_entry=entry,
        up_to=12 * 60,
        paid_per=12 * 60,
        amount=0,
    )
    return entry


def test_the_containers_rules_and_the_time_held_refuse_and_save_nothing(
    made: made_up.MadeUp,
) -> None:
    before = counts()
    outcomes = bring_in(
        made,
        bookings=[
            booking("NOBODY", "nobody@harbourview.example", piece(MARCH_8, 10, 12)),
            # A Tuesday, before the day's hours, a part hour, a container
            # that is not there.
            booking("TUESDAY", made.member.email, piece(date(2027, 3, 9), 10, 12)),
            booking("EARLY", made.member.email, piece(MARCH_8, 8, 12)),
            booking(
                "HALF",
                made.member.email,
                {**piece(MARCH_8, 10, 12), "ends": "2027-03-08T11:30"},
            ),
            booking(
                "NONE",
                made.member.email,
                {**piece(MARCH_8, 10, 12), "configuration": 9},
            ),
            # The made-up booking holds 1 March 11:00 to 13:00.
            booking("TAKEN", made.member.email, piece(made_up.A_DAY, 12, 14)),
            booking("PAST", made.member.email, piece(date(2027, 1, 25), 10, 12)),
            # One piece refused refuses the entry: its other piece is not saved.
            booking(
                "WHOLE",
                made.member.email,
                piece(MARCH_15, 10, 12),
                piece(made_up.A_DAY, 12, 14),
            ),
        ],
    )
    assert results(outcomes) == {
        "NOBODY": "no member",
        "TUESDAY": "not these hours",
        "EARLY": "not these hours",
        "HALF": "not these hours",
        "NONE": "no container",
        "TAKEN": "taken",
        "PAST": "past",
        "WHOLE": "taken",
    }
    assert counts() == before


def test_a_time_under_way_or_skipped_by_summer_time_is_refused(
    made: made_up.MadeUp,
) -> None:
    before = counts()
    outcomes = bring_in(
        made,
        # 1 February, a Monday: 10:00 to 15:00 in Oslo spans NOW (12:00 UTC).
        bookings=[
            booking("UNDERWAY", made.member.email, piece(date(2027, 2, 1), 10, 15))
        ],
        blocks=[
            {
                "key": "GAP",
                "asset": 1,
                # Oslo's clock skips 02:00 to 03:00 on 28 March 2027.
                "starts": "2027-03-28T02:30",
                "ends": "2027-03-28T05:00",
                "note": "no such moment",
            }
        ],
    )
    assert results(outcomes) == {"UNDERWAY": "under way", "GAP": "not these hours"}
    assert counts() == before


def test_times_are_on_the_containers_clock_in_winter_and_in_summer(
    made: made_up.MadeUp,
) -> None:
    summer = date(2027, 4, 5)  # a Monday, after Oslo's clock moved on 28 March
    one_off = {
        "key": "E1",
        "category": "organization",
        "title": "Board meeting",
        "contact": made.administrator.email,
        "asset": 1,
        "starts": "2027-04-12T18:00",
        "ends": "2027-04-12T20:00",
    }
    outcomes = bring_in(
        made,
        bookings=[
            booking("WINTER", made.member.email, piece(MARCH_8, 10, 12)),
            booking("SUMMER", made.member.email, piece(summer, 10, 12)),
        ],
        events=[one_off],
    )
    assert results(outcomes) == {
        "WINTER": "brought in",
        "SUMMER": "brought in",
        "E1": "made",
    }
    held = sorted(
        (b.taken_time.starts_at, b.taken_time.ends_at)
        for b in brought_in().select_related("taken_time")
    )
    # Oslo is an hour ahead of UTC in winter, two in summer.
    assert held == [
        (
            datetime(2027, 3, 8, 9, 0, tzinfo=UTC),
            datetime(2027, 3, 8, 11, 0, tzinfo=UTC),
        ),
        (
            datetime(2027, 4, 5, 8, 0, tzinfo=UTC),
            datetime(2027, 4, 5, 10, 0, tzinfo=UTC),
        ),
    ]
    day = EventDay.across_organizations.get(event__title="Board meeting")
    assert (day.starts_at, day.ends_at) == (
        datetime(2027, 4, 12, 16, 0, tzinfo=UTC),
        datetime(2027, 4, 12, 18, 0, tzinfo=UTC),
    )
    assert results(bring_in(made, events=[one_off])) == {"E1": "in already"}


def test_a_re_run_after_its_time_says_in_already_not_past(
    made: made_up.MadeUp,
) -> None:
    entry = booking("B1", made.member.email, piece(MARCH_8, 10, 12))
    assert results(bring_in(made, bookings=[entry])) == {"B1": "brought in"}
    later = old_booking_import.read(
        {"format": 1, "bookings": [entry], "blocks": [], "events": []}
    )
    outcomes = old_booking_import.bring_in(
        made.organization,
        made.administrator,
        later,
        now=datetime(2027, 3, 9, 12, 0, tzinfo=UTC),
    )
    assert results(outcomes) == {"B1": "in already"}


def test_a_brought_in_booking_cancelled_since_is_not_brought_in_again(
    made: made_up.MadeUp,
) -> None:
    entry = booking("B1", made.member.email, piece(MARCH_8, 10, 12))
    assert results(bring_in(made, bookings=[entry])) == {"B1": "brought in"}
    brought = brought_in().get()
    TakenTime.across_organizations.filter(pk=brought.taken_time_id).update(
        state=BookingState.CANCELLED
    )
    before = counts()
    assert results(bring_in(made, bookings=[entry])) == {"B1": "in already"}
    assert counts() == before


def test_two_repeats_that_differ_only_by_weekday_are_both_made(
    made: made_up.MadeUp,
) -> None:
    monday = repeat_event(made)
    tuesday = {**monday, "key": "E2", "weekdays": [1]}
    outcomes = bring_in(made, events=[monday, tuesday])
    assert results(outcomes) == {"E1": "made", "E2": "made"}
    assert Event.across_organizations.filter(title="Open house").count() == 2


def test_check_says_what_bring_in_does_and_saves_nothing(made: made_up.MadeUp) -> None:
    parts = {
        "bookings": [
            booking("B1", made.member.email, piece(MARCH_8, 10, 12)),
            # The same time again: the check sees the entry before it.
            booking("B2", made.member.email, piece(MARCH_8, 11, 13)),
        ],
        "blocks": [
            {
                "key": "K1",
                "asset": 1,
                "starts": "2027-03-15T10:00",
                "ends": "2027-03-15T14:00",
                "note": "no account yet",
            }
        ],
        "events": [repeat_event(made)],
    }
    before = counts()
    blocks = Block.across_organizations.count()
    checked = check(made, **parts)
    assert counts() == before
    assert Block.across_organizations.count() == blocks
    made_now = bring_in(made, **parts)
    # The same outcomes; an order's reference is drawn anew each time.
    assert [(o.key, o.result) for o in checked] == [(o.key, o.result) for o in made_now]
    assert [o.detail for o in checked[1:]] == [o.detail for o in made_now[1:]]
    assert results(made_now) == {
        "B1": "brought in",
        "B2": "taken",
        "K1": "made",
        "E1": "made",
    }


def repeat_event(made: made_up.MadeUp) -> dict[str, Any]:
    """Open house on Mondays 10:00 to 14:00, three weeks from 8 March."""
    return {
        "key": "E1",
        "title": "Open house",
        "category": "open_house",
        "contact": made.administrator.email,
        "description": "",
        "asset": 1,
        "weekdays": [0],
        "first_date": MARCH_8.isoformat(),
        "end_date": MARCH_22.isoformat(),
        "start_time": "10:00",
        "end_time": "14:00",
    }


def test_a_repeat_after_the_bookings_skips_their_days_and_names_them(
    made: made_up.MadeUp,
) -> None:
    outcomes = bring_in(
        made,
        bookings=[booking("B1", made.member.email, piece(MARCH_8, 10, 22))],
        blocks=[
            {
                "key": "K1",
                "asset": 1,
                "starts": "2027-03-15T10:00",
                "ends": "2027-03-15T22:00",
                "note": "no account yet",
            }
        ],
        events=[repeat_event(made)],
    )
    assert results(outcomes) == {"B1": "brought in", "K1": "made", "E1": "made"}
    assert outcomes[-1].detail == ("2027-03-08 booked", "2027-03-15 blocked")
    event = Event.across_organizations.get(title="Open house")
    taken = [
        day.day
        for day in EventDay.across_organizations.filter(
            event=event, removed_at=None
        ).order_by("day")
    ]
    assert taken == [MARCH_22]
    # The block keeps its key in the note; run again, both are in already.
    assert Block.across_organizations.filter(note="K1: no account yet").count() == 1
    again = bring_in(
        made,
        blocks=[
            {
                "key": "K1",
                "asset": 1,
                "starts": "2027-03-15T10:00",
                "ends": "2027-03-15T22:00",
                "note": "no account yet",
            }
        ],
        events=[repeat_event(made)],
    )
    assert results(again) == {"K1": "in already", "E1": "in already"}


def test_a_block_or_a_one_off_event_over_a_booking_is_taken(
    made: made_up.MadeUp,
) -> None:
    outcomes = bring_in(
        made,
        blocks=[
            {
                "key": "K1",
                "asset": 1,
                "starts": "2027-03-01T10:00",
                "ends": "2027-03-01T14:00",
                "note": "over the made-up booking",
            }
        ],
        events=[
            {
                "key": "E1",
                "title": "Board meeting",
                "category": "organization",
                "contact": made.administrator.email,
                "description": "",
                "asset": 1,
                "starts": "2027-03-01T12:00",
                "ends": "2027-03-01T14:00",
            }
        ],
    )
    assert results(outcomes) == {"K1": "taken", "E1": "taken"}
    assert not Block.across_organizations.filter(note__startswith="K1").exists()
    assert not Event.across_organizations.filter(title="Board meeting").exists()


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"format": 2},
        {"format": 1, "orders": []},
        {"format": 1, "bookings": [{"key": "B1", "holder": "a@b.c", "pieces": []}]},
        {"format": 1, "bookings": [booking("B1", "a@b.c", piece(MARCH_8, 10, 12))] * 2},
        {"format": 1, "bookings": [booking("B 1", "a@b.c", piece(MARCH_8, 10, 12))]},
        {
            "format": 1,
            "bookings": [
                booking(
                    "B1",
                    "a@b.c",
                    {**piece(MARCH_8, 10, 12), "starts": "2027-02-30T10:00"},
                )
            ],
        },
        {
            "format": 1,
            "bookings": [
                booking("B1", "a@b.c", {**piece(MARCH_8, 10, 12), "configuration": "1"})
            ],
        },
        {
            "format": 1,
            "events": [
                {
                    "key": "E1",
                    "title": "x",
                    "category": "party",
                    "contact": "a@b.c",
                    "starts": "2027-03-08T10:00",
                    "ends": "2027-03-08T12:00",
                }
            ],
        },
    ],
)
def test_a_document_this_does_not_read_is_refused_whole(raw: Any) -> None:
    with pytest.raises(old_booking_import.DocumentError):
        old_booking_import.read(raw)


def test_the_command_checks_then_brings_in_and_prints_no_name(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "bookings": [booking("B1", made.member.email, piece(MARCH_8, 10, 12))],
            }
        )
    )

    def run(*more: str) -> dict[str, Any]:
        out = StringIO()
        call_command(
            "import_old_bookings",
            "harbourview",
            str(path),
            "--administrator",
            made.administrator.email,
            *more,
            stdout=out,
        )
        text = out.getvalue()
        assert made.member.email not in text and made.member.full_name not in text
        result: dict[str, Any] = json.loads(text)
        return result

    checked = run()
    assert checked["saved"] is False
    assert checked["counts"] == {"booking: brought in": 1}
    assert not brought_in().exists()
    brought = run("--bring-in")
    assert brought["saved"] is True
    assert brought["entries"][0]["result"] == "brought in"
    assert brought_in().count() == 1
    with pytest.raises(CommandError):
        call_command(
            "import_old_bookings",
            "harbourview",
            str(path),
            "--administrator",
            made.member.email,
        )
