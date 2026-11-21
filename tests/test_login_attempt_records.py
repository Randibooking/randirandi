# SPDX-License-Identifier: AGPL-3.0-only
"""Attempt records survive malformed submissions, worker delay and queue expiry."""

from datetime import timedelta
from unittest import mock

import pytest
from django.db import OperationalError
from django.test import Client
from psycopg.errors import SerializationFailure

from randirandi.core.login_codes import VerifiedCode, issue_code, verify_code
from randirandi.core.login_delivery import enqueue_code, send_pending_codes
from randirandi.core.member_data import utc_now
from randirandi.core.models import LoginAttempt, LoginCode, LoginRequest, Member
from randirandi.core.retention import purge_organization
from randirandi.core.second_factor import verify_authenticator
from tests import made_up
from tests.capacity_app import application_login
from tests.test_login_sessions import get, page_settings, post  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
BROWSER = "fictional_browser_binding_only_123456789"
NETWORK = "192.0.2.48"


def fresh_members() -> made_up.MadeUp:
    made = made_up.everything()
    for model in (LoginAttempt, LoginCode, LoginRequest):
        model.objects.of(made.organization).delete()
    return made


@pytest.mark.parametrize(
    ("path", "data", "kind", "outcome"),
    [
        ("/login/", {"identifier": "invalid private text"}, "request", "unknown"),
        ("/login/code/", {"code": "123456"}, "verify", "refused"),
        ("/login/authenticator/", {"code": "123456"}, "mfa", "refused"),
    ],
)
def test_unbound_submission_has_one_private_record(
    path: str,
    data: dict[str, str],
    kind: str,
    outcome: str,
) -> None:
    made = fresh_members()
    client = Client(enforce_csrf_checks=True)
    with application_login():
        get(client, "/login/")
        assert not LoginAttempt.objects.of(made.organization).exists()
        assert post(client, path, **data).status_code in {302, 400}
        row = LoginAttempt.objects.of(made.organization).get()
        assert (row.kind, row.outcome, row.member_id) == (kind, outcome, None)
        assert row.network == "127.0.0.1"
        assert "invalid private text" not in str(row.__dict__)


def test_oversized_code_preserves_member_and_consumes_a_guess() -> None:
    made = fresh_members()
    with application_login():
        issued = issue_code(
            organization=made.organization,
            member_id=made.member.pk,
            browser=BROWSER,
            network=NETWORK,
        )
        assert issued is not None
        assert (
            verify_code(
                organization=made.organization,
                challenge=issued.challenge,
                browser=BROWSER,
                code="9" * 2048,
                network=NETWORK,
            )
            is None
        )
        row = LoginAttempt.objects.of(made.organization).get(kind="verify")
        assert row.member_id == made.member.pk and row.outcome == "refused"
        assert LoginCode.objects.of(made.organization).get().attempts == 1


def test_stale_admin_authority_retains_scoped_member_record() -> None:
    made = fresh_members()
    proof = VerifiedCode(made.administrator, made.administrator.get_session_auth_hash())
    with application_login():
        Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
            authenticator_generation=1
        )
        assert verify_authenticator(proof, "123456", network=NETWORK) is None
        row = LoginAttempt.objects.of(made.organization).get()
        assert (row.member_id, row.kind, row.outcome) == (
            made.administrator.pk,
            "mfa",
            "refused",
        )


def test_exhausted_factor_contention_records_refusal_without_retrying_factor() -> None:
    made = fresh_members()
    proof = VerifiedCode(made.administrator, made.administrator.get_session_auth_hash())
    error = OperationalError("fictional contention")
    error.__cause__ = SerializationFailure()
    with (
        application_login(),
        mock.patch(
            "randirandi.core.second_factor.organization_transaction", side_effect=error
        ) as factor,
    ):
        assert verify_authenticator(proof, "123456", network=NETWORK) is None
        assert factor.call_count == 1
        row = LoginAttempt.objects.of(made.organization).get()
        assert (row.member_id, row.kind, row.outcome) == (
            made.administrator.pk,
            "mfa",
            "limited",
        )


@pytest.mark.parametrize("via_retention", [False, True])
def test_expired_queue_preserves_one_original_record(via_retention: bool) -> None:
    made = fresh_members()
    moment = utc_now(None)
    with application_login():
        enqueue_code(
            organization=made.organization,
            identifier=made.member.email,
            browser=BROWSER,
            network=NETWORK,
        )
        original = moment - timedelta(minutes=11)
        LoginRequest.objects.of(made.organization).update(at=original)
        for _ in range(2):
            if via_retention:
                purge_organization(made.organization, now=moment)
            else:
                with mock.patch(
                    "randirandi.core.login_delivery.utc_now", return_value=moment
                ):
                    send_pending_codes()
        row = LoginAttempt.objects.of(made.organization).get()
        assert (row.at, row.network, row.kind, row.outcome, row.member_id) == (
            original,
            NETWORK,
            "request",
            "unknown",
            None,
        )
        assert not LoginRequest.objects.of(made.organization).exists()
        assert not LoginCode.objects.of(made.organization).exists()


def test_delayed_delivery_records_request_time_and_fresh_code_expiry() -> None:
    made = fresh_members()
    moment = utc_now(None)
    with application_login():
        enqueue_code(
            organization=made.organization,
            identifier=made.member.email,
            browser=BROWSER,
            network=NETWORK,
        )
        original = moment - timedelta(minutes=4)
        LoginRequest.objects.of(made.organization).update(at=original)
        with mock.patch("randirandi.core.login_delivery.utc_now", return_value=moment):
            send_pending_codes()
        row = LoginAttempt.objects.of(made.organization).get()
        assert row.at == original and row.outcome == "issued"
        assert LoginCode.objects.of(made.organization).get().expires_at == (
            moment + timedelta(minutes=10)
        )
