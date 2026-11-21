# SPDX-License-Identifier: AGPL-3.0-only
"""The Members and a member's page : the gate, CSRF and origin,
each filter, the search and each sort, the pages and the lack of an "all", the
page's sections and their empty states, the inspected event, Block and Unblock
with their refusals, another organization's members, and that no network
address is ever answered."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from randirandi.core import admin_views
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import erase_identity, export_member
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    AuditEvent,
    Authenticator,
    ImportOutcome,
    LoginAttempt,
    LoginCode,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    MemberStatus,
    Organization,
    Origin,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_admin_bookings import answer, book, order_of, request_for
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_login_sessions import NOW as CODE_TIME
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
LIST = "/admin/members/list/"
NETWORK = "192.0.2.77"
FIRST = datetime(2027, 1, 10, 8, 0, tzinfo=UTC)


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def person(
    made: made_up.MadeUp,
    name: str,
    *,
    unit: str | None = None,
    address: str = "",
    flat: str = "",
    status: MemberStatus = MemberStatus.ACTIVE,
    phone: str | None = None,
    role: Any = None,
    origin: Origin = Origin.MEMBER_IMPORT,
) -> Member:
    first, _, last = name.partition(" ")
    number = sum(ord(letter) * (i + 1) for i, letter in enumerate(name))
    found = Member.objects.create(
        organization=made.organization,
        role=role or made.standard,
        full_name=name,
        first_name=first,
        last_name=last,
        email=f"{name.lower().replace(' ', '.')}@harbourview.example",
        phone=f"+47912{number % 100000:05d}" if phone is None else phone,
        status=status,
        origin=origin,
    )
    if unit is not None:
        MemberMembership.objects.create(
            organization=made.organization,
            member=found,
            membership=Membership.objects.create(
                organization=made.organization,
                identifier=unit,
                address=address,
                address_unit=flat,
                section="Entrance B",
                postal_code="4321",
                city="Northbridge",
            ),
        )
    return found


def login(
    made: made_up.MadeUp, who: Member, kind: str, outcome: str, when: datetime
) -> None:
    LoginAttempt.objects.create(
        organization=made.organization,
        member=who,
        at=when,
        network=NETWORK,
        kind=kind,
        outcome=outcome,
    )


def members(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    return answer(admin_views.members(request_for(made, LIST, query)))


def names(found: dict[str, Any]) -> list[str]:
    return [row["name"] for row in found["members"]]


def one(made: made_up.MadeUp, who: Member) -> dict[str, Any]:
    path = f"/admin/members/{who.pk}/data/"
    return answer(admin_views.member_data(request_for(made, path, {}), who.pk))


def send(
    made: made_up.MadeUp,
    act: str,
    who: int,
    body: bytes = b"{}",
    *,
    by: Member | None = None,
    confirmed: bool = True,
) -> HttpResponse:
    request = RequestFactory().post(
        f"/admin/members/{who}/{act}/",
        body,
        content_type="application/json",
        HTTP_HOST=HOST,
        secure=True,
    )
    request.session = SessionStore()
    request.session["admin_confirmed"] = confirmed
    request.user = by or made.administrator
    view: Callable[..., HttpResponse] = (
        admin_views.member_block if act == "block" else admin_views.member_unblock
    )
    return view(request, who)


def events_of(org: Organization, action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.of(org).filter(action=action).order_by("pk"))


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return events_of(made.organization, action)


# --- The gate, CSRF and origin ----------------------------------------------------


def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    paths: dict[Callable[..., HttpResponse], str] = {
        admin_views.members: LIST,
        admin_views.member_data: f"/admin/members/{made.member.pk}/data/",
    }
    for view, path in paths.items():
        extra = () if view is admin_views.members else (made.member.pk,)
        for who, confirmed in (
            (made.administrator, False),
            (made.member, True),
            (elsewhere.administrator, True),
        ):
            request = request_for(made, path, {})
            request.session["admin_confirmed"] = confirmed
            request.user = who
            with pytest.raises(PermissionDenied):
                view(request, *extra)
    for act in ("block", "unblock"):
        with pytest.raises(PermissionDenied):
            send(made, act, made.member.pk, confirmed=False)
        with pytest.raises(PermissionDenied):
            send(made, act, made.member.pk, by=made.member)
        with pytest.raises(PermissionDenied):
            send(made, act, made.member.pk, by=elsewhere.administrator)
    assert (
        Member.objects.of(made.organization).get(pk=made.member.pk).status == "active"
    )
    assert not events(made, AuditAction.BLOCK_MEMBER)
    assert not events(made, AuditAction.INSPECT)


def test_the_methods_are_the_ones_each_call_takes(made: made_up.MadeUp) -> None:
    for view, path, args in (
        (admin_views.members, LIST, ()),
        (admin_views.member_data, "/admin/members/1/data/", (1,)),
    ):
        request = RequestFactory().post(path, HTTP_HOST=HOST, secure=True)
        request.session = SessionStore()
        request.session["admin_confirmed"] = True
        request.user = made.administrator
        assert view(request, *args).status_code == 405
    for view in (admin_views.member_block, admin_views.member_unblock):
        request = RequestFactory().get(
            "/admin/members/1/block/", HTTP_HOST=HOST, secure=True
        )
        request.session = SessionStore()
        request.session["admin_confirmed"] = True
        request.user = made.administrator
        assert view(request, 1).status_code == 405


def test_a_signed_out_browser_goes_to_sign_in(made: made_up.MadeUp) -> None:
    for method, path in (
        ("get", LIST),
        ("get", f"/admin/members/{made.member.pk}/"),
        ("get", f"/admin/members/{made.member.pk}/data/"),
        ("post", f"/admin/members/{made.member.pk}/block/"),
        ("post", f"/admin/members/{made.member.pk}/unblock/"),
    ):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_posts(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    path = f"/admin/members/{made.member.pk}/block/"
    with application_login():
        sign_in_administrator(client, made.administrator)
        token = str(config(get(client, "/admin/members/").content.decode())["csrf"])

        def post_it(headers: dict[str, Any]) -> HttpResponse:
            response = client.post(
                path,
                "{}",
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )
            assert isinstance(response, HttpResponse)
            return response

        assert post_it({"HTTP_ORIGIN": "https://" + HOST}).status_code == 403
        wrong = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://elsewhere.example"}
        assert post_it(wrong).status_code == 403
        made.member.refresh_from_db()
        assert made.member.status == "active"
        right = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://" + HOST}
        found = answer(post_it(right))
    assert found["status"] == "blocked"


def test_a_member_page_address_is_the_places_page_with_the_member(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        response = get(client, f"/admin/members/{made.member.pk}/")
        ui = config(response.content.decode())
        assert response.status_code == 200
        assert ui["place"] == "members" and ui["member"] == made.member.pk
        assert config(get(client, "/admin/members/").content.decode())["member"] is None
    assert not events(made, AuditAction.INSPECT)


# --- The list ------------------------------------------------------------------------


def test_the_list_answers_the_columns_as_drawn(made: made_up.MadeUp) -> None:
    other = person(
        made, "Robin Lake", unit="H0302", address="4 Elm Street", flat="H0302"
    )
    other.phone = ""
    other.save()
    found = members(made)
    assert found["count"] == 3 and found["per_page"] == 25
    assert found["page"] == 1 and found["pages"] == 1
    assert names(found) == ["Alex Rivers", "Robin Lake", "Sam Field"]
    alex, robin, sam = found["members"]
    assert alex == {
        "id": made.member.pk,
        "name": "Alex Rivers",
        "email": "alex@harbourview.example",
        "phone": "+4790000001",
        "units": ["H0201"],
        "role": "Standard",
        "status": "active",
        "status_label": "Active",
        "last_login": None,
    }
    assert robin["phone"] == "" and robin["units"] == ["H0302"]
    assert sam["role"] == "Board" and sam["units"] == []
    assert found["statuses"] == [
        {"value": "active", "label": "Active"},
        {"value": "disabled", "label": "Disabled"},
        {"value": "blocked", "label": "Blocked"},
    ]
    assert found["roles"] == [
        {"value": made.board.pk, "label": "Board"},
        {"value": made.standard.pk, "label": "Standard"},
    ]


def test_another_organizations_members_are_never_listed(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    found = members(made)
    assert names(found) == ["Alex Rivers", "Sam Field"]
    assert elsewhere.member.email not in json.dumps(found)
    assert {row["label"] for row in found["roles"]} == {"Board", "Standard"}
    assert members(made, role=elsewhere.standard.pk)["members"] == []


def test_the_status_filter(made: made_up.MadeUp) -> None:
    person(made, "Blake Gray", status=MemberStatus.DISABLED)
    blocked = person(made, "Casey Moon")
    blocked.status = MemberStatus.BLOCKED
    blocked.save()
    assert names(members(made, status="active")) == ["Alex Rivers", "Sam Field"]
    assert names(members(made, status="disabled")) == ["Blake Gray"]
    assert names(members(made, status="blocked")) == ["Casey Moon"]
    assert len(members(made)["members"]) == 4


def test_the_role_filter(made: made_up.MadeUp) -> None:
    assert names(members(made, role=made.board.pk)) == ["Sam Field"]
    assert names(members(made, role=made.standard.pk)) == ["Alex Rivers"]
    assert members(made, role=made.board.pk, status="blocked")["members"] == []


def test_the_search_reads_name_email_phone_and_unit(made: made_up.MadeUp) -> None:
    person(made, "Robin Lake", unit="K7", address="12 Birch Road", flat="H0405")
    person(
        made,
        "Blake Gray",
        unit="K8",
        address="4 Elm Street",
        flat="H0101",
        phone="+4791234567",
    )
    assert names(members(made, q="lake")) == ["Blake Gray", "Robin Lake"]
    assert names(members(made, q="ROBIN LA")) == ["Robin Lake"]
    assert names(members(made, q="rivers")) == ["Alex Rivers"]
    assert names(members(made, q="gray@harbour")) == ["Blake Gray"]
    assert names(members(made, q="+47 912 34 567")) == ["Blake Gray"]
    assert names(members(made, q="9123")) == ["Blake Gray"]
    assert names(members(made, q="K7")) == ["Robin Lake"]
    assert names(members(made, q="birch")) == ["Robin Lake"]
    assert names(members(made, q="h0101")) == ["Blake Gray"]
    assert members(made, q="nobody")["members"] == []
    # A percent sign or an underscore is the character itself.
    assert members(made, q="%")["members"] == []
    assert members(made, q="a_e")["members"] == []
    assert members(made, q="   ")["count"] == 4
    assert names(members(made, q="lake", status="disabled")) == []


def test_name_sorts_by_name_and_starts_ascending(made: made_up.MadeUp) -> None:
    for name in ("blake gray", "Casey Moon"):
        person(made, name)
    assert names(members(made)) == [
        "Alex Rivers",
        "blake gray",
        "Casey Moon",
        "Sam Field",
    ]
    assert names(members(made, sort="name", order="desc")) == [
        "Sam Field",
        "Casey Moon",
        "blake gray",
        "Alex Rivers",
    ]
    assert names(members(made, sort="name", order="asc")) == names(members(made))


def test_unit_sorts_by_the_first_unit(made: made_up.MadeUp) -> None:
    person(made, "Robin Lake", unit="A5")
    person(made, "Blake Gray", unit="Z9")
    assert names(members(made, sort="unit")) == [
        "Robin Lake",
        "Alex Rivers",
        "Blake Gray",
        "Sam Field",
    ]
    # A member with no unit is last either way.
    assert names(members(made, sort="unit", order="desc")) == [
        "Blake Gray",
        "Alex Rivers",
        "Robin Lake",
        "Sam Field",
    ]


def signed_in(who: Member, when: datetime | None) -> None:
    Member.objects.of(who.organization_id).filter(pk=who.pk).update(last_login_at=when)


def test_last_login_sorts_newest_first_and_never_is_last(made: made_up.MadeUp) -> None:
    robin = person(made, "Robin Lake")
    blake = person(made, "Blake Gray")
    signed_in(made.member, FIRST)
    signed_in(robin, FIRST + timedelta(days=2))
    signed_in(blake, FIRST + timedelta(days=1))
    found = members(made, sort="login")
    assert names(found) == ["Robin Lake", "Blake Gray", "Alex Rivers", "Sam Field"]
    assert [row["last_login"] for row in found["members"]] == [
        "2027-01-12, 09:00:00",
        "2027-01-11, 09:00:00",
        "2027-01-10, 09:00:00",
        None,
    ]
    signed_in(made.administrator, FIRST + timedelta(days=5))
    assert names(members(made, sort="login")) == [
        "Sam Field",
        "Robin Lake",
        "Blake Gray",
        "Alex Rivers",
    ]
    assert names(members(made, sort="login", order="asc")) == [
        "Alex Rivers",
        "Blake Gray",
        "Robin Lake",
        "Sam Field",
    ]
    # Never is the least login: first going up, last going down.
    signed_in(blake, None)
    assert names(members(made, sort="login", order="asc"))[0] == "Blake Gray"
    assert names(members(made, sort="login"))[-1] == "Blake Gray"


def test_the_last_login_outlives_the_login_attempts(made: made_up.MadeUp) -> None:
    signed_in(made.member, FIRST - timedelta(days=200))
    LoginAttempt.objects.of(made.organization).delete()
    (row,) = members(made, q="alex")["members"]
    assert row["last_login"] == "2026-06-24, 10:00:00"


def test_attempts_alone_are_no_last_login(made: made_up.MadeUp) -> None:
    login(made, made.member, "verify", "accepted", FIRST)
    (row,) = members(made, q="alex")["members"]
    assert row["last_login"] is None


def last_login(who: Member) -> datetime | None:
    return Member.objects.of(who.organization_id).get(pk=who.pk).last_login_at


def test_a_member_signs_in_with_the_code_and_only_an_accepted_code_counts(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        code = request_code(client, made.member)
        assert last_login(made.member) is None
        assert post(client, "/login/code/", code="000000").status_code == 400
        assert last_login(made.member) is None
        assert post(client, "/login/code/", code=code).status_code == 302
    assert last_login(made.member) is not None


def test_an_administrator_signs_in_with_the_authenticator_code(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        code = request_code(client, made.administrator)
        assert post(client, "/login/code/", code=code).status_code == 302
        # The login code alone does not finish an administrator's sign-in.
        assert last_login(made.administrator) is None
        assert get(client, "/login/authenticator/").status_code == 200
        device = Authenticator.objects.of(made.organization).get(
            member=made.administrator
        )
        assert post(client, "/login/authenticator/", code="000000").status_code == 400
        assert last_login(made.administrator) is None
        assert (
            post(client, "/login/authenticator/", code=token(device)).status_code == 302
        )
    assert last_login(made.administrator) is not None
    assert last_login(made.member) is None


def test_the_export_holds_the_last_login_and_erasure_clears_it(
    made: made_up.MadeUp,
) -> None:
    signed_in(made.member, FIRST)
    with application_login():
        export = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator, now=CODE_TIME),
        )
    assert export["member"]["last_login_at"] == "2027-01-10T08:00:00Z"
    erase_identity(made.member)
    assert last_login(made.member) is None


def test_the_list_is_one_page_at_a_time_with_no_all(made: made_up.MadeUp) -> None:
    for number in range(28):
        person(made, f"Person {number:02d}")
    first = members(made)
    assert (first["count"], first["pages"], len(first["members"])) == (30, 2, 25)
    second = members(made, page=2)
    assert len(second["members"]) == 5
    assert not set(names(first)) & set(names(second))
    assert members(made, page=3)["members"] == []
    for bad in ("all", "0", "-1", "1.5", "", "9999999999", "1 2"):
        response = admin_views.members(request_for(made, LIST, {"page": bad}))
        assert response.status_code == 400
    for bad in ("all", "0", "25", "100"):
        asked = admin_views.members(request_for(made, LIST, {"per_page": bad}))
        assert answer(asked)["per_page"] == 25
        assert len(answer(asked)["members"]) == 25
    for bad in ("all", "-1"):
        asked = admin_views.members(request_for(made, LIST, {"limit": bad}))
        assert len(answer(asked)["members"]) == 25


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "gone"},
        {"status": ""},
        {"role": "x"},
        {"role": "0"},
        {"role": "-3"},
        {"role": "1.5"},
        {"sort": "email"},
        {"sort": ""},
        {"order": "newest"},
        {"order": ""},
        {"q": "x" * 101},
        {"page": "x"},
    ],
)
def test_a_malformed_filter_answers_400(
    made: made_up.MadeUp, bad: dict[str, str]
) -> None:
    response = admin_views.members(request_for(made, LIST, bad))
    assert response.status_code == 400
    assert json.loads(response.content) == {"error": "bad-request"}


def test_showing_a_page_of_the_list_is_one_look_up_each(
    made: made_up.MadeUp,
) -> None:
    before = AuditEvent.objects.of(made.organization).count()
    members(made)
    members(made, q="alex", sort="login")
    added = AuditEvent.objects.of(made.organization).order_by("pk")[before:]
    assert [(e.action, e.object_kind, e.object_id) for e in added] == [
        (AuditAction.LISTED_MEMBERS, "organization", made.organization.pk)
    ] * 2


def test_the_number_of_queries_does_not_grow_with_the_rows(
    made: made_up.MadeUp,
) -> None:
    def count() -> int:
        with CaptureQueriesContext(connection) as seen:
            assert admin_views.members(request_for(made, LIST, {})).status_code == 200
        return len(seen)

    few = count()
    for number in range(20):
        found = person(made, f"Person {number:02d}", unit=f"U{number}")
        login(made, found, "verify", "accepted", FIRST)
    assert count() == few


# --- A member's page ------------------------------------------------------------


def test_the_page_holds_its_sections(made: made_up.MadeUp) -> None:
    made.membership.address = "2 Elm Street"
    made.membership.address_unit = "H0201"
    made.membership.section = "Entrance A"
    made.membership.postal_code = "4321"
    made.membership.city = "Northbridge"
    made.membership.save()
    found = one(made, made.member)
    assert {key: found[key] for key in ("id", "name", "first_name", "last_name")} == {
        "id": made.member.pk,
        "name": "Alex Rivers",
        "first_name": "Alex",
        "last_name": "Rivers",
    }
    assert found["email"] == "alex@harbourview.example"
    assert found["phone"] == "+4790000001" and found["role"] == "Standard"
    assert (found["status"], found["status_label"], found["own"]) == (
        "active",
        "Active",
        False,
    )
    assert found["units"] == [
        {
            "identifier": "H0201",
            "address": "2 Elm Street",
            "flat": "H0201",
            "section": "Entrance A",
            "postal_code": "4321",
            "city": "Northbridge",
        }
    ]
    assert set(found) == {
        "id",
        "name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "role",
        "role_id",
        "roles",
        "status",
        "status_label",
        "own",
        "administrator",
        "origin",
        "units",
        "history",
        "logins",
    }


def test_the_page_knows_whose_it_is(made: made_up.MadeUp) -> None:
    assert one(made, made.administrator)["own"] is True


def test_where_the_member_came_from(made: made_up.MadeUp) -> None:
    first = MemberImport.objects.of(made.organization).get()
    second = MemberImport.objects.create(
        organization=made.organization,
        parser="made-up-members",
        outcome=ImportOutcome.APPLIED,
    )
    robin = person(made, "Robin Lake")
    event = AccessEvent.objects.create(
        organization=made.organization,
        member=robin,
        change=MemberStatus.ACTIVE,
        cause=AccessCause.MEMBER_IMPORT,
        member_import=second,
        at=FIRST,
    )
    assert event.pk and first.pk < second.pk
    assert one(made, robin)["origin"] == {
        "kind": "import",
        "import": 2,
        "at": "2027-01-10, 09:00:00",
    }
    # Sam was set up by the operator; no access event holds a date.
    assert one(made, made.administrator)["origin"] == {
        "kind": "operator",
        "import": None,
        "at": None,
    }
    # The fixture's access event names no import.
    assert one(made, made.member)["origin"]["kind"] == "import"
    assert one(made, made.member)["origin"]["import"] is None


def test_a_member_with_two_units_lists_both(made: made_up.MadeUp) -> None:
    second = Membership.objects.create(
        organization=made.organization, identifier="H0101", address="4 Elm Street"
    )
    MemberMembership.objects.create(
        organization=made.organization, member=made.member, membership=second
    )
    found = one(made, made.member)
    assert [unit["identifier"] for unit in found["units"]] == ["H0101", "H0201"]
    (row,) = members(made, q="alex")["members"]
    assert row["units"] == ["H0101", "H0201"]
    assert one(made, made.administrator)["units"] == []


def test_the_status_history_runs_oldest_first_with_its_causes(
    made: made_up.MadeUp,
) -> None:
    robin = person(made, "Robin Lake")
    imported = MemberImport.objects.of(made.organization).get()

    def access(
        change: str,
        cause: AccessCause,
        days: int,
        member_import: MemberImport | None = None,
        administrator: Member | None = None,
    ) -> None:
        AccessEvent.objects.create(
            organization=made.organization,
            member=robin,
            change=change,
            cause=cause,
            at=FIRST + timedelta(days=days),
            member_import=member_import,
            administrator=administrator,
        )

    access("active", AccessCause.MEMBER_IMPORT, 0, member_import=imported)
    admin = made.administrator
    access("blocked", AccessCause.ADMINISTRATOR, 1, administrator=admin)
    access("active", AccessCause.ADMINISTRATOR, 2, administrator=admin)
    access("disabled", AccessCause.MEMBER_IMPORT, 3, member_import=imported)
    assert one(made, robin)["history"] == [
        {
            "at": "2027-01-10, 09:00:00",
            "status": "active",
            "status_label": "Active",
            "cause": "Member import 1",
        },
        {
            "at": "2027-01-11, 09:00:00",
            "status": "blocked",
            "status_label": "Blocked",
            "cause": "Sam Field",
        },
        {
            "at": "2027-01-12, 09:00:00",
            "status": "active",
            "status_label": "Active",
            "cause": "Unblocked by Sam Field",
        },
        {
            "at": "2027-01-13, 09:00:00",
            "status": "disabled",
            "status_label": "Disabled",
            "cause": "Member import 1: not in the file",
        },
    ]
    assert one(made, made.administrator)["history"] == []


def test_the_login_history_is_newest_first_in_the_drawings_words(
    made: made_up.MadeUp,
) -> None:
    assert one(made, made.administrator)["logins"] == []
    elsewhere = made_up.everything("elsewhere")
    login(elsewhere, elsewhere.member, "verify", "accepted", FIRST)
    for minutes, kind, outcome in (
        (0, "request", "issued"),
        (1, "verify", "refused"),
        (2, "verify", "accepted"),
        (3, "mfa", "accepted"),
        (4, "request", "limited"),
    ):
        login(
            made, made.administrator, kind, outcome, FIRST + timedelta(minutes=minutes)
        )
    shown = [
        (row["at"], row["kind_label"], row["outcome_label"])
        for row in one(made, made.administrator)["logins"]
    ]
    assert shown == [
        ("2027-01-10, 09:04:00", "Asked for a login code", "Too many tries"),
        ("2027-01-10, 09:03:00", "Entered the authenticator code", "Accepted"),
        ("2027-01-10, 09:02:00", "Entered the login code", "Accepted"),
        ("2027-01-10, 09:01:00", "Entered the login code", "Wrong or expired code"),
        ("2027-01-10, 09:00:00", "Asked for a login code", "Code sent"),
    ]
    newest = one(made, made.administrator)["logins"][0]
    assert newest["kind"] == "request"
    # Only this member's: the made-up member holds the one attempt of the fixture.
    assert len(one(made, made.member)["logins"]) == 1


def test_no_network_address_is_ever_answered(made: made_up.MadeUp) -> None:
    login(made, made.member, "verify", "accepted", FIRST)
    login(made, made.administrator, "mfa", "accepted", FIRST)
    texts = [
        json.dumps(members(made)),
        json.dumps(members(made, sort="login", q="alex")),
        json.dumps(one(made, made.member)),
        json.dumps(one(made, made.administrator)),
        send(made, "block", made.member.pk).content.decode(),
    ]
    for text in texts:
        assert NETWORK not in text and "192.0.2." not in text
        assert "network" not in text and "address_" not in text


def test_opening_a_page_writes_one_inspected_event(made: made_up.MadeUp) -> None:
    assert events(made, AuditAction.INSPECT) == []
    one(made, made.member)
    (event,) = events(made, AuditAction.INSPECT)
    assert (event.object_kind, event.object_id) == ("member", made.member.pk)
    assert event.administrator_id == made.administrator.pk
    one(made, made.member)
    one(made, made.administrator)
    assert len(events(made, AuditAction.INSPECT)) == 3
    members(made)
    assert len(events(made, AuditAction.INSPECT)) == 3


def test_another_organizations_member_is_not_found(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    for who in (elsewhere.member.pk, 10**9, 2**62, 2**62 + 1, 2**70):
        path = f"/admin/members/{who}/data/"
        response = admin_views.member_data(request_for(made, path, {}), who)
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "not-found"}
    assert not events(made, AuditAction.INSPECT)


def test_a_members_bookings_are_the_bookings_asked_with_the_member(
    made: made_up.MadeUp,
) -> None:
    robin = person(made, "Robin Lake")
    ask = request_for(made, "/admin/bookings/list/", {"member": made.member.pk})
    already = [row["number"] for row in answer(admin_views.bookings(ask))["bookings"]]
    assert already
    mine = book(made, order_of(made), day=5).order
    theirs = book(made, order_of(made, member=robin), day=6).order
    found = answer(admin_views.bookings(ask))
    assert [row["number"] for row in found["bookings"]] == [mine.number, *already]
    assert found["count"] == len(already) + 1
    assert theirs.number not in [row["number"] for row in found["bookings"]]
    none = request_for(made, "/admin/bookings/list/", {"member": made.administrator.pk})
    assert answer(admin_views.bookings(none))["bookings"] == []
    for bad in ("x", "0", "-1", "1.5"):
        asked = request_for(made, "/admin/bookings/list/", {"member": bad})
        assert admin_views.bookings(asked).status_code == 400
    elsewhere = made_up.everything("elsewhere")
    other = request_for(made, "/admin/bookings/list/", {"member": elsewhere.member.pk})
    assert answer(admin_views.bookings(other))["bookings"] == []


# --- Block and Unblock -----------------------------------------------------------


def test_block_then_unblock_changes_the_state_and_the_history(
    made: made_up.MadeUp,
) -> None:
    found = answer(send(made, "block", made.member.pk))
    assert (found["status"], found["status_label"]) == ("blocked", "Blocked")
    assert [(row["status"], row["cause"]) for row in found["history"]][1:] == [
        ("blocked", "Sam Field")
    ]
    made.member.refresh_from_db()
    assert made.member.status == "blocked"
    (event,) = events(made, AuditAction.BLOCK_MEMBER)
    assert (event.object_id, event.administrator_id) == (
        made.member.pk,
        made.administrator.pk,
    )
    # The answer is the page after the change: it is no look-up.
    assert not events(made, AuditAction.INSPECT)

    found = answer(send(made, "unblock", made.member.pk))
    assert found["status"] == "active"
    assert [(row["status"], row["cause"]) for row in found["history"]][1:] == [
        ("blocked", "Sam Field"),
        ("active", "Unblocked by Sam Field"),
    ]
    assert len(events(made, AuditAction.UNBLOCK_MEMBER)) == 1
    assert (
        AccessEvent.objects.of(made.organization).filter(member=made.member).count()
        == 3
    )


def test_block_ends_unused_login_codes(made: made_up.MadeUp) -> None:
    answer(send(made, "block", made.member.pk))
    assert (
        not LoginCode.objects.of(made.organization)
        .filter(member=made.member, used_at__isnull=True)
        .exists()
    )


def test_a_second_press_acts_once(made: made_up.MadeUp) -> None:
    answer(send(made, "block", made.member.pk))
    again = send(made, "block", made.member.pk)
    assert again.status_code == 409
    assert json.loads(again.content) == {"error": "already-blocked"}
    assert len(events(made, AuditAction.BLOCK_MEMBER)) == 1
    answer(send(made, "unblock", made.member.pk))
    again = send(made, "unblock", made.member.pk)
    assert again.status_code == 409
    assert json.loads(again.content) == {"error": "not-blocked"}
    assert len(events(made, AuditAction.UNBLOCK_MEMBER)) == 1
    assert (
        AccessEvent.objects.of(made.organization).filter(member=made.member).count()
        == 3
    )


def test_an_administrator_cannot_block_themselves(made: made_up.MadeUp) -> None:
    refused = send(made, "block", made.administrator.pk)
    assert refused.status_code == 409
    assert json.loads(refused.content) == {"error": "self"}
    made.administrator.refresh_from_db()
    assert made.administrator.status == "active"
    assert not events(made, AuditAction.BLOCK_MEMBER)
    assert (
        not AccessEvent.objects.of(made.organization)
        .filter(member=made.administrator)
        .exists()
    )


def test_another_administrator_can_be_blocked_and_unblocked(
    made: made_up.MadeUp,
) -> None:
    second = person(made, "Jo Admin", role=made.board, origin=Origin.ADMINISTRATOR)
    found = answer(send(made, "block", second.pk))
    assert found["status"] == "blocked"
    answer(send(made, "unblock", second.pk))
    # Blocking the other way round, by the blocked one, is refused at the gate.
    second.refresh_from_db()
    assert second.status == "active"


def test_a_disabled_member_can_be_blocked_but_not_unblocked(
    made: made_up.MadeUp,
) -> None:
    gone = person(made, "Blake Gray", status=MemberStatus.DISABLED)
    refused = send(made, "unblock", gone.pk)
    assert refused.status_code == 409
    assert json.loads(refused.content) == {"error": "not-blocked"}
    gone.refresh_from_db()
    assert gone.status == "disabled"
    found = answer(send(made, "block", gone.pk))
    assert found["status"] == "blocked"
    # Unblock goes back to what the member list says: not listed, so disabled.
    Member.objects.of(made.organization).filter(pk=gone.pk).update(listed=False)
    assert answer(send(made, "unblock", gone.pk))["status"] == "disabled"


def test_another_organizations_member_is_not_found_in_every_post(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    for act in ("block", "unblock"):
        for who in (elsewhere.member.pk, 10**9, 2**62 + 1):
            refused = send(made, act, who)
            assert refused.status_code == 404
            assert json.loads(refused.content) == {"error": "not-found"}
    elsewhere.member.refresh_from_db()
    assert elsewhere.member.status == "active"
    for org in (made.organization, elsewhere.organization):
        assert not events_of(org, AuditAction.BLOCK_MEMBER)


@pytest.mark.parametrize(
    "bad", [b"not json", b"[]", b'{"x": 1}', b'"block"', b"\xff", b"[" * 100000]
)
def test_a_body_other_than_empty_answers_400(made: made_up.MadeUp, bad: bytes) -> None:
    for act in ("block", "unblock"):
        assert send(made, act, made.member.pk, bad).status_code == 400
    made.member.refresh_from_db()
    assert made.member.status == "active"
    assert not events(made, AuditAction.BLOCK_MEMBER)


def test_an_empty_body_is_as_good_as_an_empty_object(made: made_up.MadeUp) -> None:
    assert answer(send(made, "block", made.member.pk, b""))["status"] == "blocked"


def test_a_blocked_member_is_listed_blocked(made: made_up.MadeUp) -> None:
    answer(send(made, "block", made.member.pk))
    assert names(members(made, status="blocked")) == ["Alex Rivers"]
