# SPDX-License-Identifier: AGPL-3.0-only
"""The Members and a member's page as the admin pages read them .

Every answer is read only. The filtering, the sorting, the search and the
paging of the list are one database query each; the units of the 25 members
of a page are looked up together, so the number of queries does not grow with
them. There is no way to read the whole list at once: it answers one page at
a time. The network address of a login attempt is never selected.
"""

from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.db.models import Exists, Expression, F, OuterRef, Q, Subquery
from django.db.models.functions import Lower

from randirandi.core import audit_log
from randirandi.core.booking_admin import PER_PAGE, _paging, _stamp
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    LoginAttempt,
    Member,
    MemberImport,
    MemberMembership,
    MemberStatus,
    Organization,
    Origin,
    Role,
)

STATUS_WORDS = {
    MemberStatus.ACTIVE.value: "Active",
    MemberStatus.DISABLED.value: "Disabled",
    MemberStatus.BLOCKED.value: "Blocked",
}
SORTS = ("name", "unit", "login")
# What a login attempt says, as the drawing words it.
KIND_WORDS = {
    "request": "Asked for a login code",
    "verify": "Entered the login code",
    "mfa": "Entered the authenticator code",
}
OUTCOME_WORDS = {
    "issued": "Code sent",
    "unknown": "Code sent",
    "limited": "Too many tries",
    "accepted": "Accepted",
    "refused": "Wrong or expired code",
}


@dataclass(frozen=True)
class MembersQuery:
    status: str | None = None
    role: int | None = None
    search: str = ""
    sort: str = "name"
    # None: the sort's own first direction (name and unit ascending, last
    # login newest first).
    ascending: bool | None = None
    page: int = 1


def _first_unit(org: Organization) -> Subquery:
    return Subquery(
        MemberMembership.objects.of(org)
        .filter(member=OuterRef("pk"))
        .order_by("membership__identifier", "membership__address", "pk")
        .values("membership__identifier")[:1]
    )


def _on_unit(org: Organization, search: str) -> Exists:
    return Exists(
        MemberMembership.objects.of(org)
        .filter(member=OuterRef("pk"))
        .filter(
            Q(membership__identifier__icontains=search)
            | Q(membership__address__icontains=search)
            | Q(membership__address_unit__icontains=search)
        )
    )


def members(org: Organization, query: MembersQuery) -> dict[str, object]:
    zone = ZoneInfo(org.time_zone)
    found = Member.objects.of(org).annotate(
        who=audit_log.member_shown(),
        first_unit=_first_unit(org),
    )
    if query.status is not None:
        found = found.filter(status=query.status)
    if query.role is not None:
        found = found.filter(role=query.role)
    if query.search:
        # A phone is kept without spaces, so it is searched without them.
        digits = query.search.replace(" ", "")
        found = found.filter(
            Q(who__icontains=query.search)
            | Q(email__icontains=query.search)
            | (Q(phone__icontains=digits) if digits else Q(pk__in=[]))
            | _on_unit(org, query.search)
        )
    count = found.count()
    descending = (
        query.sort == "login" if query.ascending is None else not query.ascending
    )
    first: Expression | F
    if query.sort == "name":
        first = Lower("who")
    elif query.sort == "unit":
        first = F("first_unit")
    else:
        first = F("last_login_at")
    # "Never" is the least login, so it comes first going up and last going
    # down; a member with no unit (an erased one) is always last.
    if descending:
        key = first.desc(nulls_last=True)
    elif query.sort == "login":
        key = first.asc(nulls_first=True)
    else:
        key = first.asc(nulls_last=True)
    start = (query.page - 1) * PER_PAGE
    page = list(
        found.order_by(key, "pk").values(
            "pk", "who", "email", "phone", "role__name", "status", "last_login_at"
        )[start : start + PER_PAGE]
    )
    units: dict[int, list[str]] = defaultdict(list)
    for member_id, identifier in (
        MemberMembership.objects.of(org)
        .filter(member_id__in=[row["pk"] for row in page])
        .order_by("membership__identifier", "membership__address", "pk")
        .values_list("member_id", "membership__identifier")
    ):
        units[member_id].append(identifier)
    return {
        **_paging(count, query.page),
        "members": [
            {
                "id": row["pk"],
                "name": row["who"],
                "email": row["email"],
                "phone": row["phone"],
                "units": units[row["pk"]],
                "role": row["role__name"],
                "status": row["status"],
                "status_label": STATUS_WORDS[row["status"]],
                "last_login": None
                if row["last_login_at"] is None
                else _stamp(row["last_login_at"], zone),
            }
            for row in page
        ],
        "statuses": [{"value": k, "label": v} for k, v in STATUS_WORDS.items()],
        "roles": [
            {"value": pk, "label": name}
            for pk, name in Role.objects.of(org)
            .order_by("name", "pk")
            .values_list("pk", "name")
        ],
        # The role the Add member sheet starts on (its value is a role's id).
        "standard_role": org.standard_role_id,
    }


def member(org: Organization, found: Member, viewer: Member) -> dict[str, object]:
    """One member's page: the details, the units, the status history and the
    login history (the bookings are the Bookings' list, asked with the member)."""
    zone = ZoneInfo(org.time_zone)
    names = dict(
        Member.objects.of(org)
        .annotate(shown=audit_log.member_shown())
        .filter(
            Q(pk=found.pk)
            | Q(
                pk__in=AccessEvent.objects.of(org)
                .filter(member=found, administrator__isnull=False)
                .values("administrator")
            )
        )
        .values_list("pk", "shown")
    )
    imports = {
        pk: number
        for number, pk in enumerate(
            MemberImport.objects.of(org).order_by("pk").values_list("pk", flat=True),
            start=1,
        )
    }
    events = list(AccessEvent.objects.of(org).filter(member=found).order_by("at", "pk"))
    history = []
    for index, event in enumerate(events):
        if event.cause == AccessCause.MEMBER_IMPORT:
            number = imports.get(event.member_import_id or 0)
            cause = "Member import" if number is None else f"Member import {number}"
            if event.change == MemberStatus.DISABLED:
                cause += ": not in the file"
        else:
            name = names.get(event.administrator_id or 0, "an administrator")
            if event.change == MemberStatus.BLOCKED:
                cause = name
            elif index == 0 and event.change == MemberStatus.ACTIVE:
                cause = f"Added by {name}"
            else:
                cause = f"Unblocked by {name}"
        history.append(
            {
                "at": _stamp(event.at, zone),
                "status": event.change,
                "status_label": STATUS_WORDS[event.change],
                "cause": cause,
            }
        )
    first = events[0] if events else None
    if first is not None and first.cause == AccessCause.MEMBER_IMPORT:
        number = imports.get(first.member_import_id or 0)
        origin = {"kind": "import", "import": number}
    elif (
        first is not None
        and first.change == MemberStatus.ACTIVE
        and first.administrator_id is not None
        and found.origin == Origin.ADMINISTRATOR
    ):
        origin = {"kind": "administrator", "import": None}
    elif found.origin == Origin.ADMINISTRATOR:
        origin = {"kind": "operator", "import": None}
    else:
        origin = {"kind": None, "import": None}
    origin["at"] = None if first is None else _stamp(first.at, zone)
    return {
        "id": found.pk,
        "name": names[found.pk],
        "first_name": found.first_name,
        "last_name": found.last_name,
        "email": found.email,
        "phone": found.phone,
        "role": found.role.name,
        "role_id": found.role_id,
        "roles": [
            {"value": pk, "label": name}
            for pk, name in Role.objects.of(org)
            .order_by("name", "pk")
            .values_list("pk", "name")
        ],
        "status": found.status,
        "status_label": STATUS_WORDS[found.status],
        "own": found.pk == viewer.pk,
        "administrator": found.is_staff,
        "origin": origin,
        "units": [
            {
                "identifier": unit.identifier,
                "address": unit.address,
                "flat": unit.address_unit,
                "section": unit.section,
                "postal_code": unit.postal_code,
                "city": unit.city,
            }
            for unit in (
                row.membership
                for row in MemberMembership.objects.of(org)
                .filter(member=found)
                .select_related("membership")
                .order_by("membership__identifier", "membership__address", "pk")
            )
        ],
        "history": history,
        "logins": [
            {
                "at": _stamp(at, zone),
                "kind": kind,
                "kind_label": KIND_WORDS[kind],
                "outcome": outcome,
                "outcome_label": OUTCOME_WORDS[outcome],
            }
            for at, kind, outcome in LoginAttempt.objects.of(org)
            .filter(member=found)
            .order_by("-at", "-pk")
            .values_list("at", "kind", "outcome")
        ],
    }
