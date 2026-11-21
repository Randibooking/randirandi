# SPDX-License-Identifier: AGPL-3.0-only
"""the member's outcome read runs the pending provider read
itself, so the page after Vipps tells the outcome without waiting for the
worker. Only the provider wire is fictional; made-up members only."""

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.test import Client as Browser

from randirandi import vipps
from randirandi.core import booking_outcome, payment_callbacks, payments
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.models import Member, Payment
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import get, sign_in
from tests.test_ordering import NOW, checkout
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payment_return import another_member

pytestmark = pytest.mark.django_db(transaction=True)


def returned(made: Example, wire: Wire) -> Payment:
    """A paid order the member has come back to Vipps' return for: a read is
    pending, none has been made."""
    order = checkout(made)
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    payment_callbacks.request_reconciliation(made.org, order.public_id, now=NOW)
    return payment


def vipps_says(wire: Wire, payment: Payment, state: str) -> None:
    wire.payments[payment.provider_reference].state = state
    if state == "AUTHORIZED":
        wire.payments[payment.provider_reference].authorized = payment.amount


def read(made: Example, payment: Payment, member: Member | None = None) -> Any:
    return booking_outcome.read(
        made.org, member or made.member, payment.order.public_id, now=NOW
    )


def gets(wire: Wire) -> list[str]:
    return [path for method, path, _, _ in wire.calls if method == "GET"]


@pytest.mark.parametrize(
    ("vipps", "state", "booking"),
    [
        ("ABORTED", "not-completed", "expired"),
        ("TERMINATED", "not-completed", "expired"),
        ("EXPIRED", "not-completed", "expired"),
        ("AUTHORIZED", "confirmed", "confirmed"),
    ],
)
def test_a_pending_read_is_run_by_the_outcome_read(
    made: Example, wire: Wire, vipps: str, state: str, booking: str
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, vipps)
        found = read(made, payment)
    assert found is not None
    assert found["state"] == state
    assert found["text"] == ("confirmed" if state == "confirmed" else "not completed")
    assert [row["state"] for row in found["bookings"]] == [booking]
    payment.refresh_from_db()
    assert payment.provider_checked_at == NOW


def test_the_throttle_keeps_a_second_read_from_a_second_call(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "ABORTED")
        read(made, payment)
        calls = len(wire.calls)
        read(made, payment)
    assert len(wire.calls) == calls


def test_a_busy_order_stays_settling_and_calls_nobody(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "ABORTED")
        calls = len(wire.calls)
        with psycopg.connect(**connection.get_connection_params()) as other:
            other.execute("SELECT pg_advisory_lock(%s)", [-payment.order_id])
            found = read(made, payment)
            other.execute("SELECT pg_advisory_unlock(%s)", [-payment.order_id])
    assert found is not None and found["state"] == "settling"
    assert len(wire.calls) == calls


def test_a_provider_failure_stays_settling_and_says_nothing_of_it(
    made: Example, wire: Wire, caplog: pytest.LogCaptureFixture
) -> None:
    with application_login():
        payment = returned(made, wire)
        del wire.payments[payment.provider_reference]  # the wire answers 404
        with caplog.at_level(logging.DEBUG):
            found = read(made, payment)
    assert found is not None and found["state"] == "settling"
    assert [row["state"] for row in found["bookings"]] == ["held"]
    assert payment.provider_reference not in json.dumps(found)
    assert payment.provider_reference not in caplog.text
    assert "outcome read failed" in caplog.text
    payment.refresh_from_db()
    assert payment.provider_checked_at is None  # the worker still has it to do


def test_no_pending_read_makes_no_call(made: Example, wire: Wire) -> None:
    order = checkout(made)
    with application_login():
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
        calls = len(wire.calls)
        found = booking_outcome.read(made.org, made.member, order.public_id, now=NOW)
    assert found is not None and found["state"] == "held"
    assert len(wire.calls) == calls


def test_another_members_order_is_not_read_and_calls_nobody(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "ABORTED")
        calls = len(wire.calls)
        assert read(made, payment, another_member(made)) is None
    assert len(wire.calls) == calls
    payment.refresh_from_db()
    assert payment.provider_checked_at is None


def test_the_worker_and_the_outcome_read_make_one_read_between_them(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "ABORTED")
        read(made, payment)
        calls = len(wire.calls)
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=1))
    assert len(wire.calls) == calls


def test_the_page_address_gives_the_answer_after_the_return(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "ABORTED")
        browser = Browser()
        assert sign_in(browser, made.member).status_code == 302
        body = json.loads(
            get(browser, f"/booking/order/{payment.order.public_id}/").content
        )
    assert body["state"] == "not-completed"
    assert uuid.UUID(body["id"]) == payment.order.public_id


def timed(wire: Wire) -> list[float]:
    """The socket timeout in force at each provider call, from now on."""
    seen: list[float] = []
    client = settings.VIPPS_SECRETS._clients["VIPPS"]
    inner = client._transport

    def record(
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        seen.append(vipps._timeout.get())
        return inner(host, method, path, headers, body)

    client._transport = record
    return seen


def test_the_page_read_bounds_each_call_and_the_worker_does_not(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "AUTHORIZED")
        seen = timed(wire)
        read(made, payment)
        page = list(seen)
        seen.clear()
        payment_callbacks.request_reconciliation(
            made.org, payment.order.public_id, now=NOW + timedelta(seconds=30)
        )
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=40))
    assert page and set(page) == {booking_outcome.READ_TIMEOUT} == {3}
    assert seen and set(seen) == {vipps.TIMEOUT} == {10}


def test_a_call_that_times_out_leaves_the_answer_settling(
    made: Example, wire: Wire, caplog: pytest.LogCaptureFixture
) -> None:
    with application_login():
        payment = returned(made, wire)
        client = settings.VIPPS_SECRETS._clients["VIPPS"]

        def slow(
            host: str,
            method: str,
            path: str,
            headers: Mapping[str, str],
            body: bytes | None,
        ) -> tuple[int, bytes]:
            raise TimeoutError("private-timeout-text")

        client._transport = slow
        with caplog.at_level(logging.DEBUG):
            found = read(made, payment)
    assert found is not None and found["state"] == "settling"
    assert "private-timeout-text" not in caplog.text
    assert "outcome read failed" in caplog.text


def test_a_timeout_after_capture_is_completed_by_the_workers_read(
    made: Example, wire: Wire
) -> None:
    with application_login():
        payment = returned(made, wire)
        vipps_says(wire, payment, "AUTHORIZED")
        # Vipps takes the capture; the reply is lost to the page's short timeout.
        wire.lose_after.add("capture")
        found = read(made, payment)
        assert found is not None and found["state"] == "settling"
        payment.refresh_from_db()
        # Left behind: authorized, capture intent saved, read still pending.
        assert payment.state == "authorized" and payment.capture_key is not None
        assert payment.provider_checked_at is None
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=6))
        done = read(made, payment)
    assert done is not None and done["state"] == "confirmed"
    assert len(wire.mutations) == 1  # the same key: captured once
