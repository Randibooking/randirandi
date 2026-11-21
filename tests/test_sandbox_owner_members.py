# SPDX-License-Identifier: AGPL-3.0-only
"""The command that makes the operator's own sandbox addresses members."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from randirandi.core.models import Member, MemberStatus, Origin
from randirandi.environment import Switches
from tests import made_up

LISTED = ("owner@example.test", "second@example.test")


def sandbox(listed: tuple[str, ...] = LISTED) -> override_settings:
    return override_settings(
        ENVIRONMENT="sandbox", SWITCHES=Switches(True, True, True, False, listed)
    )


@pytest.mark.django_db
def test_the_listed_addresses_become_active_standard_members_once() -> None:
    org = made_up.everything().organization
    before = Member.objects.of(org).count()
    with sandbox():
        call_command("add_sandbox_owner_members", org.subdomain, stdout=StringIO())
        rows = list(Member.objects.of(org).filter(email__in=LISTED).order_by("email"))
        again = StringIO()
        call_command("add_sandbox_owner_members", org.subdomain, stdout=again)
    assert Member.objects.of(org).count() == before + 2
    assert [row.email for row in rows] == list(LISTED)
    for row in rows:
        assert row.status == MemberStatus.ACTIVE
        assert row.origin == Origin.ADMINISTRATOR
        assert row.role_id == org.standard_role_id and not row.is_staff
    assert "Added 0; 2" in again.getvalue()
    assert (
        list(Member.objects.of(org).filter(email__in=LISTED).order_by("email")) == rows
    )


@pytest.mark.django_db
def test_an_existing_member_is_left_as_it_is() -> None:
    made = made_up.everything()
    made.member.email = LISTED[0]
    made.member.save(update_fields=["email"])
    with sandbox():
        call_command("add_sandbox_owner_members", made.organization.subdomain)
    made.member.refresh_from_db()
    assert made.member.origin == Origin.MEMBER_IMPORT
    assert Member.objects.of(made.organization).filter(email=LISTED[0]).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("environment", "listed"),
    [("production", LISTED), ("sandbox", ())],
)
def test_the_command_refuses_outside_a_sandbox_or_without_a_list(
    environment: str, listed: tuple[str, ...]
) -> None:
    org = made_up.everything().organization
    before = Member.objects.of(org).count()
    with (
        override_settings(
            ENVIRONMENT=environment, SWITCHES=Switches(True, True, True, False, listed)
        ),
        pytest.raises(CommandError, match="only be added in a sandbox"),
    ):
        call_command("add_sandbox_owner_members", org.subdomain)
    assert Member.objects.of(org).count() == before


@pytest.mark.django_db
def test_the_command_refuses_an_unknown_organization_and_an_admin_standard_role() -> (
    None
):
    org = made_up.everything().organization
    with sandbox():
        with pytest.raises(CommandError, match="No organization"):
            call_command("add_sandbox_owner_members", "does-not-exist")
        standard = org.standard_role
        assert standard is not None
        standard.admin_access = True
        standard.save(update_fields=["admin_access"])
        with pytest.raises(CommandError, match="standard role"):
            call_command("add_sandbox_owner_members", org.subdomain)
    assert not Member.objects.of(org).filter(email__in=LISTED).exists()
