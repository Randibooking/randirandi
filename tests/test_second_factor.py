# SPDX-License-Identifier: AGPL-3.0-only
"""Real administrator factors, resets, replay and tenant boundaries."""

import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.test import Client, override_settings
from django_otp.oath import TOTP  # type: ignore[import-untyped]

from randirandi import labels
from randirandi.core.audit_types import AuditAction
from randirandi.core.login_codes import VerifiedCode
from randirandi.core.member_data import export_member
from randirandi.core.models import AuditEvent, Authenticator, Member, SecurityNotice
from randirandi.core.second_factor import (
    prepare_authenticator,
    reset_authenticator,
    reset_member_authenticator,
    verify_authenticator,
)
from randirandi.core.security_notices import send_security_notices
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.operator_erasure import erase
from tests.test_access_gate import PASSWORD, with_password
from tests.test_login_sessions import (  # noqa: F401
    NOW,
    get,
    page_settings,
    post,
    request_code,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def clocks() -> Iterator[None]:
    with (
        mock.patch("randirandi.core.login_session_views.utc_now", return_value=NOW),
        mock.patch("randirandi.core.authenticator_views.utc_now", return_value=NOW),
        mock.patch("randirandi.core.second_factor.utc_now", return_value=NOW),
    ):
        yield


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def proof(member: Member) -> VerifiedCode:
    member.refresh_from_db()
    return VerifiedCode(member, member.get_session_auth_hash())


def token(device: Authenticator, offset: int = 0) -> str:
    clock = TOTP(bytes.fromhex(device.plain_secret()), step=30, digits=6)
    clock.time = NOW.timestamp() + offset
    return f"{clock.token():06d}"


def email_factor(client: Client, member: Member) -> None:
    response = post(client, "/login/code/", code=request_code(client, member))
    assert (
        response.status_code == 302
        and response.headers["Location"] == "/login/authenticator/"
    )
    assert "_auth_user_id" not in client.session
    assert get(client, "/login/authenticator/").status_code == 200


def test_real_admin_enrollment_two_factors_rotation_and_qr_access(
    made: made_up.MadeUp,
) -> None:
    client = Client(enforce_csrf_checks=True)
    with application_login():
        email_factor(client, made.administrator)
        pending_key = client.session.session_key
        assert pending_key is not None
        assert get(client).status_code == 302
        device = Authenticator.objects.of(made.organization).get(
            member=made.administrator
        )
        assert not device.confirmed
        qr = f"/login/authenticator/qr/{device.pk}/"
        image = get(client, qr)
        assert image.status_code == 200 and image["Content-Type"] == "image/svg+xml"
        assert "no-store" in image["Cache-Control"]
        assert get(Client(), qr).status_code == 404
        response = post(client, "/login/authenticator/", code=token(device))
        assert response.status_code == 302 and response.headers["Location"] == "/"
        assert client.session["admin_confirmed"] is True
        assert client.session.session_key != pending_key
        assert not SessionStore(session_key=pending_key).exists(pending_key)
        device.refresh_from_db()
        assert device.confirmed and get(client).headers["Location"] == "/book/"
        assert get(client, "/login/authenticator/").headers["Location"] == "/"
        assert get(client).headers["Location"] == "/book/"
        assert get(client, qr).status_code == 404
        assert (
            AuditEvent.objects.of(made.organization)
            .filter(action=AuditAction.ADMIN_LOGIN, object_id=made.administrator.pk)
            .count()
            == 1
        )
        assert (
            AuditEvent.objects.of(made.organization)
            .filter(
                action=AuditAction.ENROLL_AUTHENTICATOR, object_id=made.administrator.pk
            )
            .count()
            == 1
        )


def test_enrolled_login_does_not_show_secret_and_refuses_replay(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    email_factor(client, made.administrator)
    device = Authenticator.objects.of(made.organization).get(member=made.administrator)
    used = token(device)
    post(client, "/login/authenticator/", code=used)
    post(client, "/logout/")
    email_factor(client, made.administrator)
    page = get(client, "/login/authenticator/").content.decode()
    assert (
        '"setupKey": ""' in page
        and device.secret not in page
        and device.plain_secret() not in page
    )
    assert post(client, "/login/authenticator/", code=used).status_code == 400
    assert "_auth_user_id" not in client.session
    assert (
        post(client, "/login/authenticator/", code=token(device, 30)).status_code == 302
    )


def test_five_wrong_attempts_are_shared_across_new_email_proofs(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    email_factor(client, made.administrator)
    device = Authenticator.objects.of(made.organization).get(member=made.administrator)
    for _ in range(5):
        assert post(client, "/login/authenticator/", code="bad").status_code == 400
    email_factor(client, made.administrator)
    assert post(client, "/login/authenticator/", code=token(device)).status_code == 400
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("change", ["expiry", "revoke", "email", "role", "reset"])
def test_pending_factor_requires_current_authority(
    made: made_up.MadeUp, change: str
) -> None:
    client = Client()
    email_factor(client, made.administrator)
    device = Authenticator.objects.of(made.organization).get(member=made.administrator)
    if change == "expiry":
        session = client.session
        session["pending_at"] = NOW.timestamp() - 600
        session.save()
    elif change == "reset":
        reset_authenticator(
            organization=made.organization,
            member_id=made.administrator.pk,
            no_other_administrator_available=True,
        )
    else:
        values: dict[str, dict[str, str | object]] = {
            "revoke": {"status": "disabled"},
            "email": {"email": "changed@example.test"},
            "role": {"role": made.standard},
        }
        Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
            **values[change]
        )
    assert (
        post(client, "/login/authenticator/", code=token(device)).headers["Location"]
        == "/login/"
    )
    assert "_auth_user_id" not in client.session


def test_competing_codes_accept_once_under_application_permissions(
    made: made_up.MadeUp,
) -> None:
    verified = proof(made.administrator)
    device = prepare_authenticator(verified)
    assert device is not None
    code = token(device)

    def run() -> bool:
        try:
            return verify_authenticator(verified, code, network="192.0.2.5") is not None
        finally:
            connections.close_all()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: run(), range(2))) == [False, True]


def test_reset_invalidates_sessions_and_queues_each_admin_privately(
    made: made_up.MadeUp,
) -> None:
    client = Client()
    email_factor(client, made.administrator)
    device = Authenticator.objects.of(made.organization).get(member=made.administrator)
    post(client, "/login/authenticator/", code=token(device))
    made.member.role = made.board
    made.member.save(update_fields=["role"])
    other_device = prepare_authenticator(proof(made.member))
    assert other_device is not None
    verify_authenticator(proof(made.member), token(other_device), network="192.0.2.5")
    with application_login():
        reset_authenticator(
            organization=made.organization,
            member_id=made.administrator.pk,
            actor=made.member,
        )
        assert get(client).status_code == 302
        assert (
            not Authenticator.objects.of(made.organization)
            .filter(member=made.administrator)
            .exists()
        )
        notices = SecurityNotice.objects.of(made.organization).filter(
            event__action=AuditAction.RESET_AUTHENTICATOR
        )
        assert notices.count() == 2
        mail.outbox.clear()
        send_security_notices()
        assert {tuple(message.to) for message in mail.outbox} == {
            (made.member.email,),
            (made.administrator.email,),
        }
        assert all(
            device.secret not in str(message.body)
            and device.plain_secret() not in str(message.body)
            and not message.cc
            and not message.bcc
            for message in mail.outbox
        )
        assert not notices.filter(delivered_at=None).exists()
        send_security_notices()
        assert len(mail.outbox) == 2


def test_reset_refuses_self_cross_org_member_and_unconfirmed_operator(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("elsewhere", day=made_up.A_DAY + timedelta(days=7))
    for actor in (made.administrator, made.member, other.administrator, None):
        with pytest.raises(PermissionDenied):
            reset_authenticator(
                organization=made.organization,
                member_id=made.administrator.pk,
                actor=actor,
            )
    assert (
        not AuditEvent.objects.of(made.organization)
        .filter(action=AuditAction.RESET_AUTHENTICATOR)
        .exists()
    )


def test_export_and_erasure_never_disclose_or_retain_authenticator(
    made: made_up.MadeUp,
) -> None:
    device = prepare_authenticator(proof(made.administrator))
    assert device is not None
    exported = export_member(
        actor=made.administrator,
        member_id=made.administrator.pk,
        **fresh(made.administrator, now=NOW),
    )
    assert device.secret not in str(exported) and "secret" not in str(exported)
    assert device.plain_secret() not in str(exported)
    erase(made, made.administrator.pk)
    assert (
        not Authenticator.objects.of(made.organization)
        .filter(member=made.administrator)
        .exists()
    )


def test_refused_young_audit_deletion_does_not_destroy_queued_alert(
    made: made_up.MadeUp,
) -> None:
    reset_authenticator(
        organization=made.organization,
        member_id=made.administrator.pk,
        no_other_administrator_available=True,
    )
    event = AuditEvent.objects.of(made.organization).get(
        action=AuditAction.OPERATOR_RESET_AUTHENTICATOR
    )
    with application_login():
        assert (
            AuditEvent.objects.of(made.organization).filter(pk=event.pk).delete()[0]
            == 0
        )
        assert (
            SecurityNotice.objects.of(made.organization)
            .filter(event=event, delivered_at=None)
            .count()
            == 1
        )


def test_failed_reset_alert_retries_without_logging_mail_data(
    made: made_up.MadeUp, caplog: pytest.LogCaptureFixture
) -> None:
    from django.core.mail.backends.locmem import EmailBackend
    from django.db import connection

    reset_authenticator(
        organization=made.organization,
        member_id=made.administrator.pk,
        no_other_administrator_available=True,
    )

    def fail(*args: object, **kwargs: object) -> int:
        assert not connection.in_atomic_block
        raise RuntimeError("private@example.test sensitive-server-response")

    with (
        mock.patch("randirandi.core.security_notices.utc_now", return_value=NOW),
        mock.patch.object(EmailBackend, "send_messages", side_effect=fail) as sender,
    ):
        send_security_notices()
        send_security_notices()
        assert sender.call_count == 1
    assert (
        "private@example.test" not in caplog.text
        and "sensitive-server-response" not in caplog.text
    )
    pending = SecurityNotice.objects.of(made.organization).filter(delivered_at=None)
    assert pending.count() == 1
    with mock.patch(
        "randirandi.core.security_notices.utc_now",
        return_value=NOW + timedelta(minutes=5),
    ):
        send_security_notices()
    assert not pending.exists()


@pytest.mark.keeps_authenticators
@pytest.mark.parametrize("retained", ["confirmed", "reset", "attempt", "alert"])
def test_schema_downgrade_refuses_retained_factors_resets_and_attempts(
    made: made_up.MadeUp, retained: str
) -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from randirandi.core.models import LoginAttempt

    if retained == "confirmed":
        Authenticator.objects.of(made.organization).update(confirmed=True)
    elif retained == "reset":
        Member.objects.of(made.organization).filter(pk=made.member.pk).update(
            authenticator_generation=1
        )
    elif retained == "attempt":
        LoginAttempt.objects.create(
            organization=made.organization,
            member=made.administrator,
            at=NOW,
            network="192.0.2.5",
            kind="mfa",
            outcome="refused",
        )
    else:
        SecurityNotice.objects.of(made.organization).update(delivered_at=None)
    # The floor plans migration would refuse first .
    made_up.plain_floor_plans(made.organization)
    # 0044 refuses first while an authenticator exists; take the row away to
    # reach the older refusal for the other three.
    if retained != "confirmed":
        Authenticator.objects.of(made.organization).delete()
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(
            RuntimeError,
            match="Reversing 0044 is refused"
            if retained == "confirmed"
            else "Cannot reverse administrator authentication",
        ):
            MigrationExecutor(connection).migrate([("core", "0006_login_requests")])
    finally:
        MigrationExecutor(connection).migrate(latest)
    # Refusal happens before the first reverse operation, retaining every row.
    assert Authenticator.objects.of(made.organization).count() == (
        1 if retained == "confirmed" else 0
    )
    assert SecurityNotice.objects.of(made.organization).count() == 1
    assert (
        MigrationExecutor(connection).migration_plan(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
        == []
    )


def test_concurrent_setup_uses_one_key_and_guessing_stays_limited(
    made: made_up.MadeUp,
) -> None:
    verified = proof(made.administrator)

    def prepare(_index: int) -> tuple[int, str]:
        try:
            device = prepare_authenticator(verified)
            assert device is not None
            return device.pk, device.plain_secret()
        finally:
            connections.close_all()

    def guess(_index: int) -> VerifiedCode | None:
        try:
            return verify_authenticator(verified, "invalid", network="192.0.2.7")
        finally:
            connections.close_all()

    with application_login(), ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(prepare, range(4)))) == 1
        assert all(result is None for result in pool.map(guess, range(8)))
    from randirandi.core.models import LoginAttempt

    attempts = LoginAttempt.objects.of(made.organization).filter(kind="mfa")
    assert attempts.filter(outcome="refused").count() == 5
    assert attempts.filter(outcome="limited").count() == 3


def test_a_member_reset_from_the_page_signs_in_with_the_email_code_and_sets_up_again(
    made: made_up.MadeUp,
) -> None:
    made.member.role = made.board
    made.member.save(update_fields=["role"])
    old = prepare_authenticator(proof(made.member))
    assert old is not None
    verify_authenticator(proof(made.member), token(old), network="192.0.2.5")
    # The signed-in administrator resets the other on their own current code.
    reset_member_authenticator(
        actor=made.administrator,
        member_id=made.member.pk,
        code=fresh(made.administrator, now=NOW)["code"],
        network="192.0.2.6",
    )
    assert (
        not Authenticator.objects.of(made.organization)
        .filter(member=made.member)
        .exists()
    )
    client = Client()
    with application_login():
        email_factor(client, made.member)
        page = get(client, "/login/authenticator/").content.decode()
        assert '"setupKey": ""' not in page
        new = Authenticator.objects.of(made.organization).get(member=made.member)
        assert not new.confirmed and new.secret != old.secret
        response = post(client, "/login/authenticator/", code=token(new))
        assert response.status_code == 302 and response.headers["Location"] == "/"
        assert client.session["admin_confirmed"] is True
    new.refresh_from_db()
    assert new.confirmed


@pytest.mark.parametrize("language", ["en", "nb"])
def test_with_no_key_set_a_code_is_refused_and_enrolment_answers_a_plain_refusal(
    made: made_up.MadeUp, settings: Any, language: str
) -> None:
    made.organization.language = language
    made.organization.save(update_fields=["language"])
    device = prepare_authenticator(proof(made.administrator))
    assert device is not None
    good = token(device)
    key = settings.AUTHENTICATOR_KEY
    settings.AUTHENTICATOR_KEY = ""
    # Checking a code: refused like any wrong code, not a server error.
    assert (
        verify_authenticator(proof(made.administrator), good, network="192.0.2.5")
        is None
    )
    # Enrolment over HTTP, with a pending device and with none.
    for pending in (True, False):
        settings.AUTHENTICATOR_KEY = key
        client = Client()
        email_factor(client, made.administrator)
        settings.AUTHENTICATOR_KEY = ""
        if not pending:
            Authenticator.objects.of(made.organization).delete()
        for response in (
            get(client, "/login/authenticator/"),
            post(client, "/login/authenticator/", code=good),
        ):
            assert response.status_code == 503
            assert (
                labels.word(made.organization, "login.auth-unavailable")
                in response.content.decode()
            )
            assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("language", ["en", "nb"])
def test_authenticator_language_covers_setup_native_form_and_refusal(
    made: made_up.MadeUp, language: str, confirmed: bool
) -> None:
    made.organization.language = language
    made.organization.save(update_fields=["language"])
    client = Client(enforce_csrf_checks=True)
    email_factor(client, made.administrator)
    device = Authenticator.objects.of(made.organization).get(member=made.administrator)
    if confirmed:
        device.confirmed = True
        device.save(update_fields=["confirmed"])
    response = get(client, "/login/authenticator/")
    # The JSON catalogue contains every translation, even unused ones. Check
    # the rendered fallback independently of the enhanced React form.
    text = re.sub(
        r"<script\b[^>]*>.*?</script>", "", response.content.decode(), flags=re.S
    )
    title = "login.auth-title" if confirmed else "login.auth-setup-title"
    help_key = "login.auth-help" if confirmed else "login.auth-setup-help"
    for key in (
        title,
        help_key,
        "login.auth-code",
        "login.auth-restart",
        "login.title",
    ):
        assert labels.word(made.organization, key) in text
    assert f'lang="{language}"' in text
    if not confirmed:
        assert labels.word(made.organization, "login.auth-key") in text
        assert labels.word(made.organization, "login.auth-qr") in text
    refused = post(client, "/login/authenticator/", code="invalid")
    assert refused.status_code == 400
    assert labels.word(made.organization, "login.auth-error") in re.sub(
        r"<script\b[^>]*>.*?</script>", "", refused.content.decode(), flags=re.S
    )
    assert "_auth_user_id" not in client.session


def test_production_gate_does_not_replace_administrator_factors(
    made: made_up.MadeUp,
) -> None:
    client = Client(enforce_csrf_checks=True)
    with override_settings(ENVIRONMENT="production", ACCESS_GATE_PASSWORD=PASSWORD):
        assert get(client, "/login/").status_code == 401
        client.defaults["HTTP_AUTHORIZATION"] = with_password(PASSWORD)["Authorization"]
        email_factor(client, made.administrator)
        assert "_auth_user_id" not in client.session
        device = Authenticator.objects.of(made.organization).get(
            member=made.administrator
        )
        assert post(client, "/login/authenticator/", code="bad").status_code == 400
        assert (
            post(client, "/login/authenticator/", code=token(device)).status_code == 302
        )
        assert client.session["admin_confirmed"] is True
        with override_settings(ACCESS_GATE_PASSWORD=""):
            fresh = Client(enforce_csrf_checks=True)
            email_factor(fresh, made.administrator)
            assert "_auth_user_id" not in fresh.session
