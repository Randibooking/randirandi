# SPDX-License-Identifier: AGPL-3.0-only
"""Durable email delivery without identity lookup in the request path."""

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest import mock

import pytest
from django.core import mail
from django.core.mail import EmailMessage
from django.core.mail.backends.locmem import EmailBackend
from django.db import close_old_connections, connection, connections

from randirandi.core import login_delivery
from randirandi.core.login_codes import verify_code
from randirandi.core.login_delivery import enqueue_code, send_pending_codes
from randirandi.core.login_identity import identifier_digest
from randirandi.core.member_data import export_member, utc_now
from randirandi.core.models import LoginAttempt, LoginCode, LoginRequest, Member
from randirandi.core.retention import purge_organization
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.mail_checks import check_system_mail
from tests.operator_erasure import erase

pytestmark = pytest.mark.django_db(transaction=True)
BROWSER = "fictional_browser_binding_only_123456789"
NETWORK = "192.0.2.48"


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def enqueue(made: made_up.MadeUp, identifier: str | None = None) -> str:
    return enqueue_code(
        organization=made.organization,
        identifier=made.member.email if identifier is None else identifier,
        browser=BROWSER,
        network=NETWORK,
    )


@pytest.mark.parametrize("by_phone", [False, True])
def test_worker_delivers_browser_bound_code_with_application_permissions(
    made: made_up.MadeUp, by_phone: bool
) -> None:
    with application_login():
        challenge = enqueue(made, made.member.phone if by_phone else None)
        assert not mail.outbox
        assert (
            not LoginCode.objects.of(made.organization)
            .filter(challenge=challenge)
            .exists()
        )
        row = LoginRequest.objects.of(made.organization).get(challenge=challenge)
        assert row.browser_digest != BROWSER
        assert row.identifier_digest == identifier_digest(
            made.organization.pk, made.member.phone if by_phone else made.member.email
        )
        assert made.member.email not in str(row.__dict__)
        assert made.member.phone not in str(row.__dict__)
        send_pending_codes()
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == [made.member.email]
        check_system_mail(message, made.organization, reply=False)
        match = re.search(r"\b[0-9]{6}\b", str(message.body))
        assert match is not None
        proof = verify_code(
            organization=made.organization,
            challenge=challenge,
            browser=BROWSER,
            code=match.group(),
            network=NETWORK,
        )
        assert proof is not None and proof.member == made.member
        assert (
            not LoginRequest.objects.of(made.organization)
            .filter(challenge=challenge)
            .exists()
        )
        send_pending_codes()
        assert len(mail.outbox) == 1


def test_queued_revoked_unknown_or_other_org_identity_never_receives_email(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("elsewhere")
    enqueue(made)
    enqueue(made, "unknown@example.test")
    enqueue(made, other.member.email)
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        status="disabled"
    )
    send_pending_codes()
    assert not mail.outbox
    assert (
        LoginAttempt.objects.of(made.organization)
        .filter(network=NETWORK, outcome="unknown")
        .count()
        == 3
    )


def test_queue_is_bounded_before_background_identity_lookup(
    made: made_up.MadeUp,
) -> None:
    for _ in range(22):
        enqueue(made, "unknown@example.test")
    assert (
        LoginRequest.objects.of(made.organization).filter(network=NETWORK).count() == 20
    )
    assert (
        LoginAttempt.objects.of(made.organization)
        .filter(network=NETWORK, outcome="limited")
        .count()
        == 2
    )
    send_pending_codes()
    assert (
        not LoginRequest.objects.of(made.organization).filter(network=NETWORK).exists()
    )
    assert not mail.outbox


def test_competing_workers_send_only_once(made: made_up.MadeUp) -> None:
    enqueue(made)
    barrier = Barrier(2)

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            send_pending_codes()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    assert len(mail.outbox) == 1


def test_provider_exception_is_not_logged_or_retried(
    made: made_up.MadeUp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    challenge = enqueue(made)
    with mock.patch(
        "django.core.mail.backends.locmem.EmailBackend.send_messages",
        side_effect=RuntimeError("recipient private@example.test code 987654"),
    ) as send:
        send_pending_codes()
        send_pending_codes()
        assert send.call_count == 1
    assert "login email delivery failed" in caplog.text
    assert "private@example.test" not in caplog.text and "987654" not in caplog.text
    assert (
        not LoginRequest.objects.of(made.organization)
        .filter(challenge=challenge)
        .exists()
    )


def test_stale_queue_expires_and_personal_export_erasure_are_scoped(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("elsewhere")
    challenge = enqueue(made)
    other_challenge = enqueue(other)
    exported = export_member(
        actor=made.administrator, member_id=made.member.pk, **fresh(made.administrator)
    )
    assert len(exported["login_requests"]) == 1
    assert set(exported["login_requests"][0]) == {"id", "at", "claimed_at"}
    erase(made, made.member.pk)
    assert (
        not LoginRequest.objects.of(made.organization)
        .filter(challenge=challenge)
        .exists()
    )
    row = LoginRequest.objects.of(other.organization).get(challenge=other_challenge)
    row.at = utc_now(None) - timedelta(minutes=10)
    row.save(update_fields=["at"])
    purge_organization(other.organization)
    assert (
        not LoginRequest.objects.of(other.organization)
        .filter(challenge=other_challenge)
        .exists()
    )
    send_pending_codes()
    assert not mail.outbox


def test_claimed_requests_are_not_counted_twice(made: made_up.MadeUp) -> None:
    moment = utc_now(None)
    challenge = enqueue(made)
    LoginRequest.objects.of(made.organization).filter(challenge=challenge).update(
        identifier_digest="",
        claimed_at=moment,
    )
    LoginAttempt.objects.of(made.organization).bulk_create(
        [
            LoginAttempt(
                organization=made.organization,
                at=moment,
                network=NETWORK,
                kind="request",
                outcome="unknown",
            )
            for _ in range(19)
        ]
    )
    twentieth = enqueue(made)
    assert (
        LoginRequest.objects.of(made.organization).filter(challenge=twentieth).exists()
    )
    twenty_first = enqueue(made)
    assert (
        not LoginRequest.objects.of(made.organization)
        .filter(challenge=twenty_first)
        .exists()
    )


def test_real_background_worker_releases_connection_and_does_not_hold_transaction(
    made: made_up.MadeUp,
) -> None:
    enqueue(made)
    started, release = Event(), Event()
    closed: list[Callable[[], bool]] = []

    def send(backend: EmailBackend, messages: list[EmailMessage]) -> int:
        raw = connection.connection
        assert raw is not None
        closed.append(lambda: bool(raw.closed))
        assert not connection.in_atomic_block and connection.get_autocommit()
        started.set()
        assert release.wait(10)
        return 1

    with mock.patch(
        "django.core.mail.backends.locmem.EmailBackend.send_messages", new=send
    ):
        try:
            login_delivery.wake_delivery()
            assert started.wait(10)
            # A stalled SMTP call does not keep the organization locked.
            enqueue(made, "another@example.test")
            login_delivery.wake_delivery()
            assert len(closed) == 1
        finally:
            release.set()
            assert login_delivery._wake_lock.acquire(timeout=10)
            login_delivery._wake_lock.release()
    assert all(is_closed() for is_closed in closed)
