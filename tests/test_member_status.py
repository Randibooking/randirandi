# SPDX-License-Identifier: AGPL-3.0-only
"""A member's status: active, disabled or blocked .

An import disables, an administrator blocks, and a block outlasts every import.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client

from randirandi.core.login_codes import issue_code
from randirandi.core.login_identity import identifier_digest
from randirandi.core.member_admin import (
    block_member,
    first_administrator,
    unblock_member,
)
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    AuditEvent,
    ImportOutcome,
    LoginCode,
    LoginRequest,
    Member,
    MemberStatus,
    Origin,
)
from randirandi.core.retention import purge_organization
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.made_up import import_members
from tests.operator_erasure import erase
from tests.test_login_sessions import get, page_settings, sign_in  # noqa: F401
from tests.test_member_import import row as plain_row
from tests.test_member_import import table

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2028, 3, 1, 12, tzinfo=UTC)


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def row(number: int, **change: str) -> tuple[str, ...]:
    # Phone numbers that the made-up members do not have.
    return plain_row(number, **{"phone": f"+4781{number:06d}", **change})


def listed_people(made: made_up.MadeUp, count: int = 5) -> made_up.Source:
    """Five people from a list. Alex Rivers of the made-up organization stays
    out of it: that member is not from this list."""
    Member.objects.of(made.organization).filter(pk__in=[made.member.pk]).update(
        origin=Origin.ADMINISTRATOR
    )
    source = made_up.Source(made.organization)
    import_members(source, table(*(row(n) for n in range(1, count + 1))))
    return source


def person(made: made_up.MadeUp, number: int) -> Member:
    return Member.objects.of(made.organization).get(email=f"member{number}@example.com")


def changes(member: Member) -> list[tuple[str, str]]:
    return list(
        AccessEvent.objects.of(member.organization_id)
        .filter(member=member)
        .order_by("pk")
        .values_list("change", "cause")
    )


def block(made: made_up.MadeUp, member: Member) -> Member:
    return block_member(actor=made.administrator, member_id=member.pk)


def test_an_import_leaves_a_blocked_member_blocked_and_keeps_what_the_list_says(
    made: made_up.MadeUp,
) -> None:
    source = listed_people(made)
    blocked = block(made, person(made, 2))
    others = [row(n) for n in (1, 3, 4, 5)]

    # Off the list: still blocked, no longer listed.
    run = import_members(source, table(*others))
    blocked.refresh_from_db()
    assert run.outcome == ImportOutcome.APPLIED and run.revoked == 0
    assert (blocked.status, blocked.listed) == (MemberStatus.BLOCKED, False)
    assert blocked.unlisted_at is not None

    # Back on it, under a new name: still blocked, listed, details updated.
    run = import_members(source, table(*others, row(2, first_name="Renamed")))
    blocked.refresh_from_db()
    assert run.outcome == ImportOutcome.APPLIED and run.granted == 0
    assert (blocked.status, blocked.listed) == (MemberStatus.BLOCKED, True)
    assert blocked.first_name == "Renamed"
    assert changes(blocked) == [
        ("active", "member import"),
        ("blocked", "administrator"),
    ]


def test_the_safety_stop_counts_active_members_only(made: made_up.MadeUp) -> None:
    source = listed_people(made)
    block(made, person(made, 5))
    # One of four active members would be disabled: more than 20%.
    run = import_members(source, table(*(row(n) for n in (1, 2, 3, 5))))
    assert run.outcome == ImportOutcome.STOPPED
    assert person(made, 4).status == MemberStatus.ACTIVE
    assert person(made, 5).status == MemberStatus.BLOCKED


def test_a_list_with_problems_sets_listed_for_those_on_it_only(
    made: made_up.MadeUp,
) -> None:
    source = listed_people(made)
    gone = block(made, person(made, 2))
    run = import_members(
        source, table(*(row(n) for n in (1, 3, 4, 5)), row(1, email="bad"))
    )
    gone.refresh_from_db()
    assert run.report["removals_deferred"]
    assert gone.listed is True


@pytest.mark.parametrize(
    "on_list,after",
    [(True, MemberStatus.ACTIVE), (False, MemberStatus.DISABLED)],
)
def test_unblock_goes_back_to_what_the_list_says(
    made: made_up.MadeUp, on_list: bool, after: MemberStatus
) -> None:
    source = listed_people(made)
    block(made, person(made, 2))
    people = (1, 2, 3, 4, 5) if on_list else (1, 3, 4, 5)
    import_members(source, table(*(row(n) for n in people)))

    unblocked = unblock_member(actor=made.administrator, member_id=person(made, 2).pk)

    assert unblocked.status == after
    assert changes(unblocked)[-1] == (after, AccessCause.ADMINISTRATOR)
    last = AccessEvent.objects.of(made.organization).filter(member=unblocked).last()
    assert last is not None and last.administrator_id == made.administrator.pk


def test_unblock_of_a_member_the_import_disabled_before_the_block_is_disabled(
    made: made_up.MadeUp,
) -> None:
    source = listed_people(made)
    import_members(source, table(*(row(n) for n in (1, 3, 4, 5))))
    assert person(made, 2).status == MemberStatus.DISABLED
    block(made, person(made, 2))
    unblocked = unblock_member(actor=made.administrator, member_id=person(made, 2).pk)
    assert unblocked.status == MemberStatus.DISABLED


def test_unblock_of_a_listed_member_without_an_email_is_disabled(
    made: made_up.MadeUp,
) -> None:
    nameless = Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Pat Quill",
        status=MemberStatus.DISABLED,
        origin=Origin.ADMINISTRATOR,
    )
    block(made, nameless)
    assert unblock_member(actor=made.administrator, member_id=nameless.pk).status == (
        MemberStatus.DISABLED
    )


def test_block_ends_codes_and_waiting_requests_and_the_session(
    made: made_up.MadeUp,
    page_settings: None,  # noqa: F811
) -> None:
    client = Client()
    sign_in(client, made.member)
    assert get(client).headers["Location"] == "/book/"
    code = LoginCode.objects.create(
        organization=made.organization,
        member=made.member,
        scrambled_code="unusable-test-value",
        expires_at=made_up.at(made_up.A_DAY, 10),
    )
    waiting = LoginRequest.objects.create(
        organization=made.organization,
        identifier_digest=identifier_digest(made.organization.pk, made.member.email),
        browser_digest="a" * 64,
        challenge="b" * 32,
        network="192.0.2.1",
        at=NOW,
    )
    before = made.member.get_session_auth_hash()

    block(made, made.member)

    code.refresh_from_db()
    made.member.refresh_from_db()
    assert code.used_at is not None
    assert not LoginRequest.objects.of(made.organization).filter(pk=waiting.pk).exists()
    assert made.member.status == MemberStatus.BLOCKED and not made.member.is_active
    assert made.member.get_session_auth_hash() != before
    assert get(client).headers["Location"].startswith("/login/")
    assert "_auth_user_id" not in client.session


def test_a_blocked_member_cannot_ask_for_a_code(made: made_up.MadeUp) -> None:
    block(made, made.member)
    assert (
        issue_code(
            organization=made.organization,
            member_id=made.member.pk,
            browser="b" * 40,
            network="192.0.2.1",
        )
        is None
    )


def test_block_and_unblock_each_write_one_audit_event(made: made_up.MadeUp) -> None:
    block(made, made.member)
    unblock_member(actor=made.administrator, member_id=made.member.pk)
    events = list(
        AuditEvent.objects.of(made.organization)
        .filter(action__in=["blocked member", "unblocked member"])
        .order_by("pk")
        .values_list("action", "object_kind", "object_id", "administrator_id")
    )
    assert events == [
        ("blocked member", "member", made.member.pk, made.administrator.pk),
        ("unblocked member", "member", made.member.pk, made.administrator.pk),
    ]


def test_an_administrator_cannot_block_themselves(made: made_up.MadeUp) -> None:
    with pytest.raises(ValidationError, match="themselves"):
        block(made, made.administrator)
    made.administrator.refresh_from_db()
    assert made.administrator.is_staff
    assert (
        not AuditEvent.objects.of(made.organization)
        .filter(action="blocked member")
        .exists()
    )


def test_blocking_a_blocked_member_and_unblocking_another_are_refused(
    made: made_up.MadeUp,
) -> None:
    with pytest.raises(ValidationError, match="not blocked"):
        unblock_member(actor=made.administrator, member_id=made.member.pk)
    block(made, made.member)
    with pytest.raises(ValidationError, match="already blocked"):
        block(made, made.member)
    assert changes(made.member) == [
        ("active", "member import"),
        ("blocked", "administrator"),
    ]
    assert (
        AuditEvent.objects.of(made.organization).filter(action="blocked member").count()
        == 1
    )


@pytest.mark.parametrize("actor_kind", ["member", "disabled", "foreign"])
def test_only_an_active_administrator_of_the_organization_blocks(
    made: made_up.MadeUp, actor_kind: str
) -> None:
    actor = made.administrator
    if actor_kind == "member":
        actor = made.member
    elif actor_kind == "disabled":
        Member.objects.of(made.organization).filter(pk=actor.pk).update(
            status=MemberStatus.DISABLED
        )
    else:
        actor = made_up.everything("other").administrator
    spare = Member.objects.create(
        organization=made.organization,
        role=made.standard,
        email="spare@example.com",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    with pytest.raises((PermissionDenied, Member.DoesNotExist)):
        block_member(actor=actor, member_id=spare.pk)
    spare.refresh_from_db()
    assert spare.status == MemberStatus.ACTIVE


def test_a_member_of_another_organization_is_not_found(made: made_up.MadeUp) -> None:
    other = made_up.everything("other")
    with pytest.raises(Member.DoesNotExist):
        block_member(actor=made.administrator, member_id=other.member.pk)


def test_block_keeps_bookings(made: made_up.MadeUp) -> None:
    block(made, made.member)
    made.booking.refresh_from_db()
    assert made.booking.member_id == made.member.pk


def test_block_and_unblock_run_as_the_application(made: made_up.MadeUp) -> None:
    with application_login():
        block(made, made.member)
        unblock_member(actor=made.administrator, member_id=made.member.pk)
    made.member.refresh_from_db()
    assert made.member.status == MemberStatus.ACTIVE


def test_the_first_administrator_must_be_active(made: made_up.MadeUp) -> None:
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        status=MemberStatus.BLOCKED
    )
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.standard
    )
    with pytest.raises(ValidationError, match="already be active"):
        first_administrator(made.organization, made.member.pk, "Board")


def unlisted_member(
    made: made_up.MadeUp, status: MemberStatus, email: str, at: datetime | None
) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Robin Shore",
        email=email,
        status=status,
        listed=False,
        unlisted_at=at,
        origin=Origin.MEMBER_IMPORT,
    )


def test_retention_erases_a_disabled_or_blocked_member_off_the_list_for_the_months(
    made: made_up.MadeUp,
) -> None:
    long_ago = NOW.replace(year=2020)
    disabled = unlisted_member(
        made, MemberStatus.DISABLED, "robin@example.com", long_ago
    )
    blocked = unlisted_member(made, MemberStatus.BLOCKED, "kim@example.com", long_ago)
    recent = unlisted_member(made, MemberStatus.BLOCKED, "lee@example.com", NOW)
    unknown = unlisted_member(made, MemberStatus.DISABLED, "sky@example.com", None)

    counts = purge_organization(made.organization, now=NOW)

    for member in (disabled, blocked, recent, unknown):
        member.refresh_from_db()
    assert counts["members"] == 2
    assert (disabled.email, disabled.status) == ("", MemberStatus.DISABLED)
    assert (blocked.email, blocked.status, blocked.listed) == (
        "",
        MemberStatus.DISABLED,
        False,
    )
    assert recent.email and unknown.email


def test_block_and_unblock_do_not_move_the_retention_clock(
    made: made_up.MadeUp,
) -> None:
    # Off the list 11 of 12 months ago, then blocked and unblocked.
    start = NOW.replace(year=2027)
    member = unlisted_member(made, MemberStatus.DISABLED, "robin@example.com", start)
    block(made, member)
    unblocked = unblock_member(actor=made.administrator, member_id=member.pk)
    assert unblocked.status == MemberStatus.DISABLED and unblocked.unlisted_at == start
    assert purge_organization(made.organization, now=start)["members"] == 0
    assert purge_organization(made.organization, now=NOW)["members"] == 1


def test_a_blocked_member_unlisted_during_the_block_gets_the_time_and_loses_it_again(
    made: made_up.MadeUp,
) -> None:
    source = listed_people(made)
    blocked = block(made, person(made, 2))
    assert blocked.unlisted_at is None
    others = [row(n) for n in (1, 3, 4, 5)]

    import_members(source, table(*others))
    blocked.refresh_from_db()
    assert blocked.status == MemberStatus.BLOCKED and not blocked.listed
    assert blocked.unlisted_at is not None
    first = blocked.unlisted_at

    # A second import that still misses the member keeps the first time.
    import_members(source, table(*others))
    blocked.refresh_from_db()
    assert blocked.unlisted_at == first

    import_members(source, table(*others, row(2)))
    blocked.refresh_from_db()
    assert blocked.status == MemberStatus.BLOCKED and blocked.listed
    assert blocked.unlisted_at is None


def test_an_import_that_disables_a_member_sets_the_time(made: made_up.MadeUp) -> None:
    source = listed_people(made)
    import_members(source, table(*(row(n) for n in (1, 3, 4, 5))))
    gone = person(made, 2)
    assert gone.status == MemberStatus.DISABLED and gone.unlisted_at is not None


@pytest.mark.parametrize("call", [block_member, unblock_member])
def test_a_missing_member_id_in_the_organization_is_not_found(
    made: made_up.MadeUp, call: Any
) -> None:
    missing = Member.objects.of(made.organization).order_by("-pk").first()
    assert missing is not None
    with pytest.raises(Member.DoesNotExist):
        call(actor=made.administrator, member_id=missing.pk + 1000)
    assert (
        not AuditEvent.objects.of(made.organization)
        .filter(action__in=["blocked member", "unblocked member"])
        .exists()
    )


def test_erasing_a_blocked_member_leaves_them_disabled_and_not_listed(
    made: made_up.MadeUp,
) -> None:
    block(made, made.member)
    erase(made, made.member.pk)
    made.member.refresh_from_db()
    assert (made.member.status, made.member.listed) == (MemberStatus.DISABLED, False)
    assert made.member.email == ""


def test_the_export_carries_the_status_and_its_history(made: made_up.MadeUp) -> None:
    block(made, made.member)
    result = export_member(
        actor=made.administrator, member_id=made.member.pk, **fresh(made.administrator)
    )
    assert result["member"]["status"] == "blocked"
    assert result["member"]["listed"] is True
    assert result["member"]["unlisted_at"] is None
    assert "access" not in result["member"]
    assert [row["change"] for row in result["access_events"]] == ["active", "blocked"]


def fails(sql_text: str, constraint: str, *values: int | str | None) -> None:
    with (
        pytest.raises(IntegrityError) as error,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql_text, values)
    assert constraint in str(error.value)


def test_the_checks_hold_for_raw_sql(made: made_up.MadeUp) -> None:
    # An active member has an email.
    fails(
        "UPDATE core_member SET email = '', status = 'active' WHERE id = %s",
        "member_active_needs_an_email",
        made.member.pk,
    )
    # A block is an administrator's.
    insert = (
        "INSERT INTO core_accessevent "
        "(organization_id, member_id, change, at, cause, administrator_id) "
        "VALUES (%s, %s, 'blocked', now(), %s, %s)"
    )
    fails(
        insert,
        "accessevent_blocked_by_administrator",
        made.organization.pk,
        made.member.pk,
        "member import",
        None,
    )
    fails(
        insert,
        "accessevent_blocked_by_administrator",
        made.organization.pk,
        made.member.pk,
        "administrator",
        None,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            insert,
            [
                made.organization.pk,
                made.member.pk,
                "administrator",
                made.administrator.pk,
            ],
        )
    # The audit vocabulary has the two new actions and no other.
    audit = (
        "INSERT INTO core_auditevent "
        "(organization_id, administrator_id, action, object_kind, object_id, at) "
        "VALUES (%s, %s, %s, 'member', %s, now())"
    )
    for action in ("blocked member", "unblocked member"):
        with connection.cursor() as cursor:
            cursor.execute(
                audit,
                [made.organization.pk, made.administrator.pk, action, made.member.pk],
            )
    fails(
        audit,
        "audit_fixed_action",
        made.organization.pk,
        made.administrator.pk,
        "removed member",
        made.member.pk,
    )
