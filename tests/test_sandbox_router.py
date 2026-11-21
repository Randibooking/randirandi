# SPDX-License-Identifier: AGPL-3.0-only
"""The sandbox router and members an administrator added : real
mail only when every recipient is the operator's or an active member an
administrator added (an "added member" audit event targets them, and no other
member has the address without one); everything else is printed whole. The
router reads the member rows for each message, so a later change stops real
mail at once."""

import io
from unittest import mock

import pytest
from django.core import mail
from django.test import override_settings

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import Member, MemberStatus, Origin
from randirandi.mail_configuration import mailers
from tests import made_up

pytestmark = pytest.mark.django_db
OWNER = "owner@example.test"
TESTER = "tester@testers.example"
CREDENTIALS = {
    "RANDIRANDI_SMTP_USER": "fictional-project",
    "RANDIRANDI_SMTP_PASSWORD": "fictional-test-only",
}


def added(
    made: made_up.MadeUp,
    email: str = TESTER,
    *,
    status: MemberStatus = MemberStatus.ACTIVE,
    origin: Origin = Origin.ADMINISTRATOR,
    event: bool = True,
) -> Member:
    """A member; with `event`, one an administrator added (the "added member"
    audit event targets them)."""
    found = Member.objects.create(
        organization=made.organization,
        role=made.standard,
        full_name="Robin Tester",
        email=email,
        status=status,
        origin=origin,
    )
    if event:
        record_event(
            actor=made.administrator, action=AuditAction.ADD_MEMBER, target=found
        )
    return found


def message(
    *to: str, cc: tuple[str, ...] = (), bcc: tuple[str, ...] = ()
) -> mail.EmailMessage:
    return mail.EmailMessage(
        "Code", "Your fictional code", "post@example.test", list(to), cc=cc, bcc=bcc
    )


def route(*messages: mail.EmailMessage) -> tuple[list[str], list[str]]:
    """The addresses that went by SMTP and the ones printed."""
    configuration = mailers(CREDENTIALS, console=True, sandbox_to=[OWNER])
    with (
        override_settings(MAILERS=configuration),
        mock.patch("smtplib.SMTP") as plain,
        mock.patch("sys.stdout", new_callable=io.StringIO) as printed,
    ):
        plain.return_value.sendmail.return_value = {}
        with mail.mailers["default"] as connection:
            connection.send_messages(list(messages))
        by_smtp = [
            call.args[1][0] for call in plain.return_value.sendmail.call_args_list
        ]
    return by_smtp, [
        line.removeprefix("To: ")
        for line in printed.getvalue().splitlines()
        if line.startswith("To: ")
    ]


def test_an_active_member_an_administrator_added_is_mailed() -> None:
    added(made_up.everything())
    assert route(message(TESTER)) == ([TESTER], [])


def test_the_owner_and_such_a_member_together_are_mailed() -> None:
    added(made_up.everything())
    by_smtp, printed = route(message(OWNER, TESTER), message(OWNER))
    assert (by_smtp, printed) == ([OWNER, OWNER], [])


def test_the_address_is_compared_in_lower_case_and_in_every_recipient_field() -> None:
    added(made_up.everything())
    assert route(message("Tester@Testers.Example"))[1] == []
    assert route(message(OWNER, cc=(TESTER,), bcc=(OWNER,)))[1] == []


def test_a_message_with_any_other_recipient_is_printed_whole() -> None:
    added(made_up.everything())
    for other in (
        message(TESTER, "stranger@example.test"),
        message(TESTER, cc=("stranger@example.test",)),
        message(TESTER, bcc=("stranger@example.test",)),
    ):
        by_smtp, printed = route(other)
        assert by_smtp == [] and len(printed) == 1


def test_an_address_that_is_no_member_is_printed() -> None:
    added(made_up.everything())
    assert route(message("stranger@example.test")) == ([], ["stranger@example.test"])


def test_a_message_without_recipients_is_printed_not_sent() -> None:
    by_smtp, _ = route(message())
    assert by_smtp == []


@pytest.mark.parametrize("status", [MemberStatus.DISABLED, MemberStatus.BLOCKED])
def test_a_disabled_or_blocked_member_is_printed(status: MemberStatus) -> None:
    added(made_up.everything(), status=status)
    assert route(message(TESTER)) == ([], [TESTER])


def test_a_member_from_an_import_is_printed() -> None:
    made = made_up.everything()
    assert made.member.origin == Origin.MEMBER_IMPORT
    assert made.member.status == MemberStatus.ACTIVE
    assert route(message(made.member.email)) == ([], [made.member.email])
    added(made, origin=Origin.MEMBER_IMPORT, event=False)
    assert route(message(TESTER)) == ([], [TESTER])


def test_a_later_disabling_or_blocking_stops_real_mail_at_once() -> None:
    found = added(made_up.everything())
    assert route(message(TESTER))[0] == [TESTER]
    for status in (MemberStatus.BLOCKED, MemberStatus.DISABLED):
        Member.across_organizations.filter(pk=found.pk).update(status=status)
        assert route(message(TESTER)) == ([], [TESTER])
        Member.across_organizations.filter(pk=found.pk).update(
            status=MemberStatus.ACTIVE
        )
        assert route(message(TESTER))[0] == [TESTER]


def test_an_erased_member_no_longer_matches() -> None:
    found = added(made_up.everything())
    Member.across_organizations.filter(pk=found.pk).update(
        email="", status=MemberStatus.DISABLED
    )
    assert route(message(TESTER)) == ([], [TESTER])


def test_a_mixed_batch_is_sorted_message_by_message() -> None:
    made = made_up.everything()
    added(made)
    by_smtp, printed = route(
        message(OWNER), message(made.member.email), message(TESTER)
    )
    assert by_smtp == [OWNER, TESTER]
    assert printed == [made.member.email]


def test_one_connection_reads_the_member_again_for_each_message() -> None:
    added(made_up.everything())
    configuration = mailers(CREDENTIALS, console=True, sandbox_to=[OWNER])
    with (
        override_settings(MAILERS=configuration),
        mock.patch("smtplib.SMTP") as plain,
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mail.mailers["default"] as connection,
    ):
        plain.return_value.sendmail.return_value = {}
        connection.send_messages([message(TESTER)])
        assert plain.return_value.sendmail.call_count == 1
        Member.across_organizations.filter(email=TESTER).update(
            status=MemberStatus.BLOCKED
        )
        connection.send_messages([message(TESTER)])
        assert plain.return_value.sendmail.call_count == 1


def test_origin_alone_is_not_proof_of_an_add() -> None:
    # What the first-administrator command leaves: the origin, no add event.
    added(made_up.everything(), event=False)
    assert route(message(TESTER)) == ([], [TESTER])


def test_an_address_another_member_has_without_an_add_is_printed() -> None:
    first = made_up.everything()
    added(first)
    assert route(message(TESTER))[0] == [TESTER]
    # The same address, an import's member in another organization.
    other = made_up.everything("elsewhere")
    added(other, origin=Origin.MEMBER_IMPORT, event=False)
    assert route(message(TESTER)) == ([], [TESTER])


def test_a_blocked_second_member_with_the_address_and_no_add_stops_the_mail() -> None:
    made = made_up.everything()
    added(made)
    added(
        made_up.everything("elsewhere"),
        status=MemberStatus.BLOCKED,
        origin=Origin.MEMBER_IMPORT,
        event=False,
    )
    assert route(message(TESTER)) == ([], [TESTER])


def test_the_same_address_added_in_two_organizations_is_mailed() -> None:
    added(made_up.everything())
    added(made_up.everything("elsewhere"))
    assert route(message(TESTER))[0] == [TESTER]


def test_the_permission_ends_when_the_email_changes_after_the_add() -> None:
    made = made_up.everything()
    found = added(made)
    assert route(message(TESTER))[0] == [TESTER]
    record_event(
        actor=made.administrator, action=AuditAction.CHANGE_MEMBER_EMAIL, target=found
    )
    Member.across_organizations.filter(pk=found.pk).update(
        email="elsewhere@testers.example"
    )
    assert route(message("elsewhere@testers.example")) == (
        [],
        ["elsewhere@testers.example"],
    )
    # Changing it back does not bring the permission back.
    Member.across_organizations.filter(pk=found.pk).update(email=TESTER)
    assert route(message(TESTER)) == ([], [TESTER])


def test_a_change_before_the_add_does_not_end_the_permission() -> None:
    made = made_up.everything()
    found = added(made, event=False)
    record_event(
        actor=made.administrator, action=AuditAction.CHANGE_MEMBER_EMAIL, target=found
    )
    record_event(actor=made.administrator, action=AuditAction.ADD_MEMBER, target=found)
    assert route(message(TESTER))[0] == [TESTER]
