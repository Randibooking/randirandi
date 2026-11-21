# SPDX-License-Identifier: AGPL-3.0-only
"""Scoped member data services and transaction boundary ."""

import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from django.contrib.postgres.aggregates import ArrayAgg
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import validate_email
from django.db import OperationalError, connection, transaction
from django.db.models import F, Q, QuerySet, Value
from django.utils import timezone

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.login_identity import identifier_digest
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    AuditEvent,
    Authenticator,
    Block,
    Booking,
    BookingCategory,
    BookingConditions,
    BookingLine,
    BookingState,
    CapacityAllocation,
    Event,
    EventDay,
    EventNotice,
    LoginAttempt,
    LoginCode,
    LoginRequest,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    MemberStatus,
    Order,
    Organization,
    Origin,
    Payment,
    Refund,
    Role,
    SecurityNotice,
    TakenTime,
)


def utc_now(value: datetime | None) -> datetime:
    value = timezone.now() if value is None else value
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError("An aware datetime is required.")
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("Datetime exceeds the supported range.") from None


def organization_transaction[T](
    organization_id: int, action: Callable[[Organization], T]
) -> T:
    """Own a coherent write transaction; serialize with imports and checkout."""
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Member data operations require their own transaction.")
    for attempt in range(5):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"
                    )
                org = Organization.objects.select_for_update(no_key=True).get(
                    pk=organization_id
                )
                result = action(org)
            return result
        except OperationalError as error:
            if (
                getattr(error.__cause__, "sqlstate", None) not in {"40001", "40P01"}
                or attempt == 4
            ):
                raise
            # Concurrent serializable transactions otherwise retry in lockstep.
            # Sleep only after rollback, without holding a database lock.
            time.sleep(0.01 * 2**attempt + secrets.randbelow(10) / 1000)
    raise AssertionError("Unreachable")


def _subject(org: Organization, actor_id: int, member_id: int) -> tuple[Member, Member]:
    actor = Member.objects.of(org).select_related("role").filter(pk=actor_id).first()
    if actor is None or not actor.is_staff:
        raise PermissionDenied(
            "An active administrator of this organization is required."
        )
    return actor, Member.objects.of(org).get(pk=member_id)


def _id(member_id: int) -> None:
    if type(member_id) is not int or member_id < 1:
        raise ValueError("A positive member identifier is required.")


def pending_requests(member: Member) -> QuerySet[LoginRequest]:
    identifiers = [
        identifier_digest(member.organization_id, value)
        for value in (member.email, member.phone)
        if value
    ]
    return LoginRequest.objects.of(member.organization_id).filter(
        identifier_digest__in=identifiers
    )


def _named_in(report: Any, member_id: int) -> bool:
    """Whether a run's report holds the member's id: among the members it
    disabled or proposed to disable, or a finding's candidates."""
    if not isinstance(report, dict):
        return False
    for key in ("disabled", "proposed_revocations"):
        if member_id in (report.get(key) or []):
            return True
    return any(
        isinstance(item, dict) and member_id in (item.get("members") or [])
        for item in report.get("findings") or []
    )


def _without(report: dict[str, Any], member_id: int) -> dict[str, Any]:
    """The report with the member's id taken out wherever it is named."""
    clean = dict(report)
    for key in ("disabled", "proposed_revocations"):
        if key in clean:
            clean[key] = [pk for pk in clean[key] if pk != member_id]
    if "findings" in clean:
        clean["findings"] = [
            {
                **item,
                "members": [pk for pk in item.get("members", []) if pk != member_id],
            }
            if isinstance(item, dict) and "members" in item
            else item
            for item in clean["findings"]
        ]
    return clean


def erase_identity(member: Member) -> None:
    """Internal primitive; caller holds the organization transaction and authority."""
    org = member.organization_id
    MemberMembership.objects.of(org).filter(member=member).delete()
    # Include old addresses whose links were removed by earlier imports.
    Membership.objects.of(org).filter(membermembership__isnull=True).delete()
    Authenticator.objects.of(org).filter(member=member).delete()
    SecurityNotice.objects.of(org).filter(recipient=member).delete()
    # An event keeps no contact person once the member is erased; admin lists
    # it as needing one . The events the member made keep their
    # link, as a block's do.
    Event.objects.of(org).filter(contact=member).update(contact=None)
    EventNotice.objects.of(org).filter(recipient=member).delete()
    LoginCode.objects.of(org).filter(member=member).delete()
    LoginAttempt.objects.of(org).filter(member=member).delete()
    pending_requests(member).delete()
    # A copy kept for an uncertain payment create goes with the phone.
    Payment.objects.of(org).filter(order__member=member).update(create_phone="")
    AccessEvent.objects.of(org).filter(member=member).delete()
    for run in MemberImport.objects.of(org):
        if _named_in(run.report, member.pk):
            run.report = _without(run.report, member.pk)
            run.save(update_fields=["report"])
    member.full_name = member.first_name = member.last_name = ""
    member.email = member.phone = ""
    member.status = MemberStatus.DISABLED
    member.listed = False
    member.last_booking_category = None
    member.last_login_at = None
    member.save(
        update_fields=[
            "full_name",
            "first_name",
            "last_name",
            "email",
            "phone",
            "status",
            "listed",
            "last_booking_category",
            "last_login_at",
        ]
    )


def operator_erase_member(*, organization: Organization, member_id: int) -> bool:
    """The operator erases a member on the member's written request .

    Refuses an erased member, a booking still to come and an order held for
    payment, naming the reason and no personal data. An active administrator
    is erased like any member; the result is True when that left the
    organization without an active administrator. Erasure is audited as the
    operator's, with the member as the actor.
    """
    _id(member_id)

    def apply(org: Organization) -> bool:
        member = Member.objects.of(org).select_related("role").get(pk=member_id)
        if not member.email:
            raise PermissionDenied("The member is already erased.")
        # A held booking is an order waiting for payment, however old its hold.
        if (
            Booking.objects.of(org)
            .filter(member=member)
            .filter(
                Q(taken_time__state=BookingState.HELD)
                | Q(
                    taken_time__state=BookingState.CONFIRMED,
                    taken_time__ends_at__gt=timezone.now(),
                )
            )
            .exists()
        ):
            raise PermissionDenied(
                "The member has a booking still to come or an order held for "
                "payment; an administrator cancels those first."
            )
        last_administrator = member.is_staff and not (
            Member.objects.of(org)
            .filter(status=MemberStatus.ACTIVE, role__admin_access=True)
            .exclude(pk=member.pk)
            .exists()
        )
        record_event(
            actor=member, action=AuditAction.OPERATOR_ERASE_MEMBER, target=member
        )
        erase_identity(member)
        return last_administrator

    return organization_transaction(organization.pk, apply)


def notify_administrators(org: Organization, event: AuditEvent) -> None:
    """One security notice for the event to each active administrator, in the
    caller's transaction. A notice is unique per event and recipient, so an
    event is announced once however many members it concerned."""
    for recipient in Member.objects.of(org).filter(
        status=MemberStatus.ACTIVE, role__admin_access=True
    ):
        SecurityNotice.objects.create(
            organization=org, recipient=recipient, event=event
        )


def gained_admin_access(
    org: Organization, administrator: Member, member: Member
) -> None:
    """Every gain of admin access, in the caller's transaction after the member
    is saved : one "gave admin access" event on the member, its
    actor the administrator who did it (the member when none did: the operator
    convention of the first administrator), and an email to every active
    administrator, the member too when active, tied to that event. The email
    names no one; the event is the reference."""
    notify_administrators(
        org,
        record_event(
            actor=administrator, action=AuditAction.GAVE_ADMIN_ACCESS, target=member
        ),
    )


def _change_role(
    org: Organization, administrator: Member, member: Member, role_id: int
) -> tuple[bool, bool]:
    """Give `member` the role; (changed, gained admin access). Refusals are
    ValidationErrors coded "unknown-role", "own-role" (an administrator
    changing their own role) and "last-administrator"."""
    role = Role.objects.of(org).filter(pk=role_id).first()
    if role is None:
        raise ValidationError(
            "The role is not one of the organization's.", "unknown-role"
        )
    if role.pk == member.role_id:
        return False, False
    if member.pk == administrator.pk:
        raise ValidationError(
            "An administrator cannot change their own role.", "own-role"
        )
    # The role's own admin access decides, whatever the member's status.
    had_access = member.role.admin_access
    if member.is_staff and not role.admin_access:
        others = (
            Member.objects.of(org)
            .filter(status=MemberStatus.ACTIVE, role__admin_access=True)
            .exclude(pk=member.pk)
        )
        if not others.exists():
            raise ValidationError(
                "The organization needs an active administrator.", "last-administrator"
            )
    member.role = role
    return True, role.admin_access and not had_access


PHONE_FORMAT = re.compile(r"^\+[1-9][0-9]{5,14}$")


def _clean_member(org: Organization, member: Member, fields: list[str]) -> None:
    """Validate the member as corrected; ValidationError coded "invalid-name",
    "invalid-phone" or "phone-taken"."""
    if "phone" in fields and member.phone.replace(" ", ""):
        phone = member.phone.replace(" ", "")
        if not PHONE_FORMAT.fullmatch(phone):
            raise ValidationError("The phone number is not valid.", "invalid-phone")
        if Member.objects.of(org).filter(phone=phone).exclude(pk=member.pk).exists():
            raise ValidationError(
                "The phone number is another member's.", "phone-taken"
            )
    try:
        member.full_clean()
    except ValidationError:
        raise ValidationError(
            "The name or phone is not valid.", "invalid-name"
        ) from None


def correct_member(
    *,
    actor: Member,
    member_id: int,
    changes: Mapping[str, str],
    code: str,
    network: str,
    role_id: int | None = None,
    now: datetime | None = None,
) -> Member:
    """Correct names, phone and role. Email changes are `change_member_email`.

    Acts only on the administrator's current authenticator code .
    A full name given without first and last name also sets them: the first
    word is the first name, the rest the last name. `role_id` is a role of the
    organization ; an administrator cannot change their own
    role, and the last active administrator cannot lose admin access.
    """
    # second_factor imports this module, so it is imported where it is used.
    from randirandi.core.second_factor import with_fresh_code

    _id(member_id)
    if role_id is not None and type(role_id) is not int:
        raise ValueError("A role identifier is required.")
    if (not changes and role_id is None) or set(changes) - {
        "full_name",
        "first_name",
        "last_name",
        "phone",
    }:
        raise ValidationError(
            "Only names, phone and role can be corrected here; "
            "an email change is made with its own call."
        )
    if any(not isinstance(value, str) for value in changes.values()):
        raise ValidationError("Member details must be text.")
    values = {field: value.strip() for field, value in changes.items()}
    if "full_name" in values and not {"first_name", "last_name"} & set(values):
        first, _, rest = values["full_name"].strip().partition(" ")
        values["first_name"], values["last_name"] = first, rest.strip()

    def apply(org: Organization) -> Member:
        _administrator, member = _subject(org, actor.pk, member_id)
        if not member.email:
            raise ValidationError(
                "An erased member cannot be restored by correction.", "erased"
            )
        changed_phone = "phone" in values and (
            values["phone"].replace(" ", "") != member.phone
        )
        for field, value in values.items():
            setattr(member, field, value)
        fields = list(values)
        gained = False
        if role_id is not None:
            changed, gained = _change_role(org, _administrator, member, role_id)
            if changed:
                fields.append("role")
        if values:
            _clean_member(org, member, list(values))
        member.save(update_fields=fields)
        record_event(
            actor=_administrator, action=AuditAction.CORRECT_MEMBER, target=member
        )
        if gained:
            gained_admin_access(org, _administrator, member)
        # Old codes must not remain usable with a changed login phone.
        if changed_phone:
            LoginCode.objects.of(org).filter(member=member).delete()
        return member

    return with_fresh_code(actor, code, apply, network=network, now=now)


def add_member(
    *,
    actor: Member,
    full_name: str,
    email: str,
    phone: str,
    role_id: int,
    code: str,
    network: str,
    now: datetime | None = None,
) -> Member:
    """An administrator adds one member .

    The member is active, `Origin.ADMINISTRATOR` (a member import never
    disables or changes it) and has no membership: booking never reads one.
    The name is split as `correct_member` splits it; the phone is optional.
    The add, its "active" access event, the audit event and, for a role with
    admin access, the gain of admin access are one transaction, run only on
    the administrator's current authenticator code. Concurrent adds of one
    email meet at the organization lock, so one wins and the rest are refused.
    Refusals are ValidationErrors coded "invalid-name", "invalid-email",
    "email-taken", "invalid-phone", "phone-taken" and "unknown-role".
    """
    from randirandi.core.second_factor import with_fresh_code

    if (
        not all(isinstance(v, str) for v in (full_name, email, phone))
        or type(role_id) is not int
    ):
        raise ValueError("Text and a role identifier are required.")
    name = full_name.strip()
    if not name:
        raise ValidationError("The name is not valid.", "invalid-name")
    first, _, rest = name.partition(" ")
    address = email.strip().lower()
    number = phone.replace(" ", "")
    try:
        validate_email(address)
    except ValidationError:
        raise ValidationError("The email is not valid.", "invalid-email") from None
    if len(address) > 254:
        raise ValidationError("The email is not valid.", "invalid-email")
    if number and not PHONE_FORMAT.fullmatch(number):
        raise ValidationError("The phone number is not valid.", "invalid-phone")

    def apply(org: Organization) -> Member:
        administrator, _ = _subject(org, actor.pk, actor.pk)
        role = Role.objects.of(org).filter(pk=role_id).first()
        if role is None:
            raise ValidationError(
                "The role is not one of the organization's.", "unknown-role"
            )
        if Member.objects.of(org).filter(email=address).exists():
            raise ValidationError("The email is another member's.", "email-taken")
        if number and Member.objects.of(org).filter(phone=number).exists():
            raise ValidationError(
                "The phone number is another member's.", "phone-taken"
            )
        member = Member(
            organization=org,
            role=role,
            status=MemberStatus.ACTIVE,
            origin=Origin.ADMINISTRATOR,
            full_name=name,
            first_name=first,
            last_name=rest.strip(),
            email=address,
            phone=number,
        )
        try:
            member.full_clean()
        except ValidationError as refused:
            fields = set(getattr(refused, "error_dict", {}))
            reason = (
                "invalid-email"
                if "email" in fields
                else "invalid-phone"
                if "phone" in fields
                else "invalid-name"
            )
            raise ValidationError("The member is not valid.", reason) from None
        member.save(force_insert=True)
        AccessEvent.objects.create(
            organization=org,
            member=member,
            change=MemberStatus.ACTIVE,
            cause=AccessCause.ADMINISTRATOR,
            administrator=administrator,
        )
        record_event(actor=administrator, action=AuditAction.ADD_MEMBER, target=member)
        if role.admin_access:
            gained_admin_access(org, administrator, member)
        return member

    return with_fresh_code(actor, code, apply, network=network, now=now)


def change_member_email(
    *,
    actor: Member,
    member_id: int,
    email: str,
    code: str,
    network: str,
    now: datetime | None = None,
) -> Member:
    """Change a member's email .

    The change, the member's dropped login codes and waiting login requests,
    the audit event and a notice for the OLD address are one transaction; the
    notice goes out with the security notices (`security_notices`). Refusals
    are ValidationErrors coded "invalid-email", "erased", "blocked",
    "unchanged" and "email-taken". An erased member has no address to move;
    a blocked member is refused so that the change is made on purpose, after
    Unblock, not while login is stopped.
    """
    from randirandi.core.second_factor import with_fresh_code

    _id(member_id)
    if not isinstance(email, str):
        raise ValidationError("The email must be text.", "invalid-email")
    new = email.strip().lower()
    try:
        validate_email(new)
    except ValidationError:
        raise ValidationError("The email is not valid.", "invalid-email") from None
    if len(new) > 254:
        raise ValidationError("The email is not valid.", "invalid-email")

    def apply(org: Organization) -> Member:
        administrator, member = _subject(org, actor.pk, member_id)
        if not member.email:
            raise ValidationError("The member is erased.", "erased")
        if member.status == MemberStatus.BLOCKED:
            raise ValidationError("The member is blocked.", "blocked")
        old = member.email
        if new == old:
            raise ValidationError("That is the member's email.", "unchanged")
        if Member.objects.of(org).filter(email=new).exclude(pk=member.pk).exists():
            raise ValidationError("The email is another member's.", "email-taken")
        # Requests are found by the identifiers the member has now.
        pending_requests(member).delete()
        LoginCode.objects.of(org).filter(member=member).delete()
        member.email = new
        member.save(update_fields=["email"])
        event = record_event(
            actor=administrator, action=AuditAction.CHANGE_MEMBER_EMAIL, target=member
        )
        SecurityNotice.objects.create(
            organization=org, recipient=member, event=event, address=old
        )
        return member

    return with_fresh_code(actor, code, apply, network=network, now=now)


def export_member(
    *,
    actor: Member,
    member_id: int,
    code: str,
    network: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-ready snapshot; no credentials or other members' details.

    Acts only on the administrator's current authenticator code .
    """
    from randirandi.core.second_factor import with_fresh_code

    _id(member_id)

    def capture(org: Organization) -> dict[str, Any]:
        _administrator, member = _subject(org, actor.pk, member_id)
        record_event(
            actor=_administrator, action=AuditAction.EXPORT_MEMBER, target=member
        )
        bookings = Booking.objects.of(org).filter(member=member)
        orders = Order.objects.of(org).filter(member=member)
        payments = Payment.objects.of(org).filter(order__in=orders)
        audit_bookings = Booking.objects.of(org).filter(
            Q(member=member) | Q(cancelled_by=member)
        )
        audit_times = TakenTime.objects.of(org).filter(
            Q(booking__in=audit_bookings) | Q(block__made_by=member)
        )
        # The events the member made or is the contact for, and their days.
        events = Event.objects.of(org).filter(Q(made_by=member) | Q(contact=member))
        related_audit = Q(administrator=member) | Q(
            object_kind="member", object_id=member.pk
        )
        # Only personal subject records and direct actor activity, not shared
        # organization configuration reached via a booking or role.
        for kind, rows in (
            ("order", orders),
            ("booking", audit_bookings),
            ("payment", payments),
            (
                "refund",
                Refund.objects.of(org).filter(
                    Q(payment__in=payments) | Q(made_by=member)
                ),
            ),
            (
                "booking_line",
                BookingLine.objects.of(org).filter(booking__in=audit_bookings),
            ),
            ("taken_time", audit_times),
            (
                "capacity_allocation",
                CapacityAllocation.objects.of(org).filter(taken_time__in=audit_times),
            ),
            ("membership", Membership.objects.of(org).filter(members=member)),
            (
                "member_membership",
                MemberMembership.objects.of(org).filter(member=member),
            ),
            ("authenticator", Authenticator.objects.of(org).filter(member=member)),
            (
                "security_notice",
                SecurityNotice.objects.of(org).filter(recipient=member),
            ),
            ("login_code", LoginCode.objects.of(org).filter(member=member)),
            ("login_attempt", LoginAttempt.objects.of(org).filter(member=member)),
            ("login_request", pending_requests(member)),
            (
                "access_event",
                AccessEvent.objects.of(org).filter(
                    Q(member=member) | Q(administrator=member)
                ),
            ),
            ("member_import", MemberImport.objects.of(org).filter(started_by=member)),
            ("block", Block.objects.of(org).filter(made_by=member)),
            ("event", events),
            ("event_day", EventDay.objects.of(org).filter(event__in=events)),
        ):
            related_audit |= Q(
                object_kind=kind, object_id__in=rows.values_list("pk", flat=True)
            )
        columns = ("id", "administrator_id", "action", "object_kind", "object_id", "at")
        audit_records = list(
            AuditEvent.objects.of(org).filter(related_audit).values(*columns)
        )
        seen = {row["id"] for row in audit_records}
        frontier = seen
        # Inspection can inspect an audit row itself. Follow that finite graph
        # without adding unrelated events or returning other members' values.
        while frontier:
            more = list(
                AuditEvent.objects.of(org)
                .filter(object_kind="audit_event", object_id__in=frontier)
                .exclude(pk__in=seen)
                .values(*columns)
            )
            frontier = {row["id"] for row in more}
            seen.update(frontier)
            audit_records.extend(more)
        audit_records.sort(key=lambda row: (row["at"], row["id"]))
        category_name = None
        if member.last_booking_category_id is not None:
            category_name = (
                BookingCategory.objects.of(org)
                .values_list("name", flat=True)
                .get(pk=member.last_booking_category_id)
            )
        data = {
            "organization_id": org.pk,
            "member": {
                name: getattr(member, name)
                for name in (
                    "id",
                    "full_name",
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "status",
                    "listed",
                    "unlisted_at",
                    "last_login_at",
                    "origin",
                    "role_id",
                )
            }
            | {"last_booking_category_name": category_name},
            "memberships": list(
                Membership.objects.of(org)
                .filter(members=member)
                .order_by("pk")
                .values(
                    "id",
                    "identifier",
                    "address",
                    "address_unit",
                    "section",
                    "postal_code",
                    "city",
                    "country",
                )
            ),
            "access_events": list(
                AccessEvent.objects.of(org)
                .filter(member=member)
                .order_by("at", "pk")
                .values(
                    "id",
                    "change",
                    "at",
                    "cause",
                    "member_import_id",
                    "administrator_id",
                )
            ),
            "authenticator": list(
                Authenticator.objects.of(org)
                .filter(member=member)
                .values("id", "confirmed", "created_at")
            ),
            "security_notices": list(
                SecurityNotice.objects.of(org)
                .filter(recipient=member)
                .values("id", "event_id", "address", "created_at", "delivered_at")
            ),
            "login_requests": list(
                pending_requests(member)
                .order_by("at", "pk")
                .values("id", "at", "claimed_at")
            ),
            "login_attempts": list(
                LoginAttempt.objects.of(org)
                .filter(member=member)
                .order_by("at", "pk")
                .values("id", "at", "network", "kind", "outcome")
            ),
            "orders": list(
                orders.order_by("pk").values(
                    "id",
                    "public_id",
                    "number",
                    "reference",
                    "created_at",
                    "hold_expires_at",
                    "total",
                    "confirmation_due_at",
                    "confirmation_tried_at",
                    "confirmation_sent_at",
                    # The booking conditions the order accepted, and when
                    # ; empty on an order from before M11.
                    "conditions_id",
                    "conditions_accepted_at",
                )
            ),
            # The frozen texts those orders accepted : the organization's
            # words and values, no personal data.
            "accepted_conditions": list(
                BookingConditions.objects.of(org)
                .filter(pk__in=orders.values_list("conditions_id", flat=True))
                .order_by("pk")
                .values(
                    "id",
                    "standard_version",
                    "digest",
                    "organization_name",
                    "contact_email",
                    "free_cancellation_days",
                    "title",
                    "intro",
                    "sections",
                    "created_at",
                )
            ),
            "bookings": list(
                bookings.order_by("pk").values(
                    "id",
                    "public_id",
                    "order_id",
                    "booking_type_id",
                    "booking_category_id",
                    "booking_type_name",
                    "booking_category_name",
                    "cancelled_at",
                    "cancelled_by_id",
                    "reminded_at",
                    "reminder_tried_at",
                    "booking_configuration__number",
                    "taken_time__asset_id",
                    "taken_time__starts_at",
                    "taken_time__ends_at",
                    "taken_time__state",
                    "taken_time__mode",
                    "taken_time__quantity",
                    # The spots it holds, by number and in number order (the
                    # order of a pick is not kept); none when it has the
                    # whole asset.
                    spot_numbers=ArrayAgg(
                        "taken_time__allocations__spot__number",
                        filter=Q(taken_time__allocations__spot__isnull=False),
                        order_by=F("taken_time__allocations__spot__number"),
                        default=Value([]),
                    ),
                )
            ),
            "booking_lines": list(
                BookingLine.objects.of(org)
                .filter(booking__in=bookings)
                .order_by("pk")
                .values(
                    "id",
                    "booking_id",
                    "configuration_number",
                    "price_book_number",
                    "up_to",
                    "quantity",
                    "amount",
                    "billing_basis",
                    "capacity_quantity",
                    "capacity_unit",
                    # The group of spots it was priced for, by the number the
                    # member sees; none when it was priced for no group.
                    "spot_group__number",
                )
            ),
            "payments": list(
                Payment.objects.of(org)
                .filter(order__in=orders)
                .order_by("pk")
                .values(
                    "id",
                    "order_id",
                    "payment_provider",
                    "provider_reference",
                    "currency",
                    "amount",
                    "state",
                    "captured_amount",
                    "captured_at",
                    "refunded_amount",
                    # What the daily report showed for the capture .
                    "fee_amount",
                    "payout_id",
                    "retired_booking_amount",
                    "retired_refund_amount",
                    # What the member saw in the Vipps app, while its start
                    # runs. The phone's copy stays out : the
                    # member's own phone is in the record above.
                    "create_description",
                    "create_receipt",
                    "details_sent_at",
                )
            ),
            "refunds": list(
                Refund.objects.of(org)
                .filter(payment__order__in=orders)
                .order_by("pk")
                .values(
                    "id",
                    "payment_id",
                    "booking_id",
                    "amount",
                    "made_at",
                    "made_by_id",
                    "provider_reference",
                    "automatic",
                    "completed_at",
                    "payout_id",
                )
            ),
            # Actor activity is personal data too; include links, never the
            # other member's identity, transaction values or free-text notes.
            "administered_access_events": list(
                AccessEvent.objects.of(org)
                .filter(administrator=member)
                .order_by("pk")
                .values("id", "member_id", "at", "change")
            ),
            "started_imports": list(
                MemberImport.objects.of(org)
                .filter(started_by=member)
                .order_by("pk")
                .values(
                    "id", "parser", "file_name", "file_size", "started_at", "outcome"
                )
            ),
            # Runs whose report names the member (disabled, proposed to be
            # disabled, or a finding's candidate); the report's other ids and
            # the file's contents stay out.
            "named_in_imports": [
                {"id": run.pk, "started_at": run.started_at, "outcome": run.outcome}
                for run in MemberImport.objects.of(org).order_by("pk")
                if _named_in(run.report, member.pk)
            ],
            "cancelled_bookings": list(
                Booking.objects.of(org)
                .filter(cancelled_by=member)
                .order_by("pk")
                .values("id", "cancelled_at")
            ),
            "made_refunds": list(
                Refund.objects.of(org)
                .filter(made_by=member)
                .order_by("pk")
                .values("id", "payment_id", "booking_id", "made_at")
            ),
            "made_blocks": list(
                Block.objects.of(org)
                .filter(made_by=member)
                .order_by("pk")
                .values("id", "taken_time_id", "created_at")
            ),
            # The events the member made, and those they are the contact
            # for; ids and plain fields, never the free text of a title or a
            # description.
            "made_events": list(
                Event.objects.of(org)
                .filter(made_by=member)
                .order_by("pk")
                .values("id", "category", "asset_id", "created_at")
            ),
            "contact_for_events": list(
                Event.objects.of(org)
                .filter(contact=member)
                .order_by("pk")
                .values("id", "category", "asset_id", "show_email", "show_phone")
            ),
            "event_notices": list(
                EventNotice.objects.of(org)
                .filter(recipient=member)
                .order_by("pk")
                .values("id", "skipped_day_id", "claimed_at", "delivered_at")
            ),
            "audit_events": audit_records,
        }
        return cast(dict[str, Any], json.loads(json.dumps(data, cls=DjangoJSONEncoder)))

    return with_fresh_code(actor, code, capture, network=network, now=now)
