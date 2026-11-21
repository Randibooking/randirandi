# SPDX-License-Identifier: AGPL-3.0-only
"""An administrator cancels a booking or one of its lines, with the refund : the lines
it takes, the refund queued or none, the busy and repeat
refusals, the events, the member's email and the time freed."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.db import connections

from randirandi.core import (
    audit_log,
    booking_admin,
    booking_outcome,
    payment_refunds,
    payments,
)
from randirandi.core import payment_adjustments as adjustments
from randirandi.core import payment_callbacks as callbacks
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.audit_types import AuditAction
from randirandi.core.booking_emails import GIVE_UP, RETRY, send_cancellations
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    AuditEvent,
    Booking,
    BookingState,
    Member,
    Order,
    Payment,
    Refund,
)
from randirandi.core.ordering import create_order
from randirandi.core.payments import PaymentBusyError, PaymentUnavailableError
from randirandi.core.retention import after_months, purge_organization
from randirandi.vipps import PaymentStatus
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.mail_checks import check_system_mail
from tests.test_booking_emails import SEND, free, text_of
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_member_bookings import order_of
from tests.test_ordering import NOW, checkout, request
from tests.test_payment_adjustments import administrator as administrator
from tests.test_payment_adjustments import paid
from tests.test_payment_capture import Money, ready, rule_sees
from tests.test_payment_capture import money as money
from tests.test_payments import Provider
from tests.test_payments import provider as provider

pytestmark = pytest.mark.django_db(transaction=True)
# The first line (10:00 to 12:00 on DAY) is under way at this moment.
UNDER_WAY = at(DAY, 11)
AFTER = at(DAY, 15)


def cancel(
    administrator: Member,
    order: Order,
    *,
    line: Booking | None = None,
    refund: bool = True,
    key: uuid.UUID | None = None,
    now: Any = NOW,
) -> adjustments.Cancelled:
    return adjustments.cancel(
        administrator=administrator,
        order_public_id=order.public_id,
        booking_public_id=None if line is None else line.public_id,
        request_key=key or uuid.uuid4(),
        refund=refund,
        now=now,
    )


def states(order: Order) -> list[str]:
    return [
        row.taken_time.state
        for row in order.bookings.select_related("taken_time").order_by("pk")
    ]


def events(made: Example, action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.of(made.org).filter(action=action).order_by("pk"))


def refund_of(made: Example, result: adjustments.Cancelled) -> Refund:
    assert result.refund_id is not None
    return Refund.objects.of(made.org).get(pk=result.refund_id)


def refused(code: str) -> Any:
    return pytest.raises(adjustments.CancelRefusedError, check=lambda e: e.code == code)


@pytest.fixture
def no_provider(monkeypatch: pytest.MonkeyPatch) -> mock.Mock:
    """A cancel that must send nothing to the provider, not even a read."""
    refuse = mock.Mock(side_effect=AssertionError("The provider was called."))
    monkeypatch.setattr(adjustments, "client_for", refuse)
    return refuse


# --- What can be cancelled ------------------------------------------------------


def test_a_whole_paid_booking_is_cancelled_and_its_amount_queued_for_refund(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    key = uuid.uuid4()
    with application_login():
        result = cancel(administrator, payment.order, key=key)
        assert result.booking_ids == (first.pk, second.pk)
        assert result.refund_id is not None
        refund = refund_of(made, result)
        # The whole of both lines, by the saved amounts; one refund for the two.
        assert refund.amount == 400 and refund.booking_id is None
        assert refund.made_by == administrator and refund.refund_key == key
        assert refund.completed_at is None and not refund.automatic
        assert states(payment.order) == ["cancelled", "cancelled"]
        assert rule_sees(made, payment.order) == {"cancelled"}
        # Nothing is sent until the existing worker runs.
        assert money.refunds == []
        reconciliation.reconcile_requested(now=NOW)
    assert money.refunds[0][1:] == (400, "NOK", str(key))
    refund.refresh_from_db()
    assert refund.completed_at == NOW
    for row in (first, second):
        row.refresh_from_db()
        assert row.cancelled_by == administrator and row.cancelled_at == NOW
        assert row.cancellation_key == key and row.cancel_mail_due_at == NOW


def test_one_line_is_cancelled_by_its_whole_amount_and_the_other_stays(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    result = cancel(administrator, payment.order, line=second)
    assert result.booking_ids == (second.pk,) and result.refund_id is not None
    refund = refund_of(made, result)
    assert refund.amount == 200 and refund.booking_id == second.pk
    assert states(payment.order) == ["confirmed", "cancelled"]
    first.refresh_from_db()
    assert first.cancelled_at is None and first.cancellation_key is None
    # The time is free again: a member can book it right after.
    again = create_order(
        member=made.member,
        requests=[request(made, starts_at=at(hour=12), ends_at=at(hour=14))],
        now=NOW,
        accepted_version=made_up.accepted(made.member),
    )
    assert again.bookings.get().taken_time.state in {"held", "confirmed"}
    # And the other line is cancelled by the rest of the booking, once the
    # worker has sent the first refund.
    reconciliation.reconcile_requested(now=NOW)
    last = cancel(administrator, payment.order)
    assert last.booking_ids == (first.pk,)
    assert refund_of(made, last).amount == 200


def test_a_line_under_way_is_cancelled_by_its_whole_amount(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    result = cancel(administrator, payment.order, line=first, now=UNDER_WAY)
    assert refund_of(made, result).amount == 200
    # The booking, from the same moment, takes the line still to come.
    reconciliation.reconcile_requested(now=UNDER_WAY)
    rest = cancel(administrator, payment.order, now=UNDER_WAY)
    assert rest.booking_ids == (second.pk,)


def test_a_whole_cancel_leaves_the_lines_that_are_over(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    now = at(DAY, 12, 30)
    result = cancel(administrator, payment.order, now=now)
    assert result.booking_ids == (second.pk,)
    assert states(payment.order) == ["confirmed", "cancelled"]
    refund = refund_of(made, result)
    assert refund.amount == 200 and refund.booking_id == second.pk
    with refused("past"):
        cancel(administrator, payment.order, line=first, now=now)
    with refused("past"):
        cancel(administrator, payment.order, now=AFTER)


def test_a_booking_being_paid_is_cancelled_whole_through_the_unpaid_cancel(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = ready(made, money, multiple=True)
    first = payment.order.bookings.order_by("pk")[0]
    key = uuid.uuid4()
    with application_login():
        with refused("whole-only"):
            cancel(administrator, payment.order, line=first)
        # The switch means nothing here: the order was not paid.
        result = cancel(administrator, payment.order, refund=True, key=key)
        assert result.refund_id is None and len(result.booking_ids) == 2
        assert states(payment.order) == ["cancelled", "cancelled"]
        assert rule_sees(made, payment.order) == {"cancelled"}
        assert cancel(administrator, payment.order, key=key) == result
        reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.cancel_key == key
    assert money.cancellations == [(payment.provider_reference, str(key))]
    assert not Refund.objects.of(made.org).exists() and money.refunds == []
    assert [e.action for e in AuditEvent.objects.of(made.org).order_by("pk")] == [
        AuditAction.CANCEL_BOOKING_LINE,
        AuditAction.CANCEL_BOOKING_LINE,
        AuditAction.CANCEL_PAYMENT,
    ]


def test_a_free_booking_cancels_without_a_refund_or_the_provider(
    made: Example, administrator: Member, no_provider: mock.Mock
) -> None:
    free(made)
    order = order_of(made, 1, 2)
    first, second = order.bookings.select_related("taken_time").order_by("pk")
    assert order.total == 0 and not Payment.objects.of(made.org).exists()
    one = cancel(administrator, order, line=first, refund=True)
    both = cancel(administrator, order, refund=True)
    assert one.refund_id is None and both.refund_id is None
    assert both.booking_ids == (second.pk,)
    assert states(order) == ["cancelled", "cancelled"]
    assert not Refund.objects.of(made.org).exists()
    no_provider.assert_not_called()


def test_what_is_over_or_already_cancelled_is_refused(
    made: Example, administrator: Member, no_provider: mock.Mock
) -> None:
    free(made)
    order = order_of(made, 1)
    line = order.bookings.get()
    with refused("past"):
        cancel(administrator, order, line=line, now=at(DAY + timedelta(days=2)))
    with refused("past"):
        cancel(administrator, order, now=at(DAY + timedelta(days=2)))
    with refused("not-found"):
        cancel(administrator, order, line=Booking(public_id=uuid.uuid4()))
    cancel(administrator, order, line=line)
    with refused("already-cancelled"):
        cancel(administrator, order, line=line)
    with refused("already-cancelled"):
        cancel(administrator, order)
    assert len(events(made, AuditAction.CANCEL_BOOKING_LINE)) == 1


def test_expired_lines_are_not_cancellable(
    made: Example, administrator: Member, no_provider: mock.Mock
) -> None:
    free(made)
    order = order_of(made, 1)
    line = order.bookings.get()
    line.taken_time.state = BookingState.EXPIRED
    line.taken_time.save(update_fields=["state"])
    with refused("not-cancellable"):
        cancel(administrator, order, line=line)
    with refused("not-cancellable"):
        cancel(administrator, order)


def test_another_organizations_administrator_finds_no_booking(
    made: Example, administrator: Member
) -> None:
    from tests import made_up

    free(made)
    order = order_of(made, 1)
    elsewhere = made_up.everything("elsewhere")
    with refused("not-found"):
        cancel(elsewhere.administrator, order)
    assert states(order) == ["confirmed"]
    assert not AuditEvent.objects.of(made.org).exists()


def test_a_member_cannot_cancel(made: Example) -> None:
    from django.core.exceptions import PermissionDenied

    free(made)
    order = order_of(made, 1)
    with pytest.raises(PermissionDenied):
        cancel(made.member, order)
    assert states(order) == ["confirmed"]


# --- With and without the refund ----------------------------------------------


def test_without_the_refund_nothing_is_sent_to_the_provider(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
) -> None:
    payment = paid(made, money)
    money.reads.clear()
    key = uuid.uuid4()
    result = cancel(administrator, payment.order, refund=False, key=key)
    assert result.refund_id is None and len(result.booking_ids) == 2
    assert states(payment.order) == ["cancelled", "cancelled"]
    assert not Refund.objects.of(made.org).exists()
    reconciliation.reconcile_requested(now=NOW)
    assert money.reads == [] and money.refunds == [] and money.cancellations == []
    payment.refresh_from_db()
    assert payment.refunded_amount == 0 and payment.captured_amount == 400
    # The audit log names the lines, and no refund.
    assert len(events(made, AuditAction.CANCEL_BOOKING_LINE)) == 2
    assert not events(made, AuditAction.REQUEST_REFUND)
    # A repeat of the key answers the same.
    assert cancel(administrator, payment.order, refund=False, key=key) == result


def test_a_repeat_cancels_once_and_a_busy_order_is_refused(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    key = uuid.uuid4()
    first = cancel(administrator, payment.order, key=key)
    assert cancel(administrator, payment.order, key=key) == first
    assert cancel(administrator, payment.order, line=None, key=key) == first
    assert Refund.objects.of(made.org).count() == 1
    assert len(events(made, AuditAction.CANCEL_BOOKING_LINE)) == 2
    assert len(events(made, AuditAction.REQUEST_REFUND)) == 1
    # The key belongs to this request: another order's line or another
    # administrator cannot use it.
    other = Member.objects.create(
        organization=made.org,
        role=administrator.role,
        status="active",
        email="other-admin@fictional.example",
        origin="administrator",
    )
    with refused("key-in-use"):
        cancel(other, payment.order, key=key)


def test_a_double_press_at_the_same_moment_is_busy_for_the_second(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    entered, release = Event(), Event()

    def pause() -> None:
        entered.set()
        assert release.wait(timeout=10)

    money.before_read = pause
    key = uuid.uuid4()

    def press() -> adjustments.Cancelled:
        try:
            return cancel(administrator, payment.order, key=key)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(press)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(PaymentBusyError):
                cancel(administrator, payment.order, key=key)
        finally:
            release.set()
        result = worker.result(timeout=10)
    assert cancel(administrator, payment.order, key=key) == result
    assert Refund.objects.of(made.org).count() == 1


def test_an_unfinished_refund_blocks_another_refund_but_not_a_cancel_without_one(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    cancel(administrator, payment.order, line=first)
    with pytest.raises(PaymentBusyError):
        cancel(administrator, payment.order, line=second)
    assert states(payment.order) == ["cancelled", "confirmed"]
    result = cancel(administrator, payment.order, line=second, refund=False)
    assert result.refund_id is None
    assert states(payment.order) == ["cancelled", "cancelled"]


def test_a_refund_is_refused_when_the_provider_and_the_order_disagree(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    money.snapshots[payment.provider_reference] = replace(
        money.snapshots[payment.provider_reference], refunded=100
    )
    with refused("recovery"):
        cancel(administrator, payment.order)
    assert states(payment.order) == ["confirmed", "confirmed"]
    assert not Refund.objects.of(made.org).exists()


def test_a_second_cancel_refunds_only_what_remains(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    cancel(administrator, payment.order, line=first)
    reconciliation.reconcile_requested(now=NOW)
    last = cancel(administrator, payment.order)
    assert refund_of(made, last).amount == 200
    assert last.booking_ids == (second.pk,)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.refunded_amount == payment.captured_amount == 400
    assert [row[1] for row in money.refunds] == [200, 200]


# --- The events, the member's pages and the export ----------------------------


def test_each_line_gets_an_event_and_a_refund_one_more_by_the_administrator(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    result = cancel(administrator, payment.order)
    lines = events(made, AuditAction.CANCEL_BOOKING_LINE)
    assert [(e.object_kind, e.object_id) for e in lines] == [
        ("booking", first.pk),
        ("booking", second.pk),
    ]
    (requested,) = events(made, AuditAction.REQUEST_REFUND)
    assert (requested.object_kind, requested.object_id) == ("refund", result.refund_id)
    assert {e.administrator_id for e in [*lines, requested]} == {administrator.pk}
    # Ids only, and the audit log shows them and opens the booking.
    shown: Any = audit_log.answer(made.org, audit_log.Query())
    named = [e for e in shown["events"] if e["action"] == "cancelled booking line"]
    assert len(named) == 2
    assert all(
        e["action_label"] == "Cancelled booking line"
        and e["what"]["booking"] == payment.order.number
        for e in named
    )
    exported = export_member(
        actor=administrator, member_id=made.member.pk, **fresh(administrator)
    )
    assert [
        row["object_id"]
        for row in exported["audit_events"]
        if row["action"] == "cancelled booking line"
    ] == [first.pk, second.pk]
    by_administrator = export_member(
        actor=administrator, member_id=administrator.pk, **fresh(administrator)
    )
    assert len(by_administrator["cancelled_bookings"]) == 2
    assert len(by_administrator["made_refunds"]) == 1


def test_the_members_own_pages_show_the_booking_as_cancelled(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, _second = payment.order.bookings.order_by("pk")
    cancel(administrator, payment.order, line=first)
    mine = booking_outcome.bookings(made.org, made.member, now=NOW)
    shown = {row["state"] for row in mine["coming"]}
    assert shown == {"cancelled", "confirmed"}


# --- The page's answer ----------------------------------------------------------


def test_the_page_says_what_can_be_cancelled_and_what_happened(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(booking_admin, "_now", lambda: NOW)
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    page: Any = booking_admin.booking(made.org, payment.order)
    assert [line["coming"] for line in page["lines"]] == [True, True]
    cancel(administrator, payment.order, line=first, refund=False)
    cancel(administrator, payment.order, line=second)
    reconciliation.reconcile_requested(now=NOW)
    send_cancellations(now=NOW + timedelta(seconds=5))
    page = booking_admin.booking(made.org, payment.order)
    assert [line["coming"] for line in page["lines"]] == [False, False]
    assert [line["state"] for line in page["lines"]] == ["cancelled", "cancelled"]
    texts = [row["text"] for row in page["history"]]
    assert sum("cancelled by" in text for text in texts) == 2
    assert texts.count("Not refunded") == 1
    assert f"Refund of 2 kr requested by Member {administrator.pk}" in texts
    assert "Refunded 2 kr through Vipps" in texts
    assert texts.count("Cancellation email sent to Test Member") == 2


# --- The cancellation email -----------------------------------------------------


def test_the_member_gets_one_email_for_a_cancel_naming_its_times_and_refund(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    cancel(administrator, payment.order)
    mail.outbox.clear()
    send_cancellations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    assert message.to == [made.member.email] and not message.cc and not message.bcc
    assert message.subject == "Your booking on Monday 1 March is cancelled"
    check_system_mail(message, made.org, reply=True)
    plain, html = text_of(message, "text/plain"), text_of(message, "text/html")
    import html as markup

    for text in (plain, markup.unescape(html)):
        assert "Cancelled" in text and f"Reference {payment.order.reference}" in text
        assert "The harbourview housing co-op" in text
        assert text.count("Monday 1 March") == 1  # one date, two lines under it
        assert "10:00\N{EN DASH}12:00" in text and "12:00\N{EN DASH}14:00" in text
        assert "4 kr is on its way back through Vipps." in text
        assert f"Questions? Write to {made.org.contact_email}." in text
        assert made.member.email not in text
    # Once.
    send_cancellations(now=NOW + timedelta(minutes=10))
    assert len(mail.outbox) == 1
    rows = Booking.objects.of(made.org).filter(order=payment.order)
    assert {row.cancel_mailed_at for row in rows} == {NOW + timedelta(seconds=5)}


def test_a_cancel_without_a_refund_says_no_money_is_returned(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first, second = payment.order.bookings.order_by("pk")
    cancel(administrator, payment.order, line=first, refund=False)
    mail.outbox.clear()
    send_cancellations(now=NOW)
    (message,) = mail.outbox
    plain = text_of(message, "text/plain")
    assert "No refund." in plain and "returned through Vipps" not in plain
    # Only the cancelled line is named; the other cancel gets its own email.
    assert plain.count("Monday 1 March") == 1 and "10:00\N{EN DASH}12:00" in plain
    cancel(administrator, payment.order, line=second, refund=False)
    send_cancellations(now=NOW + timedelta(seconds=5))
    assert len(mail.outbox) == 2
    assert "12:00\N{EN DASH}14:00" in text_of(mail.outbox[1], "text/plain")


def test_a_failed_send_is_tried_again_and_a_late_one_is_given_up(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    caplog: pytest.LogCaptureFixture,
) -> None:
    free(made)
    order = order_of(made, 1)
    cancel(administrator, order)
    mail.outbox.clear()
    with mock.patch(SEND, side_effect=OSError("fictional outage")):
        send_cancellations(now=NOW)
    assert mail.outbox == []
    row = order.bookings.get()
    assert row.cancel_mail_tried_at == NOW and row.cancel_mailed_at is None
    # Not before the retry time.
    send_cancellations(now=NOW + RETRY - timedelta(seconds=1))
    assert mail.outbox == []
    send_cancellations(now=NOW + RETRY)
    assert len(mail.outbox) == 1
    row.refresh_from_db()
    assert row.cancel_mailed_at == NOW + RETRY
    assert "No refund." in text_of(mail.outbox[0], "text/plain")
    # Given up after a day without a send.
    second = order_of(made, 2)
    cancel(administrator, second)
    with caplog.at_level("WARNING"):
        send_cancellations(now=NOW + GIVE_UP)
    assert len(mail.outbox) == 1
    given_up = second.bookings.get()
    assert given_up.cancel_mail_due_at is None and given_up.cancel_mailed_at is None
    assert "booking cancellation given up" in caplog.text


def test_a_member_who_is_not_active_is_sent_nothing(
    made: Example, administrator: Member
) -> None:
    free(made)
    order = order_of(made, 1)
    cancel(administrator, order)
    Member.objects.of(made.org).filter(pk=made.member.pk).update(
        status="disabled", email=""
    )
    mail.outbox.clear()
    send_cancellations(now=NOW)
    row = order.bookings.get()
    assert mail.outbox == []
    assert row.cancel_mail_due_at is None and row.cancel_mailed_at is None
    send_cancellations(now=NOW + timedelta(hours=1))
    assert mail.outbox == []


def test_a_message_that_cannot_be_built_is_logged_and_tried_again(
    made: Example, administrator: Member, caplog: pytest.LogCaptureFixture
) -> None:
    free(made)
    order = order_of(made, 1)
    cancel(administrator, order)
    mail.outbox.clear()
    with (
        mock.patch(
            "randirandi.core.booking_emails.cancellation_message",
            side_effect=ValueError("Alex Rivers"),
        ),
        caplog.at_level("WARNING"),
    ):
        send_cancellations(now=NOW)
    assert mail.outbox == []
    assert "booking cancellation not built: organization-" in caplog.text
    assert "Alex Rivers" not in caplog.text
    assert order.bookings.get().cancel_mail_tried_at == NOW
    send_cancellations(now=NOW + RETRY)
    assert len(mail.outbox) == 1


# --- A refund without a booking is the last one on its payment ------------------


def paid_three(made: Example, money: Money) -> Payment:
    """Three paid lines of 200: two on DAY and one the day after; captured."""
    later = at(DAY + timedelta(days=1), 10)
    order = checkout(
        made,
        request(made),
        request(made, starts_at=at(hour=12), ends_at=at(hour=14)),
        request(made, starts_at=later, ends_at=later + timedelta(hours=2)),
    )
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", order.total, order.total, 0, 0, 0
    )
    callbacks.request_reconciliation(made.org, order.public_id, now=NOW)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    assert payment.captured_amount == 600
    return payment


def test_a_line_then_the_booking_refund_each_amount_once(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid_three(made, money)
    first, second, third = payment.order.bookings.order_by("pk")
    one = cancel(administrator, payment.order, line=first)
    reconciliation.reconcile_requested(now=NOW)
    rest = cancel(administrator, payment.order)
    assert rest.booking_ids == (second.pk, third.pk)
    reconciliation.reconcile_requested(now=NOW)
    reconciliation.reconcile_requested(now=NOW)
    refunds = list(Refund.objects.of(made.org).order_by("pk"))
    assert [(r.pk, r.amount, r.booking_id) for r in refunds] == [
        (one.refund_id, 200, first.pk),
        (rest.refund_id, 400, None),
    ]
    assert all(r.completed_at is not None for r in refunds)
    # The provider was asked for exactly those amounts, once each.
    assert [row[1] for row in money.refunds] == [200, 400]
    payment.refresh_from_db()
    assert payment.refunded_amount == payment.captured_amount == 600


def test_after_a_whole_cancel_that_leaves_a_line_over_nothing_more_moves(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid_three(made, money)
    first, second, third = payment.order.bookings.order_by("pk")
    now = at(DAY, 12, 30)
    done = cancel(administrator, payment.order, now=now)
    assert done.booking_ids == (second.pk, third.pk)
    assert refund_of(made, done).booking_id is None
    reconciliation.reconcile_requested(now=now)
    assert [row[1] for row in money.refunds] == [400]
    refunds = Refund.objects.of(made.org).count()
    for line in (first, second, None):
        for refund in (True, False):
            with pytest.raises(adjustments.CancelRefusedError):
                cancel(administrator, payment.order, line=line, refund=refund, now=now)
    # The refund rule refuses a line's own refund beside one without a booking.
    with pytest.raises(PaymentUnavailableError, match="not attributable"):
        adjustments.request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            booking_public_id=first.public_id,
            request_key=uuid.uuid4(),
            now=now,
        )
    assert Refund.objects.of(made.org).count() == refunds
    assert [row[1] for row in money.refunds] == [400]
    assert states(payment.order) == ["confirmed", "cancelled", "cancelled"]


def test_retention_after_a_refund_without_a_booking_keeps_the_totals(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid_three(made, money)
    first, second, third = payment.order.bookings.order_by("pk")
    now = at(DAY, 12, 30)
    cancel(administrator, payment.order, now=now)
    reconciliation.reconcile_requested(now=now)
    payment_refunds.alert_completed(now=now)
    moment = after_months(first.taken_time.ends_at, made.org.booking_retention_months)
    with application_login():
        assert purge_organization(made.org, now=moment)["bookings"] == 1
    payment.refresh_from_db()
    kept = list(payment.order.bookings.select_related("taken_time").order_by("pk"))
    assert [row.pk for row in kept] == [second.pk, third.pk]
    refunds = list(Refund.objects.of(made.org))
    # What was thrown away plus what is left is the payment, and the same for
    # the refunds: the refund without a booking stays a row.
    assert payment.retired_booking_amount == 200
    assert (
        payment.retired_booking_amount
        + sum(line.total for row in kept for line in row.lines.all())
        == payment.amount
    )
    assert payment.retired_refund_amount + sum(r.amount for r in refunds) == (
        payment.refunded_amount
    )
    assert [(r.amount, r.booking_id) for r in refunds] == [(400, None)]
    reconciliation.reconcile_requested(now=moment)
    assert [row[1] for row in money.refunds] == [400]
