# SPDX-License-Identifier: AGPL-3.0-only
"""The audit log as the Audit log page reads it .

An event holds ids only. The names are looked up here, from the current
records, for the 25 events of the page asked for and for a search; nothing
is stored. The filtering, the sorting, the search and the paging are one
database query each, never the whole log in memory.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import models
from django.db.models import CharField, Expression, F, Q, Value
from django.db.models.functions import Cast, Coalesce, Concat, LPad, NullIf, Trim

from randirandi.core.audit_types import OBJECT_KINDS, AuditAction
from randirandi.core.models import (
    Asset,
    AssetInventory,
    AuditEvent,
    Authenticator,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    Event,
    EventDay,
    Member,
    MemberImport,
    Order,
    Organization,
    Payment,
    PriceBook,
    PriceBookEntry,
    Refund,
    Role,
    Space,
    Spot,
    SpotGroup,
)

PER_PAGE = 25
Expr = Expression | F
# The actions the page words differently; the others are the stored text with a
# capital first letter.
ACTION_WORDS = {
    AuditAction.INSPECT: "Looked up",
    AuditAction.ADMIN_LOGIN: "Signed in",
}


def action_label(action: str) -> str:
    return ACTION_WORDS.get(AuditAction(action)) or action[0].upper() + action[1:]


def _text(*parts: str | Expr) -> Expression:
    return Concat(
        *(Value(part) if isinstance(part, str) else part for part in parts),
        output_field=CharField(),
    )


def _number(path: str, width: int = 0) -> Expr:
    digits = Cast(F(path), CharField())
    return LPad(digits, width, Value("0")) if width else digits


def _member_name(prefix: str = "") -> Expr:
    """The member's name; empty when the member has none (erased)."""
    full, first, last = (
        F(prefix + part) for part in ("full_name", "first_name", "last_name")
    )
    return Coalesce(
        NullIf(full, Value("")),
        NullIf(Trim(_text(first, " ", last)), Value("")),
        Value(""),
    )


def member_shown(prefix: str = "") -> Expr:
    """The member as the pages name them: the name, or "Member 14" for one
    with no name (erased). `prefix` reaches the member from another row, as
    in "member__"."""
    return Coalesce(
        NullIf(_member_name(prefix), Value("")),
        _text("Member ", _number(prefix + "pk")),
        output_field=CharField(),
    )


@dataclass(frozen=True)
class Kind:
    """What the page calls a kind of object, and where it finds a name."""

    label: str
    model: type[models.Model]
    # The name or number the page shows; empty when the record has none.
    name: Callable[[], Expr]
    # The number of the booking (the order) whose page the record leads to;
    # none for a record that has no such page.
    order: Callable[[], Expr] | None = None


def _own_name() -> Expr:
    return F("name")


def _spot_name() -> Expr:
    return Coalesce(NullIf(F("name"), Value("")), _number("number"))


def _container_number() -> Expr:
    return _number("number", 3)


def _author_name() -> Expr:
    return _member_name("member__")


def _order_reference() -> Expr:
    return F("reference")


def _of_order(path: str, text: str = "") -> Callable[[], Expr]:
    """The reference of the order a record belongs to ."""
    return lambda: _text(text, F(path))


def _own_number() -> Expr:
    return F("number")


def _payment_order() -> Expr:
    return F("order__number")


def _refund_order() -> Expr:
    return F("payment__order__number")


def _unnamed() -> Expr:
    return Value("")


def _event_day_name() -> Expr:
    """The event's title and the date of the day : a day's
    record names its date."""
    return _text(F("event__title"), " ", Cast(F("day"), CharField()))


KINDS: dict[str, Kind] = {
    "organization": Kind("Organization", Organization, _own_name),
    "space": Kind("Space", Space, _own_name),
    "asset_inventory": Kind("Floor plan", AssetInventory, _own_name),
    "spot_group": Kind("Group", SpotGroup, _own_name),
    "spot": Kind("Spot", Spot, _spot_name),
    "asset": Kind("Asset", Asset, _own_name),
    "booking_type": Kind("Booking type", BookingType, _own_name),
    "booking_category": Kind("Category", BookingCategory, _own_name),
    "booking_configuration": Kind("Container", BookingConfiguration, _container_number),
    "price_book": Kind("Price book", PriceBook, _own_name),
    "price_book_entry": Kind("Price", PriceBookEntry, _unnamed),
    "role": Kind("Role", Role, _own_name),
    "member": Kind("Member", Member, _member_name),
    "authenticator": Kind("Authenticator", Authenticator, _author_name),
    "member_import": Kind("Member import", MemberImport, _unnamed),
    "order": Kind("Reference", Order, _order_reference, _own_number),
    "booking": Kind(
        "Reference", Booking, _of_order("order__reference"), _payment_order
    ),
    "payment": Kind(
        "Payment", Payment, _of_order("order__reference", "Reference "), _payment_order
    ),
    "event": Kind("Event", Event, lambda: F("title")),
    "event_day": Kind("Event day", EventDay, _event_day_name),
    "refund": Kind(
        "Refund",
        Refund,
        _of_order("payment__order__reference", "Reference "),
        _refund_order,
    ),
}


# The kinds that have a page or a place of their own in the admin, by what the
# page opens by; the others (a login code, a taken time) have none. A booking's
# page is `booking`, not a target.
LINKED = frozenset(
    {
        "organization", "space", "asset_inventory", "spot_group", "spot", "asset",
        "booking_type", "booking_category", "booking_configuration", "price_book",
        "price_book_entry", "role", "member", "authenticator", "member_import",
        "event",
    }
)  # fmt: skip


def kind_label(kind: str) -> str:
    return KINDS[kind].label if kind in KINDS else kind.replace("_", " ").capitalize()


def _rows(kind: Kind, org: Organization) -> models.QuerySet[models.Model]:
    if kind.model is Organization:
        return Organization.objects.filter(pk=org.pk)
    return kind.model.objects.of(org)  # type: ignore[attr-defined, no-any-return]


def _shown(kind: Kind) -> Expr:
    """The name, or the id when the record has no name."""
    return Coalesce(
        NullIf(kind.name(), Value("")),
        Cast("pk", CharField()),
        output_field=CharField(),
    )


@dataclass(frozen=True)
class Query:
    since: datetime | None = None
    until: datetime | None = None
    by: int | None = None
    action: str | None = None
    search: str = ""
    newest_first: bool = True
    page: int = 1


def _search(org: Organization, search: str) -> Q:
    """The events whose "kind name" text contains the search.

    The name is not on the event, so each kind with a name is asked for its
    records whose name contains the search (after the kind's word, when the
    search begins with it), in one subquery on that kind's table. A search of
    3 characters or more that begins a word of a kind's label takes every
    event of the kind, also those whose record is gone.
    """
    wanted = Q()
    for kind in OBJECT_KINDS:
        label = kind_label(kind)
        if kind in KINDS:
            # The name only; a search that begins with the kind's word and a
            # space ("Reference K7Q") asks for the name after it.
            lead = label.lower() + " "
            rest = search[len(lead) :] if search.lower().startswith(lead) else search
            rows = (
                _rows(KINDS[kind], org)
                .annotate(what=_shown(KINDS[kind]))
                .filter(Q(what__icontains=search) | Q(what__icontains=rest))
            )
            wanted |= Q(object_kind=kind, object_id__in=rows.values("pk"))
        elif found := re.fullmatch(
            rf"(?:{re.escape(label)} )?([0-9]{{1,18}})", search, re.I
        ):
            wanted |= Q(object_kind=kind, object_id=int(found[1]))
        if len(search) >= 3 and any(
            word.startswith(search.lower()) for word in label.lower().split()
        ):
            wanted |= Q(object_kind=kind)
    return wanted


def _names(
    org: Organization, events: list[AuditEvent]
) -> dict[tuple[str, int], tuple[str, int | None]]:
    """The names of what the events acted on, with the number of the booking
    whose page each leads to, per kind in one query."""
    ids: dict[str, set[int]] = {}
    for event in events:
        if event.object_kind in KINDS:
            ids.setdefault(event.object_kind, set()).add(event.object_id)
    found: dict[tuple[str, int], tuple[str, int | None]] = {}
    for kind, wanted in ids.items():
        spec = KINDS[kind]
        rows = _rows(spec, org).filter(pk__in=wanted).annotate(what=_shown(spec))
        if spec.order is None:
            for pk, text in rows.values_list("pk", "what"):
                found[kind, pk] = (text, None)
        else:
            for pk, text, number in rows.annotate(number_of=spec.order()).values_list(
                "pk", "what", "number_of"
            ):
                found[kind, pk] = (text, number)
    return found


def _targets(
    org: Organization,
    events: list[AuditEvent],
    names: dict[tuple[str, int], tuple[str, int | None]],
) -> dict[tuple[str, int], dict[str, object]]:
    """Where each event's object can be opened: its kind and id as the page for
    it knows them. An authenticator is its member's page; a member import is
    its run's number; an event's day is its event. A record that is gone, and a
    kind with no page, have no target."""
    asked = {(event.object_kind, event.object_id) for event in events}
    targets: dict[tuple[str, int], dict[str, object]] = {}
    for kind, pk in asked:
        if kind == "block" or ((kind, pk) in names and kind in LINKED):
            targets[kind, pk] = {"kind": kind, "id": pk}
    owners = [pk for kind, pk in targets if kind == "authenticator"]
    for pk, member in (
        Authenticator.objects.of(org)
        .filter(pk__in=owners)
        .values_list("pk", "member_id")
    ):
        targets["authenticator", pk] = {"kind": "member", "id": member}
    # A day of an event leads to the event's page.
    days = [pk for kind, pk in asked if kind == "event_day" and (kind, pk) in names]
    for pk, event in (
        EventDay.objects.of(org).filter(pk__in=days).values_list("pk", "event_id")
    ):
        targets["event_day", pk] = {"kind": "event", "id": event}
    if any(kind == "member_import" for kind, _ in targets):
        from randirandi.core import member_imports  # imports this module

        numbers = member_imports.run_numbers(org)
        for kind, pk in list(targets):
            if kind == "member_import":
                targets[kind, pk] = {"kind": kind, "id": numbers[pk]}
    return targets


def _administrators(org: Organization, ids: Iterable[int]) -> dict[int, str]:
    rows = Member.objects.of(org).filter(pk__in=ids).annotate(what=_member_name())
    return {pk: text or f"Member {pk}" for pk, text in rows.values_list("pk", "what")}


def _what(
    event: AuditEvent,
    names: dict[tuple[str, int], tuple[str, int | None]],
    targets: dict[tuple[str, int], dict[str, object]],
) -> dict[str, object]:
    """What the event acted on: its kind and name, the number of the booking
    whose page it leads to (none when it leads nowhere) and its target, the
    kind and id the page for it is opened by (none when there is none)."""
    key = (event.object_kind, event.object_id)
    name, booking = names.get(key, (str(event.object_id), None))
    return {
        "kind": kind_label(event.object_kind),
        "name": name,
        "booking": booking,
        "target": targets.get(key),
    }


def answer(org: Organization, query: Query) -> dict[str, object]:
    zone = ZoneInfo(org.time_zone)
    events = AuditEvent.objects.of(org)
    if query.since is not None:
        events = events.filter(at__gte=query.since)
    if query.until is not None:
        events = events.filter(at__lt=query.until)
    if query.by is not None:
        events = events.filter(administrator=query.by)
    if query.action is not None:
        events = events.filter(action=query.action)
    if query.search:
        events = events.filter(_search(org, query.search))
    count = events.count()
    order = ("at", "id") if not query.newest_first else ("-at", "-id")
    start = (query.page - 1) * PER_PAGE
    page = list(events.order_by(*order)[start : start + PER_PAGE])
    names = _names(org, page)
    targets = _targets(org, page, names)
    by = _administrators(org, {event.administrator_id for event in page})
    everyone = _administrators(
        org, AuditEvent.objects.of(org).values_list("administrator", flat=True)
    )
    return {
        "count": count,
        "per_page": PER_PAGE,
        "page": query.page,
        "pages": -(-count // PER_PAGE),
        "events": [
            {
                "id": event.pk,
                "at": event.at.astimezone(zone).strftime("%Y-%m-%d, %H:%M:%S"),
                "administrator": {
                    "id": event.administrator_id,
                    "name": by[event.administrator_id],
                },
                "action": event.action,
                "action_label": action_label(event.action),
                "what": _what(event, names, targets),
            }
            for event in page
        ],
        "administrators": [
            {"id": pk, "name": everyone[pk]}
            for pk in sorted(everyone, key=lambda pk: (everyone[pk].lower(), pk))
        ],
        "actions": sorted(
            (
                {"value": value, "label": action_label(value)}
                for value in AuditAction.values
            ),
            key=lambda row: row["label"],
        ),
    }
