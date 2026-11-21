# SPDX-License-Identifier: AGPL-3.0-only
"""The admin's Blocks endpoints : the gate, CSRF and origin, the
malformed requests, the list with each filter and order, the preview, make and
remove, another organization's assets and blocks, the events, and the audit
log that shows both actions."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from randirandi.core import admin_views, audit_log, booking_admin
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    Asset,
    AuditEvent,
    Block,
    BookingConfiguration,
    ConfigurationCategory,
    Member,
    TakenTime,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
NEW_YORK = ZoneInfo("America/New_York")
# Before every day the tests use; a test moves it where it needs.
NOW = datetime(2027, 3, 9, 12, 0, tzinfo=UTC)
# Mondays: the made-up container is open 10:00 to 22:00 on them, by the hour.
MONDAYS = [date(2027, 3, 1) + timedelta(days=7 * week) for week in range(12)]
MARCH_8, MARCH_15, MARCH_22, MARCH_29 = MONDAYS[1:5]


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


@pytest.fixture(autouse=True)
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booking_admin, "_now", lambda: NOW)


def local(day: date, hour: int, minute: int = 0) -> str:
    """A moment as the page sends it: on the asset's clock."""
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}"


def request_to(
    who: Member,
    method: str,
    path: str,
    data: Any = None,
    *,
    confirmed: bool = True,
) -> Any:
    factory = RequestFactory()
    if method == "get":
        request = factory.get(path, data or {}, HTTP_HOST=HOST, secure=True)
    else:
        body = data if isinstance(data, bytes) else json.dumps(data or {}).encode()
        request = factory.post(
            path, body, content_type="application/json", HTTP_HOST=HOST, secure=True
        )
    request.session = SessionStore()
    request.session["admin_confirmed"] = confirmed
    request.user = who
    return request


def call(
    made: made_up.MadeUp,
    which: str,
    data: Any = None,
    *,
    who: Member | None = None,
    confirmed: bool = True,
    block: int = 0,
) -> HttpResponse:
    """A Blocks view called directly, by default as a confirmed administrator.
    `remove` takes the block's id."""
    method = "get" if which in {"list", "lay"} else "post"
    path = (
        f"/admin/blocks/{block}/remove/"
        if which == "remove"
        else f"/admin/blocks/{which}/"
    )
    request = request_to(
        who or made.administrator, method, path, data, confirmed=confirmed
    )
    if which == "remove":
        return admin_views.blocks_remove(request, block)
    view = {
        "list": admin_views.blocks,
        "lay": admin_views.blocks_lay,
        "make": admin_views.blocks_make,
    }[which]
    return view(request)


def answer(response: HttpResponse, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    found = json.loads(response.content)
    assert isinstance(found, dict)
    return found


def listed(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    return answer(call(made, "list", query))


def stretch(
    made: made_up.MadeUp,
    start: str,
    end: str,
    asset: Asset | int | None = None,
    **more: Any,
) -> dict[str, Any]:
    target = asset or made.asset
    pk = target if isinstance(target, int) else target.pk
    return {"asset": pk, "start": start, "end": end, **more}


def take(
    made: made_up.MadeUp,
    day: date,
    start_hour: int,
    end_hour: int,
    *,
    note: str = "",
    by: Member | None = None,
    configuration: BookingConfiguration | None = None,
    created_at: datetime | None = None,
) -> Block:
    """A block on the container's asset, between two UTC hours of the day."""
    configuration = configuration or made.configuration
    return Block.objects.take(
        organization=made.organization,
        asset=configuration.asset,
        booking_configuration=configuration,
        starts_at=made_up.at(day, start_hour),
        ends_at=made_up.at(day, end_hour),
        note=note,
        made_by=by or made.administrator,
        created_at=created_at,
    )


def deck(made: made_up.MadeUp) -> BookingConfiguration:
    """An asset on New York's clock, open every day 10:00 to 14:00 by the hour."""
    asset = Asset.objects.create(
        organization=made.organization, name="Harbor Deck", time_zone=str(NEW_YORK)
    )
    container = BookingConfiguration.objects.create(
        organization=made.organization,
        asset=asset,
        booking_type=made.booking_type,
        period_type="flexible",
        period_start=600,
        period_end=840,
        end_day=0,
        period_start_weekdays=list(range(7)),
        interval=60,
        minimum_length=60,
        maximum_length=240,
        status="enabled",
        price_book=made.price_book,
    )
    ConfigurationCategory.objects.create(
        organization=made.organization,
        booking_configuration=container,
        booking_category=made.private,
    )
    return container


# --- The gate, CSRF and origin ---------------------------------------------------


@pytest.mark.parametrize("which", ["list", "lay", "make", "remove"])
def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp, which: str
) -> None:
    block = take(made, MARCH_15, 9, 11)
    elsewhere = made_up.everything("elsewhere")
    data = stretch(made, local(MARCH_8, 10), local(MARCH_8, 12))

    def ask(who: Member, confirmed: bool = True) -> HttpResponse:
        return call(
            made,
            which,
            data,
            who=who,
            confirmed=confirmed,
            block=block.pk if which == "remove" else 0,
        )

    with pytest.raises(PermissionDenied):
        ask(made.administrator, confirmed=False)
    with pytest.raises(PermissionDenied):
        ask(made.member)
    with pytest.raises(PermissionDenied):
        ask(elsewhere.administrator)
    assert Block.objects.of(made.organization).filter(pk=block.pk).exists()
    assert Block.objects.of(made.organization).count() == 2


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    made: made_up.MadeUp,
) -> None:
    block = take(made, MARCH_15, 9, 11)
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        for path, method in (
            ("/admin/blocks/list/", "post"),
            ("/admin/blocks/lay/", "post"),
            ("/admin/blocks/make/", "get"),
            (f"/admin/blocks/{block.pk}/remove/", "get"),
        ):
            response = getattr(client, method)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
    for path in ("/admin/blocks/list/", "/admin/blocks/make/"):
        response = Client().post(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")
    assert Block.objects.of(made.organization).filter(pk=block.pk).exists()


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    body = json.dumps(stretch(made, local(MARCH_15, 10), local(MARCH_15, 12)))
    with application_login():
        sign_in_administrator(client, made.administrator)
        token = str(config(get(client, "/admin/blocks/").content.decode())["csrf"])

        def send(path: str, headers: dict[str, Any]) -> HttpResponse:
            response = client.post(
                path,
                body,
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        right = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://" + HOST}
        wrong = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://elsewhere.example"}
        make = "/admin/blocks/make/"
        assert send(make, {"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        assert send(make, wrong).status_code == 403
        assert Block.objects.of(made.organization).count() == 1
        found = answer(send(make, right))
        (pk,) = found["blocks"]
        remove = f"/admin/blocks/{pk}/remove/"
        assert send(remove, {"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        assert send(remove, wrong).status_code == 403
        assert Block.objects.of(made.organization).filter(pk=pk).exists()
        assert answer(send(remove, right)) == {"removed": pk}
    assert Block.objects.of(made.organization).count() == 1


# --- Malformed requests ---------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        {"when": "week"},
        {"when": "everything"},
        {"when": "day"},
        {"when": "period", "from": "2027-03-01"},
        {"when": "day", "from": "2027-3-1"},
        {"when": "day", "from": "1999-12-31"},
        {"order": "newest"},
        {"page": "0"},
        {"page": "x"},
    ],
)
def test_a_malformed_list_query_is_a_400(
    made: made_up.MadeUp, query: dict[str, str]
) -> None:
    assert answer(call(made, "list", query), 400) == {"error": "bad-request"}


def test_a_malformed_stretch_is_a_400_and_an_asset_not_here_a_404(
    made: made_up.MadeUp,
) -> None:
    good = stretch(made, local(MARCH_8, 10), local(MARCH_8, 12))
    elsewhere = made_up.everything("elsewhere")
    for bad in (
        {**good, "asset": "x"},
        {**good, "asset": 0},
        {**good, "asset": 2**70},
        {**good, "asset": True},
        {**good, "asset": None},
        {**good, "start": "2027-03-08 10:00"},
        {**good, "start": "2027-03-08T10:00:00"},
        {**good, "start": "2027-13-08T10:00"},
        {**good, "start": None},
        {**good, "start": "1999-12-31T10:00"},
        {**good, "end": "2101-01-01T10:00"},
        # The hour the clocks skip on 28 March 2027, 02:00 to 03:00 in Oslo.
        {**good, "end": "2027-03-28T02:30"},
        # Longer than a year.
        {**good, "end": "2028-03-09T10:00"},
    ):
        asked = {key: value for key, value in bad.items() if value is not None}
        assert answer(call(made, "lay", asked), 400) == {"error": "bad-request"}, bad
        assert answer(call(made, "make", bad), 400) == {"error": "bad-request"}, bad
    missing = {"error": "not-found"}
    for asset in (999_999_999, elsewhere.asset.pk):
        data = {**good, "asset": asset}
        assert answer(call(made, "lay", data), 404) == missing
        assert answer(call(made, "make", data), 404) == missing
    assert Block.objects.of(made.organization).count() == 1
    assert Block.objects.of(elsewhere.organization).count() == 1


@pytest.mark.parametrize(
    "bad",
    [
        b"not json",
        b"[]",
        b"{}",
        {"asset": 1, "start": "x", "end": "y", "extra": 1},
        {"asset": 1, "start": "2027-03-08T10:00"},
    ],
)
def test_a_malformed_make_body_is_a_400(made: made_up.MadeUp, bad: Any) -> None:
    if isinstance(bad, dict):
        bad = {**bad, "asset": made.asset.pk}
    assert answer(call(made, "make", bad), 400) == {"error": "bad-request"}
    assert Block.objects.of(made.organization).count() == 1


def test_a_note_is_text_of_at_most_500_characters(made: made_up.MadeUp) -> None:
    data = stretch(made, local(MARCH_8, 10), local(MARCH_8, 12))
    for note in (5, ["x"], "x" * 501):
        assert answer(call(made, "make", {**data, "note": note}), 400)
    answer(call(made, "make", {**data, "note": "  Deep clean  "}))
    assert Block.objects.of(made.organization).latest("pk").note == "Deep clean"


# --- The list -----------------------------------------------------------------------


def test_the_list_has_each_cell_as_the_drawing_has_it(made: made_up.MadeUp) -> None:
    sanding = Block.objects.of(made.organization).get()
    take(made, MARCH_15, 9, 11, note="Deep clean", created_at=made_up.at(MARCH_8, 8))
    rows = listed(made, when="coming")["blocks"]
    assert rows == [
        {
            "id": rows[0]["id"],
            "start_date": "2027-03-15",
            "start_time": "10:00",
            "end_date": "2027-03-15",
            "end_time": "12:00",
            "asset": "Function room",
            "booking_type": "Room hire",
            "note": "Deep clean",
            "made_by": "Sam Field",
            "created": "2027-03-08, 09:00:00",
            "coming": True,
        }
    ]
    past = listed(made, when="past")["blocks"]
    # A block from before the creation time was kept shows no time.
    assert [(row["id"], row["created"]) for row in past] == [(sanding.pk, None)]
    assert past[0]["start_time"] == "15:00" and past[0]["note"] == "Floor sanding"


def test_each_filter_and_order(made: made_up.MadeUp) -> None:
    early = Block.objects.of(made.organization).get()  # 1 March
    march_8 = take(made, MARCH_8, 9, 10)
    march_15 = take(made, MARCH_15, 9, 10)
    march_22 = take(made, MARCH_22, 9, 10)

    def ids(**query: Any) -> list[int]:
        return [row["id"] for row in listed(made, **query)["blocks"]]

    # Now is 9 March, 12:00: two are past, two are coming.
    assert ids() == [march_15.pk, march_22.pk] == ids(when="coming")
    assert ids(order="latest") == [march_22.pk, march_15.pk]
    assert ids(when="past") == [march_8.pk, early.pk]
    assert ids(when="past", order="earliest") == [early.pk, march_8.pk]
    assert ids(when="day", **{"from": "2027-03-08"}) == [march_8.pk]
    assert ids(when="day", **{"from": "2027-03-09"}) == []
    assert ids(when="period", **{"from": "2027-03-08", "to": "2027-03-15"}) == [
        march_8.pk,
        march_15.pk,
    ]
    # The wrong way round reads the same.
    assert ids(when="period", **{"from": "2027-03-15", "to": "2027-03-08"}) == [
        march_8.pk,
        march_15.pk,
    ]
    assets = [{"id": made.asset.pk, "name": "Function room"}]
    assert listed(made, when="day", **{"from": "2027-03-09"}) == {
        "count": 0,
        "per_page": 25,
        "page": 1,
        "pages": 0,
        "assets": assets,
        "blocks": [],
    }
    # Another organization's assets are not offered.
    made_up.everything("elsewhere")
    assert listed(made)["assets"] == assets


def test_a_block_under_way_is_coming_and_one_that_ended_is_past(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = take(made, MARCH_15, 9, 12)
    during = made_up.at(MARCH_15, 10)
    monkeypatch.setattr(booking_admin, "_now", lambda: during)
    (row,) = listed(made)["blocks"]
    assert (row["id"], row["coming"]) == (block.pk, True)
    after = made_up.at(MARCH_15, 12)
    monkeypatch.setattr(booking_admin, "_now", lambda: after)
    assert listed(made)["blocks"] == []
    assert next(r["id"] for r in listed(made, when="past")["blocks"]) == block.pk


def test_the_list_pages(made: made_up.MadeUp) -> None:
    for week in range(2, 12):
        take(made, MONDAYS[week], 9, 10)
        take(made, MONDAYS[week], 11, 12)
        take(made, MONDAYS[week], 13, 14)
    first = listed(made)
    assert (first["count"], first["pages"], first["per_page"]) == (30, 2, 25)
    assert len(first["blocks"]) == 25
    assert len(listed(made, page=2)["blocks"]) == 5
    assert listed(made, page=3)["blocks"] == []


def test_the_list_reads_in_a_constant_number_of_queries(made: made_up.MadeUp) -> None:
    def count() -> int:
        with CaptureQueriesContext(connection) as queries:
            listed(made, when="period", **{"from": "2027-01-01", "to": "2027-12-31"})
        return len(queries)

    take(made, MARCH_15, 9, 10)
    few = count()
    for week in range(3, 12):
        take(made, MONDAYS[week], 9, 10)
    assert count() == few


def test_times_are_on_each_assets_own_clock(made: made_up.MadeUp) -> None:
    euro = deck(made)
    # 10:00 to 12:00 on the deck's New York clock (UTC-5): 15:00 to 17:00 UTC.
    answer(
        call(
            made,
            "make",
            stretch(made, local(MARCH_8, 10), local(MARCH_8, 12), euro.asset),
        )
    )
    (stored,) = TakenTime.objects.of(made.organization).filter(asset=euro.asset)
    assert stored.starts_at == datetime(2027, 3, 8, 15, tzinfo=UTC)
    (row,) = [
        r for r in listed(made, when="past")["blocks"] if r["asset"] == "Harbor Deck"
    ]
    assert (row["start_date"], row["start_time"]) == ("2027-03-08", "10:00")
    assert (row["end_date"], row["end_time"]) == ("2027-03-08", "12:00")
    # The same wall time on the Oslo asset is an hour earlier in UTC.
    answer(call(made, "make", stretch(made, local(MARCH_8, 10), local(MARCH_8, 12))))
    oslo = TakenTime.objects.of(made.organization).get(
        asset=made.asset, starts_at=datetime(2027, 3, 8, 9, tzinfo=UTC)
    )
    assert oslo.ends_at == datetime(2027, 3, 8, 11, tzinfo=UTC)
    days = listed(made, when="day", **{"from": "2027-03-08"})["blocks"]
    assert {r["asset"] for r in days} == {"Harbor Deck", "Function room"}


# --- Make and remove ---------------------------------------------------------------


def test_make_saves_the_blocks_with_their_events(made: made_up.MadeUp) -> None:
    found = answer(
        call(
            made,
            "make",
            stretch(
                made, local(MARCH_8, 18), local(MARCH_15, 12), note="Floor sanding"
            ),
        )
    )
    blocks = list(Block.objects.of(made.organization).filter(pk__in=found["blocks"]))
    assert len(blocks) == 2
    assert [b.taken_time.starts_at for b in sorted(blocks, key=lambda b: b.pk)] == [
        datetime(2027, 3, 8, 17, tzinfo=UTC),
        datetime(2027, 3, 15, 9, tzinfo=UTC),
    ]
    assert {(b.note, b.made_by_id, b.created_at is not None) for b in blocks} == {
        ("Floor sanding", made.administrator.pk, True)
    }
    events = AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.MADE_BLOCK, object_id__in=found["blocks"]
    )
    assert sorted(e.object_id for e in events) == sorted(found["blocks"])
    assert {(e.object_kind, e.administrator_id) for e in events} == {
        ("block", made.administrator.pk)
    }


def test_remove_frees_the_time_and_writes_its_event_before_the_delete(
    made: made_up.MadeUp,
) -> None:
    block = take(made, MARCH_15, 9, 11)
    taken_time = block.taken_time_id
    assert answer(call(made, "remove", block=block.pk)) == {"removed": block.pk}
    assert not Block.objects.of(made.organization).filter(pk=block.pk).exists()
    assert not TakenTime.objects.of(made.organization).filter(pk=taken_time).exists()
    (event,) = AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.REMOVED_BLOCK
    )
    assert (event.object_kind, event.object_id, event.administrator_id) == (
        "block",
        block.pk,
        made.administrator.pk,
    )
    # The time is free for the same stretch again.
    answer(call(made, "make", stretch(made, local(MARCH_15, 10), local(MARCH_15, 12))))


def test_remove_takes_a_block_under_way_but_not_one_that_is_over(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    under_way, over = take(made, MARCH_15, 9, 12), take(made, MARCH_22, 9, 12)
    monkeypatch.setattr(booking_admin, "_now", lambda: made_up.at(MARCH_15, 10))
    assert answer(call(made, "remove", block=under_way.pk))["removed"] == under_way.pk
    monkeypatch.setattr(booking_admin, "_now", lambda: made_up.at(MARCH_22, 12))
    assert answer(call(made, "remove", block=over.pk), 409) == {"error": "past"}
    assert Block.objects.of(made.organization).filter(pk=over.pk).exists()
    removed = AuditEvent.objects.of(made.organization).filter(
        action=AuditAction.REMOVED_BLOCK
    )
    assert [e.object_id for e in removed] == [under_way.pk]


def test_remove_does_not_reach_another_organizations_block(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    theirs = Block.objects.of(elsewhere.organization).get()
    for number in (theirs.pk, 10**9, 0):
        assert answer(call(made, "remove", block=number), 404) == {"error": "not-found"}
    assert Block.objects.of(elsewhere.organization).filter(pk=theirs.pk).exists()
    # Removing twice: the second finds nothing.
    mine = take(made, MARCH_15, 9, 11)
    answer(call(made, "remove", block=mine.pk))
    assert answer(call(made, "remove", block=mine.pk), 404) == {"error": "not-found"}
    assert (
        AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.REMOVED_BLOCK)
        .count()
        == 1
    )


def test_the_audit_log_shows_both_actions_without_a_link(
    made: made_up.MadeUp,
) -> None:
    found = answer(
        call(made, "make", stretch(made, local(MARCH_15, 10), local(MARCH_15, 12)))
    )
    (pk,) = found["blocks"]
    answer(call(made, "remove", block=pk))
    log: Any = audit_log.answer(made.organization, audit_log.Query())
    rows = [e for e in log["events"] if e["what"]["name"] == str(pk)]
    assert sorted(e["action_label"] for e in rows) == ["Made block", "Removed block"]
    assert {(e["what"]["kind"], e["what"]["booking"]) for e in rows} == {
        ("Block", None)
    }
    actions = {a["value"]: a["label"] for a in log["actions"]}
    assert actions["removed block"] == "Removed block"
    filtered = audit_log.answer(
        made.organization, audit_log.Query(action="removed block")
    )
    assert filtered["count"] == 1
