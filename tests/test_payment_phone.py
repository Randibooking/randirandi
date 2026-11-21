# SPDX-License-Identifier: AGPL-3.0-only
"""The member's saved phone reaches Vipps in its format, and a retry repeats it."""

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest

from randirandi.core import payment_reconciliation, payment_recovery, payments
from randirandi.core.member_data import erase_identity
from randirandi.core.models import Member, Payment
from randirandi.vipps import Client, VippsError, VippsResponseError, vipps_phone
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_ordering import NOW, checkout

pytestmark = pytest.mark.django_db(transaction=True)
PHONE = "+4790000001"
VIPPS_PHONE = "4790000001"


def creates(wire: Wire) -> list[dict[str, Any]]:
    return [
        json.loads(body or b"")
        for method, path, _headers, body in wire.calls
        if method == "POST" and path == "/epayment/v1/payments"
    ]


def raw_creates(wire: Wire) -> list[bytes]:
    return [
        body or b""
        for method, path, _headers, body in wire.calls
        if method == "POST" and path == "/epayment/v1/payments"
    ]


def set_phone(made: Example, phone: str) -> None:
    Member.objects.of(made.org).filter(pk=made.member.pk).update(phone=phone)
    made.member.refresh_from_db()


def start(made: Example) -> Any:
    order = checkout(made)
    with application_login():
        return payments.checkout(
            member=made.member, order_public_id=order.public_id, now=NOW
        )


def test_body_carries_the_members_phone_in_vipps_format(
    made: Example, wire: Wire
) -> None:
    set_phone(made, PHONE)
    assert start(made) is not None
    (body,) = creates(wire)
    assert body["customer"] == {"phoneNumber": VIPPS_PHONE}
    payment = Payment.objects.of(made.org).get()
    assert payment.create_completed_at == NOW and payment.create_phone == ""


def test_member_without_a_phone_sends_no_customer(made: Example, wire: Wire) -> None:
    assert made.member.phone == ""
    assert start(made) is not None
    (body,) = creates(wire)
    assert "customer" not in body


def test_a_number_vipps_would_refuse_sends_no_customer(
    made: Example, wire: Wire
) -> None:
    set_phone(made, "+471234")
    assert start(made) is not None
    (body,) = creates(wire)
    assert "customer" not in body


def test_retry_after_a_lost_reply_sends_identical_bytes_despite_a_new_phone(
    made: Example, wire: Wire
) -> None:
    set_phone(made, PHONE)
    wire.lose_after.add("create")
    order = checkout(made)
    with application_login(), pytest.raises(VippsError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    pending = Payment.objects.of(made.org).get()
    assert pending.create_completed_at is None and pending.create_phone == VIPPS_PHONE
    set_phone(made, "+4790000002")
    with application_login():
        result = payments.checkout(
            member=made.member, order_public_id=order.public_id, now=NOW
        )
    # The fake answers 409 (an error) when a body differs under the same key.
    assert result is not None
    first, second = raw_creates(wire)
    assert first == second
    assert json.loads(second)["customer"] == {"phoneNumber": VIPPS_PHONE}
    assert Payment.objects.of(made.org).get().create_phone == ""


def test_recovery_create_repeats_the_frozen_phone(made: Example, wire: Wire) -> None:
    set_phone(made, PHONE)
    wire.fail_before.add("create")
    order = checkout(made)
    with application_login(), pytest.raises(VippsError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    set_phone(made, "")
    payment = Payment.objects.of(made.org).get()
    payment_recovery.read(payment, now=NOW)
    first, second = raw_creates(wire)
    assert first == second
    assert json.loads(second)["customer"] == {"phoneNumber": VIPPS_PHONE}
    assert Payment.objects.of(made.org).get().create_phone == ""


def test_erasing_the_member_clears_a_pending_phone_copy(
    made: Example, wire: Wire
) -> None:
    set_phone(made, PHONE)
    wire.fail_before.add("create")
    order = checkout(made)
    with application_login(), pytest.raises(VippsError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    assert Payment.objects.of(made.org).get().create_phone == VIPPS_PHONE
    erase_identity(made.member)
    assert Payment.objects.of(made.org).get().create_phone == ""


def test_nothing_logs_or_prints_the_phone(
    made: Example,
    wire: Wire,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_phone(made, PHONE)
    caplog.set_level(logging.DEBUG)
    wire.lose_after.add("create")
    order = checkout(made)
    with application_login():
        with pytest.raises(VippsError) as error:
            payments.checkout(
                member=made.member, order_public_id=order.public_id, now=NOW
            )
        assert payments.checkout(
            member=made.member, order_public_id=order.public_id, now=NOW
        )
    assert len(creates(wire)) == 2  # the phone really was sent both times
    out = capsys.readouterr()
    seen = caplog.text + out.out + out.err + repr(error.value)
    assert VIPPS_PHONE not in seen and PHONE not in seen
    assert "phoneNumber" not in seen


def pending_create(made: Example, wire: Wire, how: str) -> Payment:
    """A payment whose create left no redirect; `how` is what the wire did."""
    set_phone(made, PHONE)
    getattr(wire, how).add("create")
    order = checkout(made)
    with application_login(), pytest.raises(VippsError):
        payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get()
    assert payment.create_phone == VIPPS_PHONE and payment.create_completed_at is None
    return payment


@pytest.mark.parametrize("state", ["EXPIRED", "ABORTED", "TERMINATED"])
def test_copy_is_empty_after_the_payment_ends(
    made: Example, wire: Wire, state: str
) -> None:
    payment = pending_create(made, wire, "lose_after")
    wire.payments[payment.provider_reference].state = state
    payment_reconciliation.reconcile(made.org.pk, payment.order_id, now=NOW)
    ended = Payment.objects.of(made.org).get()
    assert ended.state != "created" and ended.create_phone == ""


def test_copy_stays_while_the_payment_is_only_created(
    made: Example, wire: Wire
) -> None:
    payment = pending_create(made, wire, "lose_after")
    payment_reconciliation.reconcile(made.org.pk, payment.order_id, now=NOW)
    kept = Payment.objects.of(made.org).get()
    assert kept.state == "created" and kept.create_phone == VIPPS_PHONE


def test_hold_expiry_alone_keeps_the_copy_for_the_recovery_create(
    made: Example, wire: Wire
) -> None:
    payment = pending_create(made, wire, "fail_before")
    late = NOW + timedelta(hours=1)
    payment_recovery.release_ineligible(made.org, payment, late)
    assert Payment.objects.of(made.org).get().create_phone == VIPPS_PHONE
    # The recovery create still has it, and sends it.
    payment_reconciliation.reconcile(made.org.pk, payment.order_id, now=late)
    first, second = raw_creates(wire)
    assert first == second
    assert json.loads(second)["customer"] == {"phoneNumber": VIPPS_PHONE}


@pytest.mark.parametrize("path", ["checkout", "recovery"])
@pytest.mark.parametrize(
    ("status", "kept"),
    [(400, False), (403, False), (422, False), (401, True), (408, True),
     (409, True), (429, True), (500, True), (0, True)],
)  # fmt: skip
def test_unretryable_refusal_empties_the_copy_and_others_keep_it(
    made: Example,
    wire: Wire,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    status: int,
    kept: bool,
) -> None:
    payment = pending_create(made, wire, "fail_before")
    real = Client._payment

    def refuse(self: Client, method: str, *args: Any, **kwargs: Any) -> Any:
        if method != "POST":
            return real(self, method, *args, **kwargs)
        raise VippsError(status)

    monkeypatch.setattr(Client, "_payment", refuse)
    with application_login(), pytest.raises(VippsError):
        if path == "checkout":
            order = payment.order
            payments.checkout(
                member=made.member, order_public_id=order.public_id, now=NOW
            )
        else:
            payment_recovery.read(payment, now=NOW)
    after = Payment.objects.of(made.org).get().create_phone
    assert after == (VIPPS_PHONE if kept else "")


def test_a_malformed_vipps_reply_keeps_the_copy(
    made: Example, wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = pending_create(made, wire, "fail_before")

    real = Client._payment

    def malformed(self: Client, method: str, *args: Any, **kwargs: Any) -> Any:
        if method != "POST":
            return real(self, method, *args, **kwargs)
        raise VippsResponseError(400)

    monkeypatch.setattr(Client, "_payment", malformed)
    with application_login(), pytest.raises(VippsResponseError):
        payment_recovery.read(payment, now=NOW)
    assert Payment.objects.of(made.org).get().create_phone == VIPPS_PHONE


def test_one_format_rule_serves_the_service_and_the_client(
    made: Example, wire: Wire
) -> None:
    assert vipps_phone(PHONE) == vipps_phone(VIPPS_PHONE) == VIPPS_PHONE
    assert vipps_phone("+471234") == "" and vipps_phone("") == ""
    client = wire_client(made)
    with pytest.raises(ValueError):
        client.create(
            "ref",
            100,
            "NOK",
            "https://x.example/r",
            key="k",
            description="d",
            phone=PHONE,
        )
    with pytest.raises(ValueError):
        client.create(
            "ref",
            100,
            "NOK",
            "https://x.example/r",
            key="k",
            description="d",
            phone="4712",
        )


def wire_client(made: Example) -> Client:
    return payments.client_for(
        Payment(
            currency="NOK",
            create_key=uuid.uuid4(),
            credential_name="VIPPS",
            merchant_number="123456",
        )
    )
