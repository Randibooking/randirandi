# SPDX-License-Identifier: AGPL-3.0-only
"""Whole-list reconciliation, with no product- or customer-specific fields.

Only this function applies imports. A parser (randirandi/member_parsers.py)
supplies a complete MemberTable; the parser's column_map maps canonical field
names to the file's column headings and its layout says how the table is laid
out (core.member_layout).
The organization lock serializes imports and administrator bootstrap. Future
member-edit services must take the same lock.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TypedDict

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import DataError, IntegrityError, transaction
from django.utils import timezone

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import gained_admin_access
from randirandi.core.member_layout import (
    LIMITS,
    Problem,
    Row,
    compile_plan,
    read_rows,
)
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    Booking,
    BookingState,
    ImportOutcome,
    LoginCode,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    MemberStatus,
    Organization,
    Origin,
    Role,
)
from randirandi.member_file import MemberTable
from randirandi.member_parsers import Parser

NAMES = ("full_name", "first_name", "last_name")
# Membership values set and updated by the import; not part of its identity.
DETAILS = ("address_unit", "section", "postal_code", "city", "country")


class Finding(TypedDict):
    code: str
    rows: list[int]
    members: list[int]


@dataclass(repr=False)
class Person:
    values: dict[str, str]
    rows: list[int] = field(default_factory=list)
    memberships: set[tuple[str, str]] = field(default_factory=set)
    # The other membership values the list gives each membership.
    details: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    problems: set[str] = field(default_factory=set)
    member: Member | None = None
    candidates: set[int] = field(default_factory=set)


def _valid(values: dict[str, str]) -> bool:
    if not values["membership"] or not values["email"] or not values["full_name"]:
        return False
    if any(
        len(value) > LIMITS[name] or any(ord(c) < 32 or ord(c) == 127 for c in value)
        for name, value in values.items()
    ):
        return False
    if values["country"] and not re.fullmatch(r"[A-Z]{2}", values["country"]):
        return False
    if values["phone"] and not re.fullmatch(r"\+[1-9][0-9]{5,14}", values["phone"]):
        return False
    try:
        validate_email(values["email"])
    except ValidationError:
        return False
    return True


def _people(rows: list[Row]) -> tuple[list[Person], list[tuple[int, list[Person]]]]:
    """The people of the list, and the rows whose stated count differs."""
    grouped: dict[str, Person] = {}
    phones: dict[str, set[str]] = defaultdict(set)
    shared: dict[tuple[str, str], dict[tuple[str, ...], list[Person]]] = defaultdict(
        lambda: defaultdict(list)
    )
    differing: list[tuple[int, list[Person]]] = []
    for row in rows:
        key = (row.membership["membership"], row.membership["address"])
        detail = tuple(row.membership[name] for name in DETAILS)
        here: list[Person] = []
        for values in row.people:
            person = grouped.setdefault(values["email"], Person(values))
            if row.number not in person.rows:
                person.rows.append(row.number)
            here.append(person)
            if values["phone"]:
                phones[values["phone"]].add(values["email"])
            person.memberships.add(key)
            person.details[key] = detail
            shared[key][detail].append(person)
            if not _valid({**values, **row.membership}):
                person.problems.add("invalid_member")
            if any(person.values[name] != values[name] for name in (*NAMES, "phone")):
                person.problems.add("inconsistent_person")
        if row.stated:
            # The stated count is compared, never a reason to skip.
            readable = row.stated.isascii() and row.stated.isdigit()
            if not readable or int(row.stated) != len(row.people):
                differing.append((row.number, here))
    for emails in phones.values():
        if len(emails) > 1:
            for email in emails:
                grouped[email].problems.add("phone_in_multiple_rows")
    for details in shared.values():
        if len(details) > 1:
            # Two rows give one membership two different values (builder's pick).
            for people in details.values():
                for person in people:
                    person.problems.add("inconsistent_membership")
    return list(grouped.values()), differing


def _match(
    people: list[Person],
    members: list[Member],
    memberships: dict[int, set[tuple[str, str]]],
) -> None:
    emails = {member.email: member for member in members if member.email}
    phones = {member.phone: member for member in members if member.phone}
    matched: dict[int, list[Person]] = defaultdict(list)
    for person in people:
        values = person.values
        by_email = emails.get(values["email"])
        by_phone = phones.get(values["phone"])
        if by_email and by_phone and by_email.pk != by_phone.pk:
            person.problems.add("identity_conflict")
            person.candidates.update((by_email.pk, by_phone.pk))
        person.member = by_email or by_phone
        if person.member is None:
            candidates = [
                member
                for member in members
                if member.email
                and member.first_name.strip().casefold()
                == values["first_name"].casefold()
                and member.last_name.strip().casefold()
                == values["last_name"].casefold()
                and (member.full_name or f"{member.first_name} {member.last_name}")
                .strip()
                .casefold()
                == values["full_name"].casefold()
                and person.memberships & memberships[member.pk]
            ]
            person.candidates.update(member.pk for member in candidates)
            if len(candidates) == 1:
                person.member = candidates[0]
            elif candidates:
                person.problems.add("ambiguous_member")
        if person.member:
            member = person.member
            person.candidates.add(member.pk)
            matched[member.pk].append(person)
            if member.email != values["email"]:
                person.problems.add("email_change_requires_administrator")
    for people_for_member in matched.values():
        if len(people_for_member) > 1:
            for person in people_for_member:
                person.problems.add("member_in_multiple_groups")


def _status(member: Member, change: MemberStatus, run: MemberImport) -> None:
    member.status = change
    member.save(update_fields=["status"])
    AccessEvent.objects.create(
        organization_id=run.organization_id,
        member=member,
        change=change,
        cause=AccessCause.MEMBER_IMPORT,
        member_import=run,
    )
    if change == MemberStatus.DISABLED:
        # Codes of a disabled member cannot work again once the member is
        # active again. The session hash covers the status and the latest event.
        LoginCode.objects.of(run.organization_id).filter(
            member=member,
            used_at__isnull=True,
        ).update(used_at=timezone.now())


def _memberships(person: Person, member: Member, *, remove: bool) -> bool:
    current = MemberMembership.objects.of(member.organization_id).filter(member=member)
    before = set(current.values_list("membership_id", flat=True))
    wanted = set()
    updated = False
    for identifier, address in sorted(person.memberships):
        detail = dict(zip(DETAILS, person.details[(identifier, address)], strict=True))
        membership, made = Membership.objects.of(member.organization_id).get_or_create(
            identifier=identifier,
            address=address,
            defaults={"organization_id": member.organization_id, **detail},
        )
        if not made and any(getattr(membership, n) != v for n, v in detail.items()):
            for name, value in detail.items():
                setattr(membership, name, value)
            membership.save(update_fields=list(detail))
            updated = True
        wanted.add(membership.pk)
        if membership.pk not in before:
            MemberMembership.objects.create(
                organization_id=member.organization_id,
                member=member,
                membership=membership,
            )
    if remove:
        current.exclude(membership_id__in=wanted).delete()
    return ((wanted != before) if remove else bool(wanted - before)) or updated


def _apply(
    run: MemberImport,
    table: MemberTable,
    parser: Parser,
    organization: Organization,
    started_by: Member | None = None,
) -> None:
    plan = compile_plan(table, parser.column_map, parser.layout)
    rows = [] if isinstance(plan, Problem) else read_rows(plan)
    problem = plan if isinstance(plan, Problem) else Problem("")
    if not problem.code and not any(row.people for row in rows):
        problem = Problem("empty_list")
    if problem.code:
        # A list with nobody on it is refused (stopped); a file the parser
        # cannot read is failed. Nothing was changed either way.
        run.outcome = (
            ImportOutcome.STOPPED
            if problem.code == "empty_list"
            else ImportOutcome.FAILED
        )
        run.report = {
            "reason": problem.code,
            "findings": [],
            "removals_deferred": True,
            "future_bookings": [],
        }
        if problem.heading:
            run.report["column"] = problem.heading
        return
    standard = (
        Role.objects.of(organization)
        .select_for_update()
        .filter(pk=organization.standard_role_id)
        .first()
        if organization.standard_role_id is not None
        else None
    )
    if standard is None or standard.admin_access:
        run.outcome = ImportOutcome.STOPPED
        run.report = {
            "reason": "standard_role_must_be_non_admin",
            "findings": [],
            "removals_deferred": True,
            "future_bookings": [],
        }
        return
    members = list(Member.objects.of(organization).select_for_update().order_by("pk"))
    memberships: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for member_id, identifier, address in MemberMembership.objects.of(
        organization
    ).values_list("member_id", "membership__identifier", "membership__address"):
        memberships[member_id].add((identifier, address))
    people, differing = _people(rows)
    _match(people, members, memberships)
    defer = any(person.problems for person in people)
    findings: list[Finding] = []
    seen: set[int] = set()
    skipped_rows: set[int] = set()
    for person in people:
        seen.update(person.candidates)
        codes = person.problems
        if person.member and person.member.origin == Origin.ADMINISTRATOR:
            codes = codes | {"administrator_managed"}
        for code in sorted(codes):
            findings.append(
                {
                    "code": code,
                    "rows": person.rows,
                    "members": sorted(person.candidates),
                }
            )
        if codes:
            skipped_rows.update(person.rows)
    run.skipped += len(skipped_rows)
    for number, here in differing:
        findings.append(
            {
                "code": "people_count_differs",
                "rows": [number],
                "members": sorted({pk for person in here for pk in person.candidates}),
            }
        )
    managed = [member for member in members if member.origin == Origin.MEMBER_IMPORT]
    # A blocked member is never disabled or made active here; the 20% stop
    # counts the active ones only.
    active = [member for member in managed if member.status == MemberStatus.ACTIVE]
    revoke = [] if defer else [member for member in active if member.pk not in seen]
    run.report = {
        "reason": "",
        "findings": findings,
        "removals_deferred": defer,
        "future_bookings": [],
    }
    if len(revoke) * 5 > len(active):
        run.outcome = ImportOutcome.STOPPED
        run.report["reason"] = "revocation_limit"
        run.report["proposed_revocations"] = [member.pk for member in revoke]
        run.report["active"] = len(active)
        return
    gained: list[Member] = []
    for person in people:
        member = person.member
        if person.problems or (member and member.origin == Origin.ADMINISTRATOR):
            continue
        new = member is None
        if member is None:
            member = Member.objects.create(
                organization=organization,
                role=standard,
                status=MemberStatus.ACTIVE,
                origin=Origin.MEMBER_IMPORT,
                full_name=person.values["full_name"],
                first_name=person.values["first_name"],
                last_name=person.values["last_name"],
                email=person.values["email"],
                phone=person.values["phone"],
            )
            run.created += 1
        changed = False
        if not new:
            changed_fields = [
                name
                for name in (*NAMES, "phone")
                if getattr(member, name) != person.values[name]
            ]
            for name in changed_fields:
                setattr(member, name, person.values[name])
            if changed_fields:
                member.save(update_fields=changed_fields)
                changed = True
        changed = _memberships(person, member, remove=not defer) or changed
        if changed and not new:
            run.changed += 1
        if new or member.status == MemberStatus.DISABLED:
            _status(member, MemberStatus.ACTIVE, run)
            run.granted += 1
            # A listed member whose role has admin access gains it by becoming
            # active .
            if not new and member.role.admin_access:
                gained.append(member)
    for member in revoke:
        _status(member, MemberStatus.DISABLED, run)
        run.revoked += 1
    for member in gained:
        # The administrator who started the run; with none, the member (the
        # operator convention of the first administrator).
        gained_admin_access(organization, started_by or member, member)
    # What the list says is kept for every managed member, a blocked one too.
    # A list with problems cannot say who is missing from it.
    on_list = {member.pk for member in managed if member.pk in seen}
    Member.objects.of(organization).filter(pk__in=on_list).update(
        listed=True, unlisted_at=None
    )
    if not defer:
        Member.objects.of(organization).filter(
            pk__in={member.pk for member in managed} - on_list, listed=True
        ).update(listed=False, unlisted_at=timezone.now())
    run.report["disabled"] = [member.pk for member in revoke]
    run.report["future_bookings"] = list(
        Booking.objects.of(organization)
        .filter(
            member_id__in=[member.pk for member in revoke],
            taken_time__state__in=[BookingState.HELD, BookingState.CONFIRMED],
            taken_time__ends_at__gt=timezone.now(),
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    run.outcome = ImportOutcome.APPLIED


def import_members(
    organization: Organization,
    parser: Parser,
    table: MemberTable,
    *,
    started_by: Member | None = None,
    file_name: str = "",
    file_size: int = 0,
) -> MemberImport:
    """Apply a complete list, or store why it stopped. Reports contain no PII.

    The file is never kept: the run keeps its name and size, and the parser's
    id. A failed write rolls back all member changes and leaves a failed run. A
    database outage can prevent even recording the failure and is propagated.
    The caller must show/dispatch report findings. Email delivery and
    administrator approval of email changes use their dedicated services.
    This API cannot approve or apply an email change.
    """
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if started_by is not None:
            actor = Member.objects.of(organization).filter(pk=started_by.pk).first()
            if actor is None or not actor.is_staff:
                raise PermissionDenied(
                    "An active administrator of this organization is required."
                )
            started_by = actor
        run = MemberImport.objects.create(
            organization=organization,
            parser=parser.id,
            file_name=file_name[:255],
            file_size=file_size,
            started_by=started_by,
            outcome=ImportOutcome.FAILED,
        )
        if started_by is not None:
            record_event(
                actor=started_by, action=AuditAction.IMPORT_MEMBERS, target=run
            )
        try:
            with transaction.atomic():
                _apply(run, table, parser, organization, started_by)
        except IntegrityError, DataError:
            run.created = run.changed = run.granted = run.revoked = run.skipped = 0
            run.outcome = ImportOutcome.FAILED
            run.report = {
                "reason": "write_conflict",
                "findings": [],
                "removals_deferred": True,
                "future_bookings": [],
            }
        run.save(
            update_fields=[
                "outcome",
                "created",
                "changed",
                "granted",
                "revoked",
                "skipped",
                "report",
            ]
        )
        return run
