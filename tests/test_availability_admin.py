# SPDX-License-Identifier: AGPL-3.0-only
"""An asset's availability on the admin screens : a weekly
availability from a date, a closure by date and the public holidays, each with a
preview of what it cancels, then the cancellation and refund (god mode, W10 and
W11), the audit, the gate, concurrent edits and the application's own login."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier, Event
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import Client
from django.urls import resolve

from randirandi.core import (
    asset_admin,
    ordering,
    payment_adjustments,
    payments,
)
from randirandi.core import availability_admin as admin
from randirandi.core import payment_callbacks as callbacks
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core.asset_admin import LayoutRefusedError
from randirandi.core.audit_types import AuditAction
from randirandi.core.booking_emails import send_cancellations
from randirandi.core.models import (
    Asset,
    AuditEvent,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    Booking,
    BookingState,
    Member,
    Payment,
    Refund,
)
from randirandi.core.ordering import OrderUnavailableError
from randirandi.vipps import PaymentStatus, VippsError
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_blocks import answer, request_to
from tests.test_admin_shell import config, sign_in_administrator
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, get, page_settings  # noqa: F401
from tests.test_ordering import NOW, checkout, request
from tests.test_payment_adjustments import administrator as administrator
from tests.test_payment_adjustments import paid
from tests.test_payment_capture import Money
from tests.test_payment_capture import money as money
from tests.test_payments import Provider
from tests.test_payments import provider as provider
from tests.test_second_factor import clocks, token  # noqa: F401
from tests.test_setup_screens import admin_in

pytestmark = pytest.mark.django_db(transaction=True)
FULL = [[0, 1440]]
TUESDAY = DAY + timedelta(days=1)


def a_week(monday: list[list[int]] | None = None) -> list[list[list[int]]]:
    """A week around the clock, with Monday's spans as given."""
    days = [[list(span) for span in FULL] for _ in range(7)]
    if monday is not None:
        days[0] = monday
    return days


def new_week(week: list[list[list[int]]], start: date = DAY) -> dict[str, Any]:
    return {"kind": "week-make", "start_date": start.isoformat(), "week": week}


def closure(first: date, last: date | None = None, reason: str = "") -> dict[str, Any]:
    return {
        "kind": "closure-make",
        "first_date": first.isoformat(),
        "last_date": (last or first).isoformat(),
        "reason": reason,
    }


def preview(who: Member, made: Example, fields: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(admin.PreviewedError) as caught:
        admin.apply(who, made.asset.pk, {**fields, "preview": True}, now=NOW)
    return caught.value.preview


def save(
    who: Member,
    made: Example,
    fields: dict[str, Any],
    shown: dict[str, Any] | None = None,
) -> tuple[int, list[dict[str, object]]]:
    """Save a change with the token of its preview, then cancel what it strands."""
    shown = shown or preview(who, made, fields)
    saved = admin.apply(
        who, made.asset.pk, {**fields, "expect": shown["token"]}, now=NOW
    )
    return saved.cancelled, list(saved.unfinished)


def events(made: Example, action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.of(made.org).filter(action=action).order_by("pk"))


def states(made: Example) -> list[str]:
    return list(
        Booking.objects.of(made.org)
        .order_by("taken_time__starts_at")
        .values_list("taken_time__state", flat=True)
    )


def nothing_kept(made: Example) -> None:
    assert AvailabilityCalendar.objects.of(made.org).count() == 1
    assert not AvailabilityClosure.objects.of(made.org).exists()
    assert not AuditEvent.objects.of(made.org).exists()


# --- A narrower week: preview, cancel, refund, mail, audit ---------------------------


def test_a_narrower_week_previews_then_cancels_and_refunds_what_it_removes(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    # Monday is open until 12:00 from the 1st: the 12:00 to 14:00 booking is
    # gone, the 10:00 to 12:00 one stays.
    fields = new_week(a_week([[0, 12 * 60]]))
    shown = preview(administrator, made, fields)
    assert (shown["bookings"], shown["members"]) == (1, 1)
    assert shown["refund"] == [{"currency": payment.currency, "amount": 200}]
    (line,) = shown["listed"]
    assert line["start"] == "2027-03-01 12:00" and line["end"] == "2027-03-01 14:00"
    assert (line["number"], line["state"]) == (payment.order.number, "paid")
    assert (line["member"], line["amount"]) == (made.member.full_name, 200)
    # A preview keeps nothing: no calendar, no event, nothing cancelled.
    nothing_kept(made)
    assert states(made) == ["confirmed", "confirmed"]

    assert save(administrator, made, fields, shown) == (1, [])

    assert states(made) == ["confirmed", "cancelled"]
    first, second = payment.order.bookings.order_by("taken_time__starts_at")
    assert second.cancelled_by == administrator and first.cancelled_at is None
    # One event for the change and one for the cancelled booking; its refund.
    (made_event,) = events(made, AuditAction.MADE_AVAILABILITY)
    assert made_event.administrator == administrator
    assert made_event.object_kind == "availability_calendar"
    (cancel_event,) = events(made, AuditAction.CANCEL_BOOKING_LINE)
    assert cancel_event.object_id == second.pk
    assert len(events(made, AuditAction.REQUEST_REFUND)) == 1
    refund = Refund.objects.of(made.org).get()
    assert (refund.amount, refund.booking_id, refund.made_by) == (
        200,
        second.pk,
        administrator,
    )
    # The refund goes to the provider's fake by the worker, once.
    reconciliation.reconcile_requested(now=NOW)
    assert [row[1] for row in money.refunds] == [200]
    # The cancellation mail, as the existing cancel sends it.
    assert second.cancel_mail_due_at == NOW
    mail.outbox.clear()
    send_cancellations(now=NOW)
    (message,) = mail.outbox
    assert message.to == [made.member.email]
    # The time that stays open still sells; the removed time does not.
    assert len(made.offers()) < 10
    with pytest.raises(OrderUnavailableError):
        checkout(made, request(made, starts_at=at(hour=12), ends_at=at(hour=13)))


def test_a_booking_partly_in_the_removed_time_is_cancelled_and_refunded_whole(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    # From 11:00: the 10:00 to 12:00 booking loses its first hour only.
    fields = new_week(a_week([[11 * 60, 1440]]))
    shown = preview(administrator, made, fields)
    assert shown["bookings"] == 1
    assert shown["refund"] == [{"currency": payment.currency, "amount": 200}]
    (line,) = shown["listed"]
    assert line["start"] == "2027-03-01 10:00" and line["amount"] == 200
    assert save(administrator, made, fields, shown) == (1, [])
    assert states(made) == ["cancelled", "confirmed"]
    reconciliation.reconcile_requested(now=NOW)
    assert [row[1] for row in money.refunds] == [200]


def test_a_closure_by_date_takes_every_booking_of_the_day_in_one_refund(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    fields = closure(DAY, reason="repair")
    shown = preview(administrator, made, fields)
    assert (shown["bookings"], shown["members"]) == (2, 1)
    assert shown["refund"] == [{"currency": payment.currency, "amount": 400}]
    assert [row["start"][-5:] for row in shown["listed"]] == ["10:00", "12:00"]
    assert save(administrator, made, fields, shown) == (2, [])
    assert states(made) == ["cancelled", "cancelled"]
    row = AvailabilityClosure.objects.of(made.org).get()
    assert (row.first_date, row.last_date, row.reason) == (DAY, DAY, "repair")
    (made_event,) = events(made, AuditAction.MADE_CLOSURE)
    assert (made_event.object_kind, made_event.object_id) == (
        "availability_closure",
        row.pk,
    )
    assert len(events(made, AuditAction.CANCEL_BOOKING_LINE)) == 2
    reconciliation.reconcile_requested(now=NOW)
    assert [row[1] for row in money.refunds] == [400]
    # The day shows closed in the offer.
    assert made.offers() == ()
    assert len(made.offers(TUESDAY)) == 10


def test_a_change_that_removes_nothing_previews_zero_and_saves_without_a_token(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    paid(made, money)
    far = DAY + timedelta(days=30)
    shown = preview(administrator, made, closure(far))
    assert (shown["bookings"], shown["members"], shown["refund"]) == (0, 0, [])
    saved = admin.apply(administrator, made.asset.pk, closure(far), now=NOW)
    assert saved.found == []
    assert AvailabilityClosure.objects.of(made.org).count() == 1
    assert states(made) == ["confirmed", "confirmed"]


def test_a_removed_calendar_strands_what_only_it_opened(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    # Monday is closed in the calendar in force; a calendar from the 1st opens it.
    made.available([], weekday=0)
    later = AvailabilityCalendar.objects.create(
        organization=made.org, asset=made.asset, start_date=DAY
    )
    for weekday in range(7):
        AvailableSpan.objects.create(
            organization=made.org,
            availability_calendar=later,
            weekday=weekday,
            start_time=0,
            end_time=1440,
        )
    payment = paid(made, money)
    fields = {"kind": "week-remove", "id": later.pk}
    shown = preview(administrator, made, fields)
    assert (shown["bookings"], shown["members"]) == (2, 1)
    assert shown["refund"] == [{"currency": payment.currency, "amount": 400}]
    assert save(administrator, made, fields, shown) == (2, [])
    assert not AvailabilityCalendar.objects.of(made.org).filter(pk=later.pk).exists()
    assert (
        not AvailableSpan.objects.of(made.org)
        .filter(availability_calendar_id=later.pk)
        .exists()
    )
    assert len(events(made, AuditAction.REMOVED_AVAILABILITY)) == 1
    assert states(made) == ["cancelled", "cancelled"]


def test_a_calendar_in_force_is_not_changed_or_removed_and_a_date_is_free(
    made: Example, administrator: Member
) -> None:
    for kind in ("week-remove", "week-change"):
        fields: dict[str, Any] = {"kind": kind, "id": made.availability.pk}
        if kind == "week-change":
            fields |= {"start_date": "2027-03-05", "week": a_week()}
        with pytest.raises(LayoutRefusedError) as refused:
            preview(administrator, made, fields)
        assert refused.value.code == "not-coming"
    with pytest.raises(LayoutRefusedError) as refused:
        preview(administrator, made, new_week(a_week(), date(2027, 2, 27)))
    assert refused.value.code == "past"
    coming = admin.apply(administrator, made.asset.pk, new_week(a_week(), DAY), now=NOW)
    assert coming.found == []
    with pytest.raises(LayoutRefusedError) as refused:
        preview(administrator, made, new_week(a_week()))
    assert refused.value.code == "date-taken"
    # A coming calendar is changed to another date and week, and joins spans.
    row = AvailabilityCalendar.objects.of(made.org).get(start_date=DAY)
    admin.apply(
        administrator,
        made.asset.pk,
        {
            "kind": "week-change",
            "id": row.pk,
            "start_date": "2027-03-08",
            "week": a_week([[0, 600], [600, 700], [900, 960]]),
        },
        now=NOW,
    )
    row.refresh_from_db()
    assert row.start_date == date(2027, 3, 8)
    assert sorted(
        AvailableSpan.objects.of(made.org)
        .filter(availability_calendar=row, weekday=0)
        .values_list("start_time", "end_time")
    ) == [(0, 700), (900, 960)]
    assert len(events(made, AuditAction.CHANGED_AVAILABILITY)) == 1


def test_a_closure_is_changed_and_removed_until_it_is_over(
    made: Example, administrator: Member
) -> None:
    for first in (DAY, DAY + timedelta(days=9)):
        admin.apply(
            administrator, made.asset.pk, closure(first, reason="repair"), now=NOW
        )
    near, far = AvailabilityClosure.objects.of(made.org).order_by("first_date")
    admin.apply(
        administrator,
        made.asset.pk,
        {
            "kind": "closure-change",
            "id": near.pk,
            "first_date": "2027-03-02",
            "last_date": "2027-03-04",
            "reason": "",
        },
        now=NOW,
    )
    near.refresh_from_db()
    assert (near.first_date, near.last_date, near.reason) == (
        date(2027, 3, 2),
        date(2027, 3, 4),
        "",
    )
    admin.apply(
        administrator,
        made.asset.pk,
        {"kind": "closure-remove", "id": far.pk},
        now=NOW,
    )
    assert AvailabilityClosure.objects.of(made.org).count() == 1
    assert len(events(made, AuditAction.CHANGED_CLOSURE)) == 1
    assert len(events(made, AuditAction.REMOVED_CLOSURE)) == 1
    # Once it is over, a closure stays as history.
    over = NOW + timedelta(days=30)
    with pytest.raises(LayoutRefusedError) as refused:
        admin.apply(
            administrator,
            made.asset.pk,
            {"kind": "closure-remove", "id": near.pk},
            now=over,
        )
    assert refused.value.code == "over"
    with pytest.raises(LayoutRefusedError) as refused:
        admin.apply(administrator, made.asset.pk, closure(date(2027, 2, 1)), now=NOW)
    assert refused.value.code == "past"


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "nothing"},
        {"kind": "closure-make", "first_date": "2027-03-01"},
        {"kind": "closure-make", "first_date": "2027-3-1", "last_date": "2027-03-01"},
        {"kind": "closure-make", "first_date": "1999-12-31", "last_date": "2027-03-01"},
        {"kind": "closure-make", "first_date": "2027-03-01", "last_date": "2101-01-01"},
        {
            "kind": "closure-make",
            "first_date": "2027-03-01",
            "last_date": "2027-03-01",
            "note": "free text",
        },
        {"kind": "closure-remove", "id": "1"},
        {"kind": "closure-remove", "id": 0},
        {"kind": "week-make", "start_date": "2027-03-05", "week": []},
        {"kind": "week-make", "start_date": "2027-03-05", "week": [[]] * 8},
        {"kind": "week-make", "start_date": "2027-03-05", "week": [[[1, "2"]]] * 7},
        {"kind": "holidays", "year": 2027, "dates": []},
        {"kind": "holidays", "year": 2027, "dates": ["2027-03-02"]},
        {"kind": "holidays", "year": 2030, "dates": ["2030-12-25"]},
    ],
)
def test_a_malformed_change_is_refused_and_changes_nothing(
    made: Example, administrator: Member, bad: dict[str, Any]
) -> None:
    with pytest.raises((ValueError, LayoutRefusedError)):
        admin.apply(administrator, made.asset.pk, bad, now=NOW)
    nothing_kept(made)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (closure(TUESDAY, DAY), "ends-first"),
        (closure(TUESDAY, reason="party"), "invalid-reason"),
        (new_week(a_week([[10, 5]])), "invalid-span"),
        (new_week(a_week([[0, 1441]])), "invalid-span"),
        (new_week(a_week([[-1, 5]])), "invalid-span"),
    ],
)
def test_a_change_the_model_refuses_says_why(
    made: Example, administrator: Member, fields: dict[str, Any], code: str
) -> None:
    with pytest.raises(LayoutRefusedError) as refused:
        admin.apply(administrator, made.asset.pk, fields, now=NOW)
    assert refused.value.code == code
    nothing_kept(made)


# --- A booking made between the preview and the save --------------------------------


def held_order(made: Example, money: Money, day: date) -> Payment:
    """An order being paid, with its payment, on a day."""
    order = checkout(made, request(made, starts_at=at(day, 10), ends_at=at(day, 12)))
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", order.total, order.total, 0, 0, 0
    )
    return payment


def test_a_priced_order_with_no_payment_is_cancelled_and_left_for_a_refund_by_hand(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    """God mode can leave an order without its payment row: the change is not
    refused; the order is cancelled with no refund, listed as unfinished, and
    not kept on the page: it has nothing to refund."""
    order = checkout(
        made, request(made, starts_at=at(TUESDAY, 10), ends_at=at(TUESDAY, 12))
    )
    assert order.total > 0
    assert not Payment.objects.of(made.org).filter(order=order).exists()
    cancelled, unfinished = save(administrator, made, closure(TUESDAY, reason="repair"))
    assert cancelled == 1
    assert unfinished == [{"number": order.number, "code": "no-payment"}]
    assert states(made) == ["cancelled"]
    assert not Refund.objects.of(made.org).exists()
    assert not events(made, AuditAction.LEFT_REFUND_BY_HAND)
    assert admin.page(made.org, made.asset, now=NOW)["by_hand"] == []


def test_captured_money_nobody_can_attribute_stays_listed_until_a_refund_exists(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    """The provider has refunded 1 that no refund row explains: the change cancels
    the bookings, queues nothing, writes the event, and the order is listed by
    hand until a refund row exists for its payment."""
    payment = paid(made, money)
    Payment.objects.of(made.org).filter(pk=payment.pk).update(refunded_amount=1)
    # A refund made before the change, which names no booking: it is no reason
    # to think this money came back.
    Refund.objects.create(
        organization=made.org,
        payment=payment,
        amount=1,
        made_by=administrator,
        completed_at=NOW,
    )
    cancelled, unfinished = save(administrator, made, closure(DAY, reason="repair"))
    assert cancelled == 2
    assert unfinished == [{"number": payment.order.number, "code": "recovery"}]
    assert states(made) == ["cancelled"] * 2
    assert Refund.objects.of(made.org).count() == 1
    (left,) = events(made, AuditAction.LEFT_REFUND_BY_HAND)
    assert (left.object_kind, left.object_id) == ("order", payment.order_id)
    (row,) = admin.page(made.org, made.asset, now=NOW)["by_hand"]
    assert row["number"] == payment.order.number
    assert row["reference"] == payment.order.reference
    assert len(admin.page(made.org, made.asset, now=NOW)["by_hand"]) == 1
    Refund.objects.create(
        organization=made.org,
        payment=payment,
        amount=1,
        made_by=administrator,
        completed_at=NOW,
    )
    assert admin.page(made.org, made.asset, now=NOW)["by_hand"] == []


def test_an_order_with_nothing_owed_is_not_listed_by_hand(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    paid(made, money)
    assert save(administrator, made, closure(DAY, TUESDAY, reason="repair"))[1] == []
    assert admin.page(made.org, made.asset, now=NOW)["by_hand"] == []
    assert not events(made, AuditAction.LEFT_REFUND_BY_HAND)


def test_a_busy_database_while_recording_the_look_up_is_busy_not_a_500(
    site: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.db import OperationalError

    from randirandi.core import admin_availability_views as views

    def busy(**_: object) -> None:
        raise OperationalError("lock timeout")

    path = f"{ROOT}/{site.asset.pk}/availability/apply/"
    monkeypatch.setattr(views, "record_event", busy)
    fields = closure(site.day)
    assert answer(call(site, path, {**fields, "preview": True}), 409) == {
        "error": "busy"
    }
    assert answer(call(site, path, {**fields, "expect": "0" * 24}), 409) == {
        "error": "busy"
    }
    assert not AvailabilityClosure.objects.of(site.organization).exists()


def test_a_booking_made_between_the_preview_and_the_save_is_caught_by_a_second_preview(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    paid(made, money)
    fields = closure(DAY, TUESDAY)
    shown = preview(administrator, made, fields)
    assert shown["bookings"] == 2
    # A member books Tuesday after the administrator saw two.
    waiting = held_order(made, money, TUESDAY)
    with pytest.raises(admin.ChangedSincePreviewError) as changed:
        admin.apply(
            administrator,
            made.asset.pk,
            {**fields, "expect": shown["token"]},
            now=NOW,
        )
    assert changed.value.code == "changed"
    fresh = changed.value.preview
    assert fresh["bookings"] == 3 and fresh["token"] != shown["token"]
    # Nothing was saved or cancelled by the refused save.
    nothing_kept(made)
    assert states(made).count("confirmed") == 2
    # No token for a change that strands bookings is the same refusal.
    with pytest.raises(admin.ChangedSincePreviewError):
        admin.apply(administrator, made.asset.pk, fields, now=NOW)
    # The second preview's token saves it, and the order being paid is cancelled
    # whole through the unpaid-order cancel, the provider's cancel called once.
    assert save(administrator, made, fields, fresh) == (3, [])
    assert sorted(states(made)) == ["cancelled"] * 3
    with application_login():
        reconciliation.reconcile_requested(now=NOW)
    assert len(money.cancellations) == 1
    assert waiting.order.bookings.get().cancelled_at is not None
    # And nothing can be booked in the closed days afterwards.
    with pytest.raises(OrderUnavailableError):
        checkout(
            made, request(made, starts_at=at(TUESDAY, 13), ends_at=at(TUESDAY, 14))
        )


def test_the_other_lines_of_an_order_being_paid_go_with_it(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    order = checkout(
        made,
        request(made, starts_at=at(DAY, 10), ends_at=at(DAY, 12)),
        request(made, starts_at=at(TUESDAY, 10), ends_at=at(TUESDAY, 12)),
    )
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", order.total, order.total, 0, 0, 0
    )
    # Only Monday closes; the order is cancelled whole, Tuesday's line too.
    fields = closure(DAY)
    shown = preview(administrator, made, fields)
    assert shown["bookings"] == 2 and shown["refund"] == []
    assert save(administrator, made, fields, shown) == (2, [])
    assert states(made) == ["cancelled", "cancelled"]


# --- The provider away, and a line of a paid order ------------------------------------


def pending_refunds(made: Example) -> list[Refund]:
    return list(
        Refund.objects.of(made.org)
        .filter(refund_key__isnull=False, completed_at__isnull=True)
        .order_by("pk")
    )


def provider_down_for(money: Money, *references: str) -> None:
    """The provider fails for these payments' references only."""

    def down() -> None:
        if not references or money.reads[-1] in references:
            raise VippsError(503)

    money.before_read = down


def the_worker(now: datetime = NOW) -> None:
    """One pass of the worker; a provider that is down does not stop it."""
    with application_login():
        reconciliation.reconcile_requested(now=now)


def paid_on(made: Example, money: Money, day: date) -> Payment:
    """A second paid order, on another day."""
    order = checkout(made, request(made, starts_at=at(day, 10), ends_at=at(day, 12)))
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", order.total, order.total, 0, 0, 0
    )
    callbacks.request_reconciliation(made.org, order.public_id, now=NOW)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    return payment


def test_a_provider_that_is_away_changes_nothing_the_bookings_are_cancelled_and_owed(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    provider_down_for(money)
    reads = len(money.reads)
    # The save asks the provider nothing: it succeeds with the provider down.
    assert save(administrator, made, closure(DAY)) == (2, [])
    assert len(money.reads) == reads
    assert AvailabilityClosure.objects.of(made.org).count() == 1
    assert states(made) == ["cancelled", "cancelled"]
    (owed,) = pending_refunds(made)
    assert owed.amount == 400 and owed.payment_id == payment.pk
    page = admin.page(made.org, made.asset, now=NOW)
    assert [
        (row["number"], row["amount"], row["currency"]) for row in page["owed"]
    ] == [(payment.order.number, 400, payment.currency)]
    # The worker tries again while the provider is away, and keeps it owed.
    the_worker()
    the_worker(NOW + timedelta(hours=1))
    assert money.refunds == [] and len(pending_refunds(made)) == 1
    assert len(admin.page(made.org, made.asset, now=NOW)["owed"]) == 1
    # The provider is back: the same intent is sent once and is no longer owed.
    money.before_read = lambda: None
    the_worker(NOW + timedelta(hours=2))
    assert [row[1] for row in money.refunds] == [400]
    assert pending_refunds(made) == []
    assert admin.page(made.org, made.asset, now=NOW)["owed"] == []
    assert Payment.objects.of(made.org).get(pk=payment.pk).refunded_amount == 400


def test_with_the_provider_down_for_one_of_two_orders_both_are_cancelled(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    first = paid(made, money)
    second = paid_on(made, money, TUESDAY)
    provider_down_for(money, first.provider_reference)
    assert save(administrator, made, closure(DAY, TUESDAY)) == (3, [])
    assert states(made) == ["cancelled"] * 3
    assert sorted(row.amount for row in pending_refunds(made)) == [200, 400]
    the_worker()
    # The order the provider answers for is refunded; the other stays owed.
    assert [row[0] for row in money.refunds] == [second.provider_reference]
    (still,) = pending_refunds(made)
    assert still.payment_id == first.pk and still.amount == 400
    owed = admin.page(made.org, made.asset, now=NOW)["owed"]
    assert [row["number"] for row in owed] == [first.order.number]
    money.before_read = lambda: None
    the_worker(NOW + timedelta(hours=1))
    assert sorted(row[0] for row in money.refunds) == sorted(
        [first.provider_reference, second.provider_reference]
    )
    assert pending_refunds(made) == []


def test_a_crash_after_the_commit_loses_nothing(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    """Nothing follows the commit that a crash could lose: the bookings are
    cancelled and the refund queued when `apply` returns, and the page the view
    builds afterwards is only a read."""
    payment = paid(made, money)
    fields = closure(DAY)
    shown = preview(administrator, made, fields)
    saved = admin.apply(
        administrator, made.asset.pk, {**fields, "expect": shown["token"]}, now=NOW
    )
    assert saved.cancelled == 2
    # A new process finds it as it is.
    assert states(made) == ["cancelled", "cancelled"]
    (owed,) = pending_refunds(made)
    assert owed.amount == 400 and owed.payment_id == payment.pk
    assert len(events(made, AuditAction.CANCEL_BOOKING_LINE)) == 2
    assert Booking.objects.of(made.org).filter(cancel_mail_due_at=NOW).count() == 2
    the_worker()
    assert [row[1] for row in money.refunds] == [400]


def test_a_crash_while_the_view_builds_its_answer_leaves_the_change_whole(
    site: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = f"{ROOT}/{site.asset.pk}/availability/apply/"
    body = closure(date(2027, 6, 1))
    token_ = answer(call(site, asset, {**body, "preview": True}))["preview"]["token"]

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the process died")

    monkeypatch.setattr(admin, "page", crash)
    with pytest.raises(RuntimeError):
        call(site, asset, {**body, "expect": token_})
    assert AvailabilityClosure.objects.of(site.organization).count() == 1


def test_an_operational_error_while_cancelling_is_busy_and_saves_nothing(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.db import OperationalError

    paid(made, money)
    fields = closure(DAY)
    shown = preview(administrator, made, fields)

    def locked(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("could not obtain lock")

    monkeypatch.setattr(payment_adjustments, "cancel_in_transaction", locked)
    with pytest.raises(OperationalError):
        admin.apply(
            administrator, made.asset.pk, {**fields, "expect": shown["token"]}, now=NOW
        )
    # Rolled back whole: no closure, nothing cancelled, nothing owed.
    assert not AvailabilityClosure.objects.of(made.org).exists()
    assert states(made) == ["confirmed", "confirmed"]
    assert pending_refunds(made) == []


def test_money_moved_on_a_booking_after_the_preview_changes_the_token(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    fields = closure(DAY)
    shown = preview(administrator, made, fields)
    first = payment.order.bookings.order_by("taken_time__starts_at").first()
    assert first is not None
    # A refund for one booking was queued elsewhere: the same bookings, less to refund.
    Refund.objects.create(
        organization=made.org,
        payment=payment,
        booking=first,
        amount=50,
        made_by=administrator,
        refund_key=uuid.uuid4(),
        refunded_before=0,
        made_at=NOW,
    )
    with pytest.raises(admin.ChangedSincePreviewError) as changed:
        admin.apply(
            administrator, made.asset.pk, {**fields, "expect": shown["token"]}, now=NOW
        )
    assert changed.value.preview["bookings"] == 2
    assert changed.value.preview["refund"] == [
        {"currency": payment.currency, "amount": 350}
    ]
    nothing_cancelled = states(made) == ["confirmed", "confirmed"]
    assert nothing_cancelled


def test_the_token_reads_each_bookings_state_and_refund() -> None:
    from types import SimpleNamespace

    def item(pk: int, state: str, refund: int) -> Any:
        booking = SimpleNamespace(pk=pk, taken_time=SimpleNamespace(state=state))
        return SimpleNamespace(booking=booking, refund=refund)

    base = [item(1, "confirmed", 200), item(2, "held", 0)]
    assert admin.token(base) == admin.token(list(reversed(base)))
    assert admin.token(base) != admin.token(
        [item(1, "confirmed", 150), item(2, "held", 0)]
    )
    assert admin.token(base) != admin.token([item(1, "held", 200), item(2, "held", 0)])


def test_a_booking_that_is_already_cancelled_is_not_stranded(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    payment = paid(made, money)
    first = payment.order.bookings.order_by("taken_time__starts_at").first()
    assert first is not None
    first.taken_time.state = BookingState.CANCELLED
    first.taken_time.save()
    shown = preview(administrator, made, closure(DAY))
    assert shown["bookings"] == 1


# --- Another organization, not an administrator, CSRF -------------------------------

ROOT = "/admin/assets"


@pytest.fixture
def site() -> made_up.MadeUp:
    return made_up.everything()


def call(
    site: made_up.MadeUp,
    path: str,
    data: Any = None,
    *,
    method: str = "post",
    who: Member | None = None,
    confirmed: bool = True,
) -> HttpResponse:
    request_ = request_to(
        who or site.administrator, method, path, data, confirmed=confirmed
    )
    found = resolve(path)
    response = found.func(request_, *found.args, **found.kwargs)
    assert isinstance(response, HttpResponse)
    return response


def every_call(site: made_up.MadeUp) -> list[tuple[str, dict[str, Any], str]]:
    base = f"{ROOT}/{site.asset.pk}/availability"
    return [
        (f"{base}/data/", {}, "get"),
        (f"{base}/month/", {"month": "2027-03"}, "get"),
        (f"{base}/holidays/", {"year": 2027}, "get"),
        (f"{base}/apply/", {**closure(date(2027, 6, 1)), "preview": True}, "post"),
    ]


def test_only_a_confirmed_administrator_of_this_organization_may(
    site: made_up.MadeUp,
) -> None:
    before = AuditEvent.objects.of(site.organization).count()
    for path, data, method in every_call(site):
        for who, confirmed in ((site.administrator, False), (site.member, True)):
            with pytest.raises(PermissionDenied):
                call(site, path, data, method=method, who=who, confirmed=confirmed)
    elsewhere = made_up.everything("elsewhere")
    for path, data, method in every_call(site):
        with pytest.raises(PermissionDenied):
            call(site, path, data, method=method, who=elsewhere.administrator)
    assert AuditEvent.objects.of(site.organization).count() == before


def test_another_organizations_asset_or_closure_is_a_404_and_nothing_changes(
    site: made_up.MadeUp,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    theirs = AvailabilityClosure.objects.create(
        organization=elsewhere.organization,
        asset=elsewhere.asset,
        first_date=date(2027, 6, 1),
        last_date=date(2027, 6, 2),
    )
    counts = [AuditEvent.objects.of(o.organization).count() for o in (elsewhere, site)]
    for path, data, method in every_call(elsewhere):
        found = answer(call(site, path, data, method=method), 404)
        assert found["error"] == "not-found", path
    mine = f"{ROOT}/{site.asset.pk}/availability/apply/"
    for body in (
        {"kind": "closure-remove", "id": theirs.pk},
        {**closure(date(2027, 6, 5)), "kind": "closure-change", "id": theirs.pk},
    ):
        found = answer(call(site, mine, body), 404)
        assert found["error"] == "not-found"
    theirs.refresh_from_db()
    assert (theirs.first_date, theirs.last_date) == (date(2027, 6, 1), date(2027, 6, 2))
    assert counts == [
        AuditEvent.objects.of(o.organization).count() for o in (elsewhere, site)
    ]
    # A huge id is a 404 too.
    huge = f"{ROOT}/{2**63}/availability/data/"
    assert answer(call(site, huge, method="get"), 404)["error"] == "not-found"


def test_the_wrong_method_is_a_405_and_a_signed_out_browser_signs_in(
    site: made_up.MadeUp,
) -> None:
    client = Client()
    with application_login():
        sign_in_administrator(client, site.administrator)
        for path, _data, method in every_call(site):
            other = "post" if method == "get" else "get"
            response = getattr(client, other)(path, HTTP_HOST=HOST, secure=True)
            assert response.status_code == 405, path
    for path, _data, method in every_call(site):
        response = getattr(Client(), method)(path, HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302, path
        assert response.headers["Location"].startswith("/login/")


def test_csrf_and_origin_are_the_other_post(site: made_up.MadeUp) -> None:
    client = Client(enforce_csrf_checks=True)
    path = f"{ROOT}/{site.asset.pk}/availability/apply/"
    with application_login():
        sign_in_administrator(client, site.administrator)
        csrf = str(config(get(client, "/admin/assets/").content.decode())["csrf"])

        def send(headers: dict[str, Any], body: dict[str, Any]) -> Any:
            return client.post(
                path,
                json.dumps(body),
                content_type="application/json",
                HTTP_HOST=HOST,
                secure=True,
                **headers,
            )

        body = closure(date(2027, 6, 1))
        right = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://" + HOST}
        wrong = {"HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://elsewhere.example"}
        assert send({"HTTP_ORIGIN": "https://" + HOST}, body).status_code == 403
        assert send(wrong, body).status_code == 403
        assert not AvailabilityClosure.objects.of(site.organization).exists()
        found = answer(send(right, body))
    assert found["saved"] is True and found["cancelled"] == 0
    assert found["unfinished"] == []
    assert [row["first"] for row in found["page"]["closures"]] == ["2027-06-01"]
    (made_event,) = AuditEvent.objects.of(site.organization).filter(
        action=AuditAction.MADE_CLOSURE
    )
    assert made_event.administrator == site.administrator


def test_the_http_save_previews_cancels_and_answers_the_page(
    site: made_up.MadeUp,
) -> None:
    """The made-up organization has a booking on its first Monday."""
    path = f"{ROOT}/{site.asset.pk}/availability/apply/"
    fields = closure(site.day)
    shown = answer(call(site, path, {**fields, "preview": True}))["preview"]
    assert shown["bookings"] >= 1
    stale = answer(call(site, path, {**fields, "expect": "0" * 24}), 409)
    assert stale["error"] == "changed" and stale["preview"]["token"] == shown["token"]
    assert not AvailabilityClosure.objects.of(site.organization).exists()
    for bad in (b"not json", b"[]", {"kind": "closure-make"}, {"kind": 5}):
        assert answer(call(site, path, bad), 400) == {"error": "bad-request"}
    refused = answer(call(site, path, closure(date(2027, 6, 2), site.day)), 409)
    assert refused["error"] == "ends-first"


def test_a_preview_that_lists_bookings_is_a_look_up_and_one_with_none_is_not(
    site: made_up.MadeUp,
) -> None:
    """Who booked is shown: one "listed bookings" event, though the preview
    itself keeps nothing; a preview that shows nobody writes none."""
    path = f"{ROOT}/{site.asset.pk}/availability/apply/"

    def looked_up() -> int:
        return (
            AuditEvent.objects.of(site.organization)
            .filter(action=AuditAction.LISTED_BOOKINGS)
            .count()
        )

    far = date(2030, 1, 7)
    nobody = answer(call(site, path, {**closure(far), "preview": True}))["preview"]
    assert nobody["bookings"] == 0 and looked_up() == 0
    shown = answer(call(site, path, {**closure(site.day), "preview": True}))["preview"]
    assert shown["listed"] and looked_up() == 1
    stale = answer(call(site, path, {**closure(site.day), "expect": "0" * 24}), 409)
    assert stale["preview"]["listed"] and looked_up() == 2
    assert not AvailabilityClosure.objects.of(site.organization).exists()


def test_the_page_the_month_and_the_holidays_are_read_only_calls(
    made: Example, administrator: Member
) -> None:
    administrator.full_name = "Sam Field"
    administrator.save()
    admin.apply(administrator, made.asset.pk, closure(DAY, TUESDAY, "holiday"), now=NOW)
    admin.apply(
        administrator,
        made.asset.pk,
        new_week(a_week([[8 * 60, 20 * 60]]), date(2027, 3, 8)),
        now=NOW,
    )
    before = AuditEvent.objects.of(made.org).count()
    page = admin.page(made.org, made.asset, now=NOW)
    assert page["today"] == "2027-02-28"
    assert page["asset"]["time_zone"] == "Europe/Oslo"
    in_force, coming = page["weeks"]
    assert (in_force["state"], in_force["until"]) == ("in-force", "2027-03-07")
    assert (coming["state"], coming["until"], coming["from"]) == (
        "coming",
        None,
        "2027-03-08",
    )
    assert coming["week"][0] == [[480, 1200]] and coming["week"][1] == [[0, 1440]]
    (row,) = page["closures"]
    assert (row["first"], row["last"], row["reason"], row["over"]) == (
        "2027-03-01",
        "2027-03-02",
        "holiday",
        False,
    )
    assert row["by"] == "Sam Field" and row["at"][:2] == "20"
    assert [r["value"] for r in page["reasons"]] == ["holiday", "repair", "vacation"]
    days = admin.month(made.org, made.asset, date(2027, 3, 1))["days"]
    assert len(days) == 31
    assert days[0] == {
        "date": "2027-03-01",
        "open": [],
        "closed": True,
        "week_starts": False,
    }
    assert days[2]["open"] == [[0, 1440]] and days[2]["closed"] is False
    assert days[7]["date"] == "2027-03-08" and days[7]["week_starts"] is True
    assert days[7]["open"] == [[480, 1200]]
    assert AuditEvent.objects.of(made.org).count() == before


# --- The public holidays -------------------------------------------------------------


def test_a_years_holidays_are_listed_with_what_is_closed_and_picked_as_closures(
    made: Example, administrator: Member
) -> None:
    listed = admin.holidays(made.org, made.asset, 2027, now=NOW)
    assert listed["years"] == [2027, 2028]
    assert [(h["date"], h["name"]) for h in listed["holidays"]][:5] == [
        ("2027-01-01", "New Year's Day"),
        ("2027-03-25", "Maundy Thursday"),
        ("2027-03-26", "Good Friday"),
        ("2027-03-28", "Easter Sunday"),
        ("2027-03-29", "Easter Monday"),
    ]
    assert [h["over"] for h in listed["holidays"]][:2] == [True, False]
    pick = ["2027-03-26", "2027-03-28", "2027-05-17"]
    admin.apply(
        administrator,
        made.asset.pk,
        {"kind": "holidays", "year": 2027, "dates": pick},
        now=NOW,
    )
    rows = list(AvailabilityClosure.objects.of(made.org).order_by("first_date"))
    assert [
        (r.first_date.isoformat(), r.last_date.isoformat(), r.reason) for r in rows
    ] == [(day, day, "holiday") for day in pick]
    assert len(events(made, AuditAction.MADE_CLOSURE)) == 3
    # Picked again, they are closed already: nothing is repeated.
    admin.apply(
        administrator,
        made.asset.pk,
        {"kind": "holidays", "year": 2027, "dates": pick},
        now=NOW,
    )
    assert AvailabilityClosure.objects.of(made.org).count() == 3
    closed = {
        h["date"]: h["closed"]
        for h in admin.holidays(made.org, made.asset, 2027, now=NOW)["holidays"]
    }
    assert [d for d, c in closed.items() if c] == pick
    with pytest.raises(ValueError, match="this one or the next"):
        admin.holidays(made.org, made.asset, 2029, now=NOW)


# --- Concurrent edits ---------------------------------------------------------------


def test_two_administrators_making_the_same_week_end_in_one_valid_state(
    made: Example, administrator: Member
) -> None:
    second = admin_in(made.org)
    barrier = Barrier(2)
    fields = new_week(a_week([[0, 600]]), date(2027, 3, 15))

    def run(who: Member) -> str:
        barrier.wait()
        try:
            admin.apply(who, made.asset.pk, fields, now=NOW)
            return "saved"
        except LayoutRefusedError as refused:
            return refused.code
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(
            f.result()
            for f in [pool.submit(run, administrator), pool.submit(run, second)]
        )
    assert results == ["date-taken", "saved"]
    assert AvailabilityCalendar.objects.of(made.org).count() == 2
    spans = AvailableSpan.objects.of(made.org).filter(
        availability_calendar__start_date=date(2027, 3, 15), weekday=0
    )
    assert list(spans.values_list("start_time", "end_time")) == [(0, 600)]


def test_a_closure_and_a_week_made_at_once_both_land(
    made: Example, administrator: Member
) -> None:
    second = admin_in(made.org)
    barrier = Barrier(2)

    def run(who: Member, fields: dict[str, Any]) -> None:
        barrier.wait()
        try:
            admin.apply(who, made.asset.pk, fields, now=NOW)
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        for future in [
            pool.submit(run, administrator, closure(DAY + timedelta(days=20))),
            pool.submit(run, second, new_week(a_week(), date(2027, 4, 12))),
        ]:
            future.result()
    assert AvailabilityClosure.objects.of(made.org).count() == 1
    assert AvailabilityCalendar.objects.of(made.org).count() == 2


def test_a_checkout_begun_before_a_closure_commits_is_refused_after_it(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The change holds the organization's row; a checkout that starts then
    waits for it with a snapshot older than the commit (it is SERIALIZABLE).
    It must fail to serialise and read the new availability, not book the
    closed day."""
    inside, outcome = Event(), {}
    real = admin.stranded

    def slow(*args: Any, **kwargs: Any) -> Any:
        inside.set()
        time.sleep(1.0)  # the checkout starts and waits for the row now
        return real(*args, **kwargs)

    monkeypatch.setattr(admin, "stranded", slow)

    def book() -> None:
        inside.wait()
        try:
            checkout(made, request(made, starts_at=at(DAY, 10), ends_at=at(DAY, 12)))
            outcome["booked"] = True
        except OrderUnavailableError:
            outcome["booked"] = False
        finally:
            connection.close()

    def close() -> None:
        try:
            admin.apply(administrator, made.asset.pk, closure(DAY), now=NOW)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(book), pool.submit(close)]:
            future.result()
    assert outcome["booked"] is False
    assert not Booking.objects.of(made.org).exists()


def test_a_checkout_that_holds_the_row_first_is_found_by_the_change(
    made: Example,
    provider: Provider,
    money: Money,
    administrator: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout holds the organization's row; the change waits for it and
    then reads the booking, which it strands."""
    inside, outcome = Event(), {}
    real = ordering._capture

    def slow(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        inside.set()
        time.sleep(1.0)  # the change starts and waits for the row now
        return found

    monkeypatch.setattr(ordering, "_capture", slow)

    def book() -> None:
        try:
            checkout(made, request(made, starts_at=at(DAY, 10), ends_at=at(DAY, 12)))
        finally:
            connection.close()

    def close() -> None:
        inside.wait()
        try:
            admin.apply(administrator, made.asset.pk, closure(DAY), now=NOW)
        except admin.ChangedSincePreviewError as changed:
            outcome["found"] = changed.preview["bookings"]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(book), pool.submit(close)]:
            future.result()
    assert outcome["found"] == 1


# --- The application's own login ---------------------------------------------------


def test_the_restricted_application_login_can_do_all_of_it(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    paid(made, money)
    held_order(made, money, TUESDAY)
    with application_login():
        fields = closure(DAY, TUESDAY, reason="repair")
        # The paid order's refund is queued and the order being paid is given its
        # cancel intent, each by the application's own rights; the worker then
        # sends both to the provider's fake, as the application login too.
        assert save(administrator, made, fields) == (3, [])
        assert states(made) == ["cancelled"] * 3
        reconciliation.reconcile_requested(now=NOW)
        assert [row[1] for row in money.refunds] == [400]
        assert len(money.cancellations) == 1
        assert pending_refunds(made) == []
        row = AvailabilityClosure.objects.of(made.org).get()
        admin.apply(
            administrator,
            made.asset.pk,
            {
                "kind": "closure-change",
                "id": row.pk,
                "first_date": "2027-03-01",
                "last_date": "2027-03-03",
                "reason": "repair",
            },
            now=NOW,
        )
        admin.apply(
            administrator,
            made.asset.pk,
            new_week(a_week([[0, 600]]), date(2027, 3, 8)),
            now=NOW,
        )
        coming = AvailabilityCalendar.objects.of(made.org).get(
            start_date=date(2027, 3, 8)
        )
        admin.apply(
            administrator,
            made.asset.pk,
            {
                "kind": "week-change",
                "id": coming.pk,
                "start_date": "2027-03-09",
                "week": a_week(),
            },
            now=NOW,
        )
        admin.apply(
            administrator,
            made.asset.pk,
            {"kind": "week-remove", "id": coming.pk},
            now=NOW,
        )
        admin.apply(
            administrator,
            made.asset.pk,
            {"kind": "closure-remove", "id": row.pk},
            now=NOW,
        )
    assert not AvailabilityClosure.objects.of(made.org).exists()


def test_an_asset_with_a_closure_is_not_removable_and_says_so(
    made: Example, administrator: Member
) -> None:
    other = Asset.objects.create(organization=made.org, name="Sauna")
    far = NOW.date() + timedelta(days=60)
    AvailabilityClosure.objects.create(
        organization=made.org, asset=other, first_date=far, last_date=far
    )
    place = asset_admin.assets_place(made.org)
    (row,) = [a for a in place["assets"] if a["id"] == other.pk]  # type: ignore[attr-defined]
    assert row["removable"] is False
    assert row["availability"] == {"in_force": None, "next": None, "closures_coming": 1}
    with pytest.raises(LayoutRefusedError) as refused:
        asset_admin.remove_asset(administrator, other.pk)
    assert (refused.value.code, refused.value.used) == ("in-use", {"closures": 1})
