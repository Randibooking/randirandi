# SPDX-License-Identifier: AGPL-3.0-only
"""The Members place's Imports tab : the gate and CSRF, the list,
a run's page, and starting a run with an .xlsx file: the parser by id, the file
never stored, the safety stop, the report, the audit events, and another
organization's runs."""

import json
from collections.abc import Callable
from functools import partial
from typing import Any
from urllib.parse import urlencode

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.utils import timezone

from randirandi.core import admin_views
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import export_member
from randirandi.core.models import Member, MemberImport, MemberStatus, Origin
from randirandi.member_parsers import MAX_UPLOAD
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_admin_bookings import answer, request_for
from tests.test_admin_members import events_of
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_login_sessions import HOST, get, page_settings  # noqa: F401
from tests.test_login_sessions import NOW as FROZEN
from tests.test_second_factor import clocks, token  # noqa: F401
from tests.xlsx import workbook

pytestmark = pytest.mark.django_db(transaction=True)
LIST = "/admin/members/imports/list/"
START = "/admin/members/imports/start/"
PARSER = "unit-members-xlsx"
HEADINGS: list[object] = [
    "Unit",
    "Address and flat",
    "Entrance",
    "Name 1",
    "Email 1",
    "Phone 1",
    "Name 2",
    "Email 2",
    "Phone 2",
]


@pytest.fixture
def made() -> made_up.MadeUp:
    found = made_up.everything()
    # Alex Rivers is not from a list: the safety stop counts the members a
    # list manages, and a first list must not look like a list that dropped him.
    Member.objects.of(found.organization).filter(pk=found.member.pk).update(
        origin=Origin.ADMINISTRATOR
    )
    return found


def unit(number: int) -> list[object]:
    return [
        f"U-{number:03d}",
        f"{number} Elm Street, H0{number}01",
        "Entrance A",
        f"First{number} Last{number}",
        f"first{number}@harbourview.example",
        f"+4791{number:06d}",
        None,
        None,
        None,
    ]


def file_of(*numbers: int, extra: list[list[object]] | None = None) -> bytes:
    return workbook([HEADINGS, *(unit(n) for n in numbers), *(extra or [])])


def start_request(
    data: bytes,
    *,
    parser: str = PARSER,
    name: str = "members.xlsx",
    by: Member | None = None,
    confirmed: bool = True,
    declared: int | None = None,
) -> Any:
    query = urlencode({"parser": parser, "name": name})
    request: Any = RequestFactory().post(
        f"{START}?{query}",
        data,
        content_type="application/octet-stream",
        HTTP_HOST=HOST,
        secure=True,
    )
    if declared is not None:
        request.META["CONTENT_LENGTH"] = str(declared)
    request.session = SessionStore()
    request.session["admin_confirmed"] = confirmed
    request.user = by
    return request


def start(made: made_up.MadeUp, data: bytes, **given: Any) -> HttpResponse:
    request = start_request(data, **given)
    request.user = given.get("by") or made.administrator
    return admin_views.member_import_start(request)


def runs(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    return answer(admin_views.member_imports_list(request_for(made, LIST, query)))


def run_page(made: made_up.MadeUp, number: int) -> dict[str, Any]:
    path = f"/admin/members/imports/{number}/data/"
    return answer(admin_views.member_import_data(request_for(made, path, {}), number))


def done(response: HttpResponse) -> dict[str, Any]:
    assert response.status_code == 200
    found = json.loads(response.content)
    assert isinstance(found, dict)
    return found


# --- The gate, the methods and CSRF ---------------------------------------------


def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    runs_before = MemberImport.across_organizations.count()
    for who, confirmed in (
        (made.administrator, False),
        (made.member, True),
        (elsewhere.administrator, True),
    ):
        listing = request_for(made, LIST, {})
        listing.session["admin_confirmed"] = confirmed
        listing.user = who
        reading = request_for(made, "/admin/members/imports/1/data/", {})
        reading.session["admin_confirmed"] = confirmed
        reading.user = who
        starting = start_request(file_of(1), by=who, confirmed=confirmed)
        calls: list[Callable[[], object]] = [
            partial(admin_views.member_imports_list, listing),
            partial(admin_views.member_import_data, reading, 1),
            partial(admin_views.member_import_start, starting),
        ]
        for call in calls:
            # The other organization's administrator is refused too: the
            # address is this organization's.
            with pytest.raises(PermissionDenied):
                call()
    assert MemberImport.across_organizations.count() == runs_before
    assert not Member.objects.of(made.organization).filter(email__startswith="first")


def test_the_methods_are_the_ones_each_call_takes(made: made_up.MadeUp) -> None:
    for view, path, method, args in (
        (admin_views.member_imports_list, LIST, "post", ()),
        (
            admin_views.member_import_data,
            "/admin/members/imports/1/data/",
            "post",
            (1,),
        ),
        (admin_views.member_import_start, START, "get", ()),
    ):
        request = getattr(RequestFactory(), method)(path, HTTP_HOST=HOST, secure=True)
        request.session = SessionStore()
        request.session["admin_confirmed"] = True
        request.user = made.administrator
        assert view(request, *args).status_code == 405


def test_a_signed_out_browser_goes_to_sign_in(made: made_up.MadeUp) -> None:
    for method, path in (
        ("get", "/admin/members/imports/"),
        ("get", "/admin/members/imports/1/"),
        ("get", LIST),
        ("get", "/admin/members/imports/1/data/"),
        ("post", START),
    ):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_guard_the_upload_and_a_real_upload_works(
    made: made_up.MadeUp,
) -> None:
    client = Client(enforce_csrf_checks=True)
    data = file_of(1, 2)
    with application_login():
        sign_in_administrator(client, made.administrator)
        token = str(config(get(client, "/admin/members/").content.decode())["csrf"])
        path = f"{START}?{urlencode({'parser': PARSER, 'name': 'members.xlsx'})}"

        def post_it(headers: dict[str, Any]) -> HttpResponse:
            response = client.post(
                path,
                data,
                content_type="application/octet-stream",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        assert post_it({"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        wrong = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://elsewhere.example"}
        assert post_it(wrong).status_code == 403
        assert not MemberImport.objects.of(made.organization).exclude(
            parser="made-up-members"
        )
        right = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://" + HOST}
        found = answer(post_it(right))
    assert found["outcome"] == "applied" and found["file"] == "members.xlsx"
    assert found["by"] == "Sam Field"


def test_an_import_page_address_is_the_members_place_with_the_tab(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        ui = config(get(client, "/admin/members/imports/").content.decode())
        assert (ui["place"], ui["imports"], ui["run"]) == ("members", True, None)
        ui = config(get(client, "/admin/members/imports/1/").content.decode())
        assert (ui["place"], ui["imports"], ui["run"]) == ("members", True, 1)
        ui = config(get(client, "/admin/members/").content.decode())
        assert (ui["imports"], ui["run"]) == (False, None)
    assert not events_of(made.organization, AuditAction.INSPECT)


# --- Starting a run ---------------------------------------------------------------


def test_a_run_reads_the_file_and_reports_what_it_did(made: made_up.MadeUp) -> None:
    data = file_of(1, 2, 3)
    found = done(start(made, data, name="members-august.xlsx"))
    assert found["number"] == 2  # the fixture's own run is the first
    assert found["outcome"] == "applied" and found["outcome_label"] == "Applied"
    assert found["counts"] == [3, 0, 0, 0, 0]
    assert found["parser"] == PARSER
    assert found["parser_name"] == "Unit members, Excel file"
    assert (found["file"], found["file_size"]) == ("members-august.xlsx", len(data))
    assert found["by"] == "Sam Field" and found["started"]
    assert found["disabled"] == [] and found["check"] == [] and found["skipped"] == []
    run = MemberImport.objects.of(made.organization).get(pk=found["id"])
    assert run.started_by_id == made.administrator.pk and run.parser == PARSER
    assert (
        Member.objects.of(made.organization)
        .filter(email="first1@harbourview.example", status=MemberStatus.ACTIVE)
        .exists()
    )
    (event,) = events_of(made.organization, AuditAction.IMPORT_MEMBERS)
    assert event.administrator_id == made.administrator.pk
    assert event.object_kind == "member_import" and event.object_id == run.pk


def test_the_file_is_never_stored(made: made_up.MadeUp) -> None:
    data = file_of(1, 2)
    found = done(start(made, data))
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM core_memberimport WHERE id = %s", [found["id"]])
        stored = list(cursor.fetchone() or ())
    assert all(not isinstance(value, bytes | memoryview) for value in stored)
    assert "first1@harbourview.example" not in json.dumps(stored, default=str)
    assert "first1@harbourview.example" not in json.dumps(found)


def test_what_the_request_may_not_be(made: made_up.MadeUp) -> None:
    def code(response: HttpResponse) -> tuple[int, object]:
        return response.status_code, json.loads(response.content)

    assert code(start(made, file_of(1), parser="nothing")) == (
        400,
        {"error": "unknown-parser"},
    )
    assert code(start(made, file_of(1), parser="made-up-members")) == (
        400,
        {"error": "unknown-parser"},
    )
    assert code(start(made, file_of(1), name="members.csv")) == (
        400,
        {"error": "wrong-file-type"},
    )
    assert code(start(made, file_of(1), name="")) == (400, {"error": "bad-request"})
    assert code(start(made, b"0" * (MAX_UPLOAD + 1))) == (
        413,
        {"error": "file-too-large"},
    )
    assert code(start(made, b"0" * 10, declared=MAX_UPLOAD + 1)) == (
        413,
        {"error": "file-too-large"},
    )
    assert MemberImport.objects.of(made.organization).count() == 1


def test_a_name_loses_its_folder_and_control_characters(
    made: made_up.MadeUp,
) -> None:
    found = done(start(made, file_of(1), name="C:\\Users\\x\\a\x07b/ list.xlsx"))
    assert found["file"] == "list.xlsx"
    assert len(done(start(made, file_of(1), name="n" * 400 + ".xlsx"))["file"]) == 255


def test_a_file_the_parser_cannot_read_is_a_failed_run_with_its_reason(
    made: made_up.MadeUp,
) -> None:
    found = done(start(made, b"this is not a workbook", name="broken.xlsx"))
    assert found["outcome"] == "failed" and found["reason"] == "invalid_xlsx"
    assert found["counts"] == [0, 0, 0, 0, 0]
    missing = [h for h in HEADINGS if h != "Email 1"]
    rows = [[v for h, v in zip(HEADINGS, unit(1), strict=True) if h != "Email 1"]]
    found = done(start(made, workbook([missing, *rows]), name="september.xlsx"))
    assert (found["outcome"], found["reason"], found["column"]) == (
        "failed",
        "missing_column",
        "Email 1",
    )
    assert (
        not Member.objects.of(made.organization)
        .filter(email__startswith="first")
        .exists()
    )


def test_the_safety_stop_leaves_everything_as_it_was(made: made_up.MadeUp) -> None:
    done(start(made, file_of(*range(1, 11)), name="all.xlsx"))
    before = Member.objects.of(made.organization).filter(status="active").count()
    found = done(start(made, file_of(1, 2, 3), name="section-b.xlsx"))
    assert found["outcome"] == "stopped" and found["outcome_label"] == "Stopped"
    assert found["reason"] == "revocation_limit"
    assert found["active"] == 10
    assert found["proposed"] == 7
    assert found["counts"] == [0, 0, 0, 0, 0]
    assert (
        Member.objects.of(made.organization).filter(status="active").count() == before
    )


def test_the_report_lists_rows_to_check_and_rows_skipped_by_code(
    made: made_up.MadeUp,
) -> None:
    broken = unit(2)
    broken[4] = None  # no email
    counted = unit(3)
    found = done(start(made, workbook([HEADINGS, unit(1), broken, counted])))
    assert found["counts"] == [2, 0, 0, 0, 1]
    assert found["skipped"] == [{"row": 3, "code": "invalid_member"}]
    assert found["check"] == []
    report = MemberImport.objects.of(made.organization).get(pk=found["id"]).report
    assert [f["rows"] for f in report["findings"]] == [[3]]
    encoded = json.dumps(report) + json.dumps(found)
    assert "First2" not in encoded and "harbourview.example" not in encoded


def test_a_disabled_member_is_named_with_the_booking_still_to_come(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: made_up.at(made_up.A_DAY, 8))
    # The listed member is Alex, who holds the fixture's booking.
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        origin=Origin.MEMBER_IMPORT
    )
    alex: list[object] = [
        "H0201",
        "1 Quay Road, H0201",
        "A",
        "Alex Rivers",
        made.member.email,
        made.member.phone,
        None,
        None,
        None,
    ]
    done(start(made, workbook([HEADINGS, alex, *(unit(n) for n in range(10, 14))])))
    found = done(start(made, file_of(10, 11, 12, 13)))
    assert found["outcome"] == "applied" and found["counts"][3] == 1
    (gone,) = found["disabled"]
    assert gone["name"] == "Alex Rivers" and gone["id"] == made.member.pk
    assert gone["bookings"] == [
        {"number": made.order.number, "reference": made.order.reference}
    ]
    made.member.refresh_from_db()
    assert made.member.status == MemberStatus.DISABLED
    # Not cancelled by the import.
    made.booking.taken_time.refresh_from_db()
    assert made.booking.taken_time.state == "confirmed"
    # A booking cancelled afterwards is no longer "still to come".
    made.booking.taken_time.state = "cancelled"
    made.booking.taken_time.save(update_fields=["state"])
    again = run_page(made, found["number"])
    assert again["disabled"][0]["bookings"] == []


def test_the_list_is_newest_first_with_the_parsers_and_counts(
    made: made_up.MadeUp,
) -> None:
    done(start(made, file_of(1), name="one.xlsx"))
    done(start(made, b"junk", name="two.xlsx"))
    found = runs(made)
    assert [r["number"] for r in found["imports"]] == [3, 2, 1]
    assert [r["file"] for r in found["imports"]] == ["two.xlsx", "one.xlsx", ""]
    assert [r["outcome"] for r in found["imports"]] == ["failed", "applied", "applied"]
    assert found["imports"][2]["parser_name"] == "Made-up members, CSV file (sandbox)"
    assert found["imports"][2]["by"] is None
    assert found["parsers"] == [
        {
            "id": PARSER,
            "name": "Unit members, Excel file",
            "reads": found["parsers"][0]["reads"],
            "accept": ".xlsx",
        }
    ]
    assert (found["count"], found["per_page"], found["page"]) == (3, 25, 1)
    assert runs(made, page=2)["imports"] == []


def test_another_organizations_runs_are_never_listed_or_opened(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    done(start(made, file_of(1), name="ours.xlsx"))
    theirs = MemberImport.objects.create(
        organization=elsewhere.organization,
        parser=PARSER,
        file_name="theirs.xlsx",
        outcome="applied",
    )
    found = runs(made)
    assert "theirs.xlsx" not in json.dumps(found) and found["count"] == 2
    # The other organization's run is not a number of ours.
    assert theirs.pk and run_page_status(made, 3) == 404
    assert run_page_status(made, 0) == 404
    assert run_page_status(made, 2**40) == 404


def run_page_status(made: made_up.MadeUp, number: int) -> int:
    path = f"/admin/members/imports/{number}/data/"
    return admin_views.member_import_data(
        request_for(made, path, {}), number
    ).status_code


def test_opening_a_run_writes_one_inspected_event_on_it(made: made_up.MadeUp) -> None:
    found = done(start(made, file_of(1)))
    assert not events_of(made.organization, AuditAction.INSPECT)
    run_page(made, found["number"])
    (event,) = events_of(made.organization, AuditAction.INSPECT)
    assert event.object_kind == "member_import" and event.object_id == found["id"]
    # Reading the list writes none.
    runs(made)
    assert len(events_of(made.organization, AuditAction.INSPECT)) == 1


def test_a_second_administrator_waits_for_the_first_run_to_finish(
    made: made_up.MadeUp,
) -> None:
    # The organization's lock is the one the reconciliation takes, so a run
    # started while another holds it waits and then runs on the new state.
    first = done(start(made, file_of(1, 2)))
    second = done(start(made, file_of(1, 2)))
    assert first["counts"][0] == 2 and second["counts"][0] == 0
    assert second["number"] == first["number"] + 1


def test_the_member_export_follows_the_runs_an_administrator_started(
    made: made_up.MadeUp,
) -> None:
    found = done(start(made, file_of(1), name="august.xlsx"))
    with application_login():
        admin = export_member(
            actor=made.administrator,
            member_id=made.administrator.pk,
            **fresh(made.administrator, now=FROZEN),
        )
    (started,) = admin["started_imports"]
    assert (started["id"], started["parser"], started["file_name"]) == (
        found["id"],
        PARSER,
        "august.xlsx",
    )
    assert started["file_size"] == found["file_size"]
    assert started["outcome"] == "applied"
    # Nothing from the file or its report.
    assert "first1@harbourview.example" not in json.dumps(admin)


def test_unknown_values_in_a_stored_report_do_not_break_the_page(
    made: made_up.MadeUp,
) -> None:
    run = MemberImport.objects.of(made.organization).get()
    run.report = {
        "findings": [{"code": "future_code", "rows": [7, 7]}],
        "disabled": ["x"],
    }
    run.save(update_fields=["report"])
    found = run_page(made, 1)
    assert found["skipped"] == [{"row": 7, "code": "future_code"}]
    assert found["disabled"] == []
    run.report = []
    run.save(update_fields=["report"])
    assert run_page(made, 1)["skipped"] == []


def test_legacy_upload_and_history_keep_identity_without_offering_the_alias(
    made: made_up.MadeUp,
) -> None:
    from randirandi.member_parsers import LEGACY_UNIT_MEMBERS

    elsewhere = made_up.everything("legacy-elsewhere")
    assert elsewhere.organization != made.organization
    client = Client(enforce_csrf_checks=True)
    with application_login():
        sign_in_administrator(client, made.administrator)
        csrf = str(config(get(client, "/admin/members/").content.decode())["csrf"])
        query = urlencode({"parser": LEGACY_UNIT_MEMBERS.id, "name": "members.xlsx"})
        path = f"{START}?{query}"
        response = client.post(
            path,
            file_of(1),
            content_type="application/octet-stream",
            HTTP_HOST=HOST,
            secure=True,
            HTTP_X_CSRFTOKEN=csrf,
            HTTP_ORIGIN="https://" + HOST,
        )
        assert isinstance(response, HttpResponse)
        found = done(response)
        record = MemberImport.objects.of(made.organization).latest("pk")
        assert record.pk != found["number"]
        assert record.parser == LEGACY_UNIT_MEMBERS.id
        listing = answer(get(client, LIST))
        assert [p["id"] for p in listing["parsers"]] == [PARSER]
        assert listing["imports"][0]["parser_name"] == "Unit members, Excel file"
        detail = answer(get(client, f"/admin/members/imports/{found['number']}/data/"))
        assert detail["parser_name"] == "Unit members, Excel file"
