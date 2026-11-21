# SPDX-License-Identifier: AGPL-3.0-only
"""Issued codes under the real database transaction and privacy boundaries."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from django.contrib.sessions.models import Session
from django.db import close_old_connections, connections, transaction
from django.test import override_settings

from randirandi.core.login_codes import (
    MEMBER_CEILING,
    MEMBER_REQUEST_LIMIT,
    IssuedCode,
    issue_code,
    verify_code,
)
from randirandi.core.member_data import export_member
from randirandi.core.models import LoginAttempt, LoginCode, Member
from randirandi.core.retention import purge_organization
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.operator_erasure import erase

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2028, 3, 1, 12, tzinfo=UTC)
BROWSER = "fictional_browser_binding_only_123456789"
NETWORK = "192.0.2.40"


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def issue(made: made_up.MadeUp, now: datetime = NOW) -> IssuedCode:
    result = issue_code(
        organization=made.organization,
        member_id=made.member.pk,
        browser=BROWSER,
        network=NETWORK,
        now=now,
    )
    assert result is not None
    return result


def verify(
    made: made_up.MadeUp,
    issued: IssuedCode,
    *,
    code: str | None = None,
    browser: str = BROWSER,
    now: datetime = NOW,
) -> Member | None:
    result = verify_code(
        organization=made.organization,
        challenge=issued.challenge,
        browser=browser,
        code=issued.code if code is None else code,
        network=NETWORK,
        now=now,
    )
    return result.member if result is not None else None


def test_code_is_keyed_bound_and_single_use_under_application_permissions(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        issued = issue(made)
        row = LoginCode.objects.of(made.organization).get(challenge=issued.challenge)
        assert len(issued.code) == 6 and issued.code.isascii() and issued.code.isdigit()
        assert row.scrambled_code != issued.code
        assert len(row.scrambled_code) == len(row.browser_digest) == 64
        assert row.browser_digest != BROWSER
        assert issued.code not in repr(issued)
        assert row.expires_at == NOW + timedelta(minutes=10)
        with override_settings(SECRET_KEY=secrets.token_hex(32)):
            assert verify(made, issued) is None
        assert verify(made, issued) == made.member
        assert verify(made, issued) is None
    assert not Session.objects.exists()


@pytest.mark.parametrize("seconds,accepted", [(599, True), (600, False), (601, False)])
def test_expiry_is_exact(made: made_up.MadeUp, seconds: int, accepted: bool) -> None:
    issued = issue(made)
    assert (
        verify(made, issued, now=NOW + timedelta(seconds=seconds)) is not None
    ) == accepted


def test_fifth_wrong_try_permanently_exhausts_code(made: made_up.MadeUp) -> None:
    issued = issue(made)
    wrong = "000001" if issued.code == "000000" else "000000"
    for _ in range(5):
        assert verify(made, issued, code=wrong) is None
    row = LoginCode.objects.of(made.organization).get(challenge=issued.challenge)
    assert row.attempts == 5 and row.used_at == NOW
    assert verify(made, issued) is None


def test_four_wrong_tries_still_allow_correct_fifth(made: made_up.MadeUp) -> None:
    issued = issue(made)
    for _ in range(4):
        assert verify(made, issued, code="not-six-digits") is None
    assert verify(made, issued) == made.member


def test_other_browser_or_organization_cannot_consume_code(
    made: made_up.MadeUp,
) -> None:
    issued = issue(made)
    other = made_up.everything("other")
    assert verify(other, issued) is None
    assert verify(made, issued, browser="different_browser_binding_123456789") is None
    row = LoginCode.objects.of(made.organization).get(challenge=issued.challenge)
    assert row.attempts == 0 and row.used_at is None
    assert verify(made, issued) == made.member


def test_new_request_invalidates_previous_code_across_browsers(
    made: made_up.MadeUp,
) -> None:
    old = issue(made)
    new = issue(made, NOW + timedelta(seconds=1))
    assert old.challenge != new.challenge
    assert verify(made, old) is None
    assert verify(made, new) == made.member


@pytest.mark.parametrize(
    "status,email",
    [
        ("disabled", "alex@example.test"),
        ("blocked", "alex@example.test"),
        ("disabled", ""),
    ],
)
def test_changed_authority_refuses_issuance_and_verification(
    made: made_up.MadeUp,
    status: str,
    email: str,
) -> None:
    issued = issue(made)
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        status=status, email=email
    )
    assert verify(made, issued) is None
    assert (
        issue_code(
            organization=made.organization,
            member_id=made.member.pk,
            browser=BROWSER,
            network=NETWORK,
            now=NOW,
        )
        is None
    )


def ask(made: made_up.MadeUp, network: str, now: datetime = NOW) -> IssuedCode | None:
    return issue_code(
        organization=made.organization,
        member_id=made.member.pk,
        browser=BROWSER,
        network=network,
        now=now,
    )


def test_member_limit_is_per_network_and_resets_at_window(
    made: made_up.MadeUp,
) -> None:
    for _ in range(5):
        assert ask(made, "192.0.2.1") is not None
    assert ask(made, "192.0.2.1") is None
    assert ask(made, "192.0.2.1", NOW + timedelta(minutes=15)) is not None


def test_a_strangers_requests_from_another_network_do_not_lock_the_member_out(
    made: made_up.MadeUp,
) -> None:
    mine = ask(made, "192.0.2.1")
    assert mine is not None
    for _ in range(10):
        ask(made, "198.51.100.9")
    # The stranger's five-per-network limit is spent; the member's is not, and
    # the stranger's requests did not void the member's live code.
    assert ask(made, "198.51.100.9") is None
    assert verify(made, mine) == made.member
    again = ask(made, "192.0.2.1")
    assert again is not None and verify(made, again) == made.member


def test_a_request_from_the_same_network_voids_the_earlier_code_only_there(
    made: made_up.MadeUp,
) -> None:
    first = ask(made, "192.0.2.1")
    elsewhere = ask(made, "192.0.2.2")
    second = ask(made, "192.0.2.1")
    assert first and elsewhere and second
    assert verify(made, first) is None
    assert verify(made, elsewhere) == made.member
    assert verify(made, second) == made.member


def test_one_strangers_twenty_requests_do_not_lock_the_member_out(
    made: made_up.MadeUp,
) -> None:
    results = [ask(made, "198.51.100.9") for _ in range(20)]
    assert sum(r is not None for r in results) == 5
    # Limited requests issue nothing and do not count toward the ceiling.
    assert ask(made, "192.0.2.1") is not None


def test_the_ceiling_counts_issued_codes_across_networks(
    made: made_up.MadeUp,
) -> None:
    for n in range(MEMBER_CEILING):
        assert ask(made, f"192.0.2.{n + 1}") is not None
    # Never more codes alive than the ceiling: one per network.
    alive = LoginCode.objects.of(made.organization).filter(
        member=made.member, used_at__isnull=True, network__isnull=False
    )
    assert alive.count() == MEMBER_CEILING
    assert ask(made, "203.0.113.1") is None
    assert ask(made, "203.0.113.1", NOW + timedelta(minutes=15)) is not None
    # Repeated requests from one network keep one code alive there.
    later = NOW + timedelta(minutes=30)
    for _ in range(3):
        assert ask(made, "203.0.113.50", later) is not None
    assert alive.filter(network="203.0.113.50", used_at__isnull=True).count() == 1


def test_a_network_the_member_signed_in_from_is_exempt_from_the_ceiling(
    made: made_up.MadeUp,
) -> None:
    first = ask(made, NETWORK)
    assert first is not None and verify(made, first) == made.member
    for n in range(MEMBER_CEILING - 1):
        assert ask(made, f"192.0.2.{n + 100}") is not None
    assert ask(made, "203.0.113.1") is None
    assert ask(made, NETWORK) is not None
    # Thirty days on, the sign-in no longer exempts the network.
    old = NOW + timedelta(days=30, minutes=1)
    for n in range(MEMBER_CEILING):
        assert ask(made, f"192.0.2.{n + 100}", old) is not None
    assert ask(made, NETWORK, old) is None


def test_ipv6_is_one_network_per_64(made: made_up.MadeUp) -> None:
    for n in range(5):
        assert ask(made, f"2001:db8:1:2::{n + 1}") is not None
    assert ask(made, "2001:db8:1:3::1") is not None  # another /64
    assert ask(made, "2001:db8:1:2:0:0:0:99") is None  # same /64 as the five
    # Addresses inside one /64 collapse to one network: only five are served.
    other = made_up.everything("other")
    for n in range(MEMBER_CEILING):
        result = issue_code(
            organization=other.organization,
            member_id=other.member.pk,
            browser=BROWSER,
            network=f"2001:db8:9:0::{n + 1}",
            now=NOW,
        )
        assert (result is not None) == (n < MEMBER_REQUEST_LIMIT)


def test_unknown_members_count_towards_network_limit(made: made_up.MadeUp) -> None:
    other = made_up.everything("other")
    for _ in range(20):
        assert (
            issue_code(
                organization=made.organization,
                member_id=other.member.pk,
                browser=BROWSER,
                network=NETWORK,
                now=NOW,
            )
            is None
        )
    assert (
        issue_code(
            organization=made.organization,
            member_id=made.member.pk,
            browser=BROWSER,
            network=NETWORK,
            now=NOW,
        )
        is None
    )
    assert (
        LoginAttempt.objects.of(made.organization)
        .filter(
            network=NETWORK,
            member__isnull=True,
            outcome="unknown",
        )
        .count()
        == 20
    )


@pytest.mark.parametrize(
    "challenge,code", [("unknown", "123456"), ("a" * 32, "x" * 200)]
)
def test_malformed_verifications_are_recorded_without_submitted_values(
    made: made_up.MadeUp,
    challenge: str,
    code: str,
) -> None:
    assert (
        verify_code(
            organization=made.organization,
            challenge=challenge,
            browser=BROWSER,
            code=code,
            network=NETWORK,
            now=NOW,
        )
        is None
    )
    row = LoginAttempt.objects.of(made.organization).get(kind="verify")
    assert row.outcome == "refused" and row.network == NETWORK
    assert row.member_id is None


def test_simultaneous_verification_accepts_exactly_once(made: made_up.MadeUp) -> None:
    issued = issue(made)
    barrier = Barrier(2)

    def submit() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return verify(made, issued) is not None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(results) == [False, True]


def test_simultaneous_requests_cannot_exceed_member_limit(made: made_up.MadeUp) -> None:
    for _ in range(4):
        issue(made)
    barrier = Barrier(2)

    def submit() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return (
                issue_code(
                    organization=made.organization,
                    member_id=made.member.pk,
                    browser=BROWSER,
                    network=NETWORK,
                    now=NOW,
                )
                is not None
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(results) == [False, True]


def test_login_records_have_thirty_day_retention_and_scoped_export_erasure(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("other")
    issue(made)
    for org, member, at in (
        (made.organization, made.member, NOW - timedelta(days=30)),
        (
            made.organization,
            made.member,
            NOW - timedelta(days=30) + timedelta(seconds=1),
        ),
        (other.organization, other.member, NOW - timedelta(days=31)),
    ):
        LoginAttempt.objects.create(
            organization=org,
            member=member,
            at=at,
            network=NETWORK,
            kind="verify",
            outcome="refused",
        )
    with application_login():
        purge_organization(made.organization, now=NOW)
        assert LoginAttempt.objects.of(made.organization).count() == 2
        assert (
            LoginAttempt.objects.of(other.organization).filter(network=NETWORK).count()
            == 1
        )
        exported = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
        assert len(exported["login_attempts"]) == 2
        assert set(exported["login_attempts"][0]) == {
            "id",
            "at",
            "network",
            "kind",
            "outcome",
        }
        erase(made, made.member.pk)
        # The administrator's own code attempt stays; the member's go.
        assert (
            not LoginAttempt.objects.of(made.organization)
            .filter(member=made.member)
            .exists()
        )
        assert LoginAttempt.objects.of(other.organization).exists()


def test_service_refuses_nested_transaction_without_writes(
    made: made_up.MadeUp,
) -> None:
    count = LoginCode.objects.of(made.organization).count()
    with transaction.atomic(), pytest.raises(RuntimeError, match="own transaction"):
        issue(made)
    assert LoginCode.objects.of(made.organization).count() == count


@pytest.mark.parametrize(
    "now", [datetime.max.replace(tzinfo=UTC), datetime.min.replace(tzinfo=UTC)]
)
def test_out_of_range_issuance_is_refused_before_writes(
    made: made_up.MadeUp, now: datetime
) -> None:
    count = LoginAttempt.objects.of(made.organization).count()
    with pytest.raises(ValueError, match="supported range"):
        issue(made, now)
    assert LoginAttempt.objects.of(made.organization).count() == count
