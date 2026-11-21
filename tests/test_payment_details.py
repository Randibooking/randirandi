# SPDX-License-Identifier: AGPL-3.0-only
"""What the member sees in the Vipps app: description, receipt and the link."""

import json
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi.core import (
    payment_receipt,
    payment_reconciliation,
    payment_recovery,
    payments,
)
from randirandi.core.models import Booking, Order, Organization, Payment
from randirandi.vipps import VippsError
from tests.capacity_app import application_login
from tests.payment_wire import Wire, WirePayment
from tests.payment_wire import wire as wire
from tests.test_calendar import Example, at
from tests.test_calendar import made as made
from tests.test_ordering import NOW, checkout, request

pytestmark = pytest.mark.django_db(transaction=True)
SITE = "https://harbourview.sandbox.example.org/"


def two_bookings(made: Example) -> Order:
    return checkout(
        made, request(made), request(made, starts_at=at(hour=13), ends_at=at(hour=14))
    )


def start(order: Order, made: Example, now: Any = NOW) -> Any:
    with application_login():
        return payments.checkout(
            member=made.member, order_public_id=order.public_id, now=now
        )


def raw_creates(wire: Wire) -> list[bytes]:
    return [
        body or b""
        for method, path, _headers, body in wire.calls
        if method == "POST" and path == "/epayment/v1/payments"
    ]


def category_calls(wire: Wire) -> list[tuple[str, bytes]]:
    return [
        (path, body or b"")
        for method, path, _headers, body in wire.calls
        if method == "PUT"
    ]


def test_create_carries_description_and_a_receipt_that_adds_up(
    made: Example, wire: Wire
) -> None:
    order = two_bookings(made)
    assert start(order, made) is not None
    (raw,) = raw_creates(wire)
    body = json.loads(raw)
    assert body["paymentDescription"] == (
        f"The harbourview housing co-op, reference {order.reference}: "
        "01.03.2027 10:00\u201312:00 and 1 more"
    )
    lines = body["receipt"]["orderLines"]
    assert [line["name"] for line in lines] == [
        "Visit, 01.03.2027 10:00\u201312:00",
        "Visit, 01.03.2027 13:00\u201314:00",
    ]
    assert [line["id"] for line in lines] == ["1", "2"]
    assert [line["totalAmount"] for line in lines] == [200, 100]
    assert sum(line["totalAmount"] for line in lines) == body["amount"]["value"] == 300
    assert body["receipt"]["bottomLine"] == {
        "currency": "NOK",
        "receiptNumber": order.reference,
    }
    assert {line["taxRate"] for line in lines} == {0}
    assert all(line["totalTaxAmount"] == 0 for line in lines)
    # Nobody's name, email or phone.
    assert "Test" not in raw.decode() and "example.com" not in raw.decode()


def test_one_booking_has_no_more(made: Example, wire: Wire) -> None:
    order = checkout(made)
    assert start(order, made) is not None
    (raw,) = raw_creates(wire)
    description = json.loads(raw)["paymentDescription"]
    assert description.endswith(
        f"reference {order.reference}: 01.03.2027 10:00\u201312:00"
    )


def test_tax_rate_splits_each_line_so_it_adds_up(made: Example, wire: Wire) -> None:
    Organization.objects.filter(pk=made.org.pk).update(tax_rate=2500)
    made.org.refresh_from_db()
    assert start(two_bookings(made), made) is not None
    (raw,) = raw_creates(wire)
    lines = json.loads(raw)["receipt"]["orderLines"]
    assert [
        (line["totalAmount"], line["totalAmountExcludingTax"], line["totalTaxAmount"])
        for line in lines
    ] == [(200, 160, 40), (100, 80, 20)]
    assert {line["taxRate"] for line in lines} == {2500}


@pytest.mark.parametrize("rate", [0, 1, 1250, 2500, 10000])
def test_tax_split_always_adds_up_and_rounds_half_up(rate: int) -> None:
    for total in range(0, 3000, 7):
        without_tax, tax = payment_receipt.tax_split(total, rate)
        assert without_tax + tax == total and tax >= 0
        exact = total * rate / (10000 + rate)
        assert abs(tax - exact) <= 0.5
    assert payment_receipt.tax_split(1, 2500) == (1, 0)
    assert payment_receipt.tax_split(3, 2500) == (2, 1)


def test_long_description_is_cut_on_a_word() -> None:
    text = "word " * 40
    cut = payment_receipt.cut(text.strip())
    assert len(cut) <= 100 and cut.endswith("word") and not cut.endswith(" ")
    assert payment_receipt.cut("x" * 150) == "x" * 100


def test_an_order_without_bookings_is_refused(made: Example) -> None:
    order = checkout(made)
    empty = cast(
        Order,
        SimpleNamespace(
            bookings=Booking.objects.of(made.org).none(),
            number=order.number,
            total=0,
            pk=order.pk,
        ),
    )
    with pytest.raises(ValueError, match="without bookings"):
        payment_receipt.build(made.org, empty)


def test_a_receipt_that_does_not_add_up_is_not_sent(
    made: Example, wire: Wire, caplog: pytest.LogCaptureFixture
) -> None:
    order = checkout(made)
    order.total += 1
    with caplog.at_level(logging.ERROR):
        description, receipt = payment_receipt.build(made.org, order)
    assert receipt == "" and description
    assert caplog.messages == [f"payment receipt refused: order={order.pk}"]


def test_the_client_drops_a_receipt_that_does_not_add_up(
    made: Example, wire: Wire
) -> None:
    order = checkout(made)
    _description, receipt = payment_receipt.build(made.org, order)
    # The saved receipt is for 200; the payment is for 201.
    Payment.objects.create(
        currency="NOK",
        organization=made.org,
        order=order,
        payment_provider="vipps",
        provider_reference=uuid.uuid4().hex,
        amount=201,
        state="created",
        create_key=uuid.uuid4(),
        credential_name="VIPPS",
        merchant_number="123456",
        return_url="https://harbourview.sandbox.example.org/payments/return/",
        create_description="d",
        create_receipt=receipt,
    )
    payment = Payment.objects.of(made.org).get()
    payments.create_at_provider(payments.client_for(payment), payment)
    (raw,) = raw_creates(wire)
    assert "receipt" not in json.loads(raw)


def test_a_retry_sends_identical_bytes_whatever_changed_since(
    made: Example, wire: Wire
) -> None:
    Organization.objects.filter(pk=made.org.pk).update(tax_rate=2500)
    made.org.refresh_from_db()
    order = two_bookings(made)
    wire.lose_after.add("create")
    with pytest.raises(VippsError):
        start(order, made)
    saved = Payment.objects.of(made.org).get()
    assert saved.create_description and saved.create_receipt
    Organization.objects.filter(pk=made.org.pk).update(
        name="Renamed co-op", tax_rate=1500
    )
    assert start(order, made) is not None
    first, second = raw_creates(wire)
    assert first == second
    assert json.loads(second)["paymentDescription"].startswith("The harbourview")
    assert json.loads(second)["receipt"]["orderLines"][0]["taxRate"] == 2500


def test_a_payment_made_before_the_description_was_saved_sends_the_old_one(
    made: Example, wire: Wire
) -> None:
    order = checkout(made)
    wire.fail_before.add("create")
    with pytest.raises(VippsError):
        start(order, made)
    Payment.objects.of(made.org).update(create_description="", create_receipt="")
    assert start(order, made) is not None
    body = json.loads(raw_creates(wire)[-1])
    assert body["paymentDescription"] == "RandiRandi booking"
    assert "receipt" not in body


def test_the_booking_category_is_sent_once_with_the_site_root(
    made: Example, wire: Wire
) -> None:
    order = checkout(made)
    assert start(order, made) is not None
    assert start(order, made) is not None
    payment = Payment.objects.of(made.org).get()
    ((path, body),) = category_calls(wire)
    assert path == f"/order-management/v2/ecom/categories/{payment.provider_reference}"
    assert json.loads(body) == {"category": "BOOKING", "orderDetailsUrl": SITE}
    assert payment.details_sent_at == NOW
    payment_reconciliation.reconcile(made.org.pk, order.pk, now=NOW + timedelta(1))
    assert len(category_calls(wire)) == 1


def test_a_refused_category_leaves_the_payment_and_recovery_retries_it(
    made: Example, wire: Wire, caplog: pytest.LogCaptureFixture
) -> None:
    order = checkout(made)
    wire.category_code = 500
    with caplog.at_level(logging.ERROR):
        result = start(order, made)
    assert result is not None
    payment = Payment.objects.of(made.org).get()
    assert payment.details_sent_at is None
    assert payment.state == "created" and payment.create_completed_at == NOW
    assert payment.redirect_url == result.redirect_url
    assert caplog.messages == [
        f"payment details failed: organization={made.org.pk} payment={payment.pk}"
    ]
    wire.category_code = 204
    payment_reconciliation.reconcile_requested(now=NOW + timedelta(minutes=1))
    payment.refresh_from_db()
    assert payment.details_sent_at == NOW + timedelta(minutes=1)
    assert payment.state == "created"
    assert len(category_calls(wire)) == 2
    payment_reconciliation.reconcile_requested(now=NOW + timedelta(minutes=2))
    assert len(category_calls(wire)) == 2


def test_failing_links_rotate_past_the_batch(
    made: Example, wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the queue is under test, so the order cleanup is left out.
    monkeypatch.setattr(payment_recovery, "release_ineligible", lambda *args: None)
    monkeypatch.setattr(
        payment_recovery, "advance", lambda _payment, status, **_: status
    )
    references = []
    for _ in range(21):
        order = Order.objects.create(
            organization=made.org, member=made.member, total=200
        )
        row = Payment.objects.create(
            currency="NOK",
            organization=made.org,
            order=order,
            amount=200,
            state="created",
            payment_provider="vipps",
            provider_reference=uuid.uuid4().hex,
            create_key=uuid.uuid4(),
            credential_name="VIPPS",
            merchant_number="123456",
            return_url="https://harbourview.sandbox.example.org/payments/return/",
            create_completed_at=NOW,
        )
        wire.payments[row.provider_reference] = WirePayment(
            row.provider_reference, 200, "NOK"
        )
        references.append(row.provider_reference)
    wire.category_code = 500
    payment_reconciliation.reconcile_requested(now=NOW + timedelta(minutes=1))
    assert len(category_calls(wire)) == 20
    payment_reconciliation.reconcile_requested(now=NOW + timedelta(minutes=2))
    tried = {path.rsplit("/", 1)[-1] for path, _body in category_calls(wire)}
    assert tried == set(references)


def test_reverse_is_refused_while_an_intent_holds_a_description(
    made: Example, wire: Wire
) -> None:
    wire.fail_before.add("create")
    order = checkout(made)
    with pytest.raises(VippsError):
        start(order, made)
    before = list(Payment.objects.of(made.org).values())
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    previous = [("core", "0023_member_last_category")]
    try:
        with pytest.raises(RuntimeError, match="holds a description"):
            MigrationExecutor(connection).migrate(previous)
        MigrationExecutor(connection).migrate(latest)
        assert list(Payment.objects.of(made.org).values()) == before
        # Once the create has finished, nothing is held and the reverse runs.
        assert start(order, made) is not None
        MigrationExecutor(connection).migrate(previous)
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_reverse_is_refused_while_an_organization_has_a_tax_rate(made: Example) -> None:
    Organization.objects.filter(pk=made.org.pk).update(tax_rate=2500)
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    previous = [("core", "0023_member_last_category")]
    try:
        with pytest.raises(RuntimeError, match="has a tax rate"):
            MigrationExecutor(connection).migrate(previous)
        MigrationExecutor(connection).migrate(latest)
        assert Organization.objects.get(pk=made.org.pk).tax_rate == 2500
        Organization.objects.filter(pk=made.org.pk).update(tax_rate=0)
        MigrationExecutor(connection).migrate(previous)
    finally:
        MigrationExecutor(connection).migrate(latest)
