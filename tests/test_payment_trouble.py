# SPDX-License-Identifier: AGPL-3.0-only
"""A payment that keeps failing its provider check says which, why and since
when (payment_trouble.py): its spells, the History lines, the
log lines through the real privacy formatter, the queue still rotating, and the
admin's view of it."""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest
from django.conf import settings
from django.db import OperationalError

from randirandi.core import booking_admin, payment_recovery, payment_trouble
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.models import Order, Payment, PaymentTrouble
from randirandi.core.retention import purge_organization
from randirandi.logs import _DATE_FORMAT, _FORMAT, STRUCK, IdsOnlyFormatter
from randirandi.vipps import PaymentStatus, VippsError, VippsResponseError
from tests.alert_queries import PAYMENT_STUCK, labels, stored
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_ordering import NOW
from tests.test_payment_capture import Money, ready
from tests.test_payment_capture import money as money
from tests.test_payments import Provider
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)
EXPIRED = NOW + timedelta(minutes=10)


@pytest.fixture(autouse=True)
def fresh_log_clock() -> None:
    payment_trouble._last_logged.clear()


def spells_of(made: Example, payment: Payment) -> list[PaymentTrouble]:
    return list(
        PaymentTrouble.objects.of(made.org)
        .filter(payment=payment)
        .order_by("since", "pk")
    )


def kept(made: Example, payment: Payment) -> dict[str, Any]:
    """The payment's latest spell, or the blank record when it has none."""
    found = spells_of(made, payment)
    if not found:
        return blank()
    last = found[-1]
    return {
        "trouble_since": last.since,
        "trouble_ended_at": last.ended_at,
        "trouble_step": last.step,
        "trouble_kind": last.kind,
        "trouble_tries": last.tries,
    }


def blank() -> dict[str, Any]:
    return {
        "trouble_since": None,
        "trouble_ended_at": None,
        "trouble_step": "",
        "trouble_kind": "",
        "trouble_tries": 0,
    }


def raises(error: Exception) -> Callable[[], None]:
    def fail() -> None:
        raise error

    return fail


def history(made: Example, payment: Payment) -> list[str]:
    order = Order.objects.of(made.org).get(pk=payment.order_id)
    page = booking_admin.booking(made.org, order)
    return [e["text"] for e in page["history"] if e["text"].startswith("Payment check")]  # type: ignore[attr-defined]


def test_a_payment_with_no_trouble_has_no_spell(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    reconciliation.reconcile_requested(now=NOW)
    assert spells_of(made, payment) == []
    assert history(made, payment) == []


def test_a_failure_then_a_success_opens_a_spell_then_ends_it(
    made: Example,
    provider: Provider,
    money: Money,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payment = ready(made, money)
    money.before_read = raises(TimeoutError("private-provider-response"))
    reconciliation.reconcile_requested(now=NOW)
    assert kept(made, payment) == {
        "trouble_since": NOW,
        "trouble_ended_at": None,
        "trouble_step": "read",
        "trouble_kind": "other",
        "trouble_tries": 1,
    }
    money.before_read = raises(VippsError())
    for tick in (1, 2):
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=10 * tick))
    assert kept(made, payment) == {
        "trouble_since": NOW,
        "trouble_ended_at": None,
        "trouble_step": "read",
        "trouble_kind": "unanswered",
        "trouble_tries": 3,
    }
    # One line when it began, however many tries.
    assert history(made, payment) == ["Payment check began failing"]
    money.before_read = lambda: None
    with caplog.at_level(logging.INFO):
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=30))
    ended = kept(made, payment)
    assert ended["trouble_ended_at"] == NOW + timedelta(seconds=30)
    assert (ended["trouble_since"], ended["trouble_tries"]) == (NOW, 3)
    payment.refresh_from_db()
    assert payment.settled_at is not None
    # Exactly two lines: began, and works again with its step, kind and tries.
    assert history(made, payment) == [
        "Payment check began failing",
        "Payment check works again "
        "(reading the payment, Vipps does not answer, 3 tries)",
    ]
    order = Order.objects.of(made.org).get(pk=payment.order_id)
    assert booking_admin.booking(made.org, order)["payment"]["trouble"] is None  # type: ignore[index]
    # Nothing the provider said, and no exception text, reached the record or the log.
    assert "private-provider-response" not in caplog.text
    assert caplog.text.count("payment reconciliation recovered:") == 1
    # The next spell starts afresh; the first is kept.
    payment_trouble.failed(
        made.org.pk, payment.order_id, VippsError(503), now=NOW + timedelta(hours=1)
    )
    assert kept(made, payment) == {
        "trouble_since": NOW + timedelta(hours=1),
        "trouble_ended_at": None,
        "trouble_step": "save",
        "trouble_kind": "unanswered",
        "trouble_tries": 1,
    }
    assert len(spells_of(made, payment)) == 2
    assert history(made, payment)[-3:] == [
        "Payment check began failing",
        "Payment check works again "
        "(reading the payment, Vipps does not answer, 3 tries)",
        "Payment check began failing",
    ]


def arm_read(made: Example, money: Money, payment: Payment, error: Exception) -> Any:
    money.before_read = raises(error)
    return NOW


def arm_capture(made: Example, money: Money, payment: Payment, error: Exception) -> Any:
    money.before_capture = raises(error)
    return NOW


def arm_refund(made: Example, money: Money, payment: Payment, error: Exception) -> Any:
    ref = payment.provider_reference
    money.snapshots[ref] = replace(money.snapshots[ref], captured=100)
    money.before_refund = raises(error)
    return EXPIRED


def arm_cancel(made: Example, money: Money, payment: Payment, error: Exception) -> Any:
    ref = payment.provider_reference
    money.snapshots[ref] = PaymentStatus(ref, "CREATED", 200, 0, 0, 0, 0)
    money.before_cancel = raises(error)
    return EXPIRED


@pytest.mark.parametrize(
    ("arm", "step"),
    [
        (arm_read, "read"),
        (arm_capture, "capture"),
        (arm_refund, "refund"),
        (arm_cancel, "cancel"),
    ],
)
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (lambda: VippsError(), "unanswered"),
        (lambda: VippsError(503), "unanswered"),
        (lambda: VippsError(429), "unanswered"),
        (lambda: VippsError(400), "unaccepted"),
        (lambda: VippsResponseError(), "unaccepted"),
        (lambda: OperationalError("private-database-text"), "database"),
        (lambda: ValueError("private-code-text"), "other"),
    ],
)
def test_each_step_and_each_kind_is_named(
    made: Example,
    provider: Provider,
    money: Money,
    caplog: pytest.LogCaptureFixture,
    arm: Callable[..., Any],
    step: str,
    error: Callable[[], Exception],
    kind: str,
) -> None:
    payment = ready(made, money)
    moment = arm(made, money, payment, error())
    reconciliation.reconcile_requested(now=moment)
    row = kept(made, payment)
    assert (row["trouble_step"], row["trouble_kind"], row["trouble_tries"]) == (
        step,
        kind,
        1,
    )
    assert row["trouble_since"] == moment
    assert "private-" not in caplog.text
    assert "private-" not in json.dumps(row, default=str)
    words = f'step="{payment_trouble.STEP_WORDS[step]}" '
    words += f'kind="{payment_trouble.KIND_WORDS[kind]}" tries=1'
    assert words in caplog.text, caplog.text


def test_the_database_work_around_the_calls_is_the_save_step(
    made: Example,
    provider: Provider,
    money: Money,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payment = ready(made, money)

    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("private")

    monkeypatch.setattr(payment_recovery, "mark_settled", broken)
    reconciliation.reconcile_requested(now=NOW)
    row = kept(made, payment)
    assert (row["trouble_step"], row["trouble_kind"]) == ("save", "database")
    assert payment_trouble.classify(ValueError()) == ("save", "other")


def test_the_record_is_best_effort_and_the_error_still_surfaces(
    made: Example,
    provider: Provider,
    money: Money,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payment = ready(made, money)
    money.before_read = raises(VippsError())

    def down(*args: object, **kwargs: object) -> None:
        raise OperationalError("private-database-text")

    monkeypatch.setattr(payment_trouble, "organization_transaction", down)
    with pytest.raises(VippsError):
        reconciliation.reconcile(made.org.pk, payment.order_id, now=NOW)
    assert kept(made, payment) == blank()
    assert "payment trouble not saved:" in caplog.text
    assert "private-database-text" not in caplog.text


def raw_lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if isinstance(r.args, tuple) and r.args and r.args[0] == prefix
    ]


def written(record: logging.LogRecord) -> str:
    formatter = IdsOnlyFormatter(_FORMAT, _DATE_FORMAT)
    return formatter.format(record)


def test_the_lines_are_one_at_the_start_then_one_a_minute_by_the_clock(
    made: Example,
    provider: Provider,
    money: Money,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payment = ready(made, money)
    money.before_read = raises(VippsError())
    for tick in range(10):
        reconciliation.reconcile_requested(now=NOW + timedelta(seconds=6 * tick))
    assert len(raw_lines(caplog, "payment reconciliation failed")) == 1
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=60))
    assert kept(made, payment)["trouble_tries"] == 11
    assert len(raw_lines(caplog, "payment reconciliation failed")) == 2
    # A blip never reaches the alert's line.
    assert not raw_lines(caplog, "payment trouble lasting")


def test_slow_tries_still_log_at_least_once_a_minute_while_it_lasts(
    made: Example, provider: Provider, money: Money, caplog: pytest.LogCaptureFixture
) -> None:
    payment = ready(made, money)
    seen: list[int] = []
    # A try takes 25 seconds (two calls to Vipps, each bounded at 10 plus work):
    # one count of tries between lines would be five minutes.
    for tick in range(0, 1501, 25):
        before = len(raw_lines(caplog, "payment trouble lasting"))
        payment_trouble.failed(
            made.org.pk,
            payment.order_id,
            VippsError(),
            now=NOW + timedelta(seconds=tick),
        )
        if len(raw_lines(caplog, "payment trouble lasting")) > before:
            seen.append(tick)
    assert seen[0] == 900
    assert seen == list(range(900, 1501, 75))
    # Never more than 75 seconds between lines, so a ten-minute window is never
    # empty and the alert neither resolves nor fires again.
    assert max(b - a for a, b in pairwise(seen)) <= 75
    assert len(raw_lines(caplog, "payment reconciliation failed")) == 21


def test_the_lasting_line_through_the_privacy_formatter_and_the_alert_rule(
    made: Example,
    provider: Provider,
    money: Money,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payment = ready(made, money)
    money.before_read = raises(VippsError(503))
    since = NOW - timedelta(minutes=20)
    PaymentTrouble.objects.create(
        organization=made.org,
        payment=payment,
        since=since,
        step="read",
        kind="unanswered",
        first_step="read",
        first_kind="unanswered",
        tries=11,
    )
    reconciliation.reconcile_requested(now=NOW)
    [record] = raw_lines(caplog, "payment trouble lasting")
    order = Order.objects.of(made.org).get(pk=payment.order_id)
    query = PAYMENT_STUCK
    expected = {
        "organization": str(made.org.pk),
        "payment": str(payment.pk),
        "name": made.org.subdomain.capitalize(),
        "reference": order.reference,
        "step": "reading the payment",
        "kind": "Vipps does not answer",
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "when": payment_trouble.oslo(since),
        "link": f"https://{made.org.subdomain}.{settings.ORGANIZATION_DOMAIN}"
        f"/admin/bookings/{order.number}/",
    }
    # Through the real formatter and the way the store keeps it (quotes escaped).
    assert labels(query, stored(written(record))) == expected
    assert '\\"' in stored(written(record))
    # The same line with ids on both sides of the eight-digit boundary: typed
    # ids survive the formatter's phone-number strike.
    for position in (1, 2):
        for number in (9_999_999, 10_000_000, 99_999_999, 100_000_000):
            args = list(record.args)  # type: ignore[arg-type]
            args[position] = number
            changed = record.__dict__ | {"args": tuple(args)}
            line = written(logging.makeLogRecord(changed))
            assert STRUCK not in line, line
            found = labels(query, stored(line))
            assert found is not None
            if position == 2:
                assert found["payment"] == str(number)
    # What the formatter would do to a bare id of eight digits.
    bare = written(
        logging.makeLogRecord(
            {"msg": "payment=12345678", "levelname": "ERROR", "name": "x"}
        )
    )
    assert STRUCK in bare
    # No member name, email or phone anywhere in it.
    assert made.member.email not in written(record)
    # An ordinary failure line is not the alert's line.
    [early] = raw_lines(caplog, "payment reconciliation failed")
    assert labels(query, stored(written(early))) is None


def make_payments(made: Example, money: Money, count: int) -> list[Payment]:
    rows = []
    for _ in range(count):
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
        )
        money.snapshots[row.provider_reference] = PaymentStatus(
            row.provider_reference, "EXPIRED", 200, 0, 0, 0, 0
        )
        rows.append(row)
    return rows


def test_payments_failing_on_every_tick_still_rotate_across_batches(
    made: Example, provider: Provider, money: Money
) -> None:
    rows = make_payments(made, money, 25)
    money.before_read = raises(VippsError())

    def tries() -> list[int]:
        return [kept(made, row)["trouble_tries"] for row in rows]

    reconciliation.reconcile_requested(now=NOW)
    assert sum(tries()) == 20
    # The five never tried come first, then the oldest attempts: every payment
    # has been tried, none starved behind the failing first twenty.
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=6))
    assert min(tries()) == 1 and sum(tries()) == 40
    assert all(kept(made, row)["trouble_since"] == NOW for row in rows[:20])
    # The five tried once, longest ago, come before any second retry.
    lagging = rows[15:20]
    assert [kept(made, row)["trouble_tries"] for row in lagging] == [1] * 5
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=12))
    assert [kept(made, row)["trouble_tries"] for row in lagging] == [2] * 5


def test_a_spell_whose_failure_changes_stays_one_alert(
    made: Example, provider: Provider, money: Money, caplog: pytest.LogCaptureFixture
) -> None:
    payment = ready(made, money)
    query = PAYMENT_STUCK
    errors = [VippsError(), VippsError(), ValueError()]
    for tick, error in enumerate(errors):
        error.trouble_step = "read" if tick < 2 else "save"  # type: ignore[attr-defined]
        payment_trouble.failed(
            made.org.pk,
            payment.order_id,
            error,
            now=NOW + timedelta(seconds=75 * 12 * (tick + 1)),
        )
    [spell] = spells_of(made, payment)
    assert (spell.step, spell.kind) == ("save", "other")
    assert (spell.first_step, spell.first_kind) == ("read", "unanswered")
    seen = {
        tuple(labels(query, stored(written(r))).items())  # type: ignore[union-attr]
        for r in raw_lines(caplog, "payment trouble lasting")
    }
    assert len(seen) == 1
    assert dict(seen.pop())["step"] == "reading the payment"
    assert history(made, payment) == ["Payment check began failing"]
    order = Order.objects.of(made.org).get(pk=payment.order_id)
    card = booking_admin.booking(made.org, order)["payment"]["trouble"]  # type: ignore[index]
    assert card["step"] == "save" and card["kind"] == "other"


def test_a_check_that_did_not_run_is_neither_a_failure_nor_a_success(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    money.before_read = raises(VippsError())
    reconciliation.reconcile_requested(now=NOW)
    # Inside the interval nothing was checked: the record stays as it was.
    reconciliation.reconcile(made.org.pk, payment.order_id, now=NOW)
    assert kept(made, payment)["trouble_ended_at"] is None
    assert kept(made, payment)["trouble_tries"] == 1


def test_retention_removes_the_spells_with_the_payment(
    made: Example, provider: Provider, money: Money
) -> None:
    payment = ready(made, money)
    money.before_read = raises(VippsError())
    reconciliation.reconcile_requested(now=NOW)
    money.before_read = lambda: None
    reconciliation.reconcile_requested(now=NOW + timedelta(seconds=30))
    assert len(spells_of(made, payment)) == 1
    done = purge_organization(made.org, now=NOW + timedelta(days=365 * 5))
    assert done["orders"] == 1
    assert not PaymentTrouble.objects.of(made.org).exists()
