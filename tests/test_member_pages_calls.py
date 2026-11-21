# SPDX-License-Identifier: AGPL-3.0-only
"""Correct a member, change their email, export their data : the
three calls of the member page, the role rules, the notice to the old address,
the export, and the audit and export records of what they add."""

import json
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail.backends.locmem import EmailBackend
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils import timezone

from randirandi.core import admin_views, member_data
from randirandi.core.audit_types import AuditAction
from randirandi.core.login_identity import identifier_digest
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    AuditEvent,
    LoginAttempt,
    LoginCode,
    LoginRequest,
    Member,
    MemberStatus,
    SecurityNotice,
)
from randirandi.core.security_notices import send_security_notices
from tests import made_up
from tests.fresh_code import fresh
from tests.test_login_sessions import HOST, page_settings  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
VIEWS: dict[str, Callable[..., HttpResponse]] = {
    "correct": admin_views.member_correct,
    "email": admin_views.member_email,
    "export": admin_views.member_export,
    "reset": admin_views.member_reset,
}


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def call(
    made: made_up.MadeUp,
    act: str,
    who: int,
    body: Any,
    *,
    by: Member | None = None,
    confirmed: bool = True,
    code: bool = True,
) -> HttpResponse:
    """Post to one of the three calls; `body` gets a current code unless it is
    bytes or `code` is false."""
    sender = by or made.administrator
    if isinstance(body, dict) and code and "code" not in body:
        body = {**body, "code": fresh(sender)["code"]}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = RequestFactory().post(
        f"/admin/members/{who}/{act}/",
        raw,
        content_type="application/json",
        HTTP_HOST=HOST,
        secure=True,
        REMOTE_ADDR="192.0.2.77",
    )
    request.session = SessionStore()
    request.session["admin_confirmed"] = confirmed
    request.user = sender
    return VIEWS[act](request, who)


def refusal(response: HttpResponse, status: int = 409) -> str:
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    return str(json.loads(response.content)["error"])


def events(made: made_up.MadeUp, action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.of(made.organization).filter(action=action))


def reload(member: Member) -> Member:
    return (
        Member.objects.of(member.organization_id)
        .select_related("role")
        .get(pk=member.pk)
    )


def audits(made: made_up.MadeUp) -> int:
    return AuditEvent.objects.of(made.organization).count()


def notices(made: made_up.MadeUp) -> int:
    return SecurityNotice.objects.of(made.organization).count()


def attempts(made: made_up.MadeUp) -> int:
    return LoginAttempt.objects.of(made.organization).filter(kind="mfa").count()


# --- The gate and the methods -----------------------------------------------------


@pytest.mark.parametrize("act", sorted(VIEWS))
def test_only_a_confirmed_administrator_of_this_organization_may(
    made: made_up.MadeUp, act: str
) -> None:
    elsewhere = made_up.everything("elsewhere")
    body = {"correct": {"full_name": "Al"}, "email": {"email": "a@b.example"}}.get(
        act, {}
    )
    for who, confirmed in (
        (made.administrator, False),
        (made.member, True),
        (elsewhere.administrator, True),
    ):
        with pytest.raises(PermissionDenied):
            call(made, act, made.member.pk, body, by=who, confirmed=confirmed)
    assert reload(made.member).email == made.member.email
    assert not attempts(made)


@pytest.mark.parametrize("act", sorted(VIEWS))
def test_a_get_is_not_taken(made: made_up.MadeUp, act: str) -> None:
    request = RequestFactory().get(
        f"/admin/members/1/{act}/", HTTP_HOST=HOST, secure=True
    )
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = made.administrator
    assert VIEWS[act](request, 1).status_code == 405


@pytest.mark.parametrize("act", sorted(VIEWS))
def test_another_organizations_member_is_a_404_and_nothing_is_tried(
    made: made_up.MadeUp, act: str
) -> None:
    elsewhere = made_up.everything("elsewhere")
    body = {"correct": {"full_name": "Al"}, "email": {"email": "a@b.example"}}.get(
        act, {}
    )
    assert refusal(call(made, act, elsewhere.member.pk, body), 404) == "not-found"
    assert refusal(call(made, act, 0, body), 404) == "not-found"
    assert refusal(call(made, act, 2**63, body), 404) == "not-found"
    assert reload(elsewhere.member).email == elsewhere.member.email
    assert not attempts(made)
    assert not events(made, AuditAction.EXPORT_MEMBER)


BAD_BODIES: list[tuple[str, Any]] = [
    ("correct", b"not json"),
    ("correct", b"[]"),
    ("correct", b'{"code": 123456}'),
    ("correct", {"full_name": "Al"}),
    ("correct", {"code": "000000"}),
    ("correct", {"code": "000000", "full_name": 7}),
    ("correct", {"code": "000000", "phone": None}),
    ("correct", {"code": "000000", "role": "1"}),
    ("correct", {"code": "000000", "role": True}),
    ("correct", {"code": "000000", "full_name": "Al", "email": "a@b.example"}),
    ("email", b"not json"),
    ("email", b"[]"),
    ("email", {"code": "000000"}),
    ("email", {"email": "a@b.example"}),
    ("email", {"email": 5, "code": "000000"}),
    ("email", {"email": "a@b.example", "code": "000000", "x": 1}),
    ("export", b"not json"),
    ("export", {}),
    ("export", {"code": 5}),
    ("export", {"code": "000000", "x": 1}),
    ("reset", b"not json"),
    ("reset", b"[]"),
    ("reset", {}),
    ("reset", {"code": 5}),
    ("reset", {"code": "000000", "x": 1}),
]


@pytest.mark.parametrize(("act", "body"), BAD_BODIES)
def test_a_malformed_body_is_refused_before_the_code_is_judged(
    made: made_up.MadeUp, act: str, body: Any
) -> None:
    before = audits(made)
    response = call(made, act, made.member.pk, body, code=False)
    assert refusal(response, 400) == "bad-request"
    assert not attempts(made)
    assert audits(made) == before


# --- Correct ----------------------------------------------------------------------


def test_a_correction_changes_names_phone_and_role_and_answers_the_page(
    made: made_up.MadeUp,
) -> None:
    LoginCode.objects.of(made.organization).filter(member=made.member).update(
        used_at=None
    )
    with mock.patch.object(member_data, "gained_admin_access") as gained:
        response = call(
            made,
            "correct",
            made.member.pk,
            {
                "full_name": " Jo Smith Berg",
                "phone": "+47 900 00 099",
                "role": made.board.pk,
            },
        )
    assert response.status_code == 200
    page = json.loads(response.content)
    assert (page["name"], page["first_name"], page["last_name"]) == (
        "Jo Smith Berg",
        "Jo",
        "Smith Berg",
    )
    assert (page["phone"], page["role"], page["role_id"]) == (
        "+4790000099",
        "Board",
        made.board.pk,
    )
    assert [role["label"] for role in page["roles"]] == ["Board", "Standard"]
    changed = reload(made.member)
    assert changed.is_staff
    assert (
        not LoginCode.objects.of(made.organization).filter(member=made.member).exists()
    )
    (event,) = events(made, AuditAction.CORRECT_MEMBER)
    assert (event.object_kind, event.object_id) == ("member", made.member.pk)
    assert gained.call_count == 1
    assert gained.call_args.args[1:] == (made.administrator, changed)


def test_a_correction_may_leave_the_phone_and_clear_it(made: made_up.MadeUp) -> None:
    LoginCode.objects.of(made.organization).filter(member=made.member).update(
        used_at=None
    )
    same = call(made, "correct", made.member.pk, {"phone": made.member.phone})
    assert same.status_code == 200
    assert LoginCode.objects.of(made.organization).filter(member=made.member).exists()
    cleared = call(made, "correct", made.member.pk, {"phone": ""})
    assert json.loads(cleared.content)["phone"] == ""
    assert reload(made.member).phone == ""


def test_a_correction_does_not_notify_when_the_role_stays_or_loses_access(
    made: made_up.MadeUp,
) -> None:
    other = Member.objects.create(
        organization=made.organization,
        role=made.board,
        full_name="Kim Lake",
        first_name="Kim",
        last_name="Lake",
        email="kim@harbourview.example",
        status=MemberStatus.ACTIVE,
        origin=made.administrator.origin,
    )
    with mock.patch.object(member_data, "gained_admin_access") as gained:
        call(made, "correct", made.member.pk, {"role": made.standard.pk})
        demoted = call(made, "correct", other.pk, {"role": made.standard.pk})
    assert demoted.status_code == 200
    assert not reload(other).is_staff
    gained.assert_not_called()


REFUSED_CORRECTIONS = [
    ({"phone": "12"}, "invalid-phone"),
    ({"phone": "+4790000002"}, "phone-taken"),
    ({"full_name": "x" * 400}, "invalid-name"),
]


@pytest.mark.parametrize(("body", "code"), REFUSED_CORRECTIONS)
def test_a_refused_correction_names_its_code_and_writes_nothing(
    made: made_up.MadeUp, body: dict[str, Any], code: str
) -> None:
    assert refusal(call(made, "correct", made.member.pk, body)) == code
    assert reload(made.member).phone == made.member.phone
    assert reload(made.member).full_name == made.member.full_name
    assert not events(made, AuditAction.CORRECT_MEMBER)


def test_the_role_rules(made: made_up.MadeUp) -> None:
    elsewhere = made_up.everything("elsewhere")
    foreign = {"role": elsewhere.board.pk}
    assert refusal(call(made, "correct", made.member.pk, foreign)) == "unknown-role"
    assert refusal(call(made, "correct", made.member.pk, {"role": 0})) == "unknown-role"
    own = call(made, "correct", made.administrator.pk, {"role": made.standard.pk})
    assert refusal(own) == "own-role"
    assert reload(made.administrator).is_staff
    # Their own role as it is, with a corrected name, is no change of role.
    same = call(
        made,
        "correct",
        made.administrator.pk,
        {"role": made.board.pk, "full_name": "Sam Fields"},
    )
    assert same.status_code == 200


def test_the_last_active_administrator_keeps_admin_access(
    made: made_up.MadeUp,
) -> None:
    def demote(by: Member) -> None:
        member_data._change_role(
            made.organization, by, reload(made.administrator), made.standard.pk
        )

    with pytest.raises(ValidationError) as refused:
        demote(made.member)
    assert refused.value.code == "last-administrator"
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        role=made.board
    )
    demote(made.member)


def test_an_erased_member_cannot_be_corrected(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        email="", phone="", status=MemberStatus.DISABLED
    )
    assert refusal(call(made, "correct", made.member.pk, {"full_name": "Al"})) == (
        "erased"
    )


# --- The code ---------------------------------------------------------------------


@pytest.mark.parametrize("act", sorted(VIEWS))
def test_a_wrong_code_is_a_refusal_the_sheet_shows(
    made: made_up.MadeUp, act: str
) -> None:
    body = {"email": "new@harbourview.example", "full_name": "Al", "code": "000000"}
    allowed = {
        "correct": {"full_name", "code"},
        "email": {"email", "code"},
        "export": {"code"},
        "reset": {"code"},
    }[act]
    fresh(made.administrator)
    before = audits(made)
    sent = {key: value for key, value in body.items() if key in allowed}
    assert refusal(call(made, act, made.member.pk, sent)) == "code-refused"
    assert reload(made.member).email == made.member.email
    assert reload(made.member).full_name == made.member.full_name
    assert audits(made) == before
    assert (
        LoginAttempt.objects.of(made.organization).filter(outcome="refused").count()
        == 1
    )


@pytest.mark.parametrize("act", sorted(VIEWS))
def test_too_many_wrong_codes_limit_the_member_even_for_the_right_one(
    made: made_up.MadeUp, act: str
) -> None:
    fresh(made.administrator)
    before = audits(made)
    extra = {"email": "new@harbourview.example", "full_name": "Al"}
    keep = {"correct": "full_name", "email": "email"}.get(act)
    sent = {keep: extra[keep]} if keep else {}
    for _ in range(5):
        assert (
            refusal(call(made, act, made.member.pk, {**sent, "code": "000000"}))
            == "code-refused"
        )
    right = fresh(made.administrator)["code"]
    assert (
        refusal(call(made, act, made.member.pk, {**sent, "code": right}))
        == "code-limited"
    )
    assert audits(made) == before


def test_a_code_with_spaces_is_taken_and_a_used_code_is_not(
    made: made_up.MadeUp,
) -> None:
    code = fresh(made.administrator)["code"]
    spaced = f"{code[:3]} {code[3:]}"
    first = call(
        made, "correct", made.member.pk, {"full_name": "Al Rivers", "code": spaced}
    )
    assert first.status_code == 200
    again = call(
        made, "correct", made.member.pk, {"full_name": "Al Rivers", "code": code}
    )
    assert refusal(again) == "code-refused"


# --- Change email -----------------------------------------------------------------


def test_an_email_change_tells_the_old_address_and_drops_logins(
    made: made_up.MadeUp,
) -> None:
    old = made.member.email
    LoginCode.objects.of(made.organization).filter(member=made.member).update(
        used_at=None
    )
    waiting = LoginRequest.objects.create(
        organization=made.organization,
        identifier_digest=identifier_digest(made.organization.pk, old),
        browser_digest="b" * 64,
        challenge="c" * 32,
        network="192.0.2.5",
        at=timezone.now(),
    )
    before = reload(made.member).get_session_auth_hash()
    response = call(
        made, "email", made.member.pk, {"email": " New.Name@Harbourview.Example "}
    )
    assert response.status_code == 200
    page = json.loads(response.content)
    assert page["email"] == "new.name@harbourview.example"
    assert reload(made.member).get_session_auth_hash() != before
    assert (
        not LoginCode.objects.of(made.organization).filter(member=made.member).exists()
    )
    assert not LoginRequest.objects.of(made.organization).filter(pk=waiting.pk).exists()
    (event,) = events(made, AuditAction.CHANGE_MEMBER_EMAIL)
    assert (event.object_kind, event.object_id) == ("member", made.member.pk)
    assert event.administrator_id == made.administrator.pk
    (notice,) = (
        SecurityNotice.objects.of(made.organization)
        .filter(recipient=made.member)
        .exclude(address="")
    )
    assert (notice.address, notice.event_id, notice.delivered_at) == (
        old,
        event.pk,
        None,
    )

    mail.outbox.clear()
    send_security_notices()
    (message,) = [m for m in mail.outbox if m.to == [old]]
    assert message.subject == "Your RandiRandi email address was changed"
    assert "changed the email address on your RandiRandi account" in message.body
    assert "Sign-in codes" not in message.body
    assert f"Audit reference: {event.pk}." in message.body
    assert "new.name@" not in message.body and "new.name@" not in str(message.message())
    notice.refresh_from_db()
    assert notice.delivered_at is not None and notice.address == ""
    sent = len(mail.outbox)
    send_security_notices()
    assert len(mail.outbox) == sent


def test_an_unsent_notice_is_kept_until_the_old_address_is_told(
    made: made_up.MadeUp,
) -> None:
    old = made.member.email
    call(made, "email", made.member.pk, {"email": "new@harbourview.example"})
    with mock.patch.object(EmailBackend, "send_messages", side_effect=OSError("down")):
        send_security_notices()
    (notice,) = (
        SecurityNotice.objects.of(made.organization)
        .filter(recipient=made.member)
        .exclude(address="")
    )
    assert (notice.address, notice.delivered_at) == (old, None)


def test_the_change_applies_to_an_administrator_and_a_disabled_member(
    made: made_up.MadeUp,
) -> None:
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        status=MemberStatus.DISABLED
    )
    ok = call(made, "email", made.member.pk, {"email": "new@harbourview.example"})
    assert ok.status_code == 200
    own = call(
        made, "email", made.administrator.pk, {"email": "sam2@harbourview.example"}
    )
    assert own.status_code == 200
    mail.outbox.clear()
    send_security_notices()
    assert sorted(m.to[0] for m in mail.outbox) == [
        "alex@harbourview.example",
        "sam@harbourview.example",
    ]


REFUSED_EMAILS = [
    ("nope", "invalid-email"),
    ("a@b", "invalid-email"),
    ("", "invalid-email"),
    ("a" * 250 + "@harbourview.example", "invalid-email"),
    ("SAM@harbourview.example", "email-taken"),
    ("alex@harbourview.example", "unchanged"),
]


@pytest.mark.parametrize(("email", "code"), REFUSED_EMAILS)
def test_a_refused_email_change_names_its_code_and_writes_nothing(
    made: made_up.MadeUp, email: str, code: str
) -> None:
    before = notices(made)
    assert refusal(call(made, "email", made.member.pk, {"email": email})) == code
    assert reload(made.member).email == made.member.email
    assert not events(made, AuditAction.CHANGE_MEMBER_EMAIL)
    assert notices(made) == before


def test_a_blocked_or_an_erased_member_keeps_their_email(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        status=MemberStatus.BLOCKED
    )
    before = notices(made)
    body = {"email": "new@harbourview.example"}
    assert refusal(call(made, "email", made.member.pk, body)) == "blocked"
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        email="", phone="", status=MemberStatus.DISABLED
    )
    assert refusal(call(made, "email", made.member.pk, body)) == "erased"
    assert notices(made) == before


# --- Export -----------------------------------------------------------------------


def test_an_export_is_a_download_of_the_members_data(made: made_up.MadeUp) -> None:
    response = call(made, "export", made.member.pk, {})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="member-{made.member.pk}.json"'
    )
    assert "no-store" in response.headers["Cache-Control"]
    data = json.loads(response.content)
    assert data["organization_id"] == made.organization.pk
    assert data["member"]["id"] == made.member.pk
    assert data["member"]["email"] == made.member.email
    assert {"memberships", "bookings", "audit_events", "security_notices"} <= set(data)
    assert "scrambled_code" not in response.content.decode()
    assert len(events(made, AuditAction.EXPORT_MEMBER)) == 1


def test_the_export_and_the_audit_cover_the_email_change(made: made_up.MadeUp) -> None:
    old = made.member.email
    call(made, "email", made.member.pk, {"email": "new@harbourview.example"})
    data = export_member(
        actor=made.administrator,
        member_id=made.member.pk,
        **fresh(made.administrator),
    )
    (notice,) = [n for n in data["security_notices"] if n["address"]]
    assert notice["address"] == old
    assert "changed member email" in {row["action"] for row in data["audit_events"]}
    assert data["member"]["email"] == "new@harbourview.example"
    # The administrator's own export names the event as theirs, by id only.
    again = export_member(
        actor=made.administrator,
        member_id=made.administrator.pk,
        **fresh(made.administrator),
    )
    assert all(
        "harbourview" not in json.dumps(row)
        for row in again["audit_events"]
        if row["action"] == "changed member email"
    )


def test_erasing_the_member_removes_the_waiting_notice(made: made_up.MadeUp) -> None:
    call(made, "email", made.member.pk, {"email": "new@harbourview.example"})
    held = SecurityNotice.objects.of(made.organization).exclude(address="")
    assert held.exists()
    member_data.erase_identity(reload(made.member))
    assert not held.exists()


def test_the_hook_fires_exactly_when_admin_access_is_gained_whatever_the_status(
    made: made_up.MadeUp,
) -> None:
    def give(member: Member, role: Any) -> int:
        with mock.patch.object(member_data, "gained_admin_access") as gained:
            response = call(made, "correct", member.pk, {"role": role.pk})
        assert response.status_code == 200
        return int(gained.call_count)

    blocked = Member.objects.of(made.organization).filter(pk=made.member.pk)
    blocked.update(status=MemberStatus.BLOCKED)
    assert give(made.member, made.board) == 1
    assert give(made.member, made.standard) == 0
    blocked.update(status=MemberStatus.DISABLED)
    assert give(made.member, made.board) == 1
    assert give(made.member, made.board) == 0
