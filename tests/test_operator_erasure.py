# SPDX-License-Identifier: AGPL-3.0-only
"""The operator erases a member on the member's written request : the command, its
refusals, its audit event and its migration. Fictional
people."""

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_admin import first_administrator
from randirandi.core.member_data import erase_identity
from randirandi.core.models import (
    AuditEvent,
    Authenticator,
    Booking,
    BookingState,
    LoginCode,
    Member,
    MemberStatus,
    Origin,
    Payment,
    SecurityNotice,
    TakenTime,
)
from randirandi.core.security_notices import send_security_notices
from tests import made_up
from tests.capacity_app import application_login
from tests.operator_erasure import settle_coming
from tests.test_login_sessions import get, page_settings, post  # noqa: F401
from tests.test_second_factor import clocks, email_factor, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0027_time_zone_and_currency")]
AFTER = [("core", "0028_operator_erases_member")]
REASONS = {
    "organization": "unknown organization or member",
    "member": "unknown organization or member",
    "other organization": "unknown organization or member",
    "erased": "already erased",
    "booking to come": "booking still to come",
    "order held": "held for payment",
}
PERSONAL = ("full_name", "first_name", "last_name", "email", "phone")


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def erase(made: made_up.MadeUp, member_id: int, *flags: str) -> str:
    output = StringIO()
    call_command(
        "erase_member", made.organization.subdomain, member_id, *flags, stdout=output
    )
    return output.getvalue()


def events(made: made_up.MadeUp) -> int:
    return (
        AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.OPERATOR_ERASE_MEMBER)
        .count()
    )


def personal(member: Member) -> dict[str, Any]:
    member.refresh_from_db()
    return {
        name: getattr(member, name)
        for name in (*PERSONAL, "status", "listed", "last_booking_category_id")
    }


def test_without_the_flag_it_refuses_and_changes_nothing(made: made_up.MadeUp) -> None:
    settle_coming(made, made.member.pk)
    before = personal(made.member)
    with pytest.raises(CommandError, match="--written-request-received"):
        erase(made, made.member.pk)
    assert personal(made.member) == before and events(made) == 0


def test_with_the_flag_it_erases_as_the_primitive_does_and_keeps_transactions(
    made: made_up.MadeUp,
) -> None:
    twin = made_up.everything("elsewhere")
    erase_identity(twin.member)
    settle_coming(made, made.member.pk)
    LoginCode.objects.create(
        organization=made.organization,
        member=made.member,
        scrambled_code="fictional",
        expires_at=made_up.at(made_up.A_DAY, 9),
    )
    with application_login():
        output = erase(made, made.member.pk, "--written-request-received")
    assert output == f"Erased member {made.member.pk}.\n"
    assert personal(made.member) == personal(twin.member)
    assert (
        not LoginCode.objects.of(made.organization).filter(member=made.member).exists()
    )
    assert Booking.objects.of(made.organization).get(pk=made.booking.pk).member_id == (
        made.member.pk
    )
    assert Payment.objects.of(made.organization).filter(pk=made.payment.pk).exists()
    event = AuditEvent.objects.of(made.organization).get(
        action=AuditAction.OPERATOR_ERASE_MEMBER
    )
    assert event.administrator_id == event.object_id == made.member.pk
    assert event.object_kind == "member"


@pytest.mark.parametrize(
    "case",
    [
        "organization",
        "member",
        "other organization",
        "erased",
        "booking to come",
        "order held",
    ],
)
def test_each_refusal_names_its_reason_and_changes_nothing(
    made: made_up.MadeUp, case: str
) -> None:
    target = made.member
    subdomain = made.organization.subdomain
    member_id = made.member.pk
    if case == "organization":
        subdomain = "nowhere"
    elif case == "member":
        member_id = made.member.pk + 100000
    elif case == "other organization":
        subdomain = made_up.everything("elsewhere").organization.subdomain
    elif case == "erased":
        settle_coming(made, target.pk)
        erase_identity(target)
    elif case == "order held":
        settle_coming(made, target.pk)
        TakenTime.objects.of(made.organization).filter(booking=made.booking).update(
            state=BookingState.HELD
        )
    before = personal(target)
    with pytest.raises(CommandError) as refusal:
        call_command("erase_member", subdomain, member_id, "--written-request-received")
    message = str(refusal.value)
    assert message.startswith("Erase refused: ")
    assert not any(value in message for value in ("Alex", "Rivers", "Sam", "example"))
    assert personal(target) == before
    assert events(made) == 0
    assert REASONS[case] in message


def test_a_cancelled_or_past_booking_does_not_stop_the_erasure(
    made: made_up.MadeUp,
) -> None:
    TakenTime.objects.of(made.organization).filter(booking=made.booking).update(
        state=BookingState.CANCELLED
    )
    erase(made, made.member.pk, "--written-request-received")
    assert personal(made.member)["email"] == "" and events(made) == 1


def test_the_audit_writer_still_requires_an_administrator_for_every_other_action(
    made: made_up.MadeUp,
) -> None:
    before = AuditEvent.objects.of(made.organization).count()
    for action in AuditAction:
        if action == AuditAction.OPERATOR_ERASE_MEMBER:
            continue
        with pytest.raises(PermissionDenied):
            record_event(actor=made.member, action=action, target=made.member)
    assert AuditEvent.objects.of(made.organization).count() == before


def test_the_operator_action_needs_the_actor_to_be_the_target(
    made: made_up.MadeUp,
) -> None:
    action = AuditAction.OPERATOR_ERASE_MEMBER
    before = AuditEvent.objects.of(made.organization).count()
    # An administrator acting on another member, or a member on an administrator.
    for actor, target in (
        (made.administrator, made.member),
        (made.member, made.administrator),
    ):
        with pytest.raises(PermissionDenied):
            record_event(actor=actor, action=action, target=target)
    # Not a member, and not of this organization.
    with pytest.raises(PermissionDenied):
        record_event(actor=made.member, action=action, target=made.organization)
    stranger = made_up.everything("elsewhere").member
    with pytest.raises(PermissionDenied):
        record_event(actor=stranger, action=action, target=made.member)
    assert AuditEvent.objects.of(made.organization).count() == before
    event = record_event(actor=made.member, action=action, target=made.member)
    assert event.administrator_id == event.object_id == made.member.pk


def test_an_active_administrator_can_be_the_operator_actor(
    made: made_up.MadeUp,
) -> None:
    event = record_event(
        actor=made.administrator,
        action=AuditAction.OPERATOR_ERASE_MEMBER,
        target=made.administrator,
    )
    assert event.administrator_id == event.object_id == made.administrator.pk
    # Another administrator is still not the target's stand-in.
    with pytest.raises(PermissionDenied):
        record_event(
            actor=made.administrator,
            action=AuditAction.OPERATOR_ERASE_MEMBER,
            target=made.member,
        )


def second_administrator(made: made_up.MadeUp) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.administrator.role,
        full_name="Robin Hale",
        first_name="Robin",
        last_name="Hale",
        email="robin@harbourview.example",
        phone="+4790000003",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )


def clear_bookings(made: made_up.MadeUp) -> None:
    TakenTime.objects.of(made.organization).update(state=BookingState.CANCELLED)


def test_the_last_administrator_is_erased_and_the_output_names_the_way_back(
    made: made_up.MadeUp,
) -> None:
    clear_bookings(made)
    output = erase(made, made.administrator.pk, "--written-request-received")
    assert output.startswith(f"Erased member {made.administrator.pk}.\n")
    assert "last active administrator" in output and "first_administrator" in output
    assert personal(made.administrator)["email"] == ""
    assert events(made) == 1
    event = AuditEvent.objects.of(made.organization).get(
        action=AuditAction.OPERATOR_ERASE_MEMBER
    )
    assert event.administrator_id == event.object_id == made.administrator.pk
    assert not (
        Member.objects.of(made.organization)
        .filter(status=MemberStatus.ACTIVE, role__admin_access=True)
        .exists()
    )


def test_one_of_two_administrators_is_erased_without_the_warning(
    made: made_up.MadeUp,
) -> None:
    clear_bookings(made)
    other = second_administrator(made)
    output = erase(made, made.administrator.pk, "--written-request-received")
    assert output == f"Erased member {made.administrator.pk}.\n"
    assert personal(made.administrator)["email"] == "" and events(made) == 1
    other.refresh_from_db()
    assert other.is_staff and other.email != ""


def test_an_administrator_with_a_booking_to_come_is_still_refused(
    made: made_up.MadeUp,
) -> None:
    TakenTime.objects.of(made.organization).update(state=BookingState.CANCELLED)
    taken = TakenTime.objects.of(made.organization).filter(booking=made.booking)
    taken.update(state=BookingState.HELD)
    Booking.objects.of(made.organization).filter(pk=made.booking.pk).update(
        member=made.administrator
    )
    with pytest.raises(CommandError, match="booking still to come"):
        erase(made, made.administrator.pk, "--written-request-received")
    assert personal(made.administrator)["email"] != "" and events(made) == 0


def test_after_the_last_administrator_is_erased_a_new_one_can_be_made(
    made: made_up.MadeUp,
) -> None:
    clear_bookings(made)
    erase(made, made.administrator.pk, "--written-request-received")
    new = first_administrator(made.organization, made.member.pk, "Board")
    assert new.pk == made.member.pk and new.is_staff
    new.refresh_from_db()
    assert new.is_staff and new.role.admin_access
    client = Client(enforce_csrf_checks=True)
    with application_login():
        email_factor(client, new)
        get(client)
        device = Authenticator.objects.of(made.organization).get(member=new)
        post(client, "/login/authenticator/", code=token(device))
        assert client.session["admin_confirmed"] is True
        assert get(client, "/admin/bookings/").status_code == 200


def test_a_pending_notice_to_an_erased_administrator_is_dropped_with_the_erasure(
    made: made_up.MadeUp,
) -> None:
    clear_bookings(made)
    event = record_event(
        actor=made.administrator,
        action=AuditAction.EXPORT_MEMBER,
        target=made.member,
    )
    notice = SecurityNotice.objects.create(
        organization=made.organization, recipient=made.administrator, event=event
    )
    erase(made, made.administrator.pk, "--written-request-received")
    mail.outbox.clear()
    with application_login():
        send_security_notices()
    # Erasure drops the notice with the recipient; nothing is left to send.
    assert (
        not SecurityNotice.objects.of(made.organization).filter(pk=notice.pk).exists()
    )
    assert mail.outbox == []


def test_an_erased_administrator_loses_admin_access_session_and_authenticator(
    made: made_up.MadeUp,
) -> None:
    clear_bookings(made)
    client = Client(enforce_csrf_checks=True)
    with application_login():
        email_factor(client, made.administrator)
        get(client)
        device = Authenticator.objects.of(made.organization).get(
            member=made.administrator
        )
        response = post(client, "/login/authenticator/", code=token(device))
        assert response.headers["Location"] == "/"
        assert client.session["admin_confirmed"] is True
        assert get(client, "/admin/bookings/").status_code == 200
        before = made.administrator.get_session_auth_hash()

        erase(made, made.administrator.pk, "--written-request-received")

        made.administrator.refresh_from_db()
        assert not made.administrator.is_staff
        assert made.administrator.get_session_auth_hash() != before
        assert get(client, "/admin/bookings/").status_code in (302, 403, 404)
        assert get(client).headers["Location"].startswith("/login/")
        assert "_auth_user_id" not in client.session
    assert not (
        Authenticator.objects.of(made.organization)
        .filter(member=made.administrator)
        .exists()
    )


def test_a_booking_under_way_refuses_and_an_ended_one_does_not(
    made: made_up.MadeUp,
) -> None:
    now = timezone.now()
    taken = TakenTime.objects.of(made.organization).filter(booking=made.booking)
    taken.update(starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    with pytest.raises(CommandError, match="booking still to come"):
        erase(made, made.member.pk, "--written-request-received")
    assert events(made) == 0
    taken.update(starts_at=now - timedelta(hours=3), ends_at=now - timedelta(hours=2))
    erase(made, made.member.pk, "--written-request-received")
    assert personal(made.member)["email"] == "" and events(made) == 1


def test_the_event_and_the_erasing_stand_or_fall_together(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    settle_coming(made, made.member.pk)

    def fail(member: Member) -> None:
        raise RuntimeError("erasing failed")

    monkeypatch.setattr("randirandi.core.member_data.erase_identity", fail)
    with pytest.raises(RuntimeError, match="erasing failed"):
        erase(made, made.member.pk, "--written-request-received")
    assert events(made) == 0
    assert personal(made.member)["email"] != ""


def test_the_administrator_form_is_gone() -> None:
    from randirandi.core import member_data

    assert not hasattr(member_data, "erase_member")


# The migration (0028): the vocabulary gains the action, both ways.


@pytest.fixture
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def migrate_to(target: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)


def leaf_nodes_are_current() -> bool:
    executor = MigrationExecutor(connection)
    return executor.migration_plan(executor.loader.graph.leaf_nodes()) == []


@pytest.mark.usefixtures("restore_current_schema")
def test_the_migration_registers_the_action_both_ways(made: made_up.MadeUp) -> None:
    migrate_to(BEFORE)
    # Raw SQL: the models read columns that the old schema does not have.
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO core_auditevent "
            "(organization_id, administrator_id, action, object_kind, object_id, at) "
            "VALUES (%s, %s, %s, 'member', %s, now())",
            [
                made.organization.pk,
                made.member.pk,
                AuditAction.OPERATOR_ERASE_MEMBER.value,
                made.member.pk,
            ],
        )
    # The current schema, which holds 0028 and what came after it.
    migrate_to(MigrationExecutor(connection).loader.graph.leaf_nodes())
    record_event(
        actor=made.member, action=AuditAction.OPERATOR_ERASE_MEMBER, target=made.member
    )
    assert events(made) == 1


@pytest.mark.usefixtures("restore_current_schema")
def test_reversal_is_refused_while_an_event_uses_the_action(
    made: made_up.MadeUp,
) -> None:
    record_event(
        actor=made.member, action=AuditAction.OPERATOR_ERASE_MEMBER, target=made.member
    )
    try:
        with pytest.raises(RuntimeError, match="operator erasure history"):
            migrate_to(BEFORE)
    finally:
        migrate_to(MigrationExecutor(connection).loader.graph.leaf_nodes())
    assert leaf_nodes_are_current()
    assert events(made) == 1
