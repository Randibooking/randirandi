# SPDX-License-Identifier: AGPL-3.0-only
"""Scheduled per-organization retention of existing data ."""

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from typing import Any

from django.contrib.sessions.models import Session
from django.db.models import F, Q, QuerySet

from randirandi.core.login_codes import expire_requests
from randirandi.core.member_data import (
    erase_identity,
    organization_transaction,
    utc_now,
)
from randirandi.core.models import (
    AccessEvent,
    AuditEvent,
    Booking,
    LoginAttempt,
    LoginCode,
    Member,
    MemberImport,
    Membership,
    MemberStatus,
    Order,
    Organization,
    Payment,
    PaymentState,
    PaymentTrouble,
    Refund,
    SecurityNotice,
    TakenTime,
)


def after_months(moment: datetime, months: int) -> datetime:
    """Calendar months in UTC, clamping the day at the destination month end."""
    moment = moment.astimezone(UTC)
    year, month = divmod(moment.year * 12 + moment.month - 1 + months, 12)
    if year > 9999:
        return datetime.max.replace(tzinfo=UTC)
    return moment.replace(
        year=year, month=month + 1, day=min(moment.day, monthrange(year, month + 1)[1])
    )


def _unsettled_orders(org: Organization) -> QuerySet[Any, Any]:
    """The orders ordinary retention leaves alone: an uncertain provider
    operation must retain the order it can charge, and reconciliation settles
    it before ordinary booking retention applies."""
    return (
        Payment.objects.of(org)
        .filter(create_key__isnull=False)
        .filter(
            Q(settled_at__isnull=True)
            | Q(state__in=[PaymentState.CREATED, PaymentState.AUTHORIZED])
            | Q(
                captured_amount__gt=F("refunded_amount"),
                order__hold_expires_at__isnull=False,
            )
            | Q(callback_received_at__gt=F("provider_checked_at"))
            | Q(callback_received_at__isnull=False, provider_checked_at__isnull=True)
            | Q(refunds__refund_key__isnull=False, refunds__alerted_at__isnull=True)
        )
        .values("order_id")
    )


def members_due(org: Organization, months: int, moment: datetime) -> list[Member]:
    """The members the erasing job erases at `moment` when members are kept
    `months` months after they were unlisted. The job and the Organization
    screen's preview  both read this."""
    return [
        member
        for member in (
            Member.objects.of(org)
            # An active member has no unlisted_at; every other one can be erased.
            .exclude(status=MemberStatus.ACTIVE)
            .exclude(full_name="", first_name="", last_name="", email="", phone="")
            .order_by("pk")
        )
        # An unknown time of unlisting is never an erasure clock.
        if member.unlisted_at is not None
        and after_months(member.unlisted_at, months) <= moment
    ]


def bookings_due(org: Organization, months: int, moment: datetime) -> list[Booking]:
    """The bookings the erasing job erases at `moment` when they are kept
    `months` months after they ended; the job and the preview share it."""
    return [
        row
        for row in Booking.objects.of(org)
        .exclude(order_id__in=_unsettled_orders(org))
        .select_related("taken_time")
        .prefetch_related("lines")
        if after_months(row.taken_time.ends_at, months) <= moment
    ]


def erasure_preview(
    org: Organization,
    member_months: int,
    booking_months: int,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """How many members and bookings the next run of the job would erase if
    members were kept `member_months` and bookings `booking_months`. Writes
    nothing."""
    moment = utc_now(now)
    return {
        "members": len(members_due(org, member_months, moment)),
        "bookings": len(bookings_due(org, booking_months, moment)),
    }


def purge_organization(
    organization: Organization, *, now: datetime | None = None
) -> dict[str, int]:
    moment = utc_now(now)

    def purge(org: Organization) -> dict[str, int]:
        counts = dict.fromkeys(
            (
                "members",
                "bookings",
                "orders",
                "login_codes",
                "login_attempts",
                "login_requests",
                "audit_events",
                "member_imports",
            ),
            0,
        )
        SecurityNotice.objects.of(org).filter(
            delivered_at__lte=moment - timedelta(days=30)
        ).delete()
        # Imports may unlink a former address. No unlinked entitlement has
        # a current member; remove those remnants even without a later erasure.
        Membership.objects.of(org).filter(membermembership__isnull=True).delete()
        codes = LoginCode.objects.of(org).filter(
            Q(used_at__lte=moment) | Q(expires_at__lte=moment)
        )
        counts["login_codes"] = codes.count()
        codes.delete()
        attempts = LoginAttempt.objects.of(org).filter(
            at__lte=moment - timedelta(days=30)
        )
        counts["login_attempts"] = attempts.count()
        attempts.delete()
        counts["login_requests"] = expire_requests(org, moment)
        for member in members_due(org, org.member_retention_months, moment):
            erase_identity(member)
            counts["members"] += 1

        due = bookings_due(org, org.booking_retention_months, moment)
        ids = [row.pk for row in due]
        taken_ids = [row.taken_time_id for row in due]
        # A newer booking can keep this order alive. Keep only money totals for
        # removed rows so later refunds can still reconcile the original payment.
        for payment in Payment.objects.of(org).filter(
            order_id__in={row.order_id for row in due}, create_key__isnull=False
        ):
            removed = [row for row in due if row.order_id == payment.order_id]
            booking_amount = sum(
                line.total for row in removed for line in row.lines.all()
            )
            refund_amount = sum(
                Refund.objects.of(org)
                .filter(payment=payment, booking_id__in=[row.pk for row in removed])
                .values_list("amount", flat=True)
            )
            Payment.objects.of(org).filter(pk=payment.pk).update(
                retired_booking_amount=F("retired_booking_amount") + booking_amount,
                retired_refund_amount=F("retired_refund_amount") + refund_amount,
            )
        Refund.objects.of(org).filter(booking_id__in=ids).delete()
        Booking.objects.of(org).filter(pk__in=ids).delete()
        TakenTime.objects.of(org).filter(pk__in=taken_ids).delete()
        counts["bookings"] = len(ids)
        # Mixed-age orders/payment records remain until their final booking expires.
        # An order created with no bookings uses its own creation date.
        emptied = {row.order_id for row in due}
        order_ids = [
            row.pk
            for row in Order.objects.of(org)
            .filter(bookings__isnull=True)
            .exclude(pk__in=_unsettled_orders(org))
            if row.pk in emptied
            or after_months(row.created_at, org.booking_retention_months) <= moment
        ]
        Refund.objects.of(org).filter(payment__order_id__in=order_ids).delete()
        PaymentTrouble.objects.of(org).filter(payment__order_id__in=order_ids).delete()
        Payment.objects.of(org).filter(order_id__in=order_ids).delete()
        Order.objects.of(org).filter(pk__in=order_ids).delete()
        counts["orders"] = len(order_ids)

        audit_moment = min(moment, utc_now(None))
        audit_ids = [
            row.pk
            for row in AuditEvent.objects.of(org)
            if after_months(row.at, 36) <= audit_moment
        ]
        # Database policy also checks the real clock; an injected future clock
        # cannot delete young audit history. Count only rows actually deleted.
        SecurityNotice.objects.of(org).filter(event_id__in=audit_ids).delete()
        counts["audit_events"] = (
            AuditEvent.objects.of(org).filter(pk__in=audit_ids).delete()[0]
        )
        import_ids = [
            row.pk
            for row in MemberImport.objects.of(org)
            if after_months(row.started_at, 36) <= moment
        ]
        # Access history retains its event/cause when its older run is removed.
        AccessEvent.objects.of(org).filter(member_import_id__in=import_ids).update(
            member_import=None
        )
        MemberImport.objects.of(org).filter(pk__in=import_ids).delete()
        counts["member_imports"] = len(import_ids)
        return counts

    return organization_transaction(organization.pk, purge)


def purge_expired() -> None:
    """System entry point: discover organizations, then scope each transaction."""
    # Framework storage is global and has its own expiry, not tenant records.
    Session.objects.filter(expire_date__lte=utc_now(None)).delete()
    for org in Organization.objects.order_by("pk").iterator():
        purge_organization(org)
