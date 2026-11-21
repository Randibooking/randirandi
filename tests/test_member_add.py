# SPDX-License-Identifier: AGPL-3.0-only
"""An administrator adds a member : the gate, CSRF, the fresh
authenticator code, every refusal, the member and what is written with it, a
concurrent add of one email, and that export and erasure follow the audit
event."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from unittest import mock

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections
from django.http import HttpResponse
from django.test import Client, RequestFactory

from randirandi.core import admin_views
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import (
    add_member,
    export_member,
    operator_erase_member,
)
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    AuditEvent,
    Authenticator,
    LoginAttempt,
    Member,
    MemberMembership,
    MemberStatus,
    Origin,
    SecurityNotice,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.test_admin_bookings import answer, request_for
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    NOW,
    get,
    page_settings,
)

pytestmark = pytest.mark.django_db(transaction=True)
ADD = "/admin/members/add/"
NEW = {
    "full_name": "Robin Tester",
    "email": "robin.tester@testers.example",
    "phone": "+47 900 00 123",
}


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def add(
    made: made_up.MadeUp,
    body: Any,
    *,
    by: Member | None = None,
    confirmed: bool = True,
    code: bool = True,
    role: bool = True,
) -> HttpResponse:
    """Post to the add call; `body` gets the standard role and a current code
    unless it is bytes or the flag is off."""
    sender = by or made.administrator
    if isinstance(body, dict):
        if role and "role" not in body:
            body = {**body, "role": made.standard.pk}
        if code and "code" not in body:
            body = {**body, "code": fresh(sender)["code"]}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = RequestFactory().post(
        ADD,
        raw,
        content_type="application/json",
        HTTP_HOST=HOST,
        secure=True,
        REMOTE_ADDR="192.0.2.77",
    )
    request.session = SessionStore()
    request.session["admin_confirmed"] = confirmed
    request.user = sender
    return admin_views.member_add(request)


def created(response: HttpResponse) -> dict[str, Any]:
    assert response.status_code == 201
    found = json.loads(response.content)
    assert isinstance(found, dict)
    return found


def refusal(response: HttpResponse, status: int = 409) -> str:
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    return str(json.loads(response.content)["error"])


def count(made: made_up.MadeUp) -> int:
    return Member.objects.of(made.organization).count()


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.of(made.organization).filter(action=action))


def attempts(made: made_up.MadeUp) -> int:
    return LoginAttempt.objects.of(made.organization).filter(kind="mfa").count()


# --- The gate -----------------------------------------------------------------------


def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    before = count(made)
    for who, confirmed in (
        (made.administrator, False),
        (made.member, True),
        (elsewhere.administrator, True),
    ):
        with pytest.raises(PermissionDenied):
            add(made, NEW, by=who, confirmed=confirmed)
    assert count(made) == before
    assert not attempts(made) and not events(made, AuditAction.ADD_MEMBER)


def test_a_get_is_not_taken(made: made_up.MadeUp) -> None:
    request = RequestFactory().get(ADD, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = made.administrator
    assert admin_views.member_add(request).status_code == 405


def test_a_signed_out_browser_goes_to_sign_in() -> None:
    response = Client().post(ADD, HTTP_HOST=HOST, secure=True)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_asked_for(made: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    with (
        application_login(),
        mock.patch("randirandi.core.login_session_views.utc_now", return_value=NOW),
        mock.patch("randirandi.core.authenticator_views.utc_now", return_value=NOW),
        mock.patch("randirandi.core.second_factor.utc_now", return_value=NOW),
    ):
        sign_in_administrator(client, made.administrator)
        token = str(config(get(client, "/admin/members/").content.decode())["csrf"])
        body = json.dumps(
            {
                **NEW,
                "role": made.standard.pk,
                "code": fresh(made.administrator, now=NOW)["code"],
            }
        )

        def post_it(headers: dict[str, Any]) -> HttpResponse:
            response = client.post(
                ADD,
                body,
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
        assert (
            not Member.objects.of(made.organization).filter(email=NEW["email"]).exists()
        )
        right = {"HTTP_X_CSRFTOKEN": token, "HTTP_ORIGIN": "https://" + HOST}
        response = post_it(right)
    assert response.status_code == 201
    assert created(response)["email"] == NEW["email"]


# --- The add ------------------------------------------------------------------------


def test_the_add_makes_an_active_member_added_by_an_administrator(
    made: made_up.MadeUp,
) -> None:
    response = add(made, NEW)
    assert response.status_code == 201
    page = json.loads(response.content)
    found = Member.objects.of(made.organization).get(pk=page["id"])
    assert (found.status, found.origin) == (
        MemberStatus.ACTIVE,
        Origin.ADMINISTRATOR,
    )
    assert (found.full_name, found.first_name, found.last_name) == (
        "Robin Tester",
        "Robin",
        "Tester",
    )
    assert (found.email, found.phone) == (NEW["email"], "+4790000123")
    assert found.role_id == made.standard.pk and not found.is_staff
    assert found.last_login_at is None
    # Booking reads no membership: none is made.
    assert not MemberMembership.objects.of(made.organization).filter(member=found)
    assert (page["name"], page["role"], page["status"]) == (
        "Robin Tester",
        "Standard",
        "active",
    )
    assert page["origin"]["kind"] == "administrator"
    assert page["history"][0]["cause"] == "Added by Sam Field"
    (access,) = AccessEvent.objects.of(made.organization).filter(member=found)
    assert (access.change, access.cause, access.administrator_id) == (
        MemberStatus.ACTIVE,
        AccessCause.ADMINISTRATOR,
        made.administrator.pk,
    )
    (audit,) = events(made, AuditAction.ADD_MEMBER)
    assert (audit.administrator_id, audit.object_kind, audit.object_id) == (
        made.administrator.pk,
        "member",
        found.pk,
    )
    # No admin access, so no notice about a gain of it.
    assert not events(made, AuditAction.GAVE_ADMIN_ACCESS)


def test_the_email_is_lower_case_and_the_phone_is_optional(
    made: made_up.MadeUp,
) -> None:
    page = created(
        add(made, {"full_name": "Kim", "email": " Kim.Lake@Testers.Example "})
    )
    assert (page["email"], page["phone"], page["first_name"], page["last_name"]) == (
        "kim.lake@testers.example",
        "",
        "Kim",
        "",
    )
    blank = created(
        add(made, {"full_name": "Lou", "email": "lou@testers.example", "phone": ""})
    )
    assert blank["phone"] == ""


def test_a_role_with_admin_access_is_a_gain_of_it(made: made_up.MadeUp) -> None:
    response = add(made, {**NEW, "role": made.board.pk})
    assert response.status_code == 201
    found = Member.objects.of(made.organization).get(email=NEW["email"])
    assert found.is_staff
    (gave,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert (gave.object_kind, gave.object_id) == ("member", found.pk)
    assert SecurityNotice.objects.of(made.organization).filter(event=gave).exists()


def test_the_list_names_the_standard_role_the_sheet_starts_on(
    made: made_up.MadeUp,
) -> None:
    found = answer(admin_views.members(request_for(made, "/admin/members/list/", {})))
    assert found["standard_role"] == made.standard.pk
    assert {role["value"] for role in found["roles"]} >= {made.standard.pk}


REFUSED = [
    ({"full_name": ""}, "invalid-name"),
    ({"full_name": "   "}, "invalid-name"),
    ({"full_name": "x" * 400}, "invalid-name"),
    ({"full_name": "A" + " " + "y" * 200}, "invalid-name"),
    ({"email": ""}, "invalid-email"),
    ({"email": "not an address"}, "invalid-email"),
    ({"email": "a" * 250 + "@testers.example"}, "invalid-email"),
    ({"phone": "12"}, "invalid-phone"),
    ({"phone": "0047900"}, "invalid-phone"),
    ({"phone": "+47abc"}, "invalid-phone"),
    ({"phone": "+4790000002"}, "phone-taken"),
    ({"phone": "+47 900 00 002"}, "phone-taken"),
    ({"email": "ALEX@harbourview.example"}, "email-taken"),
    ({"email": "sam@harbourview.example"}, "email-taken"),
    ({"role": 0}, "unknown-role"),
    ({"role": 2**40}, "unknown-role"),
]


@pytest.mark.parametrize(("change", "reason"), REFUSED)
def test_a_refusal_names_the_fields_reason_and_writes_nothing(
    made: made_up.MadeUp, change: dict[str, Any], reason: str
) -> None:
    before = count(made)
    assert refusal(add(made, {**NEW, **change})) == reason
    assert count(made) == before
    assert not events(made, AuditAction.ADD_MEMBER)
    assert not AccessEvent.objects.of(made.organization).filter(
        cause=AccessCause.ADMINISTRATOR
    )
    # A refused value rolls the whole call back, the code's use with it.
    assert attempts(made) == 0


def test_another_organizations_role_is_refused(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    before = count(made)
    for role in (elsewhere.standard, elsewhere.board):
        assert refusal(add(made, {**NEW, "role": role.pk})) == "unknown-role"
    assert count(made) == before
    assert not Member.objects.of(elsewhere.organization).filter(email=NEW["email"])


def test_the_same_email_in_another_organization_is_no_conflict(
    made: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    Member.objects.of(elsewhere.organization).filter(pk=elsewhere.member.pk).update(
        email=NEW["email"], phone="+4790000123"
    )
    assert add(made, NEW).status_code == 201
    assert Member.objects.of(made.organization).filter(email=NEW["email"]).count() == 1


def test_an_add_without_the_code_or_with_a_wrong_one_adds_nobody(
    made: made_up.MadeUp,
) -> None:
    before = count(made)
    assert refusal(add(made, NEW, code=False), 400) == "bad-request"
    assert attempts(made) == 0
    assert refusal(add(made, {**NEW, "code": "000000"})) == "code-refused"
    assert refusal(add(made, {**NEW, "code": ""})) == "code-refused"
    assert count(made) == before
    assert not events(made, AuditAction.ADD_MEMBER)


def test_too_many_wrong_codes_limit_the_add(made: made_up.MadeUp) -> None:
    for _ in range(5):
        assert refusal(add(made, {**NEW, "code": "000000"})) == "code-refused"
    # The sixth is limited even with a right code.
    assert refusal(add(made, NEW)) == "code-limited"
    assert not Member.objects.of(made.organization).filter(email=NEW["email"])


def test_an_administrator_without_a_confirmed_device_adds_nobody(
    made: made_up.MadeUp,
) -> None:
    body = {**NEW, "role": made.standard.pk, "code": fresh(made.administrator)["code"]}
    Authenticator.objects.of(made.organization).filter(
        member=made.administrator
    ).update(confirmed=False)
    assert refusal(add(made, body)) == "code-refused"
    assert not Member.objects.of(made.organization).filter(email=NEW["email"])


BAD_BODIES: list[Any] = [
    b"not json",
    b"[]",
    b'{"code": 123456}',
    {"code": "000000"},
    {"full_name": "A", "email": "a@b.example", "code": "000000"},
    {**NEW, "code": "000000", "role": "1"},
    {**NEW, "code": "000000", "role": True},
    {**NEW, "code": "000000", "role": 1.5},
    {**NEW, "code": "000000", "full_name": 7},
    {**NEW, "code": "000000", "email": None},
    {**NEW, "code": "000000", "phone": None},
    {**NEW, "code": "000000", "origin": "member import"},
    {**NEW, "code": "000000", "status": "blocked"},
]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_a_wrong_body_is_a_bad_request_and_tries_no_code(
    made: made_up.MadeUp, body: Any
) -> None:
    before = count(made)
    assert refusal(add(made, body, role=False, code=False), 400) == "bad-request"
    assert count(made) == before and attempts(made) == 0


def test_a_failure_after_the_member_is_written_rolls_everything_back(
    made: made_up.MadeUp,
) -> None:
    before = count(made)
    with (
        application_login(),
        mock.patch(
            "randirandi.core.member_data.record_event",
            side_effect=RuntimeError("audit unavailable"),
        ),
        pytest.raises(RuntimeError),
    ):
        add_member(
            actor=made.administrator,
            role_id=made.standard.pk,
            full_name=NEW["full_name"],
            email=NEW["email"],
            phone=NEW["phone"],
            code=fresh(made.administrator)["code"],
            network=fresh(made.administrator)["network"],
        )
    assert count(made) == before
    assert not AccessEvent.objects.of(made.organization).filter(
        cause=AccessCause.ADMINISTRATOR
    )


def test_two_administrators_adding_one_email_at_once_end_in_one_member(
    made: made_up.MadeUp,
) -> None:
    second = Member.objects.create(
        organization=made.organization,
        role=made.board,
        full_name="Jo Admin",
        first_name="Jo",
        last_name="Admin",
        email="jo@harbourview.example",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )
    codes = {who.pk: fresh(who) for who in (made.administrator, second)}
    barrier = Barrier(2)

    def work(who: Member) -> tuple[str, str | None]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                add_member(
                    actor=who,
                    role_id=made.standard.pk,
                    full_name=NEW["full_name"],
                    email=NEW["email"],
                    phone="",
                    code=codes[who.pk]["code"],
                    network=codes[who.pk]["network"],
                )
            except ValidationError as error:
                return "refused", error.code
            return "added", None
        finally:
            connections.close_all()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(work, (made.administrator, second)))
    assert results == [("added", None), ("refused", "email-taken")]
    assert Member.objects.of(made.organization).filter(email=NEW["email"]).count() == 1
    assert len(events(made, AuditAction.ADD_MEMBER)) == 1


# --- Export and erasure ----------------------------------------------------


def test_export_follows_the_audit_event_for_the_member_and_the_administrator(
    made: made_up.MadeUp,
) -> None:
    page = created(add(made, NEW))
    (audit,) = events(made, AuditAction.ADD_MEMBER)
    with application_login():
        theirs = export_member(
            actor=made.administrator, member_id=page["id"], **fresh(made.administrator)
        )
        mine = export_member(
            actor=made.administrator,
            member_id=made.administrator.pk,
            **fresh(made.administrator),
        )
    row = next(r for r in theirs["audit_events"] if r["action"] == "added member")
    assert (row["id"], row["object_kind"], row["object_id"]) == (
        audit.pk,
        "member",
        page["id"],
    )
    assert row["administrator_id"] == made.administrator.pk
    assert theirs["member"]["origin"] == "added by administrator"
    assert theirs["member"]["email"] == NEW["email"]
    assert theirs["memberships"] == []
    assert any(r["id"] == audit.pk for r in mine["audit_events"])
    # Another member's export does not carry it.
    with application_login():
        other = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    assert all(r["id"] != audit.pk for r in other["audit_events"])


def test_erasure_clears_the_members_details_and_keeps_the_audit_event(
    made: made_up.MadeUp,
) -> None:
    page = created(add(made, NEW))
    with application_login():
        operator_erase_member(organization=made.organization, member_id=page["id"])
    erased = Member.objects.of(made.organization).get(pk=page["id"])
    assert (erased.email, erased.phone, erased.full_name) == ("", "", "")
    assert erased.status == MemberStatus.DISABLED
    # The record of who did what stays, without a name or an address in it.
    (audit,) = events(made, AuditAction.ADD_MEMBER)
    assert audit.object_id == page["id"]
    assert not AccessEvent.objects.of(made.organization).filter(member=erased)
