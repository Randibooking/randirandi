# SPDX-License-Identifier: AGPL-3.0-only
"""the authenticator secret is stored encrypted (v1: AES-256-GCM)."""

import re
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, connection
from django.test import override_settings
from django_otp.oath import TOTP  # type: ignore[import-untyped]

from randirandi.core import authenticator_secret
from randirandi.core.login_codes import VerifiedCode
from randirandi.core.models import Authenticator, LoginAttempt, Member
from randirandi.core.second_factor import (
    prepare_authenticator,
    reset_authenticator,
    reset_member_authenticator,
    verify_authenticator,
)
from tests import made_up
from tests.fresh_code import fresh

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
KEY = "0f" * 32
OTHER_KEY = "f0" * 32
PLAIN = "1f" * 20


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def proof(member: Member) -> VerifiedCode:
    member.refresh_from_db()
    return VerifiedCode(member, member.get_session_auth_hash())


def code(device: Authenticator) -> str:
    clock = TOTP(bytes.fromhex(device.plain_secret()), step=30, digits=6)
    clock.time = NOW.timestamp()
    return f"{clock.token():06d}"


def stored_in_database(device: Authenticator) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT secret FROM core_authenticator WHERE id = %s", [device.pk]
        )
        return str(cursor.fetchone()[0])


def test_enrolment_stores_the_versioned_encrypted_form_and_verifies(
    made: made_up.MadeUp,
) -> None:
    device = prepare_authenticator(proof(made.administrator), now=NOW)
    assert device is not None
    raw = stored_in_database(device)
    assert re.fullmatch(r"v1:[A-Za-z0-9_-]{64}", raw)
    plain = device.plain_secret()
    assert re.fullmatch(r"[0-9a-f]{40}", plain) and plain not in raw
    assert verify_authenticator(
        proof(made.administrator), code(device), network="192.0.2.5", now=NOW
    )
    assert Authenticator._base_manager.get(pk=device.pk).confirmed


def test_sealing_twice_gives_different_text_and_both_open() -> None:
    first, second = (
        authenticator_secret.seal(PLAIN, key=KEY, organization_id=1, member_id=2)
        for _ in range(2)
    )
    assert first != second
    for stored in (first, second):
        assert (
            authenticator_secret.unseal(stored, key=KEY, organization_id=1, member_id=2)
            == PLAIN
        )


@pytest.mark.parametrize("damage", ["flip", "version", "short", "plain"])
def test_a_changed_row_is_refused(made: made_up.MadeUp, damage: str) -> None:
    device = prepare_authenticator(proof(made.administrator), now=NOW)
    assert device is not None
    good = code(device)
    if damage == "plain":
        # The database check refuses a plain secret outright.
        with pytest.raises(IntegrityError, match="authenticator_secret_valid"):
            Authenticator._base_manager.filter(pk=device.pk).update(secret=PLAIN)
        return
    stored = device.secret
    changed = {
        "flip": stored[:-1] + ("A" if stored[-1] != "A" else "B"),
        "version": "v2" + stored[2:],
        "short": stored[:-4],
    }[damage]
    if damage == "flip":
        Authenticator._base_manager.filter(pk=device.pk).update(secret=changed)
    else:
        with pytest.raises(IntegrityError, match="authenticator_secret_valid"):
            Authenticator._base_manager.filter(pk=device.pk).update(secret=changed)
        return
    broken = Authenticator._base_manager.get(pk=device.pk)
    with pytest.raises(authenticator_secret.SecretUnreadableError):
        broken.plain_secret()
    assert (
        verify_authenticator(
            proof(made.administrator), good, network="192.0.2.5", now=NOW
        )
        is None
    )
    assert (
        LoginAttempt.objects.of(made.organization)
        .filter(member=made.administrator, outcome="refused")
        .count()
        == 1
    )
    assert not Authenticator._base_manager.get(pk=device.pk).confirmed


def test_a_wrong_key_is_refused(made: made_up.MadeUp) -> None:
    device = prepare_authenticator(proof(made.administrator), now=NOW)
    assert device is not None
    good = code(device)
    with override_settings(AUTHENTICATOR_KEY=OTHER_KEY):
        with pytest.raises(authenticator_secret.SecretUnreadableError):
            device.plain_secret()
        assert (
            verify_authenticator(
                proof(made.administrator), good, network="192.0.2.5", now=NOW
            )
            is None
        )


def test_a_missing_or_malformed_key_is_refused_with_a_clear_message(
    made: made_up.MadeUp,
) -> None:
    device = prepare_authenticator(proof(made.administrator), now=NOW)
    assert device is not None
    for key in ("", "ab", "XY" * 32):
        with (
            override_settings(AUTHENTICATOR_KEY=key),
            pytest.raises(authenticator_secret.KeyMissingError, match="64"),
        ):
            device.plain_secret()


def test_a_row_copied_to_another_member_is_refused(made: made_up.MadeUp) -> None:
    made.member.role = made.board
    made.member.save(update_fields=["role"])
    mine = prepare_authenticator(proof(made.member), now=NOW)
    theirs = prepare_authenticator(proof(made.administrator), now=NOW)
    assert mine is not None and theirs is not None
    Authenticator._base_manager.filter(pk=theirs.pk).update(secret=mine.secret)
    with pytest.raises(authenticator_secret.SecretUnreadableError):
        Authenticator._base_manager.get(pk=theirs.pk).plain_secret()


def test_reset_and_the_operator_reset_enrol_again_in_the_encrypted_form(
    made: made_up.MadeUp,
) -> None:
    made.member.role = made.board
    made.member.save(update_fields=["role"])
    old = prepare_authenticator(proof(made.member), now=NOW)
    assert old is not None
    reset_member_authenticator(
        actor=made.administrator,
        member_id=made.member.pk,
        code=fresh(made.administrator, now=NOW)["code"],
        network="192.0.2.6",
        now=NOW,
    )
    assert not Authenticator._base_manager.filter(member=made.member).exists()
    new = prepare_authenticator(proof(made.member), now=NOW)
    assert new is not None
    assert re.fullmatch(r"v1:[A-Za-z0-9_-]{64}", stored_in_database(new))
    assert new.secret != old.secret and new.plain_secret() != old.plain_secret()
    reset_authenticator(
        organization=made.organization,
        member_id=made.member.pk,
        actor=None,
        no_other_administrator_available=True,
    )
    assert not Authenticator._base_manager.filter(member=made.member).exists()
    again = prepare_authenticator(proof(made.member), now=NOW)
    assert again is not None
    assert re.fullmatch(r"v1:[A-Za-z0-9_-]{64}", stored_in_database(again))
    assert again.plain_secret() not in (new.plain_secret(), old.plain_secret())
