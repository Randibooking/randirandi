# SPDX-License-Identifier: AGPL-3.0-only
"""The member, and the rule that one member has one email and one phone
number .
"""

from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction

from randirandi.core.models import Member, MemberStatus, Origin
from tests import made_up


def new_member(
    made: made_up.MadeUp,
    *,
    first_name: str = "Robin",
    last_name: str = "Shore",
    email: str = "robin@harbourview.example",
    phone: str = "",
    status: MemberStatus = MemberStatus.ACTIVE,
) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        status=status,
        origin=Origin.MEMBER_IMPORT,
    )


def refused(made: made_up.MadeUp, constraint: str, **fields: Any) -> None:
    with pytest.raises(IntegrityError) as error, transaction.atomic():
        new_member(made, **fields)
    cause = error.value.__cause__
    assert cause is not None
    assert cause.diag.constraint_name == constraint  # type: ignore[attr-defined]


def test_the_member_is_the_user_object_and_has_no_password() -> None:
    assert get_user_model() is Member
    for field in ("password", "last_login"):
        with pytest.raises(FieldDoesNotExist):
            Member._meta.get_field(field)


@pytest.mark.django_db
def test_an_email_belongs_to_one_member_in_an_organization() -> None:
    made = made_up.everything()

    refused(made, "member_email_once", email=made.member.email)


@pytest.mark.django_db
def test_a_phone_number_belongs_to_one_member_in_an_organization() -> None:
    made = made_up.everything()

    refused(made, "member_phone_once", phone=made.member.phone)


@pytest.mark.django_db
def test_the_same_person_can_be_a_member_of_two_organizations() -> None:
    ours = made_up.everything("harbourview")
    theirs = made_up.everything("lakeside", made_up.A_DAY + timedelta(days=7))

    twin = new_member(theirs, email=ours.member.email)

    assert twin.pk != ours.member.pk
    assert twin.email == ours.member.email
    # The two made-up organizations already share their phone numbers.
    assert theirs.member.phone == ours.member.phone


@pytest.mark.django_db
def test_an_email_cannot_be_written_in_two_ways() -> None:
    made = made_up.everything()

    refused(made, "member_email_lower_case", email="Alex@Harbourview.example")


@pytest.mark.django_db
def test_validation_writes_an_email_and_a_phone_number_in_the_one_way() -> None:
    made = made_up.everything()
    member = Member(
        organization=made.organization,
        role=made.standard,
        first_name="Robin",
        last_name="Shore",
        email=" Robin@Harbourview.example ",
        phone="+47 900 00 003",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )

    member.full_clean()

    assert member.email == "robin@harbourview.example"
    assert member.phone == "+4790000003"


@pytest.mark.django_db
@pytest.mark.parametrize("phone", ["90000003", "004790000003", "+47 90000003", "+0"])
def test_a_phone_number_is_written_with_its_country_code(phone: str) -> None:
    made = made_up.everything()

    refused(made, "member_phone_with_country_code", phone=phone)


@pytest.mark.django_db
def test_an_active_member_has_an_email() -> None:
    made = made_up.everything()

    refused(made, "member_active_needs_an_email", email="")


@pytest.mark.django_db
def test_erased_members_do_not_stand_in_each_others_way() -> None:
    # Erasing blanks the name and the contact details in place.
    made = made_up.everything()

    first, second = (
        new_member(
            made, first_name="", last_name="", email="", status=MemberStatus.DISABLED
        )
        for _ in range(2)
    )

    assert first.pk != second.pk


@pytest.mark.django_db
def test_a_disabled_member_is_not_active() -> None:
    made = made_up.everything()

    assert made.member.is_active
    assert not made.member.is_staff
    assert made.administrator.is_staff

    made.administrator.status = MemberStatus.DISABLED

    assert not made.administrator.is_active
    assert not made.administrator.is_staff


@pytest.mark.django_db
def test_a_member_is_printed_without_a_name() -> None:
    made = made_up.everything()

    assert str(made.member) == f"member {made.member.pk}"
