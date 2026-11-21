# SPDX-License-Identifier: AGPL-3.0-only
"""Admin access begins, every administrator is told once; an administrator
resets another's authenticator from the member's page ."""

from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.db import OperationalError

from randirandi.core.audit import record_event
from randirandi.core.audit_log import action_label
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_admin import (
    block_member,
    first_administrator,
    unblock_member,
)
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    Authenticator,
    LoginAttempt,
    Member,
    MemberStatus,
    Origin,
    SecurityNotice,
)
from randirandi.core.security_notices import send_security_notices
from tests import made_up
from tests.fresh_code import fresh
from tests.made_up import import_members
from tests.mail_checks import check_system_mail
from tests.test_login_sessions import page_settings  # noqa: F401
from tests.test_member_import import table
from tests.test_member_pages_calls import (
    attempts,
    audits,
    call,
    events,
    refusal,
    reload,
)
from tests.test_member_pages_calls import made as made
from tests.test_member_status import listed_people, person, row

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_notices_yet(made: made_up.MadeUp) -> None:
    """The made-up organization has a notice of its own; these tests count."""
    SecurityNotice.objects.of(made.organization).delete()


def notices_of(made: made_up.MadeUp, action: AuditAction) -> list[SecurityNotice]:
    return list(
        SecurityNotice.objects.of(made.organization)
        .filter(event__action=action)
        .order_by("recipient_id")
    )


def recipients(found: list[SecurityNotice]) -> list[int]:
    return [notice.recipient_id for notice in found]


def second_administrator(made: made_up.MadeUp) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.board,
        full_name="Kim Lake",
        first_name="Kim",
        last_name="Lake",
        listed=True,
        email="kim@harbourview.example",
        phone="+4790000003",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


# --- Every way admin access begins -----------------------------------------------


def test_a_correction_giving_admin_access_tells_every_active_administrator_once(
    made: made_up.MadeUp,
) -> None:
    kim = second_administrator(made)
    assert (
        call(made, "correct", made.member.pk, {"role": made.board.pk}).status_code
        == 200
    )
    found = notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)
    # Both administrators and the new one, once each.
    assert recipients(found) == sorted([made.administrator.pk, kim.pk, made.member.pk])
    assert len({notice.event_id for notice in found}) == 1
    (gave,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert (gave.administrator_id, gave.object_kind, gave.object_id) == (
        made.administrator.pk,
        "member",
        made.member.pk,
    )
    # The same role again changes nothing.
    call(made, "correct", made.member.pk, {"role": made.board.pk})
    call(made, "correct", made.member.pk, {"full_name": "Alex Rivers-Berg"})
    assert len(notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)) == 3


def test_a_blocked_member_given_admin_access_is_not_a_recipient(
    made: made_up.MadeUp,
) -> None:
    block_member(actor=made.administrator, member_id=made.member.pk)
    call(made, "correct", made.member.pk, {"role": made.board.pk})
    assert recipients(notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)) == [
        made.administrator.pk
    ]


def test_no_notice_for_a_demotion_a_block_or_an_ordinary_correction(
    made: made_up.MadeUp,
) -> None:
    kim = second_administrator(made)
    call(made, "correct", kim.pk, {"role": made.standard.pk})
    block_member(actor=made.administrator, member_id=kim.pk)
    call(made, "correct", made.member.pk, {"phone": "+47 900 00 077"})
    assert not SecurityNotice.objects.of(made.organization).exists()
    assert not events(made, AuditAction.GAVE_ADMIN_ACCESS)


def test_unblocking_a_member_whose_role_has_admin_access_tells_the_administrators(
    made: made_up.MadeUp,
) -> None:
    kim = second_administrator(made)
    block_member(actor=made.administrator, member_id=kim.pk)
    unblock_member(actor=made.administrator, member_id=kim.pk)
    found = notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert recipients(found) == sorted([made.administrator.pk, kim.pk])
    # Blocked and unblocked in turn: each unblock is one gain.
    block_member(actor=made.administrator, member_id=kim.pk)
    unblock_member(actor=made.administrator, member_id=kim.pk)
    assert len(notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)) == 4


def test_unblocking_an_ordinary_member_or_one_who_stays_disabled_tells_nobody(
    made: made_up.MadeUp,
) -> None:
    block_member(actor=made.administrator, member_id=made.member.pk)
    unblock_member(actor=made.administrator, member_id=made.member.pk)
    assert not SecurityNotice.objects.of(made.organization).exists()
    assert not events(made, AuditAction.GAVE_ADMIN_ACCESS)
    kim = second_administrator(made)
    Member.objects.of(made.organization).filter(pk=kim.pk).update(
        status=MemberStatus.BLOCKED, listed=False
    )
    unblock_member(actor=made.administrator, member_id=kim.pk)
    assert reload(kim).status == MemberStatus.DISABLED
    assert not SecurityNotice.objects.of(made.organization).exists()
    assert not events(made, AuditAction.GAVE_ADMIN_ACCESS)


def test_the_operators_first_administrator_is_told_once(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.standard
    )
    first_administrator(made.organization, made.member.pk, "Board")
    first_administrator(made.organization, made.member.pk, "Board")
    found = notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert recipients(found) == [made.member.pk]
    # The operator's convention: the member is the actor.
    (gave,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert gave.administrator_id == gave.object_id == made.member.pk
    mail.outbox.clear()
    send_security_notices()
    (message,) = mail.outbox
    assert message.to == [made.member.email]
    assert message.subject == "RandiRandi administrator access given"


def test_an_import_making_an_administrator_active_again_tells_the_administrators(
    made: made_up.MadeUp,
) -> None:
    source = listed_people(made)
    rows = [row(n) for n in range(1, 6)]
    away = person(made, 2)
    Member.objects.of(made.organization).filter(pk=away.pk).update(
        role=made.board, status=MemberStatus.DISABLED
    )
    run = import_members(source, table(*rows), started_by=made.administrator)
    assert run.granted == 1
    found = notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert recipients(found) == sorted([made.administrator.pk, away.pk])
    # Nothing changes the second time.
    import_members(source, table(*rows), started_by=made.administrator)
    assert len(notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)) == 2
    (gave,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert (gave.administrator_id, gave.object_id) == (made.administrator.pk, away.pk)


def test_an_import_of_ordinary_members_tells_nobody(made: made_up.MadeUp) -> None:
    source = listed_people(made)
    Member.objects.of(made.organization).filter(pk=person(made, 2).pk).update(
        status=MemberStatus.DISABLED
    )
    import_members(
        source, table(*(row(n) for n in range(1, 6))), started_by=made.administrator
    )
    assert not SecurityNotice.objects.of(made.organization).exists()
    assert not events(made, AuditAction.GAVE_ADMIN_ACCESS)


def test_an_import_with_no_administrator_behind_it_tells_them_too(
    made: made_up.MadeUp,
) -> None:
    source = listed_people(made)
    away = person(made, 2)
    Member.objects.of(made.organization).filter(pk=away.pk).update(
        role=made.board, status=MemberStatus.DISABLED
    )
    import_members(source, table(*(row(n) for n in range(1, 6))))
    assert person(made, 2).status == MemberStatus.ACTIVE
    assert recipients(notices_of(made, AuditAction.GAVE_ADMIN_ACCESS)) == sorted(
        [made.administrator.pk, away.pk]
    )
    (gave,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert gave.administrator_id == gave.object_id == away.pk


def test_the_notices_are_sent_as_the_admin_access_message_with_no_names(
    made: made_up.MadeUp,
) -> None:
    kim = second_administrator(made)
    call(made, "correct", made.member.pk, {"role": made.board.pk})
    (event,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    mail.outbox.clear()
    send_security_notices()
    assert sorted(m.to[0] for m in mail.outbox) == sorted(
        [made.administrator.email, kim.email, made.member.email]
    )
    for message in mail.outbox:
        assert message.subject == "RandiRandi administrator access given"
        check_system_mail(message, made.organization, reply=False)
        assert "given administrator access to your organization" in message.body
        assert f"Audit reference: {event.pk}." in message.body
        html = str(message.message())
        for name in ("Alex", "Rivers", "Kim", "Lake", "Sam", "Field"):
            assert name not in message.body
            assert name not in html.replace(message.to[0], "")
        # The members' addresses are not in the mail; the footer names the
        # organization's own.
        bare = html.replace(message.to[0], "")
        for who in (made.administrator, kim, made.member):
            assert who.email not in message.body.replace(message.to[0], "")
            assert who.email not in bare
        assert made.organization.contact_email in message.body
        # No member address at the organization's domain: the contact
        # address is the only one allowed.
        carried = (message.body + bare).replace(made.organization.contact_email, "")
        assert "@harbourview" not in carried
        assert not message.cc and not message.bcc
    assert (
        not SecurityNotice.objects.of(made.organization)
        .filter(delivered_at=None)
        .exists()
    )
    sent = len(mail.outbox)
    send_security_notices()
    assert len(mail.outbox) == sent


def test_the_audit_log_words_and_the_member_export_cover_the_event(
    made: made_up.MadeUp,
) -> None:
    assert action_label("gave admin access") == "Gave admin access"
    call(made, "correct", made.member.pk, {"role": made.board.pk})
    (event,) = events(made, AuditAction.GAVE_ADMIN_ACCESS)
    for subject in (made.member, made.administrator):
        data = export_member(
            actor=made.administrator,
            member_id=subject.pk,
            **fresh(made.administrator),
        )
        assert event.pk in {row["id"] for row in data["audit_events"]}
        assert "gave admin access" in {row["action"] for row in data["audit_events"]}


def test_a_notice_with_no_message_for_its_action_is_refused_not_sent_as_a_reset(
    made: made_up.MadeUp, caplog: pytest.LogCaptureFixture
) -> None:
    event = record_event(
        actor=made.administrator, action=AuditAction.INSPECT, target=made.member
    )
    notice = SecurityNotice.objects.create(
        organization=made.organization, recipient=made.administrator, event=event
    )
    mail.outbox.clear()
    with caplog.at_level("ERROR"):
        send_security_notices()
    assert not mail.outbox
    notice.refresh_from_db()
    assert notice.delivered_at is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    text = errors[0].getMessage()
    assert "no message for its action" in text
    for personal in (
        made.administrator.email,
        made.administrator.full_name,
        made.member.email,
    ):
        assert personal not in caplog.text


# --- Reset another administrator's authenticator ---------------------------------


def reset(made: made_up.MadeUp, who: int, **more: Any) -> Any:
    return call(made, "reset", who, {}, **more)


def test_an_administrator_resets_anothers_authenticator_and_every_administrator_is_told(
    made: made_up.MadeUp,
) -> None:
    kim = second_administrator(made)
    fresh(kim)
    generation = reload(kim).authenticator_generation
    response = reset(made, kim.pk)
    assert response.status_code == 200
    import json

    page = json.loads(response.content)
    assert page["id"] == kim.pk and page["administrator"] is True
    assert not Authenticator.objects.of(made.organization).filter(member=kim).exists()
    assert reload(kim).authenticator_generation == generation + 1
    (event,) = events(made, AuditAction.RESET_AUTHENTICATOR)
    assert (event.administrator_id, event.object_id) == (made.administrator.pk, kim.pk)
    assert recipients(notices_of(made, AuditAction.RESET_AUTHENTICATOR)) == sorted(
        [made.administrator.pk, kim.pk]
    )
    mail.outbox.clear()
    send_security_notices()
    assert sorted(m.to[0] for m in mail.outbox) == sorted(
        [made.administrator.email, kim.email]
    )
    assert all(
        m.subject == "RandiRandi administrator authenticator reset"
        and f"Audit reference: {event.pk}." in m.body
        for m in mail.outbox
    )


def test_the_page_says_whether_the_member_is_an_administrator(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core import member_pages

    kim = second_administrator(made)
    assert member_pages.member(made.organization, reload(kim), made.administrator)[
        "administrator"
    ]
    assert not member_pages.member(
        made.organization, reload(made.member), made.administrator
    )["administrator"]


def test_a_reset_of_yourself_is_refused_and_writes_nothing(
    made: made_up.MadeUp,
) -> None:
    before = audits(made)
    assert refusal(reset(made, made.administrator.pk)) == "self"
    assert audits(made) == before
    assert not SecurityNotice.objects.of(made.organization).exists()
    assert not events(made, AuditAction.GAVE_ADMIN_ACCESS)
    assert not attempts(made)


def test_a_reset_of_a_member_who_is_not_an_administrator_is_refused(
    made: made_up.MadeUp,
) -> None:
    fresh(made.member)
    assert refusal(reset(made, made.member.pk)) == "not-administrator"
    kim = second_administrator(made)
    block_member(actor=made.administrator, member_id=kim.pk)
    fresh(kim)
    assert refusal(reset(made, kim.pk)) == "not-administrator"
    assert Authenticator.objects.of(made.organization).filter(member=kim).exists()
    assert not events(made, AuditAction.RESET_AUTHENTICATOR)


def test_a_wrong_code_is_refused_and_five_wrong_codes_limit_it(
    made: made_up.MadeUp,
) -> None:
    kim = second_administrator(made)
    fresh(kim)
    fresh(made.administrator)
    for _ in range(5):
        assert (
            refusal(call(made, "reset", kim.pk, {"code": "000000"})) == "code-refused"
        )
    assert (
        LoginAttempt.objects.of(made.organization)
        .filter(kind="mfa", outcome="refused")
        .count()
        == 5
    )
    assert refusal(reset(made, kim.pk)) == "code-limited"
    assert Authenticator.objects.of(made.organization).filter(member=kim).exists()
    assert not events(made, AuditAction.RESET_AUTHENTICATOR)


def test_a_busy_database_is_a_409_busy(made: made_up.MadeUp) -> None:
    kim = second_administrator(made)
    with mock.patch(
        "randirandi.core.admin_views.reset_member_authenticator",
        side_effect=OperationalError("busy"),
    ):
        assert refusal(reset(made, kim.pk)) == "busy"


def test_a_used_code_does_not_reset_twice(made: made_up.MadeUp) -> None:
    kim = second_administrator(made)
    body = {"code": fresh(made.administrator)["code"]}
    assert call(made, "reset", kim.pk, body, code=False).status_code == 200
    # The same code again: its counter is used up.
    assert refusal(call(made, "reset", kim.pk, body, code=False)) == "code-refused"
    assert len(events(made, AuditAction.RESET_AUTHENTICATOR)) == 1
