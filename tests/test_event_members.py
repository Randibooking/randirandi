# SPDX-License-Identifier: AGPL-3.0-only
"""What members read of the organization's events : the events answer for signed-in
members of the organization
only, each field a member may see and each one they may not, a removed day, a
skipped day, a contact no longer active, another organization's events; and the
event's title on the members' calendar."""

import json
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import Client, RequestFactory

from randirandi.core import (
    booking_views,
    event_admin,
    event_member_views,
    event_members,
    member_calendar,
)
from randirandi.core.event_admin import Repeat
from randirandi.core.event_members import MembersQuery
from randirandi.core.models import (
    AvailabilityClosure,
    Event,
    Member,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import request_to
from tests.test_admin_shell import config
from tests.test_events import (
    MONDAY,
    NOW,
    SATURDAY,
    SATURDAY_AFTER,
    SUNDAY,
    live,
    make,
    one_off,
    open_all_week,
    weekend,
)
from tests.test_login_sessions import HOST, get, page_settings, sign_in  # noqa: F401
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def made() -> Iterator[made_up.MadeUp]:
    found = made_up.everything()
    open_all_week(found)
    yield found


@pytest.fixture(autouse=True)
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_member_views, "_now", lambda: NOW)


def read(who: Member, **query: Any) -> HttpResponse:
    request = request_to(who, "get", "/events/data/", query, confirmed=False)
    with application_login():
        return event_member_views.events(request)


def answer(who: Member, **query: Any) -> dict[str, Any]:
    response = read(who, **query)
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    found = json.loads(response.content)
    assert isinstance(found, dict)
    return found


def titles(found: dict[str, Any]) -> list[str]:
    return [row["title"] for row in found["events"]]


def pat(made: made_up.MadeUp, status: str = "active") -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Pat Person",
        first_name="Pat",
        last_name="Person",
        email="pat@harbourview.example",
        phone="+4791100001",
        status=status,
        origin="administrator",
    )


# --- Who may ask --------------------------------------------------------------------


def test_a_signed_in_member_of_the_organization_reads_the_events(
    made: made_up.MadeUp,
) -> None:
    make(made, one_off(SATURDAY, 16, 22), title="Summer party")
    for who in (made.member, made.administrator):
        assert titles(answer(who)) == ["Summer party"]


def test_signed_out_and_another_organizations_member_get_a_403(
    made: made_up.MadeUp,
) -> None:
    make(made, one_off(SATURDAY, 16, 22), title="Summer party")
    elsewhere = made_up.everything("elsewhere")
    assert read(elsewhere.member).status_code == 403
    assert read(elsewhere.administrator).status_code == 403
    request = RequestFactory().get("/events/data/", HTTP_HOST=HOST, secure=True)
    request.user = AnonymousUser()
    with application_login():
        assert event_member_views.events(request).status_code == 403


def test_a_signed_out_browser_is_sent_to_sign_in_and_the_wrong_method_is_a_405(
    made: made_up.MadeUp,
) -> None:
    make(made, one_off(SATURDAY, 16, 22))
    for path in ("/events/data/", "/events/", "/events/1/"):
        response = Client().get(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")
    client = Client()
    with application_login():
        assert sign_in(client, made.member).status_code == 302
        assert client.post(
            "/events/data/", HTTP_HOST=HOST, secure=True
        ).status_code in (
            403,
            405,
        )
        listed = get(client, "/events/data/")
        assert listed.status_code == 200
        assert [row["title"] for row in json.loads(listed.content)["events"]] == [
            "Summer party"
        ]
        for path in ("/events/", "/events/1/"):
            page = get(client, path)
            assert page.status_code == 200
            assert config(page.content.decode())["screen"] == "events"


def test_another_organizations_events_are_never_shown(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    open_all_week(elsewhere)
    mine = make(made, one_off(SATURDAY, 16, 22), title="Mine")
    theirs = make(elsewhere, one_off(SATURDAY, 16, 22), title="Theirs")
    found = answer(made.member)
    assert titles(found) == ["Mine"]
    assert [place["name"] for place in found["places"]] == [made.asset.name]
    own = event_members.listing(elsewhere.organization, MembersQuery(), NOW)
    assert [row["title"] for row in own["events"]] == ["Theirs"]
    # Asked for by its id, or through its asset, another organization's event is none.
    assert answer(made.member, event=theirs.pk)["events"] == []
    assert answer(made.member, asset=elsewhere.asset.pk)["events"] == []
    assert [row["id"] for row in answer(made.member, event=mine.pk)["events"]] == [
        mine.pk
    ]


# --- What a member reads ------------------------------------------------------------


def test_an_event_comes_with_what_members_may_see_and_nothing_else(
    made: made_up.MadeUp,
) -> None:
    make(
        made,
        one_off(SATURDAY, 16, 22),
        title="Summer party",
        description="Bring something to share",
        category="volunteer_work",
    )
    response = read(made.member)
    found = json.loads(response.content)
    row = found["events"][0]
    assert set(row) == {
        "id",
        "category",
        "title",
        "description",
        "asset",
        "contact",
        "repeat",
        "start",
        "end",
        "days",
    }
    assert row["category"] == "volunteer_work"
    assert (row["title"], row["description"]) == (
        "Summer party",
        "Bring something to share",
    )
    assert row["asset"] == {"id": made.asset.pk, "name": made.asset.name}
    assert (row["start"], row["end"]) == (
        f"{SATURDAY.isoformat()}T16:00",
        f"{SATURDAY.isoformat()}T22:00",
    )
    assert (row["repeat"], row["days"]) == (None, [])
    assert row["contact"] == {"name": "Alex Rivers", "email": made.member.email}


def test_nothing_an_administrator_alone_sees_reaches_a_member(
    made: made_up.MadeUp,
) -> None:
    closed = AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SUNDAY,
        last_date=SUNDAY,
    )
    assert closed.pk
    make(made, weekend(end=SATURDAY_AFTER), title="Open house", contact_id=pat(made).pk)
    make(made, one_off(MONDAY, 16, 22), title="Summer party")
    text = read(made.member).content.decode()
    # Who made it, the contact's id, the choices of what to show, the skipped day
    # and why, and the maker's own details.
    for hidden in (
        made.administrator.full_name,
        made.administrator.email,
        made.administrator.phone,
        "made_by",
        "show_email",
        "show_phone",
        "skipped",
        "reason",
        "closed",
        "member",
        "created",
        "told",
        "removed",
    ):
        assert hidden not in text, hidden
    assert SUNDAY.isoformat() not in text


@pytest.mark.parametrize(
    ("show_email", "show_phone", "shown"),
    [
        (True, False, {"name": "Pat Person", "email": "pat@harbourview.example"}),
        (False, True, {"name": "Pat Person", "phone": "+4791100001"}),
        (
            True,
            True,
            {
                "name": "Pat Person",
                "email": "pat@harbourview.example",
                "phone": "+4791100001",
            },
        ),
        (False, False, {"name": "Pat Person"}),
    ],
)
def test_the_name_always_and_the_email_and_phone_only_when_ticked(
    made: made_up.MadeUp, show_email: bool, show_phone: bool, shown: dict[str, str]
) -> None:
    make(
        made,
        one_off(SATURDAY, 16, 22),
        contact_id=pat(made).pk,
        show_email=show_email,
        show_phone=show_phone,
    )
    text = read(made.member).content.decode()
    assert json.loads(text)["events"][0]["contact"] == shown
    for value in ("pat@harbourview.example", "+4791100001"):
        assert (value in text) == (value in shown.values())


def test_the_contact_is_read_from_the_member_now(made: made_up.MadeUp) -> None:
    make(made, one_off(SATURDAY, 16, 22), show_email=True)
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        email="new@harbourview.example", full_name="Alex Newname"
    )
    assert answer(made.member)["events"][0]["contact"] == {
        "name": "Alex Newname",
        "email": "new@harbourview.example",
    }


@pytest.mark.parametrize("status", ["blocked", "disabled"])
def test_a_contact_no_longer_active_reads_as_none(
    made: made_up.MadeUp, status: str
) -> None:
    gone = pat(made)
    make(made, one_off(SATURDAY, 16, 22), contact_id=gone.pk, show_phone=True)
    assert answer(made.member)["events"][0]["contact"]["name"] == "Pat Person"
    Member.objects.of(made.organization).filter(pk=gone.pk).update(status=status)
    text = read(made.member).content.decode()
    assert json.loads(text)["events"][0]["contact"] is None
    assert "Pat" not in text and "+4791100001" not in text


def test_a_contact_erased_from_the_event_reads_as_none(made: made_up.MadeUp) -> None:
    make(made, one_off(SATURDAY, 16, 22))
    Event.objects.of(made.organization).update(contact=None)
    assert answer(made.member)["events"][0]["contact"] is None


def test_an_event_without_an_asset_is_in_the_list_with_no_asset(
    made: made_up.MadeUp,
) -> None:
    make(made, one_off(SATURDAY, 16, 22), title="Courtyard", asset_id=None)
    row = answer(made.member)["events"][0]
    assert row["asset"] is None
    assert answer(made.member, asset="none")["events"] == [row]
    assert answer(made.member, asset=made.asset.pk)["events"] == []
    assert answer(made.member)["places"] == []


# --- A repeat: its rule and its days ------------------------------------------------


def test_a_repeat_comes_once_with_its_rule_and_each_day_as_it_is(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=date(2027, 3, 21)), title="Open house")
    first, second, third, *_ = live(event)
    with application_login():
        event_admin.remove_day(made.administrator, second.pk, now=NOW)
        event_admin.give_day_other_hours(
            made.administrator, third.pk, 11 * 60, 13 * 60, now=NOW
        )
    (row,) = answer(made.member)["events"]
    assert row["repeat"] == {
        "weekdays": [5, 6],
        "from": "10:00",
        "to": "14:00",
        "first": SATURDAY.isoformat(),
        "end": "2027-03-21",
    }
    assert (row["start"], row["end"]) == (None, None)
    assert row["days"][:3] == [
        {"date": first.day.isoformat(), "from": "10:00", "to": "14:00"},
        {"date": second.day.isoformat(), "cancelled": True},
        {"date": third.day.isoformat(), "from": "11:00", "to": "13:00"},
    ]
    assert len(row["days"]) == 6


def test_a_skipped_day_is_not_sent_at_all(made: made_up.MadeUp) -> None:
    AvailabilityClosure.objects.create(
        organization=made.organization,
        asset=made.asset,
        first_date=SUNDAY,
        last_date=SUNDAY,
    )
    make(made, weekend(end=SATURDAY_AFTER), title="Open house")
    (row,) = answer(made.member)["events"]
    assert [day["date"] for day in row["days"]] == [
        SATURDAY.isoformat(),
        SATURDAY_AFTER.isoformat(),
    ]


def test_a_removed_event_is_gone_and_a_removed_one_off_day_is_not_listed(
    made: made_up.MadeUp,
) -> None:
    gone = make(made, one_off(SATURDAY, 16, 22), title="Gone")
    make(made, one_off(MONDAY, 16, 22), title="Stays")
    with application_login():
        event_admin.remove_event(made.administrator, gone.pk)
    assert titles(answer(made.member)) == ["Stays"]


def test_a_repeat_with_every_day_removed_or_none_made_is_still_one_event(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SATURDAY_AFTER), title="Open house")
    with application_login():
        for day in live(event):
            event_admin.remove_day(made.administrator, day.pk, now=NOW)
    (row,) = answer(made.member)["events"]
    assert all(day == {"date": day["date"], "cancelled": True} for day in row["days"])


# --- The filters ----------------------------------------------------------------------


def test_coming_and_past_and_a_period(made: made_up.MadeUp) -> None:
    make(made, one_off(SATURDAY, 16, 22), title="Soon")
    make(made, one_off(date(2027, 4, 10), 10, 12), title="Later")
    ended = make(made, weekend(end=SATURDAY_AFTER), title="Ended")
    assert ended.pk
    # The made-up clock: before every day, so nothing is past.
    assert titles(answer(made.member)) == ["Ended", "Soon", "Later"]
    assert answer(made.member, when="past")["events"] == []
    after = event_members.listing(
        made.organization,
        MembersQuery(when="past"),
        NOW + timedelta(days=60),
    )
    assert [row["title"] for row in after["events"]] == ["Later", "Soon", "Ended"]
    inside = answer(made.member, since=SATURDAY.isoformat(), until=SUNDAY.isoformat())
    assert titles(inside) == ["Ended", "Soon"]
    ended_days = [day["date"] for day in inside["events"][0]["days"]]
    assert ended_days == [SATURDAY.isoformat(), SUNDAY.isoformat()]


def test_the_past_is_what_has_ended_newest_first(made: made_up.MadeUp) -> None:
    make(made, one_off(SATURDAY, 16, 22), title="First")
    make(made, one_off(MONDAY, 16, 22), title="Second")
    make(made, one_off(date(2027, 5, 1), 16, 22), title="Far")
    late = NOW + timedelta(days=30)
    past = event_members.listing(made.organization, MembersQuery(when="past"), late)
    assert [row["title"] for row in past["events"]] == ["Second", "First"]
    coming = event_members.listing(made.organization, MembersQuery(), late)
    assert [row["title"] for row in coming["events"]] == ["Far"]


def test_category_and_asset_narrow_the_list(made: made_up.MadeUp) -> None:
    make(made, one_off(SATURDAY, 16, 22), title="Social", category="social")
    make(made, one_off(MONDAY, 16, 22), title="Dugnad", category="volunteer_work")
    make(
        made,
        one_off(SATURDAY, 10, 12),
        title="Out",
        category="organization",
        asset_id=None,
    )
    assert titles(answer(made.member, category="volunteer_work")) == ["Dugnad"]
    assert titles(answer(made.member, category="social,organization")) == [
        "Out",
        "Social",
    ]
    assert titles(answer(made.member, asset="none")) == ["Out"]
    assert titles(answer(made.member, asset=made.asset.pk)) == ["Social", "Dugnad"]
    assert titles(answer(made.member, category="open_house")) == []


@pytest.mark.parametrize(
    "query",
    [
        {"when": "week"},
        {"since": "2027-03-01"},
        {"until": "2027-03-01"},
        {"since": "2027-3-1", "until": "2027-03-02"},
        {"since": "2027-03-05", "until": "2027-03-01"},
        {"since": "2020-01-01", "until": "2027-03-01"},
        {"category": "other"},
        {"category": "social,social,social,social,social"},
        {"asset": "x"},
        {"asset": "0"},
        {"event": "0"},
        {"event": "x"},
    ],
)
def test_a_malformed_question_is_a_400(
    made: made_up.MadeUp, query: dict[str, str]
) -> None:
    assert read(made.member, **query).status_code == 400


def test_one_event_is_asked_for_by_its_id_whenever_it_is(made: made_up.MadeUp) -> None:
    old = make(made, one_off(SATURDAY, 16, 22), title="Old")
    request_late = event_members.listing(
        made.organization,
        MembersQuery(only=old.pk),
        NOW + timedelta(days=300),
    )
    assert [row["title"] for row in request_late["events"]] == ["Old"]


def test_the_words_of_a_category_are_not_sent(made: made_up.MadeUp) -> None:
    made.organization.labels = {"event.category.volunteer_work": "Dugnad"}
    made.organization.save(update_fields=["labels"])
    make(made, one_off(SATURDAY, 16, 22), category="volunteer_work")
    text = read(made.member).content.decode()
    assert "volunteer_work" in text and "Dugnad" not in text


# --- The title on the calendar (S9) ---------------------------------------------------


def calendar_of(made: made_up.MadeUp, day: date) -> member_calendar.MemberCalendar:
    made.configuration.maximum_length = 4 * 60
    made.configuration.save()
    with application_login():
        return member_calendar.member_calendar(
            member=made.member,
            asset_id=made.asset.pk,
            booking_category_id=made.private.pk,
            length=60,
            start_date=day,
            end_date=day + timedelta(days=1),
            now=NOW - timedelta(days=30),
        )


def taken_spans(made: made_up.MadeUp, day: date) -> list[Any]:
    found = calendar_of(made, day)
    return [
        span
        for container in found.containers
        for period in container.periods
        for span in period.taken
    ]


def test_the_calendar_names_an_event_by_its_title_and_id_only(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(MONDAY, 13, 15), title="Board meeting")
    (span,) = taken_spans(made, MONDAY)
    assert (span.state, span.first, span.last) == ("event", 3, 4)
    assert span.event == member_calendar.EventRef(event.pk, "Board meeting")
    assert span.mine is False


def test_two_events_one_after_the_other_are_two_spans(made: made_up.MadeUp) -> None:
    one = make(made, one_off(MONDAY, 13, 15), title="Board meeting")
    two = make(made, one_off(MONDAY, 15, 16), title="Open questions")
    spans = taken_spans(made, MONDAY)
    assert [(span.state, span.event) for span in spans] == [
        ("event", member_calendar.EventRef(one.pk, "Board meeting")),
        ("event", member_calendar.EventRef(two.pk, "Open questions")),
    ]


def test_a_removed_day_and_a_removed_event_are_not_on_the_calendar(
    made: made_up.MadeUp,
) -> None:
    mondays = Repeat(
        frozenset({0}), MONDAY, MONDAY + timedelta(days=7), 13 * 60, 15 * 60
    )
    saturday = make(made, mondays, title="Open house")
    first, second, *_ = live(saturday)
    assert [
        span.event.title for span in taken_spans(made, first.day) if span.event
    ] == ["Open house"]
    with application_login():
        event_admin.remove_day(made.administrator, first.pk, now=NOW)
    assert taken_spans(made, first.day) == []
    assert [
        span.event.title for span in taken_spans(made, second.day) if span.event
    ] == ["Open house"]
    other = make(made, one_off(MONDAY, 13, 15), title="Board meeting")
    with application_login():
        event_admin.remove_event(made.administrator, other.pk)
    assert taken_spans(made, MONDAY) == []


def test_an_event_of_another_organization_is_not_on_this_calendar(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    open_all_week(elsewhere)
    make(elsewhere, one_off(MONDAY, 13, 15), title="Theirs")
    assert taken_spans(made, MONDAY) == []


def test_the_calendar_answer_carries_the_title_of_an_event_and_of_nothing_else(
    made: made_up.MadeUp,
) -> None:
    event = make(
        made,
        one_off(MONDAY, 13, 15),
        title="Board meeting",
        description="Secret agenda",
        contact_id=pat(made).pk,
    )
    found = calendar_of(made, MONDAY)
    text = json.dumps(
        booking_views._calendar(found, ZoneInfo(made.organization.time_zone))
    )
    spans = [
        span
        for container in json.loads(text)["containers"]
        for period in container["periods"]
        for span in period["taken"]
    ]
    assert spans
    event_spans = [span for span in spans if span["state"] == "event"]
    assert event_spans[0]["event"] == {"id": event.pk, "title": "Board meeting"}
    assert all("event" not in span for span in spans if span["state"] != "event")
    for hidden in ("Secret agenda", "Pat Person", "pat@harbourview.example"):
        assert hidden not in text
