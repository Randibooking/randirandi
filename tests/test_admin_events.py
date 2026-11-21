# SPDX-License-Identifier: AGPL-3.0-only
"""Admin's event pages on the server :
the gate, CSRF and origin, the malformed requests, the list with each filter,
one event, the preview, make, change and remove, a day removed or given other
hours, the people a contact can be, the event days in a closure's way and the
closure that removes them in the same save, the words of the categories, and
the audit log that shows it all."""

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import Client

from randirandi.core import (
    admin_event_views,
    audit_log,
    event_admin,
    event_pages,
)
from randirandi.core import availability_admin as closures
from randirandi.core.asset_admin import LayoutRefusedError
from randirandi.core.models import (
    Asset,
    AuditEvent,
    AvailabilityClosure,
    Event,
    EventDay,
    EventNotice,
    EventSkippedDay,
    Member,
    TakenTime,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import answer, request_to
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_events import (
    FRIDAY,
    MONDAY,
    NOW,
    SATURDAY,
    SATURDAY_AFTER,
    SUNDAY,
    audit_actions,
    claims,
    live,
    make,
    one_off,
    open_all_week,
    weekend,
)
from tests.test_login_sessions import HOST, get, page_settings  # noqa: F401
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def made() -> Iterator[made_up.MadeUp]:
    found = made_up.everything()
    open_all_week(found)
    yield found


@pytest.fixture(autouse=True)
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_event_views, "_now", lambda: NOW)


READS = {"list", "contacts", "in-the-way", "data"}
DAY_CALLS = {"day-remove", "day-hours"}


def call(
    made: made_up.MadeUp,
    which: str,
    data: Any = None,
    *,
    who: Member | None = None,
    confirmed: bool = True,
    event: int = 0,
    day: int = 0,
) -> HttpResponse:
    """An events view called directly, by default as a confirmed administrator."""
    paths = {
        "list": "/admin/events/list/",
        "contacts": "/admin/events/contacts/",
        "in-the-way": "/admin/events/in-the-way/",
        "preview": "/admin/events/preview/",
        "make": "/admin/events/make/",
        "data": f"/admin/events/{event}/data/",
        "change": f"/admin/events/{event}/change/",
        "remove": f"/admin/events/{event}/remove/",
        "day-remove": f"/admin/events/days/{day}/remove/",
        "day-hours": f"/admin/events/days/{day}/hours/",
    }
    views: dict[str, Callable[..., HttpResponse]] = {
        "list": admin_event_views.events_list,
        "contacts": admin_event_views.contacts,
        "in-the-way": admin_event_views.in_the_way,
        "preview": admin_event_views.preview,
        "make": admin_event_views.make,
        "data": admin_event_views.event_data,
        "change": admin_event_views.change,
        "remove": admin_event_views.remove,
        "day-remove": admin_event_views.remove_day,
        "day-hours": admin_event_views.day_hours,
    }
    request = request_to(
        who or made.administrator,
        "get" if which in READS else "post",
        paths[which],
        data,
        confirmed=confirmed,
    )
    with application_login():
        if which in DAY_CALLS:
            return views[which](request, day)
        if which in {"data", "change", "remove"}:
            return views[which](request, event)
        return views[which](request)


def person(made: made_up.MadeUp, name: str = "Pat") -> Member:
    """An active member with a name, an email and a phone of their own."""
    number = Member.objects.of(made.organization).count()
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name=name,
        first_name=name,
        last_name="Person",
        email=f"{name.lower()}{number}@harbourview.example",
        phone=f"+47911{number:05d}",
        status="active",
        origin="administrator",
    )


def at(day: date, hour: int, minute: int = 0) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}"


def once(day: date, first: int, last: int) -> dict[str, Any]:
    return {"repeat": False, "start": at(day, first), "end": at(day, last)}


def weekly(
    weekdays: Sequence[int | str],
    first: date,
    until: date | None,
    start: str = "10:00",
    end: str = "14:00",
) -> dict[str, Any]:
    return {
        "repeat": True,
        "weekdays": weekdays,
        "first": first.isoformat(),
        "until": until.isoformat() if until else None,
        "from": start,
        "to": end,
    }


def form(made: made_up.MadeUp, when: dict[str, Any] | None = None, **more: Any) -> Any:
    return {
        "category": "social",
        "title": "Summer party",
        "description": "Bring something to share",
        "asset": made.asset.pk,
        "contact": made.member.pk,
        "show_email": True,
        "show_phone": False,
        "when": when or once(SATURDAY, 16, 22),
        **more,
    }


def read(
    made: made_up.MadeUp,
    which: str,
    data: Any = None,
    *,
    event: int = 0,
    day: int = 0,
) -> Any:
    return answer(call(made, which, data, event=event, day=day))


def refusal(response: HttpResponse, code: str, status: int = 409) -> dict[str, Any]:
    found = answer(response, status)
    assert found["error"] == code
    return found


# --- The gate, CSRF and origin ------------------------------------------------------


@pytest.mark.parametrize(
    "which",
    [
        "list",
        "contacts",
        "in-the-way",
        "preview",
        "make",
        "data",
        "change",
        "remove",
        "day-remove",
        "day-hours",
    ],
)
def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp, which: str
) -> None:
    event = make(made, weekend(end=SATURDAY_AFTER))
    (first, *_) = live(event)
    elsewhere = made_up.everything("elsewhere")
    data = (
        {
            "asset": made.asset.pk,
            "first": SATURDAY.isoformat(),
            "last": SATURDAY.isoformat(),
        }
        if which == "in-the-way"
        else {"from": "10:00", "to": "12:00"}
        if which == "day-hours"
        else form(made)
    )
    ids = {"event": event.pk, "day": first.pk}
    before = (
        Event.objects.of(made.organization).count(),
        EventDay.objects.of(made.organization).filter(removed_at__isnull=True).count(),
        audit_actions(made),
    )
    for who, confirmed in (
        (made.administrator, False),
        (made.member, True),
        (elsewhere.administrator, True),
    ):
        with pytest.raises(PermissionDenied):
            call(made, which, data, who=who, confirmed=confirmed, **ids)
    assert before == (
        Event.objects.of(made.organization).count(),
        EventDay.objects.of(made.organization).filter(removed_at__isnull=True).count(),
        audit_actions(made),
    )


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, method in (
            ("/admin/events/list/", "post"),
            ("/admin/events/contacts/", "post"),
            ("/admin/events/in-the-way/", "post"),
            ("/admin/events/preview/", "get"),
            ("/admin/events/make/", "get"),
            (f"/admin/events/{event.pk}/data/", "post"),
            (f"/admin/events/{event.pk}/change/", "get"),
            (f"/admin/events/{event.pk}/remove/", "get"),
            ("/admin/events/days/1/remove/", "get"),
            ("/admin/events/days/1/hours/", "get"),
        ):
            response = getattr(client, method)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
    for path in ("/admin/events/list/", "/admin/events/make/"):
        response = Client().post(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")
    assert Event.objects.of(made.organization).filter(pk=event.pk).exists()


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    body = json.dumps(form(made, once(SATURDAY, 16, 22)))
    with application_login():
        sign_in_administrator(client, made.administrator)
        token = str(config(get(client, "/admin/events/").content.decode())["csrf"])

        def send(path: str, headers: dict[str, Any], data: str = body) -> HttpResponse:
            response = client.post(
                path,
                data,
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        right = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://" + HOST}
        wrong = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://elsewhere.example"}
        make_path = "/admin/events/make/"
        assert send(make_path, {"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        assert send(make_path, wrong).status_code == 403
        assert not Event.objects.of(made.organization).exists()
        # The browser's clock is not the test's: a day far ahead is saved at any time.
        far = json.dumps(form(made, once(date(2099, 3, 6), 16, 22)))
        found = answer(send(make_path, right, far))
        pk = found["made"]
        remove = f"/admin/events/{pk}/remove/"
        assert send(remove, {"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        assert send(remove, wrong).status_code == 403
        assert Event.objects.of(made.organization).filter(pk=pk).exists()
        assert answer(send(remove, right, "{}")) == {"removed": pk}
    assert not Event.objects.of(made.organization).exists()


def test_the_events_place_and_an_events_address_are_the_admins_pages(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        listed = get(client, "/admin/events/")
        assert listed.status_code == 200
        ui = config(listed.content.decode())
        assert ui["place"] == "events" and ui["event"] is None
        assert 'href="/admin/events/"' in listed.content.decode()
        opened = get(client, f"/admin/events/{event.pk}/")
        assert opened.status_code == 200
        ui = config(opened.content.decode())
        assert ui["place"] == "events" and ui["event"] == event.pk
        assert get(client, "/admin/events/x/").status_code == 404
    elsewhere = Client()
    with application_login():
        assert get(elsewhere, f"/admin/events/{event.pk}/").status_code == 302


# --- Malformed requests -------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        {"when": "week"},
        {"when": "day"},
        {"when": "period", "from": "2027-03-01"},
        {"when": "day", "from": "2027-3-1"},
        {"category": "other"},
        {"asset": "x"},
        {"asset": "0"},
        {"order": "up"},
        {"order": "newest"},
        {"page": "0"},
        {"page": "-1"},
        {"page": "x"},
        {"q": "x" * 101},
    ],
)
def test_a_malformed_list_query_is_a_400(
    made: made_up.MadeUp, query: dict[str, str]
) -> None:
    assert refusal(call(made, "list", query), "bad-request", 400)


@pytest.mark.parametrize(
    "change",
    [
        {"when": {"repeat": False, "start": "2027-03-06T16:00"}},
        {"when": {"repeat": False, "start": "2027-03-06", "end": "2027-03-06T18:00"}},
        {"when": once(SATURDAY, 16, 22) | {"extra": 1}},
        {"when": weekly([5], SATURDAY, None) | {"from": "10:61"}},
        {"when": weekly([5], SATURDAY, None) | {"to": "25:00"}},
        {"when": weekly(["Sat"], SATURDAY, None)},
        {"when": weekly([5] * 8, SATURDAY, None)},
        {"when": "now"},
        {"asset": "1"},
        {"asset": True},
        {"contact": "1"},
        {"title": 7},
        {"show_email": "yes"},
        {"extra": 1},
        {"settle": {"x": "remove"}},
        {"settle": {"1": "gone"}},
        {"settle": {"1": {"from": "10:00"}}},
        {"settle": []},
    ],
)
def test_a_malformed_body_is_a_400_and_writes_nothing(
    made: made_up.MadeUp, change: dict[str, Any]
) -> None:
    body = {**form(made), **change}
    kept = audit_actions(made)
    for which in ("make", "preview"):
        assert refusal(call(made, which, body), "bad-request", 400)
    assert not Event.objects.of(made.organization).exists()
    assert audit_actions(made) == kept


def test_a_body_that_is_not_json_or_not_an_object_is_a_400(
    made: made_up.MadeUp,
) -> None:
    for body in (b"{", b"[]", b"7", b"\xff"):
        assert refusal(call(made, "make", body), "bad-request", 400)
    assert refusal(call(made, "day-hours", {"from": "10:00"}, day=1), "not-found", 404)


# --- The list -----------------------------------------------------------------------


def titles(found: dict[str, Any]) -> list[str]:
    return [row["title"] for row in found["events"]]


def test_the_list_filters_by_when_category_asset_and_title(
    made: made_up.MadeUp,
) -> None:
    benches = made.shared("Benches", 2)
    open_all_week(made, benches.asset)
    make(made, one_off(SATURDAY, 16, 22), title="Summer party")
    make(
        made,
        one_off(date(2027, 3, 20), 10, 12),
        title="Dugnad",
        category="volunteer_work",
        asset_id=benches.asset_id,
    )
    make(made, one_off(date(2027, 3, 27), 10, 12), title="Board meeting", asset_id=None)
    make(made, weekend(end=SATURDAY_AFTER), title="Open house", category="open_house")
    old = make(made, one_off(date(2027, 3, 8), 10, 12), title="Early")
    # It is over by the time the list is read.
    EventDay.objects.of(made.organization).filter(event=old).update(
        starts_at=NOW - timedelta(days=3), ends_at=NOW - timedelta(days=3, hours=-2)
    )
    TakenTime.objects.of(made.organization).filter(event_day__event=old).delete()
    coming = read(made, "list")
    assert titles(coming) == [
        "Open house",
        "Summer party",
        "Dugnad",
        "Board meeting",
    ]
    assert coming["count"] == 4 and coming["per_page"] == 25
    assert titles(read(made, "list", {"order": "latest"})) == [
        "Board meeting",
        "Dugnad",
        "Summer party",
        "Open house",
    ]
    past = titles(read(made, "list", {"when": "past"}))
    assert "Early" in past and "Summer party" not in past
    assert titles(read(made, "list", {"category": "volunteer_work"})) == ["Dugnad"]
    assert titles(read(made, "list", {"asset": "none"})) == ["Board meeting"]
    assert titles(read(made, "list", {"asset": benches.asset_id})) == ["Dugnad"]
    assert titles(read(made, "list", {"q": " PARTY "})) == ["Summer party"]
    assert titles(read(made, "list", {"q": "nothing like it"})) == []
    day = read(made, "list", {"when": "day", "from": SATURDAY.isoformat()})
    assert titles(day) == ["Open house", "Summer party"]
    period = read(
        made,
        "list",
        {"when": "period", "from": "2027-03-14", "to": "2027-03-21"},
    )
    assert titles(period) == ["Dugnad"]
    # A repeat comes once, by its next day.
    row = next(r for r in coming["events"] if r["title"] == "Open house")
    assert row["weekdays"] == [5, 6] and row["start"] == at(SATURDAY, 10)
    assert row["end"] == at(SATURDAY, 14)
    one = next(r for r in coming["events"] if r["title"] == "Summer party")
    assert one["weekdays"] is None and one["asset"] == {
        "id": made.asset.pk,
        "name": made.asset.name,
    }


def test_the_list_is_25_to_a_page_and_never_another_organizations(
    made: made_up.MadeUp,
) -> None:
    for number in range(27):
        make(
            made,
            one_off(date(2027, 4, 1) + timedelta(days=number), 10, 11),
            title=f"Event {number:02d}",
            asset_id=None,
        )
    elsewhere = made_up.everything("elsewhere")
    foreign = Event.objects.create(
        organization=elsewhere.organization,
        category="social",
        title="Not yours",
        made_by=elsewhere.administrator,
        contact=elsewhere.member,
    )
    assert foreign.pk
    first = read(made, "list")
    second = read(made, "list", {"page": 2})
    assert (first["count"], len(first["events"]), len(second["events"])) == (27, 25, 2)
    assert titles(second) == ["Event 25", "Event 26"]
    assert "Not yours" not in titles(first) + titles(second)
    assert read(made, "list", {"page": 3})["events"] == []


def test_an_event_without_an_active_contact_is_marked_in_the_list(
    made: made_up.MadeUp,
) -> None:
    pat = person(made)
    make(made, one_off(SATURDAY, 16, 22), title="Tidy", contact_id=pat.pk)
    make(made, one_off(date(2027, 3, 20), 10, 12), title="Fine")
    rows = {row["title"]: row for row in read(made, "list")["events"]}
    assert rows["Tidy"]["contact"] == "Pat"
    for state in ("blocked", "disabled"):
        Member.objects.of(made.organization).filter(pk=pat.pk).update(status=state)
        assert {r["title"]: r["contact"] for r in read(made, "list")["events"]}[
            "Tidy"
        ] is None
    Event.objects.of(made.organization).filter(title="Fine").update(contact=None)
    assert {r["title"]: r["contact"] for r in read(made, "list")["events"]}[
        "Fine"
    ] is None


def test_the_categories_are_keys_with_the_organizations_words(
    made: made_up.MadeUp,
) -> None:
    make(made, one_off(SATURDAY, 16, 22), category="volunteer_work")
    found = read(made, "list")
    assert found["events"][0]["category"] == "volunteer_work"
    assert [row["key"] for row in found["categories"]] == [
        "social",
        "volunteer_work",
        "organization",
        "open_house",
    ]
    assert found["categories"][1]["label"] == "Volunteer work"
    made.organization.labels = {"event.category.volunteer_work": "Dugnad"}
    made.organization.save()
    found = read(made, "list")
    assert found["categories"][1] == {"key": "volunteer_work", "label": "Dugnad"}
    assert found["categories"][0]["label"] == "Social event"
    assert {row["name"] for row in found["assets"]} >= {made.asset.name}


# --- One event ----------------------------------------------------------------------


def test_one_event_is_read_with_its_contact_maker_and_time(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22), show_phone=True)
    found = read(made, "data", event=event.pk)
    assert found["title"] == "Summer party" and found["description"]
    assert found["category"] == "social"
    assert found["asset"] == {"id": made.asset.pk, "name": made.asset.name}
    assert found["contact"] == {"id": made.member.pk, "name": made.member.full_name}
    assert (found["show_email"], found["show_phone"]) == (True, True)
    assert found["made_by"] == made.administrator.full_name
    assert found["created"] == "2027-03-01"
    assert (found["start"], found["end"]) == (at(SATURDAY, 16), at(SATURDAY, 22))
    assert found["repeat"] is None and found["days"] == [] and found["skipped"] == []
    assert found["categories"] and found["assets"]


def test_a_repeat_is_read_with_every_day_removed_or_changed_and_what_it_skipped(
    made: made_up.MadeUp,
) -> None:
    closed = AvailabilityClosureFor(made, SUNDAY)
    assert closed.pk
    event = make(made, weekend(end=date(2027, 3, 21)), title="Open house")
    first, second, third, *_ = live(event)
    with application_login():
        event_admin.remove_day(made.administrator, second.pk, now=NOW)
        event_admin.give_day_other_hours(
            made.administrator, third.pk, 11 * 60, 13 * 60, now=NOW
        )
    found = read(made, "data", event=event.pk)
    assert found["repeat"] == {
        "weekdays": [5, 6],
        "from": "10:00",
        "to": "14:00",
        "first": SATURDAY.isoformat(),
        "end": "2027-03-21",
    }
    days = {row["date"]: row for row in found["days"]}
    assert days[first.day.isoformat()] == {
        "id": first.pk,
        "date": first.day.isoformat(),
        "from": "10:00",
        "to": "14:00",
        "other": False,
        "removed": False,
        "over": False,
    }
    assert days[second.day.isoformat()]["removed"] is True
    assert days[third.day.isoformat()]["other"] is True
    assert (days[third.day.isoformat()]["from"], days[third.day.isoformat()]["to"]) == (
        "11:00",
        "13:00",
    )
    assert found["skipped"] == [
        {"date": SUNDAY.isoformat(), "reason": "closed", "name": None, "told": None}
    ]


def AvailabilityClosureFor(made: made_up.MadeUp, day: date) -> AvailabilityClosure:  # noqa: N802
    return AvailabilityClosure.objects.create(
        organization=made.organization, asset=made.asset, first_date=day, last_date=day
    )


def test_a_skipped_day_the_top_up_told_names_the_event_that_held_it_and_whom(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(), title="Open house")
    with application_login():
        # A one-off over the day the repeat reaches a year on.
        wedding = event_admin.make_event(
            made.administrator,
            event_admin.EventSpec(
                category="social",
                title="Wedding",
                description="",
                contact_id=made.member.pk,
                when=event_admin.OneOff(
                    datetime(2028, 3, 4, 10, 0),  # noqa: DTZ001
                    datetime(2028, 3, 4, 14, 0),  # noqa: DTZ001
                ),
                asset_id=made.asset.pk,
            ),
            now=NOW,
        )
        event_admin.top_up(NOW + timedelta(days=4))
    found = read(made, "data", event=event.pk)
    (skip,) = found["skipped"]
    assert skip["reason"] == "event" and skip["name"] == "Wedding"
    assert skip["told"] == {
        "date": skip_created(event),
        "to": [made.administrator.full_name],
    }
    assert EventNotice.objects.of(made.organization).count() == 1
    assert wedding.pk


def skip_created(event: Event) -> str:
    row = EventSkippedDay.objects.of(event.organization_id).get(event=event)
    return row.created_at.date().isoformat()


def test_an_event_of_another_organization_is_not_found(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    foreign = Event.objects.create(
        organization=elsewhere.organization,
        category="social",
        title="Not yours",
        made_by=elsewhere.administrator,
        contact=elsewhere.member,
    )
    event = make(made, one_off(SATURDAY, 16, 22))
    for which in ("data", "change", "remove"):
        refusal(call(made, which, form(made), event=foreign.pk), "not-found", 404)
    refusal(call(made, "data", event=2**62 + 1), "not-found", 404)
    # Its day, too.
    day = EventDay.objects.create(
        organization=elsewhere.organization,
        event=foreign,
        day=SATURDAY,
        starts_at=NOW + timedelta(days=5),
        ends_at=NOW + timedelta(days=5, hours=1),
    )
    refusal(call(made, "day-remove", {}, day=day.pk), "not-found", 404)
    refusal(
        call(made, "day-hours", {"from": "10:00", "to": "11:00"}, day=day.pk),
        "not-found",
        404,
    )
    assert Event.objects.of(elsewhere.organization).filter(pk=foreign.pk).exists()
    assert Event.objects.of(made.organization).filter(pk=event.pk).exists()


def test_the_contacts_are_active_members_found_by_name(made: made_up.MadeUp) -> None:
    pat = person(made, "Pat")
    person(made, "Pam")
    Member.objects.of(made.organization).filter(pk=pat.pk).update(status="blocked")
    elsewhere = made_up.everything("elsewhere")
    found = read(made, "contacts", {"q": "pa"})["people"]
    assert [row["name"] for row in found] == ["Pam"]
    everyone = {row["id"] for row in read(made, "contacts")["people"]}
    assert made.member.pk in everyone and pat.pk not in everyone
    assert elsewhere.member.pk not in everyone
    assert refusal(call(made, "contacts", {"q": "x" * 101}), "bad-request", 400)
    for number in range(25):
        person(made, f"Many{number:02d}")
    assert len(read(made, "contacts")["people"]) == 20


# --- The preview --------------------------------------------------------------------


def test_the_preview_of_a_one_off_says_what_it_takes_leaves_and_finds_in_the_way(
    made: made_up.MadeUp,
) -> None:
    before = (claims(made), audit_actions(made))
    found = read(made, "preview", form(made, once(MONDAY, 18, 19), title=""))
    assert found["repeat"] is False and found["refusal"] is None and not found["over"]
    assert (found["start"], found["end"]) == (at(MONDAY, 18), at(MONDAY, 19))
    assert found["way"] == [] and found["left"] == []
    assert (claims(made), audit_actions(made)) == before
    assert not Event.objects.of(made.organization).exists()
    # A booking: its number and reference, a way out of cancelling it.
    booked = read(made, "preview", form(made, once(date(2027, 3, 1), 12, 14)))
    assert booked["refusal"] == "booked"
    (way,) = booked["way"]
    assert way["reason"] == "booked" and way["number"] == made.order.number
    assert way["reference"] == made.order.reference
    assert way["ways"] == ["cancel_booking"]
    # It says when and who: reading who booked is one "listed bookings" event.
    assert way["member"] == made.member.full_name
    assert way["start"].startswith("2027-03-01T") and way["start"] < way["end"]
    assert audit_actions(made)[-1] == "listed bookings"
    assert audit_actions(made).count("listed bookings") == 1
    # No title, no category and no contact are needed to look.
    bare = form(made, once(MONDAY, 18, 19))
    for key in ("title", "category", "contact"):
        del bare[key]
    assert read(made, "preview", bare)["refusal"] is None
    # Without an asset nothing is taken or left.
    free = read(made, "preview", form(made, once(MONDAY, 18, 19), asset=None))
    assert free["left"] == [] and free["way"] == [] and free["asset"] is None


def test_the_preview_shows_the_leftover_part_of_a_cell(made: made_up.MadeUp) -> None:
    found = read(
        made,
        "preview",
        form(
            made,
            {"repeat": False, "start": at(MONDAY, 18, 30), "end": at(MONDAY, 19, 15)},
        ),
    )
    assert found["left"] == [
        [at(MONDAY, 18, 0), at(MONDAY, 18, 30)],
        [at(MONDAY, 19, 15), at(MONDAY, 20, 0)],
    ]


def test_the_preview_of_a_repeat_counts_its_skipped_days_by_reason_and_lists_them(
    made: made_up.MadeUp,
) -> None:
    AvailabilityClosureFor(made, SUNDAY)
    make(made, one_off(SATURDAY_AFTER, 12, 13), title="Fair")
    found = read(
        made,
        "preview",
        form(made, weekly([5, 6], SATURDAY, date(2027, 3, 14))),
    )
    assert found["repeat"] is True and found["refusal"] is None
    assert found["takes"] == ["2027-03-06", "2027-03-14"]
    assert [(row["date"], row["reason"]) for row in found["skipped"]] == [
        ("2027-03-07", "closed"),
        ("2027-03-13", "event"),
    ]
    assert found["skipped"][1]["way"]["title"] == "Fair"
    assert found["counts"] == {"closed": 1, "event": 1}
    assert found["left"] == []
    # The save agrees: the same days.
    saved = read(made, "make", form(made, weekly([5, 6], SATURDAY, date(2027, 3, 14))))
    assert {
        d.day.isoformat()
        for d in live(Event.objects.of(made.organization).get(pk=saved["made"]))
    } == set(found["takes"])


def test_the_preview_of_a_change_leaves_the_events_own_time_out_of_its_way(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    body = form(made, once(SATURDAY, 18, 23))
    assert read(made, "preview", body)["refusal"] == "event"
    assert read(made, "preview", body | {"event": event.pk})["refusal"] is None
    refusal(call(made, "preview", body | {"event": 10**9}), "not-found", 404)


def test_a_repeats_day_in_the_way_is_offered_removal_and_settled_in_the_preview(
    made: made_up.MadeUp,
) -> None:
    open_house = make(made, weekend(end=SATURDAY_AFTER), title="Open house")
    (day, *_) = live(open_house)
    body = form(made, once(SATURDAY, 12, 18))
    found = read(made, "preview", body)
    assert found["refusal"] == "event"
    (way,) = found["way"]
    assert way["reason"] == "event" and way["repeating"] is True
    assert way["title"] == "Open house" and way["day"] == day.pk
    assert (way["start"], way["end"]) == (at(SATURDAY, 10), at(SATURDAY, 14))
    assert way["ways"] == ["remove_day", "other_hours"] and way["settled"] is False
    settled = read(made, "preview", body | {"settle": {str(day.pk): "remove"}})
    assert settled["refusal"] is None and settled["way"][0]["settled"] is True
    hours = read(
        made,
        "preview",
        body | {"settle": {str(day.pk): {"from": "10:00", "to": "12:00"}}},
    )
    assert hours["refusal"] is None
    assert EventDay.objects.of(made.organization).get(pk=day.pk).removed_at is None


def test_a_block_a_closure_and_a_closed_day_show_in_the_way(
    made: made_up.MadeUp,
) -> None:
    from tests.test_admin_blocks import take

    block = take(made, date(2027, 3, 15), 11, 13, note="Floor sanding")
    found = read(made, "preview", form(made, once(date(2027, 3, 15), 10, 14)))
    (way,) = found["way"]
    assert found["refusal"] == "blocked" and way["reason"] == "blocked"
    assert way["note"] == "Floor sanding" and way["ways"] == ["remove_block"]
    assert block.pk
    AvailabilityClosureFor(made, date(2027, 3, 16))
    closed = read(made, "preview", form(made, once(date(2027, 3, 16), 10, 14)))
    assert closed["refusal"] == "closed" and closed["way"][0]["reason"] == "closed"
    # Over, in the past.
    over = read(made, "preview", form(made, once(date(2027, 2, 1), 10, 14)))
    assert over["over"] is True and over["refusal"] == "past"


# --- Making -------------------------------------------------------------------------


def test_make_saves_the_event_its_time_and_one_audit_event(
    made: made_up.MadeUp,
) -> None:
    found = read(made, "make", form(made, once(SATURDAY, 16, 22)))
    event = Event.objects.of(made.organization).get(pk=found["made"])
    assert (event.title, event.category, event.asset_id, event.contact_id) == (
        "Summer party",
        "social",
        made.asset.pk,
        made.member.pk,
    )
    assert event.made_by_id == made.administrator.pk
    assert (event.show_email, event.show_phone) == (True, False)
    assert claims(made) == 1
    assert audit_actions(made)[-1] == "made event"
    rows: Any = audit_log.answer(made.organization, audit_log.Query())["events"]
    assert rows[0]["what"]["target"] == {"kind": "event", "id": event.pk}


def test_make_without_an_asset_takes_no_time(made: made_up.MadeUp) -> None:
    found = read(made, "make", form(made, once(SATURDAY, 16, 22), asset=None))
    event = Event.objects.of(made.organization).get(pk=found["made"])
    assert event.asset_id is None and claims(made) == 0


def test_a_repeat_is_made_with_its_days_and_never_refused_for_what_is_in_the_way(
    made: made_up.MadeUp,
) -> None:
    AvailabilityClosureFor(made, SUNDAY)
    found = read(made, "make", form(made, weekly([5, 6], SATURDAY, date(2027, 3, 14))))
    event = Event.objects.of(made.organization).get(pk=found["made"])
    assert [d.day for d in live(event)] == [
        SATURDAY,
        date(2027, 3, 13),
        date(2027, 3, 14),
    ]
    skipped = read(made, "data", event=event.pk)["skipped"]
    assert [(s["date"], s["reason"]) for s in skipped] == [
        (SUNDAY.isoformat(), "closed")
    ]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"category": "other"}, "category"),
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"description": "x" * 2001}, "description"),
        ({"contact": None}, "contact"),
        ({"contact": 10**9}, "contact"),
        ({"asset": 10**9}, "asset"),
        ({"when": once(SATURDAY, 16, 16)}, "dates"),
        ({"when": weekly([], SATURDAY, None)}, "weekdays"),
        ({"when": weekly([7], SATURDAY, None)}, "weekdays"),
        ({"when": weekly([5], SATURDAY, None, "14:00", "10:00")}, "hours"),
        ({"when": once(date(2027, 2, 1), 10, 12)}, "past"),
    ],
)
def test_a_refused_make_is_a_409_with_its_code_and_saves_nothing(
    made: made_up.MadeUp, change: dict[str, Any], code: str
) -> None:
    kept = audit_actions(made)
    refusal(call(made, "make", form(made) | change), code)
    assert not Event.objects.of(made.organization).exists()
    assert claims(made) == 0 and audit_actions(made) == kept


def test_an_asset_and_a_contact_of_another_organization_are_refused(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    refusal(call(made, "make", form(made, asset=elsewhere.asset.pk)), "asset")
    refusal(call(made, "make", form(made, contact=elsewhere.member.pk)), "contact")
    refusal(call(made, "preview", form(made, asset=elsewhere.asset.pk)), "asset")
    assert not Event.objects.of(made.organization).exists()
    assert isinstance(elsewhere.asset, Asset)


def test_a_one_off_in_the_way_is_a_409_naming_what_holds_the_time(
    made: made_up.MadeUp,
) -> None:
    found = refusal(
        call(made, "make", form(made, once(date(2027, 3, 1), 12, 14))), "booked"
    )
    (way,) = found["way"]
    assert way["reason"] == "booked" and way["number"] == made.order.number
    assert way["member"] == made.member.full_name and way["start"] < way["end"]
    assert audit_actions(made)[-1] == "listed bookings"
    make(made, one_off(SATURDAY, 16, 22), title="Party")
    other = refusal(call(made, "make", form(made, once(SATURDAY, 18, 20))), "event")
    assert other["way"][0]["title"] == "Party"
    assert Event.objects.of(made.organization).count() == 1


def test_make_removes_a_repeats_day_in_the_same_save(made: made_up.MadeUp) -> None:
    open_house = make(made, weekend(end=SATURDAY_AFTER), title="Open house")
    (day, *_) = live(open_house)
    body = form(made, once(SATURDAY, 12, 18))
    refusal(call(made, "make", body), "event")
    refusal(call(made, "make", body | {"settle": {"999999": "remove"}}), "settle")
    found = read(made, "make", body | {"settle": {str(day.pk): "remove"}})
    assert Event.objects.of(made.organization).filter(pk=found["made"]).exists()
    assert EventDay.objects.of(made.organization).get(pk=day.pk).removed_at is not None
    assert audit_actions(made)[-2:] == ["removed event day", "made event"]


# --- Changing and removing ----------------------------------------------------------


def test_change_saves_every_field_and_answers_the_event(made: made_up.MadeUp) -> None:
    pat = person(made)
    event = make(made, one_off(SATURDAY, 16, 22))
    found = read(
        made,
        "change",
        form(
            made,
            once(SATURDAY, 17, 21),
            title="Spring party",
            category="open_house",
            contact=pat.pk,
            show_phone=True,
        ),
        event=event.pk,
    )
    assert found["title"] == "Spring party" and found["category"] == "open_house"
    assert found["contact"] == {"id": pat.pk, "name": "Pat"}
    assert (found["start"], found["end"]) == (at(SATURDAY, 17), at(SATURDAY, 21))
    assert found["show_phone"] is True
    assert audit_actions(made)[-1] == "changed event"
    refusal(
        call(
            made, "change", form(made, once(SATURDAY, 17, 21), title=""), event=event.pk
        ),
        "title",
    )
    assert Event.objects.of(made.organization).get(pk=event.pk).title == "Spring party"


def test_changing_a_one_off_to_a_repeat_and_its_asset_checks_again(
    made: made_up.MadeUp,
) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    make(made, one_off(date(2027, 3, 20), 10, 12), title="Other")
    found = read(
        made,
        "change",
        form(made, weekly([5], SATURDAY, date(2027, 3, 27), "09:00", "11:00")),
        event=event.pk,
    )
    assert found["repeat"]["weekdays"] == [5]
    assert [d["date"] for d in found["days"]] == [
        "2027-03-06",
        "2027-03-13",
        "2027-03-20",
        "2027-03-27",
    ][:2] + [d["date"] for d in found["days"]][2:]
    assert "2027-03-20" in {s["date"] for s in found["skipped"]}
    assert found["skipped"][0]["reason"] == "event"
    assert found["skipped"][0]["name"] == "Other"


def test_remove_frees_the_time_and_audits(made: made_up.MadeUp) -> None:
    event = make(made, one_off(SATURDAY, 16, 22))
    assert claims(made) == 1
    assert read(made, "remove", {}, event=event.pk) == {"removed": event.pk}
    assert claims(made) == 0 and not Event.objects.of(made.organization).exists()
    assert audit_actions(made)[-1] == "removed event"
    refusal(call(made, "remove", {}, event=event.pk), "not-found", 404)


def test_a_day_is_removed_and_stays_removed(made: made_up.MadeUp) -> None:
    event = make(made, weekend(end=SATURDAY_AFTER))
    first, second, *_ = live(event)
    found = read(made, "day-remove", {}, day=first.pk)
    assert {d["id"]: d["removed"] for d in found["days"]}[first.pk] is True
    assert audit_actions(made)[-1] == "removed event day"
    refusal(call(made, "day-remove", {}, day=first.pk), "removed")
    # Changing the repeat leaves it removed.
    changed = read(
        made,
        "change",
        form(made, weekly([5, 6], SATURDAY, SATURDAY_AFTER, "11:00", "13:00")),
        event=event.pk,
    )
    assert {d["id"]: d["removed"] for d in changed["days"]}[first.pk] is True
    assert second.pk in {d["id"] for d in changed["days"]}


def test_a_one_offs_day_and_a_day_over_cannot_be_removed(made: made_up.MadeUp) -> None:
    one = make(made, one_off(SATURDAY, 16, 22))
    (day,) = live(one)
    refusal(call(made, "day-remove", {}, day=day.pk), "one-off")
    refusal(
        call(made, "day-hours", {"from": "10:00", "to": "12:00"}, day=day.pk), "one-off"
    )
    repeating = make(made, weekend(end=SATURDAY_AFTER))
    (past, *_) = live(repeating)
    EventDay.objects.of(made.organization).filter(pk=past.pk).update(
        starts_at=NOW - timedelta(days=2), ends_at=NOW - timedelta(days=2, hours=-4)
    )
    refusal(call(made, "day-remove", {}, day=past.pk), "past")


def test_a_day_is_given_other_hours_that_are_checked(made: made_up.MadeUp) -> None:
    event = make(made, weekend(end=SATURDAY_AFTER))
    make(made, one_off(SATURDAY, 15, 18), title="Fair", asset_id=made.asset.pk)
    first = live(event)[0]
    found = read(made, "day-hours", {"from": "10:00", "to": "12:00"}, day=first.pk)
    changed = {d["id"]: d for d in found["days"]}[first.pk]
    assert (changed["from"], changed["to"], changed["other"]) == (
        "10:00",
        "12:00",
        True,
    )
    assert audit_actions(made)[-1] == "changed event day"
    in_way = refusal(
        call(made, "day-hours", {"from": "12:00", "to": "16:00"}, day=first.pk), "event"
    )
    assert in_way["way"][0]["title"] == "Fair"
    refusal(
        call(made, "day-hours", {"from": "12:00", "to": "11:00"}, day=first.pk), "hours"
    )
    refusal(
        call(made, "day-hours", {"from": "12:00", "to": "12:00", "x": 1}, day=first.pk),
        "bad-request",
        400,
    )


# --- In a closure's way -------------------------------------------------------------


def test_the_event_days_in_the_way_of_a_closure_say_which_can_be_removed(
    made: made_up.MadeUp,
) -> None:
    open_house = make(made, weekend(end=SATURDAY_AFTER), title="Open house")
    one = make(made, one_off(FRIDAY, 10, 12), title="Fair")
    found = read(
        made,
        "in-the-way",
        {
            "asset": made.asset.pk,
            "first": SATURDAY.isoformat(),
            "last": FRIDAY.isoformat(),
        },
    )["days"]
    assert [(d["title"], d["repeating"], d["date"]) for d in found] == [
        ("Open house", True, SATURDAY.isoformat()),
        ("Open house", True, SUNDAY.isoformat()),
        ("Fair", False, FRIDAY.isoformat()),
    ]
    assert found[0]["day"] == live(open_house)[0].pk and found[2]["event"] == one.pk
    assert (found[0]["start"], found[0]["end"]) == (at(SATURDAY, 10), at(SATURDAY, 14))
    by_dates = read(
        made,
        "in-the-way",
        {"asset": made.asset.pk, "dates": f"{FRIDAY.isoformat()},{SUNDAY.isoformat()}"},
    )["days"]
    assert [d["date"] for d in by_dates] == [SUNDAY.isoformat(), FRIDAY.isoformat()]
    nothing = read(
        made,
        "in-the-way",
        {
            "asset": made.asset.pk,
            "first": MONDAY.isoformat(),
            "last": MONDAY.isoformat(),
        },
    )
    assert nothing == {"days": []}


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"asset": "x", "first": "2027-03-01", "last": "2027-03-02"},
        {"asset": "1", "first": "2027-03-03", "last": "2027-03-02"},
        {"asset": "1", "first": "2027-03-01"},
        {"asset": "1", "dates": ""},
        {"asset": "1", "dates": "2027-03-01,x"},
        {"asset": "1", "dates": ",".join(["2027-03-01"] * 21)},
    ],
)
def test_a_malformed_in_the_way_query_is_a_400(
    made: made_up.MadeUp, query: dict[str, str]
) -> None:
    assert refusal(call(made, "in-the-way", query), "bad-request", 400)


def test_an_asset_of_another_organization_has_no_event_days_to_read(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    refusal(
        call(
            made,
            "in-the-way",
            {"asset": elsewhere.asset.pk, "first": "2027-03-01", "last": "2027-03-02"},
        ),
        "not-found",
        404,
    )


def closure(first: date, last: date | None = None, **more: Any) -> dict[str, Any]:
    return {
        "kind": "closure-make",
        "first_date": first.isoformat(),
        "last_date": (last or first).isoformat(),
        "reason": "",
        **more,
    }


def test_a_closure_removes_the_event_days_in_its_way_in_the_same_save(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=date(2027, 3, 14)), title="Open house")
    first, second, *_ = live(event)
    with application_login():
        with pytest.raises(LayoutRefusedError) as raised:
            closures.apply(
                made.administrator, made.asset.pk, closure(SATURDAY), now=NOW
            )
        assert raised.value.code == "event"
        closures.apply(
            made.administrator,
            made.asset.pk,
            closure(SATURDAY, SUNDAY, remove_event_days=[first.pk, second.pk]),
            now=NOW,
        )
    assert AvailabilityClosure.objects.of(made.organization).count() == 1
    assert [d.day for d in live(event)] == [SATURDAY_AFTER, date(2027, 3, 14)]
    assert audit_actions(made)[-3:] == [
        "removed event day",
        "removed event day",
        "made closure",
    ]
    assert claims(made) == 2


def test_a_preview_of_a_closure_removes_nothing_and_a_bad_day_saves_nothing(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=SATURDAY_AFTER))
    one = make(made, one_off(FRIDAY, 10, 12), title="Fair")
    first = live(event)[0]
    (fair,) = live(one)
    other = made_up.everything("elsewhere")
    with application_login():
        with pytest.raises(closures.PreviewedError):
            closures.apply(
                made.administrator,
                made.asset.pk,
                closure(SATURDAY, remove_event_days=[first.pk]) | {"preview": True},
                now=NOW,
            )
        for bad in (fair.pk, 10**9, first.pk + 1000):
            with pytest.raises(LayoutRefusedError) as raised:
                closures.apply(
                    made.administrator,
                    made.asset.pk,
                    closure(SATURDAY, FRIDAY, remove_event_days=[first.pk, bad]),
                    now=NOW,
                )
            assert raised.value.code == "event-day"
        # Only the days of the asset's own events.
        foreign = EventDay.objects.create(
            organization=other.organization,
            event=Event.objects.create(
                organization=other.organization,
                category="social",
                title="Theirs",
                made_by=other.administrator,
                contact=other.member,
            ),
            day=SATURDAY,
            starts_at=NOW + timedelta(days=5),
            ends_at=NOW + timedelta(days=6),
        )
        with pytest.raises(LayoutRefusedError):
            closures.apply(
                made.administrator,
                made.asset.pk,
                closure(SATURDAY, remove_event_days=[first.pk, foreign.pk]),
                now=NOW,
            )
    assert (
        live(event)[0].pk == first.pk
        and not AvailabilityClosure.objects.of(made.organization).exists()
    )
    assert EventDay.objects.of(other.organization).get(pk=foreign.pk).removed_at is None


def test_a_closure_that_is_moved_and_the_holidays_remove_event_days_too(
    made: made_up.MadeUp,
) -> None:
    easter_monday = date(2027, 3, 29)
    event = make(
        made,
        event_admin.Repeat(frozenset({0}), easter_monday, easter_monday, 600, 720),
        title="Walk",
    )
    (walk,) = live(event)
    with application_login():
        closures.apply(
            made.administrator,
            made.asset.pk,
            {
                "kind": "holidays",
                "year": 2027,
                "dates": [easter_monday.isoformat()],
                "remove_event_days": [walk.pk],
            },
            now=NOW,
        )
    assert AvailabilityClosure.objects.of(made.organization).count() == 1
    assert live(event) == []
    # A closure moved over a day.
    other = make(made, weekend(date(2027, 4, 3), date(2027, 4, 4)), title="Market")
    (saturday, *_) = live(other)
    with application_login():
        closures.apply(made.administrator, made.asset.pk, closure(MONDAY), now=NOW)
        row = AvailabilityClosure.objects.of(made.organization).get(first_date=MONDAY)
        closures.apply(
            made.administrator,
            made.asset.pk,
            {
                "kind": "closure-change",
                "id": row.pk,
                "first_date": date(2027, 4, 3).isoformat(),
                "last_date": date(2027, 4, 3).isoformat(),
                "reason": "",
                "remove_event_days": [saturday.pk],
            },
            now=NOW,
        )
    row.refresh_from_db()
    assert row.first_date == date(2027, 4, 3)
    assert [d.day for d in live(other)] == [date(2027, 4, 4)]


@pytest.mark.parametrize(
    "bad",
    ["1", [True], [0], [-1], list(range(1, 402)), [1.5], {"1": 1}],
)
def test_the_days_to_remove_are_a_list_of_ids(made: made_up.MadeUp, bad: Any) -> None:
    with application_login(), pytest.raises(ValueError, match=r"number|id|list"):
        closures.parse(closure(SATURDAY, remove_event_days=bad))
    with pytest.raises(ValueError, match="body"):
        closures.parse({"kind": "closure-remove", "id": 1, "remove_event_days": [1]})


# --- The block's way ----------------------------------------------------------------


def test_a_block_over_an_event_names_its_days_with_their_times(
    made: made_up.MadeUp,
) -> None:
    from tests.test_admin_blocks import call as blocks_call

    make(made, one_off(date(2027, 3, 15), 12, 14), title="Fair")
    found = answer(
        blocks_call(
            made,
            "lay",
            {
                "asset": made.asset.pk,
                "start": at(date(2027, 3, 15), 11),
                "end": at(date(2027, 3, 15), 15),
            },
        )
    )
    assert found["refusal"] == "event"
    assert found["events"][0]["title"] == "Fair"
    assert found["event_days"] == [
        {
            "event": found["events"][0]["id"],
            "title": "Fair",
            "start": at(date(2027, 3, 15), 12),
            "end": at(date(2027, 3, 15), 14),
        }
    ]
    clear = answer(
        blocks_call(
            made,
            "lay",
            {
                "asset": made.asset.pk,
                "start": at(date(2027, 3, 22), 11),
                "end": at(date(2027, 3, 22), 13),
            },
        )
    )
    assert clear["event_days"] == [] and clear["refusal"] is None
    assert event_pages.PER_PAGE == 25


# --- The audit log ------------------------------------------------------------------


def test_the_audit_log_names_each_event_action_and_links_the_event(
    made: made_up.MadeUp,
) -> None:
    kept = AuditEvent.objects.of(made.organization).count()
    found = read(made, "make", form(made, weekly([5], SATURDAY, SATURDAY_AFTER)))
    event = Event.objects.of(made.organization).get(pk=found["made"])
    (day, *_) = live(event)
    read(made, "day-remove", {}, day=day.pk)
    read(
        made,
        "change",
        form(made, weekly([5], SATURDAY, SATURDAY_AFTER), title="Moved"),
        event=event.pk,
    )
    rows: Any = audit_log.answer(made.organization, audit_log.Query())["events"]
    assert [row["action_label"] for row in rows[:3]] == [
        "Changed event",
        "Removed event day",
        "Made event",
    ]
    day_row = next(row for row in rows if row["action_label"] == "Removed event day")
    assert day_row["what"]["target"] == {"kind": "event", "id": event.pk}
    assert day_row["what"]["name"].startswith("Moved ")
    read(made, "remove", {}, event=event.pk)
    assert AuditEvent.objects.of(made.organization).count() == kept + 4


def test_a_repeat_skipping_a_booking_names_its_hours_and_member_once_audited(
    made: made_up.MadeUp,
) -> None:
    before = audit_actions(made).count("listed bookings")
    found = read(
        made,
        "preview",
        form(made, weekly([0], date(2027, 3, 1), date(2027, 3, 8), "12:00", "14:00")),
    )
    (skipped, *_) = [s for s in found["skipped"] if s["reason"] == "booked"]
    assert skipped["way"]["member"] == made.member.full_name
    assert skipped["way"]["start"] < skipped["way"]["end"]
    assert audit_actions(made).count("listed bookings") == before + 1
    # A preview that names nobody writes nothing.
    read(made, "preview", form(made, once(MONDAY, 18, 19)))
    assert audit_actions(made).count("listed bookings") == before + 1


def test_only_the_days_in_the_way_of_the_closure_or_the_holidays_are_removed(
    made: made_up.MadeUp,
) -> None:
    event = make(made, weekend(end=date(2027, 3, 14)), title="Open house")
    first, second, third, *_ = live(event)
    easter_monday = date(2027, 3, 29)
    with application_login():
        with pytest.raises(LayoutRefusedError) as raised:
            closures.apply(
                made.administrator,
                made.asset.pk,
                closure(SATURDAY, remove_event_days=[first.pk, third.pk]),
                now=NOW,
            )
        assert raised.value.code == "event-day"
        with pytest.raises(LayoutRefusedError) as raised:
            closures.apply(
                made.administrator,
                made.asset.pk,
                {
                    "kind": "holidays",
                    "year": 2027,
                    "dates": [easter_monday.isoformat()],
                    "remove_event_days": [first.pk],
                },
                now=NOW,
            )
        assert raised.value.code == "event-day"
    assert [d.pk for d in live(event)][:3] == [first.pk, second.pk, third.pk]
    assert not AvailabilityClosure.objects.of(made.organization).exists()
