# SPDX-License-Identifier: AGPL-3.0-only
"""A member's data is corrected or exported only on a fresh authenticator code."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connections
from django_otp.oath import TOTP  # type: ignore[import-untyped]
from psycopg.errors import SerializationFailure

from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import correct_member, export_member
from randirandi.core.models import AuditEvent, Authenticator, LoginAttempt, Member
from tests import made_up
from tests.capacity_app import application_login
from tests.sealed import sealed

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2028, 3, 1, 12, tzinfo=UTC)
NETWORK = "192.0.2.7"
ACTIONS = (AuditAction.CORRECT_MEMBER, AuditAction.EXPORT_MEMBER)


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def device(member: Member, *, confirmed: bool = True, last: int = 0) -> Authenticator:
    Authenticator.objects.of(member.organization_id).filter(member=member).delete()
    return Authenticator.objects.create(
        organization_id=member.organization_id,
        member=member,
        secret=sealed("1f" * 20, member.organization_id, member.pk),
        confirmed=confirmed,
        last_counter=last,
        created_at=NOW - timedelta(days=1),
    )


def token(item: Authenticator, offset: int = 0) -> str:
    clock = TOTP(bytes.fromhex(item.plain_secret()), step=30, digits=6)
    clock.time = NOW.timestamp() + offset
    return f"{clock.token():06d}"


def correct(actor: Member, member: Member, code: str, network: str = NETWORK) -> Any:
    return correct_member(
        actor=actor,
        member_id=member.pk,
        changes={"first_name": "Taylor"},
        code=code,
        network=network,
        now=NOW,
    )


def export(actor: Member, member: Member, code: str, network: str = NETWORK) -> Any:
    return export_member(
        actor=actor, member_id=member.pk, code=code, network=network, now=NOW
    )


OPERATIONS = pytest.mark.parametrize("operation", [correct, export])


def stored(made: made_up.MadeUp) -> Authenticator:
    return Authenticator.objects.of(made.organization).get(member=made.administrator)


def attempts(made: made_up.MadeUp) -> list[tuple[str, str]]:
    return list(
        LoginAttempt.objects.of(made.organization)
        .filter(kind="mfa")
        .order_by("pk")
        .values_list("kind", "outcome")
    )


def untouched(made: made_up.MadeUp) -> None:
    made.member.refresh_from_db()
    assert made.member.first_name == "Alex"
    assert not AuditEvent.objects.of(made.organization).filter(action__in=ACTIONS)


@OPERATIONS
def test_an_accepted_code_acts_once_and_is_recorded(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    item = device(made.administrator)
    with application_login():
        operation(made.administrator, made.member, token(item))
    item.refresh_from_db()
    assert item.last_counter == int(NOW.timestamp()) // 30
    assert attempts(made) == [("mfa", "accepted")]
    action = (
        AuditAction.CORRECT_MEMBER
        if operation is correct
        else AuditAction.EXPORT_MEMBER
    )
    assert AuditEvent.objects.of(made.organization).filter(action=action).count() == 1
    made.member.refresh_from_db()
    assert made.member.first_name == ("Taylor" if operation is correct else "Alex")


@OPERATIONS
def test_the_same_code_twice_is_refused_the_second_time(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    code = token(device(made.administrator))
    with application_login():
        operation(made.administrator, made.member, code)
        with pytest.raises(PermissionDenied, match="not accepted"):
            operation(made.administrator, made.member, code)
    assert attempts(made) == [("mfa", "accepted"), ("mfa", "refused")]
    assert (
        AuditEvent.objects.of(made.organization).filter(action__in=ACTIONS).count() == 1
    )


@pytest.mark.parametrize("fault", ["wrong", "used_by_login", "unconfirmed", "none"])
@OPERATIONS
def test_a_code_that_is_not_accepted_changes_nothing(
    made: made_up.MadeUp, operation: Callable[..., Any], fault: str
) -> None:
    if fault == "none":
        code = "123456"
    else:
        item = device(made.administrator, confirmed=fault != "unconfirmed")
        code = token(item)
        if fault == "wrong":
            code = f"{(int(code) + 1) % 1000000:06d}"
        elif fault == "used_by_login":
            Authenticator.objects.of(made.organization).filter(pk=item.pk).update(
                last_counter=int(NOW.timestamp()) // 30
            )
    with (
        application_login(),
        pytest.raises(
            PermissionDenied,
            match=r"The code from your authenticator app was not accepted\.",
        ),
    ):
        operation(made.administrator, made.member, code)
    untouched(made)
    assert attempts(made) == [("mfa", "refused")]
    if fault != "none":
        assert stored(made).confirmed is (fault != "unconfirmed")


@pytest.mark.parametrize("limit", ["member", "network"])
@OPERATIONS
def test_a_limited_administrator_or_network_is_told_to_wait(
    made: made_up.MadeUp, operation: Callable[..., Any], limit: str
) -> None:
    code = token(device(made.administrator))
    spread = {"member": 5, "network": 20}[limit]
    for number in range(spread):
        LoginAttempt.objects.create(
            organization=made.organization,
            member=made.administrator if limit == "member" else made.member,
            at=NOW - timedelta(minutes=14),
            network=NETWORK if limit == "network" else f"198.51.100.{number + 1}",
            kind="mfa",
            outcome="refused",
        )
    with application_login(), pytest.raises(PermissionDenied, match="Wait 15 minutes"):
        operation(made.administrator, made.member, code)
    untouched(made)
    assert (
        LoginAttempt.objects.of(made.organization)
        .filter(kind="mfa", outcome="limited")
        .count()
        == 1
    )
    assert stored(made).last_counter == 0
    # Fifteen minutes on, the same code is accepted again.
    with application_login():
        later = NOW + timedelta(minutes=16)
        export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            code=token(stored(made), 16 * 60),
            network=NETWORK,
            now=later,
        )


@OPERATIONS
def test_a_member_who_is_not_an_administrator_is_refused(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    item = device(made.member)
    with application_login(), pytest.raises(PermissionDenied):
        operation(made.member, made.member, token(item))
    untouched(made)
    item.refresh_from_db()
    assert item.last_counter == 0


@OPERATIONS
def test_another_organizations_member_is_refused_and_the_code_stays_unused(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    other = made_up.everything("other")
    code = token(device(made.administrator))
    with application_login(), pytest.raises(Member.DoesNotExist):
        operation(made.administrator, other.member, code)
    untouched(made)
    assert stored(made).last_counter == 0
    other.member.refresh_from_db()
    assert other.member.first_name == "Alex"


@OPERATIONS
def test_two_at_once_with_the_same_code_act_once(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    code = token(device(made.administrator))

    def run(_: int) -> bool:
        try:
            operation(made.administrator, made.member, code)
            return True
        except PermissionDenied:
            return False
        finally:
            connections.close_all()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == [False, True]
    assert (
        AuditEvent.objects.of(made.organization).filter(action__in=ACTIONS).count() == 1
    )
    assert sorted(attempts(made)) == [("mfa", "accepted"), ("mfa", "refused")]


@OPERATIONS
def test_exhausted_contention_records_a_limited_refusal_and_changes_nothing(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    code = token(device(made.administrator))
    error = OperationalError("fictional contention")
    error.__cause__ = SerializationFailure()
    with (
        application_login(),
        mock.patch(
            "randirandi.core.second_factor.organization_transaction", side_effect=error
        ),
        pytest.raises(PermissionDenied, match="Wait 15 minutes"),
    ):
        operation(made.administrator, made.member, code)
    untouched(made)
    assert attempts(made) == [("mfa", "limited")]


@OPERATIONS
def test_an_administrator_demoted_after_login_is_refused_with_a_valid_code(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    item = device(made.administrator)
    Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
        role=made.standard
    )
    with application_login(), pytest.raises(PermissionDenied):
        operation(made.administrator, made.member, token(item))
    untouched(made)
    item.refresh_from_db()
    assert item.last_counter == 0


@OPERATIONS
def test_the_members_device_does_not_stand_in_for_the_administrators(
    made: made_up.MadeUp, operation: Callable[..., Any]
) -> None:
    Authenticator.objects.of(made.organization).filter(
        member=made.administrator
    ).delete()
    item = device(made.member)
    with application_login(), pytest.raises(PermissionDenied, match="not accepted"):
        operation(made.administrator, made.member, token(item))
    untouched(made)
    item.refresh_from_db()
    assert item.last_counter == 0
    assert attempts(made) == [("mfa", "refused")]
