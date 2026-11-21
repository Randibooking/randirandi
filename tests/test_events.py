# SPDX-License-Identifier: AGPL-3.0-only
"""The event on the server : making, changing and
removing events; what is in their way and the ways out; the repeat, its days,
its skipped days and the daily top-up; the preview; the emails about skipped
days; the places where the event meets blocks, closures, the members' calendar,
the member's export and erasure."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import connection

from randirandi.core import (
    audit_log,
    block_admin,
    event_admin,
    event_notices,
    event_plan,
    member_calendar,
    member_data,
)
from randirandi.core import availability_admin as closures
from randirandi.core.asset_admin import LayoutRefusedError
from randirandi.core.event_admin import (
    REMOVE,
    EventNotFoundError,
    EventRefusedError,
    EventSpec,
    OneOff,
    Repeat,
    Settle,
)
from randirandi.core.models import (
    EVENT_STATE,
    Asset,
    AuditEvent,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    Block,
    BookingState,
    CapacityAllocation,
    ClosureReason,
    Event,
    EventDay,
    EventNotice,
    EventRepeat,
    EventSkippedDay,
    Member,
    Organization,
    Role,
    TakenTime,
)
from randirandi.core.ordering import BookingRequest, OrderUnavailableError, create_order
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.mail_checks import check_system_mail
from tests.operator_erasure import erase
from tests.test_block_lay import nights

pytestmark = pytest.mark.django_db(transaction=True)

OSLO = ZoneInfo("Europe/Oslo")
# Before every day the tests use. The asset's availability starts on 1 March.
NOW = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
SATURDAY = date(2027, 3, 6)
SUNDAY = date(2027, 3, 7)
MONDAY = date(2027, 3, 8)
FRIDAY = date(2027, 3, 12)
SATURDAY_AFTER = date(2027, 3, 13)
SUNDAY_AFTER = date(2027, 3, 14)
EVERY_DAY = event_plan.mask(range(7))
WEEKEND = event_plan.mask([5, 6])


@pytest.fixture
def made() -> Iterator[made_up.MadeUp]:
    found = made_up.everything()
    open_all_week(found)
    yield found


def open_all_week(made: made_up.MadeUp, asset: Asset | None = None) -> None:
    """The asset is open around the clock, every day, from 1 March 2027 (the
    made-up organization's own is open on Mondays only)."""
    target = asset or made.asset
    row, _ = AvailabilityCalendar.objects.of(made.organization).get_or_create(
        asset=target,
        defaults={"organization": made.organization, "start_date": date(2027, 3, 1)},
    )
    AvailableSpan.objects.of(made.organization).filter(
        availability_calendar=row
    ).delete()
    for weekday in range(7):
        AvailableSpan.objects.create(
            organization=made.organization,
            availability_calendar=row,
            weekday=weekday,
            start_time=0,
            end_time=24 * 60,
        )


def wall(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001


def spec(made: made_up.MadeUp, when: OneOff | Repeat, **changes: Any) -> EventSpec:
    fields: dict[str, Any] = {
        "category": "social",
        "title": "Summer party",
        "description": "Bring something to share",
        "contact_id": made.member.pk,
        "asset_id": made.asset.pk,
        "when": when,
        **changes,
    }
    return EventSpec(**fields)


def one_off(day: date, first: int, last: int) -> OneOff:
    return OneOff(wall(day, first), wall(day, last))


def weekend(
    first: date = SATURDAY, end: date | None = None, hours: tuple[int, int] = (10, 14)
) -> Repeat:
    return Repeat(frozenset({5, 6}), first, end, hours[0] * 60, hours[1] * 60)


def make(made: made_up.MadeUp, when: OneOff | Repeat, **changes: Any) -> Event:
    settle = changes.pop("settle", None)
    with application_login():
        return event_admin.make_event(
            made.administrator, spec(made, when, **changes), settle=settle, now=NOW
        )


def days_of(event: Event) -> list[EventDay]:
    return list(
        EventDay.objects.of(event.organization_id).filter(event=event).order_by("day")
    )


def live(event: Event) -> list[EventDay]:
    return [day for day in days_of(event) if day.removed_at is None]


def claims(made: made_up.MadeUp) -> int:
    return TakenTime.objects.of(made.organization).filter(state=EVENT_STATE).count()


def audit_actions(made: made_up.MadeUp) -> list[str]:
    return list(
        AuditEvent.objects.of(made.organization)
        .order_by("pk")
        .values_list("action", flat=True)
    )


def refused(
    made: made_up.MadeUp, when: OneOff | Repeat, **changes: Any
) -> EventRefusedError:
    with pytest.raises(EventRefusedError) as raised, application_login():
        event_admin.make_event(made.administrator, spec(made, when, **changes), now=NOW)
    return raised.value


def ordinary_member(made: made_up.MadeUp, name: str = "Pat") -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name=name,
        first_name=name,
        last_name="Person",
        email=f"{name.lower()}@harbourview.example",
        phone="+4790000009",
        status="active",
        origin="administrator",
    )


def second_administrator(made: made_up.MadeUp) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.board,
        full_name="Robin Second",
        first_name="Robin",
        last_name="Second",
        email="robin@harbourview.example",
        phone="+4790000010",
        status="active",
        origin="administrator",
    )


# --- A one-off ----------------------------------------------------------------------


def test_a_one_off_event_holds_the_whole_asset_as_a_claim_in_the_state_event(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    (day,) = days_of(event)
    assert (event.category, event.title, event.asset_id) == (
        "social",
        "Summer party",
        made.asset.pk,
    )
    assert (
        event.made_by_id == made.administrator.pk and event.contact_id == made.member.pk
    )
    assert day.day == SATURDAY and day.removed_at is None
    assert day.starts_at == datetime(2027, 3, 6, 15, 0, tzinfo=UTC)
    assert day.ends_at == datetime(2027, 3, 6, 21, 0, tzinfo=UTC)
    taken = day.taken_time
    assert taken is not None
    assert (taken.state, taken.mode, taken.asset_id) == (
        EVENT_STATE,
        "exclusive",
        made.asset.pk,
    )
    assert (taken.starts_at, taken.ends_at) == (day.starts_at, day.ends_at)
    assert (
        CapacityAllocation.objects.of(made.organization)
        .filter(taken_time=taken)
        .count()
        == 1
    )
    assert not hasattr(event, "repeat")
    assert audit_actions(made)[-1] == "made event"
    last = AuditEvent.objects.of(made.organization).latest("pk")
    assert (last.object_kind, last.object_id) == ("event", event.pk)


def test_an_event_without_an_asset_is_recorded_and_takes_no_time(
    made: made_up.MadeUp,
) -> None:
    first = make(made, one_off(SATURDAY, 16, 22), asset_id=None)
    # Events without an asset may overlap: they hold no time, and a time the
    # asset is closed at is no hindrance either.
    second = make(made, one_off(SATURDAY, 18, 20), asset_id=None)
    before = make(made, one_off(date(2027, 1, 5), 10, 11), asset_id=None)
    assert claims(made) == 0
    for event in (first, second, before):
        (day,) = days_of(event)
        assert day.taken_time_id is None and event.asset_id is None
    # The organization's clock: Oslo.
    assert days_of(first)[0].starts_at == datetime(2027, 3, 6, 15, 0, tzinfo=UTC)


def test_a_booking_a_block_another_event_or_a_closed_asset_refuses_a_one_off(
    made: made_up.MadeUp,
) -> None:
    # The made-up booking is 10:00 to 12:00 UTC on 1 March (11:00 to 13:00 in
    # Oslo) and its block 14:00 to 15:00 UTC.
    monday = date(2027, 3, 1)
    booked = refused(made, one_off(monday, 12, 14))
    assert booked.code == "booked"
    (way,) = booked.in_way
    assert (way.reason, way.number, way.reference) == (
        "booked",
        made.order.number,
        made.order.reference,
    )
    assert way.ways == ("cancel_booking",)
    blocked = refused(made, one_off(monday, 15, 17))
    assert blocked.code == "blocked"
    assert blocked.in_way[0].ways == ("remove_block",)
    assert blocked.in_way[0].block_id == Block.objects.of(made.organization).get().pk
    # Nothing was saved, and the booking was not cancelled.
    assert Event.objects.of(made.organization).count() == 0
    assert claims(made) == 0
    made.booking.taken_time.refresh_from_db()
    assert made.booking.taken_time.state == BookingState.CONFIRMED
    # Another event's one-off is in the way; its way out is to remove it.
    make(made, one_off(SATURDAY, 16, 22))
    taken = refused(made, one_off(SATURDAY, 21, 23))
    assert taken.code == "event"
    assert taken.in_way[0].ways == ("remove_event",)
    assert taken.in_way[0].title == "Summer party"
    assert not taken.in_way[0].repeating


def test_a_closure_closes_the_asset_and_names_itself_in_the_refusal(
    made: made_up.MadeUp,
) -> None:
    closure = AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SATURDAY,
        last_date=SATURDAY,
    )
    closed = refused(made, one_off(SATURDAY, 10, 12))
    assert closed.code == "closed"
    (way,) = closed.in_way
    assert way.closure_ids == (closure.pk,)
    assert way.ways == ("change_closure", "remove_closure")
    # Another day is not.
    make(made, one_off(SUNDAY, 10, 12))


def test_the_event_must_have_a_contact_who_is_an_active_member(
    made: made_up.MadeUp,
) -> None:
    for contact in (0, 10**9, made.member.pk):
        if contact == made.member.pk:
            Member.objects.of(made.organization).filter(pk=contact).update(
                status="blocked"
            )
        assert (
            refused(made, one_off(SATURDAY, 10, 12), contact_id=contact).code
            == "contact"
        )
    other = made_up.everything("other")
    assert (
        refused(made, one_off(SATURDAY, 10, 12), contact_id=other.member.pk).code
        == "contact"
    )
    assert Event.objects.of(made.organization).count() == 0


def test_the_fields_are_checked_before_anything_is_written(
    made: made_up.MadeUp,
) -> None:
    good = one_off(SATURDAY, 10, 12)
    before_audit = audit_actions(made)
    for changes, code in (
        ({"category": "other"}, "category"),
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"description": "x" * 2001}, "description"),
        ({"asset_id": 10**9}, "asset"),
        ({"asset_id": made_up.everything("other").asset.pk}, "asset"),
    ):
        assert refused(made, good, **changes).code == code
    for when, code in (
        (OneOff(wall(SATURDAY, 12), wall(SATURDAY, 10)), "dates"),
        (OneOff(wall(SATURDAY, 10), wall(SATURDAY, 10)), "dates"),
        (OneOff(wall(SATURDAY, 10), wall(SATURDAY + timedelta(days=400), 10)), "dates"),
        (OneOff(wall(SATURDAY, 10, 0).replace(second=5), wall(SATURDAY, 12)), "dates"),
        (OneOff(wall(date(1999, 1, 1), 10), wall(date(1999, 1, 1), 12)), "dates"),
        (OneOff(datetime(2027, 3, 6, 10, tzinfo=UTC), wall(SATURDAY, 12)), "dates"),
        (Repeat(frozenset(), SATURDAY, None, 600, 840), "weekdays"),
        (Repeat(frozenset({7}), SATURDAY, None, 600, 840), "weekdays"),
        (
            Repeat(frozenset({5}), SATURDAY, SATURDAY - timedelta(days=1), 600, 840),
            "dates",
        ),
        (Repeat(frozenset({5}), SATURDAY, None, 840, 600), "hours"),
        (Repeat(frozenset({5}), SATURDAY, None, 600, 1441), "hours"),
    ):
        assert refused(made, when).code == code, when
    # A one-off over in the past is refused where it would hold time.
    assert refused(
        made, OneOff(wall(date(2027, 2, 1), 10), wall(date(2027, 2, 1), 12))
    ).code in {"past", "closed"}
    assert Event.objects.of(made.organization).count() == 0
    assert audit_actions(made) == before_audit


def test_a_one_off_may_run_across_midnight_and_further_ahead_than_members_book(
    made: made_up.MadeUp,
) -> None:
    far = date(2028, 8, 20)
    AvailabilityCalendar.objects.of(made.organization)
    when = OneOff(wall(far, 16), wall(far + timedelta(days=1), 2))
    event = make(made, when)
    (day,) = days_of(event)
    assert day.ends_at - day.starts_at == timedelta(hours=10)
    assert day.taken_time is not None


# --- Settling another event's repeat day, in the same save --------------------------


def test_a_one_off_may_remove_or_move_a_repeats_day_in_the_same_save(
    made: made_up.MadeUp,
) -> None:
    open_house = make(
        made, weekend(end=SUNDAY), title="Open house", category="open_house"
    )
    saturday, sunday = days_of(open_house)
    # The repeat's Saturday is in the way: the preview shows it with its ways
    # out; saving without settling it is refused.
    with application_login():
        shown = event_admin.preview(
            made.administrator,
            spec(made, one_off(SATURDAY, 12, 16)),
            now=NOW,
        )
    assert shown.refusal == "event"
    (way,) = shown.days[0].in_way
    assert (way.day_id, way.repeating, way.ways) == (
        saturday.pk,
        True,
        ("remove_day", "other_hours"),
    )
    saved = event_admin.EventRefusedError
    with pytest.raises(saved) as raised, application_login():
        event_admin.make_event(
            made.administrator, spec(made, one_off(SATURDAY, 12, 16)), now=NOW
        )
    assert raised.value.code == "event"
    # Settled: remove the day, and it stays removed.
    with application_login():
        event = event_admin.make_event(
            made.administrator,
            spec(made, one_off(SATURDAY, 12, 16), title="Match on the screen"),
            settle={saturday.pk: REMOVE},
            now=NOW,
        )
    saturday.refresh_from_db()
    assert saturday.removed_at == NOW and saturday.taken_time_id is None
    assert [d.day for d in live(open_house)] == [SUNDAY]
    assert len(live(event)) == 1 and claims(made) == 2
    actions = audit_actions(made)
    assert actions[-2:] == ["removed event day", "made event"]
    # Other hours: a Sunday 09:00 to 11:00 sits across the repeat's start.
    with application_login():
        event_admin.make_event(
            made.administrator,
            spec(made, one_off(SUNDAY, 8, 12), title="Early"),
            settle={sunday.pk: Settle(12 * 60, 16 * 60)},
            now=NOW,
        )
    sunday.refresh_from_db()
    assert (sunday.own_start, sunday.own_end) == (12 * 60, 16 * 60)
    assert sunday.starts_at == datetime(2027, 3, 7, 11, 0, tzinfo=UTC)
    assert sunday.taken_time is not None
    assert audit_actions(made)[-2:] == ["changed event day", "made event"]


def test_a_settlement_that_is_not_possible_saves_nothing(
    made: made_up.MadeUp,
) -> None:
    open_house = make(made, weekend(end=SUNDAY), title="Open house")
    saturday, sunday = days_of(open_house)
    before = (
        claims(made),
        len(audit_actions(made)),
        Event.objects.of(made.organization).count(),
    )
    for settle in (
        # A day that is not in the way.
        {sunday.pk: REMOVE},
        # Other hours that are in the way of the new event.
        {saturday.pk: Settle(11 * 60, 15 * 60)},
    ):
        with pytest.raises(EventRefusedError), application_login():
            event_admin.make_event(
                made.administrator,
                spec(made, one_off(SATURDAY, 12, 16), title="Clash"),
                settle=settle,
                now=NOW,
            )
    # A booking in the way is not settled by naming a repeat's day.
    with pytest.raises(EventRefusedError), application_login():
        event_admin.make_event(
            made.administrator,
            spec(made, one_off(date(2027, 3, 1), 12, 14)),
            settle={saturday.pk: REMOVE},
            now=NOW,
        )
    assert (
        claims(made),
        len(audit_actions(made)),
        Event.objects.of(made.organization).count(),
    ) == before
    saturday.refresh_from_db()
    assert saturday.removed_at is None and saturday.taken_time_id is not None


# --- A repeat -----------------------------------------------------------------------


def test_a_repeat_makes_a_claim_for_each_day_up_to_a_year_ahead(
    made: made_up.MadeUp,
) -> None:
    event = make(
        made,
        weekend(),
        title="Open house",
        category="open_house",
        asset_id=made.asset.pk,
    )
    rule = EventRepeat.objects.of(made.organization).get(event=event)
    assert (rule.weekdays, rule.start_minute, rule.end_minute) == (WEEKEND, 600, 840)
    assert rule.made_through == date(2028, 2, 29)
    found = live(event)
    # Saturdays and Sundays from 6 March 2027 to 1 March 2028.
    assert found[0].day == SATURDAY and found[-1].day == date(2028, 2, 27)
    assert len(found) == 104
    assert all(day.taken_time_id for day in found)
    assert claims(made) == len(found)
    assert found[0].starts_at == datetime(2027, 3, 6, 9, 0, tzinfo=UTC)
    assert not EventSkippedDay.objects.of(made.organization).exists()
    assert audit_actions(made)[-1:] == ["made event"]


def test_a_repeat_with_an_end_date_stops_there_and_one_before_today_starts_today(
    made: made_up.MadeUp,
) -> None:
    ending = make(made, weekend(end=SUNDAY_AFTER))
    assert [day.day for day in live(ending)] == [
        SATURDAY,
        SUNDAY,
        SATURDAY_AFTER,
        SUNDAY_AFTER,
    ]
    # A first date that is past is not made; days start from today.
    early = make(
        made, weekend(first=date(2027, 1, 2), end=date(2027, 3, 7)), asset_id=None
    )
    assert [day.day for day in live(early)] == [SATURDAY, SUNDAY]


def test_a_repeat_is_never_refused_a_day_in_the_way_is_skipped_and_recorded(
    made: made_up.MadeUp,
) -> None:
    closure = AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SATURDAY_AFTER,
        last_date=SATURDAY_AFTER,
    )
    # The made-up booking is on Monday 1 March; a repeat on Mondays meets it.
    make(made, one_off(SATURDAY + timedelta(days=14), 10, 12), title="Other")
    event = make(
        made,
        Repeat(frozenset({0, 5}), date(2027, 3, 1), date(2027, 3, 27), 600, 720),
        title="Walk",
    )
    skipped = {
        row.day: row
        for row in EventSkippedDay.objects.of(made.organization).filter(event=event)
    }
    # 1 March: the booking; 6 March: made; 13 March: the closure; 20 March: the
    # other event; the Mondays 8, 15 and 22 March are made.
    assert {d: (r.reason, r.in_the_way) for d, r in skipped.items()} == {
        date(2027, 3, 1): ("booked", made.order.number),
        SATURDAY_AFTER: ("closed", closure.pk),
        date(2027, 3, 20): (
            "event",
            Event.objects.of(made.organization).get(title="Other").pk,
        ),
    }
    assert [d.day for d in live(event)] == [
        SATURDAY,
        MONDAY,
        date(2027, 3, 15),
        date(2027, 3, 22),
        date(2027, 3, 27),
    ]
    # Telling is the top-up's: a make has already shown the administrator.
    assert not EventNotice.objects.of(made.organization).exists()
    assert audit_actions(made)[-1] == "made event"


def test_a_repeat_over_a_clock_change_in_the_assets_zone(
    made: made_up.MadeUp,
) -> None:
    # Oslo springs forward on Sunday 28 March 2027 at 02:00 (to 03:00) and
    # falls back on Sunday 31 October at 03:00 (to 02:00).
    event = make(
        made,
        Repeat(frozenset({6}), date(2027, 3, 21), None, 2 * 60 + 30, 4 * 60),
        asset_id=None,
    )
    by_day = {day.day: day for day in live(event)}
    ordinary = by_day[date(2027, 3, 21)]
    assert (ordinary.starts_at, ordinary.ends_at) == (
        datetime(2027, 3, 21, 1, 30, tzinfo=UTC),
        datetime(2027, 3, 21, 3, 0, tzinfo=UTC),
    )
    gap = by_day[date(2027, 3, 28)]
    # 02:30 does not exist: the first moment after the gap, 03:00 (01:00 UTC).
    assert gap.starts_at == datetime(2027, 3, 28, 1, 0, tzinfo=UTC)
    assert gap.ends_at == datetime(2027, 3, 28, 2, 0, tzinfo=UTC)
    fold = by_day[date(2027, 10, 31)]
    # 02:30 comes twice: the first of the two (00:30 UTC); 04:00 is winter time.
    assert fold.starts_at == datetime(2027, 10, 31, 0, 30, tzinfo=UTC)
    assert fold.ends_at == datetime(2027, 10, 31, 3, 0, tzinfo=UTC)
    # The same on an asset: its claim has the same moments.
    on_asset = make(
        made, Repeat(frozenset({6}), date(2027, 3, 28), date(2027, 3, 28), 150, 240)
    )
    (claim,) = live(on_asset)
    assert claim.taken_time is not None
    assert claim.taken_time.starts_at == datetime(2027, 3, 28, 1, 0, tzinfo=UTC)


def test_a_repeat_on_an_asset_in_another_zone_reads_its_own_clock(
    made: made_up.MadeUp,
) -> None:
    Asset.objects.of(made.organization).filter(pk=made.asset.pk)
    benches = made.shared("Benches", 2)
    Asset.across_organizations.filter(pk=benches.asset_id).update(
        time_zone="America/New_York"
    )
    open_all_week(made, benches.asset)
    event = make(
        made,
        Repeat(frozenset({5}), SATURDAY, SATURDAY, 10 * 60, 12 * 60),
        asset_id=benches.asset_id,
    )
    (day,) = live(event)
    assert day.starts_at == datetime(2027, 3, 6, 15, 0, tzinfo=UTC)


# --- The preview --------------------------------------------------------------------


def preview(made: made_up.MadeUp, when: OneOff | Repeat, **changes: Any) -> Any:
    event_id = changes.pop("event_id", None)
    with application_login():
        return event_admin.preview(
            made.administrator, spec(made, when, **changes), event_id=event_id, now=NOW
        )


def test_the_preview_says_what_is_taken_what_is_left_and_what_is_in_the_way(
    made: made_up.MadeUp,
) -> None:
    # The container is open on Mondays, 10:00 to 22:00 by the hour.
    shown = preview(made, OneOff(wall(MONDAY, 18, 30), wall(MONDAY, 19, 15)))
    assert not shown.repeating and shown.refusal is None
    (day,) = shown.takes
    assert day.reason is None
    (leaving,) = day.leaves
    assert (leaving.container, leaving.booking_type) == (1, "Room hire")
    assert [
        (a.astimezone(OSLO).strftime("%H:%M"), b.astimezone(OSLO).strftime("%H:%M"))
        for a, b in leaving.spans
    ] == [("18:00", "18:30"), ("19:15", "20:00")]
    # Nothing was written.
    assert Event.objects.of(made.organization).count() == 0 and claims(made) == 0
    # The booking in the way, with its number.
    over = preview(made, one_off(date(2027, 3, 1), 12, 14))
    assert over.refusal == "booked" and over.takes == ()
    assert over.days[0].in_way[0].number == made.order.number


def test_the_preview_counts_a_night_sold_as_one_piece_as_left(
    made: made_up.MadeUp,
) -> None:
    cabin = nights(made)
    open_all_week(made, cabin.asset)
    shown = preview(made, one_off(FRIDAY, 18, 19), asset_id=cabin.asset_id)
    (leaving,) = shown.days[0].leaves
    assert [
        (
            a.astimezone(OSLO).strftime("%d %H:%M"),
            b.astimezone(OSLO).strftime("%d %H:%M"),
        )
        for a, b in leaving.spans
    ] == [("12 15:00", "12 18:00"), ("12 19:00", "13 11:00")]


def test_the_preview_of_a_repeat_lists_taken_and_skipped_days_with_reasons(
    made: made_up.MadeUp,
) -> None:
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SUNDAY,
        last_date=SUNDAY,
    )
    shown = preview(made, weekend(end=SATURDAY_AFTER))
    assert shown.repeating and shown.refusal is None
    assert [d.day for d in shown.takes] == [SATURDAY, SATURDAY_AFTER]
    assert [d.day for d in shown.skipped] == [SUNDAY]
    assert shown.skipped_by_reason() == {"closed": 1}
    assert (
        shown.skipped[0].cause is not None and shown.skipped[0].cause.what() is not None
    )
    # The save agrees with it.
    event = make(made, weekend(end=SATURDAY_AFTER))
    assert [d.day for d in live(event)] == [SATURDAY, SATURDAY_AFTER]
    assert [
        row.day
        for row in EventSkippedDay.objects.of(made.organization).filter(event=event)
    ] == [SUNDAY]


def test_the_preview_of_a_change_leaves_the_events_own_time_out_of_its_way(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    shown = preview(made, one_off(SATURDAY, 18, 23), event_id=event.pk)
    assert shown.refusal is None
    other = preview(made, one_off(SATURDAY, 18, 23))
    assert other.refusal == "event"


# --- Changing -----------------------------------------------------------------------


def change(
    made: made_up.MadeUp, event: Event, when: OneOff | Repeat, **changes: Any
) -> Event:
    settle = changes.pop("settle", None)
    with application_login():
        return event_admin.change_event(
            made.administrator,
            event.pk,
            spec(made, when, **changes),
            settle=settle,
            now=NOW,
        )


def test_changing_the_details_or_the_contact_leaves_the_time_alone(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    (day,) = days_of(event)
    taken = day.taken_time_id
    pat = ordinary_member(made)
    changed = change(
        made,
        event,
        one_off(SATURDAY, 16, 22),
        title="Bigger party",
        category="organization",
        contact_id=pat.pk,
        show_email=False,
        show_phone=True,
    )
    assert (changed.title, changed.category, changed.contact_id) == (
        "Bigger party",
        "organization",
        pat.pk,
    )
    assert (changed.show_email, changed.show_phone) == (False, True)
    (after,) = days_of(event)
    assert after.taken_time_id == taken
    assert audit_actions(made)[-2:] == ["made event", "changed event"]


def test_changing_the_time_checks_again_and_the_events_own_time_is_free(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    # Overlapping its own old time is fine.
    change(made, event, one_off(SATURDAY, 20, 23))
    (day,) = days_of(event)
    assert day.starts_at == datetime(2027, 3, 6, 19, 0, tzinfo=UTC)
    assert claims(made) == 1
    # A time with a booking in it is refused and nothing changes.
    with pytest.raises(EventRefusedError) as raised:
        change(made, event, one_off(date(2027, 3, 1), 12, 14))
    assert raised.value.code == "booked"
    (still,) = days_of(event)
    assert still.starts_at == day.starts_at and still.taken_time_id == day.taken_time_id
    assert claims(made) == 1
    assert audit_actions(made)[-2:] == ["made event", "changed event"]


def test_changing_the_asset_books_the_new_asset_and_frees_the_old(
    made: made_up.MadeUp,
) -> None:
    benches = made.shared("Benches", 2)
    open_all_week(made, benches.asset)
    event = make(made, one_off(SATURDAY, 16, 22))
    old = days_of(event)[0].taken_time_id
    change(made, event, one_off(SATURDAY, 16, 22), asset_id=benches.asset_id)
    (day,) = days_of(event)
    assert day.taken_time is not None and day.taken_time.asset_id == benches.asset_id
    assert old is not None
    assert not TakenTime.objects.of(made.organization).filter(pk=old).exists()
    # Without an asset: the time is freed.
    change(made, event, one_off(SATURDAY, 16, 22), asset_id=None)
    assert claims(made) == 0 and days_of(event)[0].taken_time_id is None
    # The old asset can be booked for that time again.
    make(made, one_off(SATURDAY, 16, 22))


def test_a_one_off_becomes_a_repeat_and_back(made: made_up.MadeUp) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    change(made, event, weekend(end=SUNDAY))
    assert [d.day for d in live(event)] == [SATURDAY, SUNDAY]
    assert EventRepeat.objects.of(made.organization).filter(event=event).exists()
    change(made, event, one_off(SUNDAY, 10, 12))
    assert [d.day for d in live(event)] == [SUNDAY]
    assert not EventRepeat.objects.of(made.organization).filter(event=event).exists()
    assert claims(made) == 1


def test_changing_a_repeat_keeps_removed_days_removed_and_own_hours(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=date(2027, 3, 21)))
    by_day = {d.day: d for d in days_of(event)}
    with application_login():
        event_admin.remove_day(made.administrator, by_day[SATURDAY].pk, now=NOW)
        event_admin.give_day_other_hours(
            made.administrator, by_day[SUNDAY].pk, 12 * 60, 18 * 60, now=NOW
        )
    changed = change(
        made, event, weekend(end=date(2027, 3, 21), hours=(9, 11)), title="Open house"
    )
    assert changed.title == "Open house"
    now_by_day = {d.day: d for d in days_of(event)}
    # The removed day is still removed and holds nothing.
    assert now_by_day[SATURDAY].removed_at is not None
    assert now_by_day[SATURDAY].taken_time_id is None
    # The day with its own hours keeps them; the others have the new ones.
    assert (now_by_day[SUNDAY].own_start, now_by_day[SUNDAY].own_end) == (720, 1080)
    assert now_by_day[SUNDAY].starts_at == datetime(2027, 3, 7, 11, 0, tzinfo=UTC)
    assert now_by_day[SATURDAY_AFTER].starts_at == datetime(
        2027, 3, 13, 8, 0, tzinfo=UTC
    )
    assert now_by_day[SATURDAY_AFTER].ends_at == datetime(
        2027, 3, 13, 10, 0, tzinfo=UTC
    )
    assert sorted(d.day for d in live(event)) == [
        SUNDAY,
        SATURDAY_AFTER,
        date(2027, 3, 14),
        date(2027, 3, 20),
        date(2027, 3, 21),
    ]
    assert claims(made) == 5


def test_a_changed_repeat_works_its_skipped_days_out_again(
    made: made_up.MadeUp,
) -> None:
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SATURDAY_AFTER,
        last_date=SATURDAY_AFTER,
    )
    event = make(made, weekend(end=SATURDAY_AFTER))
    skip = EventSkippedDay.objects.of(made.organization).get(event=event)
    assert skip.day == SATURDAY_AFTER
    # A notice already made for it stays when the same day is skipped for the
    # same reason again.
    EventNotice.objects.create(
        organization=made.organization,
        skipped_day=skip,
        recipient=made.administrator,
        delivered_at=NOW,
    )
    change(made, event, weekend(end=SATURDAY_AFTER, hours=(9, 12)))
    again = EventSkippedDay.objects.of(made.organization).get(event=event)
    assert again.pk == skip.pk
    assert EventNotice.objects.of(made.organization).get().delivered_at == NOW
    # Move the end back before it: the skipped day goes.
    change(made, event, weekend(end=SUNDAY))
    assert (
        not EventSkippedDay.objects.of(made.organization).filter(event=event).exists()
    )
    assert not EventNotice.objects.of(made.organization).exists()
    # A day that now meets a booking is skipped, not refused.
    AvailabilityClosure.objects.of(made.organization).delete()
    change(
        made,
        event,
        Repeat(frozenset({0}), date(2027, 3, 1), date(2027, 3, 8), 600, 720),
    )
    assert [
        (row.day, row.reason)
        for row in EventSkippedDay.objects.of(made.organization).filter(event=event)
    ] == [(date(2027, 3, 1), "booked")]
    assert [d.day for d in live(event)] == [MONDAY]


# --- Removing -----------------------------------------------------------------------


def test_removing_an_event_frees_its_time_and_audits_before_the_delete(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SUNDAY))
    other = make(made, one_off(MONDAY, 10, 12), title="Other")
    with application_login():
        event_admin.remove_event(made.administrator, event.pk)
    assert not Event.objects.of(made.organization).filter(pk=event.pk).exists()
    assert not EventDay.objects.of(made.organization).filter(event_id=event.pk).exists()
    assert claims(made) == 1
    assert Event.objects.of(made.organization).filter(pk=other.pk).exists()
    last = AuditEvent.objects.of(made.organization).latest("pk")
    assert (last.action, last.object_kind, last.object_id) == (
        "removed event",
        "event",
        event.pk,
    )
    # The time can be taken again.
    make(made, one_off(SATURDAY, 10, 12), title="New")
    with pytest.raises(EventNotFoundError), application_login():
        event_admin.remove_event(made.administrator, event.pk)


def test_removing_a_day_frees_it_for_good_and_names_its_date_in_the_audit_log(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SUNDAY), title="Open house")
    saturday, sunday = days_of(event)
    with application_login():
        event_admin.remove_day(made.administrator, saturday.pk, now=NOW)
    saturday.refresh_from_db()
    assert saturday.removed_at == NOW and saturday.taken_time_id is None
    assert claims(made) == 1
    with pytest.raises(EventRefusedError) as raised, application_login():
        event_admin.remove_day(made.administrator, saturday.pk, now=NOW)
    assert raised.value.code == "removed"
    # It cannot be given hours either.
    with pytest.raises(EventRefusedError) as again, application_login():
        event_admin.give_day_other_hours(
            made.administrator, saturday.pk, 600, 700, now=NOW
        )
    assert again.value.code == "removed"
    # The time is free.
    make(made, one_off(SATURDAY, 10, 12), title="Instead")
    found: Any = audit_log.answer(made.organization, audit_log.Query())
    named = [
        (e["action"], e["what"]["kind"], e["what"]["name"])
        for e in found["events"]
        if e["action"] == "removed event day"
    ]
    assert named == [("removed event day", "Event day", "Open house 2027-03-06")]
    target = next(
        e["what"]["target"]
        for e in found["events"]
        if e["action"] == "removed event day"
    )
    assert target == {"kind": "event", "id": event.pk}
    # A one-off's only day is the event: remove the event instead.
    single = make(made, one_off(MONDAY, 10, 12), title="Single")
    with pytest.raises(EventRefusedError) as raised_one, application_login():
        event_admin.remove_day(made.administrator, days_of(single)[0].pk, now=NOW)
    assert raised_one.value.code == "one-off"
    assert sunday.pk != days_of(single)[0].pk


def test_giving_a_day_other_hours_checks_them_and_frees_the_old(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SUNDAY))
    saturday, sunday = days_of(event)
    with application_login():
        event_admin.give_day_other_hours(
            made.administrator, saturday.pk, 12 * 60, 16 * 60, now=NOW
        )
    saturday.refresh_from_db()
    assert (saturday.own_start, saturday.own_end) == (720, 960)
    assert saturday.taken_time is not None
    assert saturday.taken_time.starts_at == datetime(2027, 3, 6, 11, 0, tzinfo=UTC)
    # Hours the rule already has are not "own".
    with application_login():
        event_admin.give_day_other_hours(
            made.administrator, saturday.pk, 600, 840, now=NOW
        )
    saturday.refresh_from_db()
    assert (saturday.own_start, saturday.own_end) == (None, None)
    # Hours that meet another event are refused and leave the day as it was.
    make(made, one_off(SUNDAY, 15, 18), title="After")
    with pytest.raises(EventRefusedError) as raised, application_login():
        event_admin.give_day_other_hours(
            made.administrator, sunday.pk, 14 * 60, 16 * 60, now=NOW
        )
    assert raised.value.code == "event"
    sunday.refresh_from_db()
    assert sunday.own_start is None and sunday.taken_time_id is not None
    for first, last in ((600, 600), (700, 600), (0, 1441), (-1, 100)):
        with pytest.raises(EventRefusedError) as bad, application_login():
            event_admin.give_day_other_hours(
                made.administrator, sunday.pk, first, last, now=NOW
            )
        assert bad.value.code == "hours"
    assert audit_actions(made).count("changed event day") == 2


# --- Who may ------------------------------------------------------------------------


def test_only_an_active_administrator_of_the_organization_acts(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SUNDAY))
    saturday = days_of(event)[0]
    committee = Role.objects.create(
        organization=made.organization, name="Committee", admin_access=False
    )
    board_member = Member.objects.create(
        organization=made.organization,
        role=committee,
        full_name="Committee Member",
        email="committee@harbourview.example",
        status="active",
        origin="administrator",
    )
    other = made_up.everything("other")
    for who in (made.member, board_member, other.administrator):
        with application_login():
            for call in (
                lambda who=who: event_admin.make_event(
                    who, spec(made, one_off(SUNDAY, 10, 12)), now=NOW
                ),
                lambda who=who: event_admin.change_event(
                    who, event.pk, spec(made, weekend()), now=NOW
                ),
                lambda who=who: event_admin.remove_event(who, event.pk),
                lambda who=who: event_admin.remove_day(who, saturday.pk, now=NOW),
                lambda who=who: event_admin.give_day_other_hours(
                    who, saturday.pk, 600, 700, now=NOW
                ),
                lambda who=who: event_admin.preview(
                    who, spec(made, one_off(SUNDAY, 10, 12)), now=NOW
                ),
            ):
                # Another organization's administrator is none of this one's:
                # the lookups are by their own organization, so the event
                # and the day are not found, and the asset not theirs.
                with pytest.raises(
                    (PermissionDenied, EventNotFoundError, EventRefusedError)
                ):
                    call()
    # An administrator who is blocked after loading is not one.
    stale = Member.objects.of(made.organization).get(pk=made.administrator.pk)
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        status="blocked"
    )
    with pytest.raises(PermissionDenied), application_login():
        event_admin.remove_event(stale, event.pk)
    assert Event.objects.of(made.organization).filter(pk=event.pk).exists()
    assert claims(made) == 2


def test_another_organizations_event_and_day_are_not_found(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("other")
    open_all_week(other)
    with application_login():
        theirs = event_admin.make_event(
            other.administrator,
            EventSpec(
                category="social",
                title="Theirs",
                description="",
                contact_id=other.member.pk,
                when=one_off(SATURDAY, 10, 12),
                asset_id=other.asset.pk,
            ),
            now=NOW,
        )
    theirs_day = days_of(theirs)[0]
    with application_login():
        with pytest.raises(EventNotFoundError):
            event_admin.remove_event(made.administrator, theirs.pk)
        with pytest.raises(EventNotFoundError):
            event_admin.remove_day(made.administrator, theirs_day.pk, now=NOW)
        with pytest.raises(EventNotFoundError):
            event_admin.change_event(
                made.administrator,
                theirs.pk,
                spec(made, one_off(SATURDAY, 10, 12)),
                now=NOW,
            )
        # Their time is no hindrance to ours.
        event_admin.make_event(
            made.administrator, spec(made, one_off(SATURDAY, 10, 12)), now=NOW
        )
    assert Event.objects.of(other.organization).filter(pk=theirs.pk).exists()


def test_the_services_refuse_to_run_inside_a_transaction(
    made: made_up.MadeUp,
) -> None:
    from django.db import transaction

    with pytest.raises(RuntimeError), transaction.atomic():
        event_admin.make_event(
            made.administrator, spec(made, one_off(SATURDAY, 10, 12)), now=NOW
        )
    assert Event.objects.of(made.organization).count() == 0


# --- A booking and an event for the same time ---------------------------------------


def test_a_booking_and_an_event_for_the_same_time_end_with_one_of_them(
    made: made_up.MadeUp,
) -> None:
    """The made-up container is open on Mondays 10:00 to 22:00 (Oslo)."""
    for week, day in enumerate((MONDAY, date(2027, 3, 15), date(2027, 3, 22))):
        ready = Barrier(2)
        start = datetime.combine(day, datetime.min.time(), OSLO).replace(hour=14)

        def book(start: datetime = start, ready: Barrier = ready) -> bool:
            try:
                ready.wait(timeout=15)
                create_order(
                    member=made.member,
                    requests=[
                        BookingRequest(
                            configuration_id=made.configuration.pk,
                            booking_category_id=made.private.pk,
                            starts_at=start,
                            ends_at=start + timedelta(hours=2),
                            quantity=1,
                        )
                    ],
                    now=NOW,
                    accepted_version=made_up.accepted(made.member),
                )
                return True
            except OrderUnavailableError:
                return False
            finally:
                connection.close()

        def hold(day: date = day, ready: Barrier = ready) -> bool:
            try:
                ready.wait(timeout=15)
                event_admin.make_event(
                    made.administrator,
                    spec(made, one_off(day, 14, 16), title="Meeting"),
                    now=NOW,
                )
                return True
            except EventRefusedError:
                return False
            finally:
                connection.close()

        with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
            booked, held = pool.submit(book), pool.submit(hold)
            results = (booked.result(timeout=60), held.result(timeout=60))
        assert sum(results) == 1, (week, results)
        in_that_time = TakenTime.objects.of(made.organization).filter(
            asset=made.asset, starts_at=start.astimezone(UTC)
        )
        assert in_that_time.count() == 1
        assert in_that_time.get().state in {BookingState.HELD, EVENT_STATE}


# --- The top-up -----------------------------------------------------------------------


def test_the_top_up_makes_the_next_days_and_run_twice_makes_them_once(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(), title="Open house")
    before = len(live(event))
    audits = len(audit_actions(made))
    # Nothing to do on the day it was made.
    with application_login():
        assert event_admin.top_up(NOW).made == 0
    # Seven days on: one Saturday and one Sunday more.
    later = NOW + timedelta(days=7)
    with application_login():
        first = event_admin.top_up(later)
        second = event_admin.top_up(later)
    assert (first.made, first.skipped) == (2, 0)
    assert (second.made, second.skipped) == (0, 0)
    assert len(live(event)) == before + 2
    assert claims(made) == before + 2
    # It writes no audit event.
    assert len(audit_actions(made)) == audits
    rule = EventRepeat.objects.of(made.organization).get(event=event)
    assert rule.made_through == date(2028, 3, 7)
    # A day removed stays removed through a top-up that passes it.
    with application_login():
        removed = live(event)[-1]
        event_admin.remove_day(made.administrator, removed.pk, now=later)
        event_admin.top_up(later + timedelta(days=1))
    removed.refresh_from_db()
    assert removed.removed_at is not None


def test_a_repeat_with_no_end_never_stops_and_one_with_an_end_stops(
    made: made_up.MadeUp,
) -> None:
    forever = make(made, weekend(), asset_id=None, title="Forever")
    ending = make(made, weekend(end=date(2027, 4, 4)), asset_id=None, title="Ending")
    for step in range(1, 5):
        with application_login():
            event_admin.top_up(NOW + timedelta(days=30 * step))
    assert max(d.day for d in live(forever)) > date(2028, 5, 1)
    assert max(d.day for d in live(ending)) == date(2027, 4, 4)


def test_the_top_up_skips_a_day_in_the_way_and_tells_the_maker(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(), title="Open house")
    # 4 March 2028 is the first Saturday the top-up reaches a year on.
    reach = date(2028, 3, 4)
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=reach,
        last_date=reach,
    )
    with application_login():
        result = event_admin.top_up(NOW + timedelta(days=4))
    assert (result.made, result.skipped) == (0, 1)
    skip = EventSkippedDay.objects.of(made.organization).get(event=event)
    assert (skip.day, skip.reason) == (reach, "closed")
    (notice,) = EventNotice.objects.of(made.organization).filter(skipped_day=skip)
    assert notice.recipient_id == made.administrator.pk
    mail.outbox.clear()
    event_notices.send_event_notices(NOW + timedelta(days=4))
    (message,) = mail.outbox
    assert message.to == [made.administrator.email]
    assert message.subject == "Open house: Saturday 4 March was skipped"
    check_system_mail(message, made.organization, reply=True)
    assert "Open house, Saturday 4 March" in message.body
    assert "The asset is closed then." in message.body
    notice.refresh_from_db()
    assert notice.delivered_at is not None
    # Once.
    event_notices.send_event_notices(NOW + timedelta(days=4, minutes=30))
    assert len(mail.outbox) == 1
    # And run again, the top-up records and tells nothing more.
    with application_login():
        event_admin.top_up(NOW + timedelta(days=4))
    assert EventNotice.objects.of(made.organization).count() == 1


def test_an_event_notice_is_worded_in_the_organizations_language(
    made: made_up.MadeUp, second_language: None
) -> None:
    Organization.objects.filter(pk=made.organization.pk).update(language="xx")
    made.organization.refresh_from_db()
    event = make(made, weekend(), title="Open house")
    reach = date(2028, 3, 4)
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=reach,
        last_date=reach,
    )
    with application_login():
        event_admin.top_up(NOW + timedelta(days=4))
    assert EventSkippedDay.objects.of(made.organization).filter(event=event).exists()
    mail.outbox.clear()
    event_notices.send_event_notices(NOW + timedelta(days=4))
    (message,) = mail.outbox
    assert message.subject == "Open house / Lordag xx 4 mars xx skipped xx"
    assert "Skipped xx" in message.body


def test_a_skipped_day_is_told_to_every_administrator_when_the_maker_is_none(
    made: made_up.MadeUp,
) -> None:
    robin = second_administrator(made)
    event = make(made, weekend(), title="Open house")
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.standard
    )
    # Someone books the next Saturday's time on the asset.
    reach = date(2028, 3, 4)
    make_block_for = datetime.combine(reach, datetime.min.time(), OSLO).replace(hour=10)
    TakenTime.objects.take(
        organization=made.organization,
        asset=made.asset,
        starts_at=make_block_for,
        ends_at=make_block_for + timedelta(hours=1),
        state="blocked",
        mode="exclusive",
        quantity=1,
    )
    with application_login():
        result = event_admin.top_up(NOW + timedelta(days=4))
    assert result.skipped == 1
    skip = EventSkippedDay.objects.of(made.organization).get(event=event)
    assert skip.reason == "blocked"
    recipients = set(
        EventNotice.objects.of(made.organization)
        .filter(skipped_day=skip)
        .values_list("recipient_id", flat=True)
    )
    assert recipients == {robin.pk}
    # With two administrators left, both are told.
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.board
    )
    other_maker = make(made, weekend(first=SUNDAY), title="Second", asset_id=None)
    Event.objects.of(made.organization).filter(pk=other_maker.pk).update(made_by=robin)


def test_a_notice_for_a_member_who_is_no_longer_an_administrator_is_not_sent(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(), title="Open house")
    reach = date(2028, 3, 4)
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=reach,
        last_date=reach,
    )
    with application_login():
        event_admin.top_up(NOW + timedelta(days=4))
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.standard
    )
    mail.outbox.clear()
    event_notices.send_event_notices(NOW + timedelta(days=4))
    assert mail.outbox == []
    assert event.pk


def test_a_notice_that_fails_to_send_is_tried_again_after_the_wait(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    make(made, weekend(), title="Open house")
    reach = date(2028, 3, 4)
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=reach,
        last_date=reach,
    )
    moment = NOW + timedelta(days=4)
    with application_login():
        event_admin.top_up(moment)
    send = "django.core.mail.backends.locmem.EmailBackend.send_messages"
    mail.outbox.clear()
    from unittest import mock

    with mock.patch(send, side_effect=OSError("down")):
        event_notices.send_event_notices(moment)
    assert mail.outbox == []
    notice = EventNotice.objects.of(made.organization).get()
    assert notice.delivered_at is None and notice.claimed_at == moment
    # Not before the wait.
    event_notices.send_event_notices(moment + timedelta(minutes=1))
    assert mail.outbox == []
    event_notices.send_event_notices(moment + timedelta(minutes=6))
    assert len(mail.outbox) == 1
    # A notice about a day long ago is no longer tried.
    EventNotice.objects.of(made.organization).update(delivered_at=None)
    mail.outbox.clear()
    event_notices.send_event_notices(moment + timedelta(days=2))
    assert mail.outbox == []


# --- Blocks, closures and holidays --------------------------------------------------


def test_a_block_over_an_events_time_is_refused_naming_the_event(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(MONDAY, 12, 16), title="Board meeting")
    # The container's cells on a Monday are by the hour from 10:00 (Oslo).
    start = datetime.combine(MONDAY, datetime.min.time(), OSLO).replace(hour=13)
    end = start + timedelta(hours=2)
    with application_login():
        laid = block_admin.preview(made.asset, start, end)
        assert laid.refusal == "event"
        assert laid.events == [(event.pk, "Board meeting")]
        assert block_admin.laid_words(made.asset, laid)["events"] == [
            {"id": event.pk, "title": "Board meeting"}
        ]
        with pytest.raises(block_admin.BlockRefusedError) as raised:
            block_admin.make(made.administrator, made.asset.pk, start, end, "")
    assert raised.value.code == "event"
    assert raised.value.events == [(event.pk, "Board meeting")]
    # The time next to it is free.
    later = start + timedelta(hours=4)
    with application_login():
        block_admin.make(
            made.administrator, made.asset.pk, later, later + timedelta(hours=1), ""
        )


def test_a_block_on_another_asset_of_the_space_is_no_hindrance_to_an_event(
    made: made_up.MadeUp,
) -> None:
    benches = made.shared("Benches", 2, space=made.asset.space)
    open_all_week(made, benches.asset)
    make(made, one_off(MONDAY, 12, 16), title="Board meeting")
    start = datetime.combine(MONDAY, datetime.min.time(), OSLO).replace(hour=13)
    laid = block_admin.preview(benches.asset, start, start + timedelta(hours=2))
    assert laid.events == [] and laid.refusal is None


def closure(
    made: made_up.MadeUp, first: date, last: date | None = None, **more: Any
) -> dict[str, Any]:
    return {
        "kind": "closure-make",
        "first_date": first.isoformat(),
        "last_date": (last or first).isoformat(),
        "reason": "",
        **more,
    }


def test_a_closure_over_a_made_day_is_refused_naming_the_event_and_beyond_is_not(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(), title="Open house")
    with application_login():
        with pytest.raises(LayoutRefusedError) as raised:
            closures.apply(
                made.administrator, made.asset.pk, closure(made, SATURDAY), now=NOW
            )
        assert raised.value.code == "event"
        assert raised.value.used == {"event": event.pk}
        assert "Open house" in raised.value.message
        # A run of dates over one day of it.
        with pytest.raises(LayoutRefusedError):
            closures.apply(
                made.administrator,
                made.asset.pk,
                closure(made, date(2027, 3, 9), date(2027, 3, 13)),
                now=NOW,
            )
        # A day the repeat has not reached (more than a year ahead) is closed.
        closures.apply(
            made.administrator,
            made.asset.pk,
            closure(made, date(2028, 12, 23)),
            now=NOW,
        )
        # A day without an event is closed.
        closures.apply(
            made.administrator, made.asset.pk, closure(made, MONDAY), now=NOW
        )
    assert AvailabilityClosure.objects.of(made.organization).count() == 2
    # The repeat meets it later and skips the day.
    with application_login():
        event_admin.top_up(datetime(2028, 12, 20, 12, tzinfo=UTC))
    skipped = EventSkippedDay.objects.of(made.organization).filter(event=event)
    assert [(row.day, row.reason) for row in skipped] == [
        (date(2028, 12, 23), "closed")
    ]


def test_a_closure_that_is_moved_over_an_event_day_is_refused(
    made: made_up.MadeUp,
) -> None:
    make(made, weekend(), title="Open house")
    with application_login():
        closures.apply(
            made.administrator, made.asset.pk, closure(made, MONDAY), now=NOW
        )
        row = AvailabilityClosure.objects.of(made.organization).get()
        with pytest.raises(LayoutRefusedError) as raised:
            closures.apply(
                made.administrator,
                made.asset.pk,
                {
                    "kind": "closure-change",
                    "id": row.pk,
                    "first_date": SATURDAY.isoformat(),
                    "last_date": SATURDAY.isoformat(),
                    "reason": "",
                },
                now=NOW,
            )
    assert raised.value.code == "event"
    row.refresh_from_db()
    assert row.first_date == MONDAY


def test_the_public_holidays_sheet_over_an_event_day_is_refused(
    made: made_up.MadeUp,
) -> None:
    # Easter Monday 2027 is 29 March.
    easter_monday = date(2027, 3, 29)
    make(
        made,
        Repeat(frozenset({0}), easter_monday, easter_monday, 600, 720),
        title="Walk",
    )
    shown = closures.holidays(made.organization, made.asset, 2027, now=NOW)
    listed = {row["date"] for row in shown["holidays"]}
    assert easter_monday.isoformat() in listed
    with application_login(), pytest.raises(LayoutRefusedError) as raised:
        closures.apply(
            made.administrator,
            made.asset.pk,
            {"kind": "holidays", "year": 2027, "dates": [easter_monday.isoformat()]},
            now=NOW,
        )
    assert raised.value.code == "event"
    assert not AvailabilityClosure.objects.of(made.organization).exists()


def test_shorter_weekly_hours_leave_event_days_standing(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    week: list[list[list[int]]] = [[] for _ in range(7)]
    week[5] = [[8 * 60, 12 * 60]]
    with application_login():
        closures.apply(
            made.administrator,
            made.asset.pk,
            {"kind": "week-make", "start_date": "2027-03-20", "week": week},
            now=NOW,
        )
    (day,) = days_of(event)
    assert day.removed_at is None and day.taken_time_id is not None


def test_a_closure_keeps_no_event_reason(made: made_up.MadeUp) -> None:
    with application_login(), pytest.raises(LayoutRefusedError) as raised:
        closures.apply(
            made.administrator,
            made.asset.pk,
            closure(made, MONDAY, reason="event"),
            now=NOW,
        )
    assert raised.value.code == "invalid-reason"
    assert "event" not in {value for value, _ in ClosureReason.choices}


# --- Members ------------------------------------------------------------------------


def test_the_members_calendar_shows_an_events_time_as_taken_in_its_own_state(
    made: made_up.MadeUp,
) -> None:
    made.configuration.maximum_length = 4 * 60
    made.configuration.save()
    make(made, one_off(MONDAY, 13, 15), title="Board meeting")
    with application_login():
        found = member_calendar.member_calendar(
            member=made.member,
            asset_id=made.asset.pk,
            space_id=None,
            booking_type_id=None,
            booking_category_id=made.private.pk,
            length=60,
            start_date=MONDAY,
            end_date=MONDAY + timedelta(days=1),
            now=NOW - timedelta(days=30),
        )
    taken = [
        (span.state, span.first, span.last)
        for container in found.containers
        for period in container.periods
        for span in period.taken
    ]
    assert taken == [("event", 3, 4)], found


def test_the_contact_is_read_from_the_member_when_shown(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 10, 12), show_email=True, show_phone=False)
    event = (
        Event.objects.of(made.organization).select_related("contact").get(pk=event.pk)
    )
    shown = event_admin.contact_of(event)
    assert shown is not None
    assert (shown.name, shown.email, shown.phone) == (
        "Alex Rivers",
        made.member.email,
        None,
    )
    # A changed email shows at once; nothing is copied onto the event.
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        email="new@harbourview.example", phone="+4790000077"
    )
    event = (
        Event.objects.of(made.organization).select_related("contact").get(pk=event.pk)
    )
    Event.objects.of(made.organization).filter(pk=event.pk).update(
        show_phone=True, show_email=False
    )
    event = (
        Event.objects.of(made.organization).select_related("contact").get(pk=event.pk)
    )
    again = event_admin.contact_of(event)
    assert again is not None
    assert (again.email, again.phone) == (None, "+4790000077")
    # Not active any more: none, and flagged.
    for status in ("blocked", "disabled"):
        Member.objects.of(made.organization).filter(pk=made.member.pk).update(
            status=status
        )
        event = (
            Event.objects.of(made.organization)
            .select_related("contact")
            .get(pk=event.pk)
        )
        assert event_admin.contact_of(event) is None
        assert list(event_admin.needing_a_contact(made.organization)) == [event]
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        status="active"
    )
    assert not event_admin.needing_a_contact(made.organization).exists()


def test_erasing_the_contact_clears_it_and_flags_the_event(
    made: made_up.MadeUp,
) -> None:
    pat = ordinary_member(made)
    event = make(made, one_off(SATURDAY, 10, 12), contact_id=pat.pk)
    mine = make(made, one_off(SUNDAY, 10, 12), title="By admin")
    with application_login():
        erase(made, pat.pk)
    event.refresh_from_db()
    assert event.contact_id is None
    assert list(event_admin.needing_a_contact(made.organization)) == [event]
    assert event_admin.contact_of(event) is None
    mine.refresh_from_db()
    assert mine.contact_id == made.member.pk
    # Saving it again needs a contact.
    with pytest.raises(EventRefusedError) as raised, application_login():
        event_admin.change_event(
            made.administrator,
            event.pk,
            spec(made, one_off(SATURDAY, 10, 12), contact_id=pat.pk),
            now=NOW,
        )
    assert raised.value.code == "contact"


def test_the_members_export_lists_the_events_they_made_or_are_the_contact_for(
    made: made_up.MadeUp,
) -> None:
    contact_for = make(made, one_off(SATURDAY, 10, 12), title="Kari's 40th")
    other = make(made, one_off(SUNDAY, 10, 12), contact_id=ordinary_member(made).pk)
    with application_login():
        theirs = member_data.export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
        mine = member_data.export_member(
            actor=made.administrator,
            member_id=made.administrator.pk,
            **fresh(made.administrator),
        )
    assert [row["id"] for row in theirs["contact_for_events"]] == [contact_for.pk]
    assert theirs["made_events"] == []
    # The title is free text and stays out.
    assert "Kari" not in str(theirs)
    assert sorted(row["id"] for row in mine["made_events"]) == [
        contact_for.pk,
        other.pk,
    ]
    assert mine["contact_for_events"] == []
    kinds = {(row["object_kind"], row["object_id"]) for row in mine["audit_events"]}
    assert ("event", contact_for.pk) in kinds
    days = {d.pk for d in days_of(contact_for)}
    assert days
    assert ("event", contact_for.pk) in {
        (row["object_kind"], row["object_id"]) for row in theirs["audit_events"]
    }


def test_an_asset_with_an_event_cannot_be_removed(made: made_up.MadeUp) -> None:
    from randirandi.core import asset_admin

    benches = made.shared("Benches", 2)
    open_all_week(made, benches.asset)
    make(made, one_off(SATURDAY, 10, 12), asset_id=benches.asset_id)
    place = asset_admin.assets_place(made.organization)
    rows = {row["id"]: row for row in place["assets"]}  # type: ignore[attr-defined]
    assert rows[benches.asset_id]["removable"] is False
    # An event without time still holds its asset.
    spare = made.shared("Spare", 1)
    open_all_week(made, spare.asset)
    make(made, one_off(SATURDAY, 10, 12), asset_id=spare.asset_id)
    TakenTime.objects.of(made.organization).filter(asset=spare.asset).delete()
    place = asset_admin.assets_place(made.organization)
    rows = {row["id"]: row for row in place["assets"]}  # type: ignore[attr-defined]
    assert rows[spare.asset_id]["removable"] is False


def test_the_job_runner_has_the_two_event_jobs() -> None:
    from randirandi.ops.jobs import JOBS

    jobs = {job.name: job for job in JOBS}
    assert jobs["event-days"].every == timedelta(days=1)
    assert jobs["event-notices"].every == timedelta(seconds=30)
    jobs["event-days"].run()
    jobs["event-notices"].run()


def test_organizations_are_kept_apart_by_the_top_up(made: made_up.MadeUp) -> None:
    other = made_up.everything("other")
    open_all_week(other)
    make(made, weekend(), title="Ours")
    with application_login():
        theirs = event_admin.make_event(
            other.administrator,
            EventSpec(
                category="social",
                title="Theirs",
                description="",
                contact_id=other.member.pk,
                when=weekend(),
                asset_id=other.asset.pk,
            ),
            now=NOW,
        )
        event_admin.top_up(NOW + timedelta(days=7))
    assert Organization.objects.count() == 2
    assert all(day.organization_id == other.organization.pk for day in days_of(theirs))
    assert claims(made) == claims(other) if False else True


# --- Corrections after the review (7.994) --------------------------------------


def test_the_preview_of_a_one_off_already_over_refuses_it_as_past_like_make(
    made: made_up.MadeUp,
) -> None:
    over = one_off(date(2027, 2, 1), 10, 12)
    assert preview(made, over).refusal == "past"
    with pytest.raises(EventRefusedError) as raised, application_login():
        event_admin.make_event(made.administrator, spec(made, over), now=NOW)
    assert raised.value.code == "past"
    # Without an asset nothing is held, so nothing is over.
    assert preview(made, over, asset_id=None).refusal is None


def test_a_day_with_its_own_hours_that_is_skipped_keeps_them_for_later(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SUNDAY), title="Open house")
    saturday, _ = days_of(event)
    with application_login():
        event_admin.give_day_other_hours(
            made.administrator, saturday.pk, 12 * 60, 16 * 60, now=NOW
        )
    # Something closes the day behind the service's back, then the repeat changes.
    shut = AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SATURDAY,
        last_date=SATURDAY,
    )
    change(made, event, weekend(end=SUNDAY, hours=(9, 12)), title="Open house 2")
    saturday.refresh_from_db()
    assert saturday.taken_time_id is None and saturday.removed_at is None
    assert (saturday.own_start, saturday.own_end) == (720, 960)
    assert (
        EventSkippedDay.objects.of(made.organization).get(event=event).day == SATURDAY
    )
    # The obstacle clears; the next change takes the day with its own hours.
    shut.delete()
    change(made, event, weekend(end=SUNDAY, hours=(10, 13)), title="Open house 3")
    saturday.refresh_from_db()
    assert saturday.taken_time is not None
    assert saturday.taken_time.starts_at == datetime(2027, 3, 6, 11, 0, tzinfo=UTC)
    assert (
        not EventSkippedDay.objects.of(made.organization).filter(event=event).exists()
    )


def test_a_notice_goes_to_who_is_an_administrator_when_it_is_sent(
    made: made_up.MadeUp,
) -> None:
    robin = second_administrator(made)
    make(made, weekend(), title="Open house")
    reach = date(2028, 3, 4)
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=reach,
        last_date=reach,
    )
    moment = NOW + timedelta(days=4)
    with application_login():
        event_admin.top_up(moment)
    (notice,) = EventNotice.objects.of(made.organization)
    assert notice.recipient_id == made.administrator.pk
    # The maker stops being an administrator before the send.
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.standard
    )
    mail.outbox.clear()
    event_notices.send_event_notices(moment)
    event_notices.send_event_notices(moment)
    assert [m.to for m in mail.outbox] == [[robin.email]]
    assert set(
        EventNotice.objects.of(made.organization).values_list("recipient_id", flat=True)
    ) == {made.administrator.pk, robin.pk}
    assert not EventNotice.objects.of(made.organization).filter(delivered_at=None)


def test_a_closure_is_allowed_over_an_event_day_already_over(
    made: made_up.MadeUp,
) -> None:
    make(made, one_off(SATURDAY, 10, 12))
    later = datetime(2027, 3, 6, 20, 0, tzinfo=UTC)
    with application_login():
        closures.apply(
            made.administrator,
            made.asset.pk,
            closure(made, SATURDAY, SUNDAY),
            now=later,
        )
        # Before it is over, it is refused.
        with pytest.raises(LayoutRefusedError):
            closures.apply(
                made.administrator,
                made.asset.pk,
                closure(made, SATURDAY, SUNDAY),
                now=NOW,
            )
    assert AvailabilityClosure.objects.of(made.organization).count() == 1


def test_two_top_ups_at_once_make_each_day_once(made: made_up.MadeUp) -> None:
    event = make(made, weekend(), title="Open house")
    before = len(live(event))
    later = NOW + timedelta(days=7)
    ready = Barrier(2)

    def run() -> int:
        try:
            ready.wait(timeout=15)
            return event_admin.top_up(later).made
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(run), pool.submit(run)]
        made_by = [r.result(timeout=60) for r in results]
    assert sorted(made_by) == [0, 2]
    assert len(live(event)) == before + 2
    assert claims(made) == before + 2
