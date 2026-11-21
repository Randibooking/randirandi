# SPDX-License-Identifier: AGPL-3.0-only
"""Bringing in an organization's bookings from its earlier calendar, with the
blocks and events that go with them (docs/setup-file.md, "Bookings from an
earlier calendar").

One document says, for each old booking, the member who holds it (by email),
and for each of its pieces the container (by number), the category and the
times on the asset's clock. A booking whose holder has no account yet is a
block with a note, so that nobody books the time meanwhile; an event is the
organization's own time.

A booking is settled before it moves (L200, 2026-10-19): it is confirmed with
nothing to pay and no payment, and its lines keep the price list's tiers at
amount 0, so a cancellation refunds nothing that RandiRandi never took. Each
piece passes the container's rules as a member's booking does (weekdays,
hours, step, length, category, what else holds the time); the member's own
limits do not apply, as an administrator brings it in: how far ahead, how
many a day and the categories of the member's role.

Bookings go first, then blocks, then events, so that a repeating event skips
the days the bookings hold and names them (ADR-0029 S5). Each entry is its
own transaction: a refused entry saves nothing and the others go on, and an
entry already in, run again, is "in already". `check` does the same inside
one transaction that is rolled back, so it says what `bring_in` would do.
A refused booking leaves its time free: the document puts a holder with no
account among the blocks.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from randirandi.core import (
    block_admin,
    calendar,
    event_admin,
    event_plan,
    ordering,
    pricing,
)
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    Asset,
    Block,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingState,
    Event,
    EventCategory,
    EventDay,
    Member,
    MemberStatus,
    Order,
    Organization,
)

FORMAT = 1
# The most entries one document may hold, of each kind.
MOST = 500
# A moment on the asset's clock, to the minute.
_LOCAL = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,15}")


class DocumentError(ValueError):
    """The document is not one this reads; nothing was checked or saved."""


@dataclass(frozen=True)
class Piece:
    configuration: int
    category: str
    starts: str
    ends: str


@dataclass(frozen=True)
class OldBooking:
    key: str
    holder: str
    pieces: tuple[Piece, ...]
    # False: no reminder email, for a booking so near that the member hears
    # of it before the reminder would say anything new.
    remind: bool = True


@dataclass(frozen=True)
class OldBlock:
    key: str
    asset: int
    starts: str
    ends: str
    note: str


@dataclass(frozen=True)
class OldEvent:
    key: str
    title: str
    category: str
    contact: str
    description: str
    asset: int | None
    # A one-off: its moments. A repeat: its weekdays (0 Monday), first and
    # last date (None: no end) and its hours as "HH:MM".
    starts: str = ""
    ends: str = ""
    weekdays: tuple[int, ...] = ()
    first_date: str = ""
    end_date: str | None = None
    start_time: str = ""
    end_time: str = ""


@dataclass(frozen=True)
class Document:
    bookings: tuple[OldBooking, ...]
    blocks: tuple[OldBlock, ...]
    events: tuple[OldEvent, ...]


@dataclass(frozen=True)
class Outcome:
    """What became of one entry. `result` is "brought in", "in already",
    "made", or the refusal: "no member", "several members", "no container",
    "no category", "not these hours", "taken", "past", "partly in" or "busy".
    `detail` holds dates and codes only, never a name."""

    key: str
    kind: str
    result: str
    detail: tuple[str, ...] = ()


# --- Reading the document ------------------------------------------------------


def _text(item: Mapping[str, Any], name: str, *, empty: bool = False) -> str:
    value = item.get(name, "" if empty else None)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise DocumentError(f"'{name}' must be text.")
    return value


def _number(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if type(value) is not int or value < 1:
        raise DocumentError(f"'{name}' must be a positive whole number.")
    return value


def _moment(item: Mapping[str, Any], name: str) -> str:
    value = _text(item, name)
    if not _LOCAL.fullmatch(value):
        raise DocumentError(f"'{name}' must be a local time, YYYY-MM-DDTHH:MM.")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise DocumentError(f"'{name}' is not a time that exists.") from None
    return value


def _day(item: Mapping[str, Any], name: str) -> str:
    try:
        return date.fromisoformat(_text(item, name)).isoformat()
    except ValueError:
        raise DocumentError(f"'{name}' must be a date, YYYY-MM-DD.") from None


def _key(item: Mapping[str, Any], seen: set[str]) -> str:
    key = _text(item, "key")
    if not _KEY.fullmatch(key) or key in seen:
        raise DocumentError("Each entry has its own key: letters, digits, '-'.")
    seen.add(key)
    return key


def _list(raw: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    items = raw.get(name, [])
    if not isinstance(items, list) or len(items) > MOST:
        raise DocumentError(f"'{name}' must be a list of at most {MOST} entries.")
    if not all(isinstance(item, dict) for item in items):
        raise DocumentError(f"Each of '{name}' must be an object.")
    return items


def read(raw: Any) -> Document:
    """The document, its shape checked; DocumentError names what is wrong
    without quoting the document."""
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise DocumentError(f"The document must be an object of format {FORMAT}.")
    if set(raw) - {"format", "bookings", "blocks", "events"}:
        raise DocumentError("The document has a part this does not read.")
    seen: set[str] = set()
    bookings = []
    for item in _list(raw, "bookings"):
        pieces = item.get("pieces")
        if (
            not isinstance(pieces, list)
            or not 0 < len(pieces) <= ordering.MAX_BOOKINGS
            or not all(isinstance(piece, dict) for piece in pieces)
        ):
            raise DocumentError("A booking has one or more pieces, each an object.")
        bookings.append(
            OldBooking(
                _key(item, seen),
                _text(item, "holder").strip().lower(),
                tuple(
                    Piece(
                        _number(piece, "configuration"),
                        _text(piece, "category"),
                        _moment(piece, "starts"),
                        _moment(piece, "ends"),
                    )
                    for piece in pieces
                ),
                _flag(item, "remind"),
            )
        )
    blocks = [
        OldBlock(
            _key(item, seen),
            _number(item, "asset"),
            _moment(item, "starts"),
            _moment(item, "ends"),
            _text(item, "note"),
        )
        for item in _list(raw, "blocks")
    ]
    events = []
    for item in _list(raw, "events"):
        asset = item.get("asset")
        if asset is not None:
            asset = _number(item, "asset")
        common = OldEvent(
            key=_key(item, seen),
            title=_text(item, "title"),
            category=_text(item, "category"),
            contact=_text(item, "contact").strip().lower(),
            description=_text(item, "description", empty=True),
            asset=asset,
        )
        if common.category not in EventCategory.values:
            raise DocumentError(
                "An event's category is one of " + ", ".join(EventCategory.values) + "."
            )
        if "weekdays" in item:
            weekdays = item["weekdays"]
            if (
                not isinstance(weekdays, list)
                or not weekdays
                or not all(type(day) is int and 0 <= day <= 6 for day in weekdays)
            ):
                raise DocumentError("'weekdays' must list 0 (Monday) to 6.")
            end = None if item.get("end_date") is None else _day(item, "end_date")
            events.append(
                replace(
                    common,
                    weekdays=tuple(sorted(set(weekdays))),
                    first_date=_day(item, "first_date"),
                    end_date=end,
                    start_time=_clock(item, "start_time"),
                    end_time=_clock(item, "end_time"),
                )
            )
        else:
            events.append(
                replace(
                    common, starts=_moment(item, "starts"), ends=_moment(item, "ends")
                )
            )
    return Document(tuple(bookings), tuple(blocks), tuple(events))


def _flag(item: Mapping[str, Any], name: str) -> bool:
    value = item.get(name, True)
    if type(value) is not bool:
        raise DocumentError(f"'{name}' must be true or false.")
    return value


def _clock(item: Mapping[str, Any], name: str) -> str:
    value = _text(item, name)
    if not re.fullmatch(r"\d{2}:\d{2}", value) or not (
        0 <= int(value[:2]) <= 24 and 0 <= int(value[3:]) < 60
    ):
        raise DocumentError(f"'{name}' must be HH:MM.")
    return value


def _minute(clock: str) -> int:
    return int(clock[:2]) * 60 + int(clock[3:])


def _at(text: str, zone: ZoneInfo) -> datetime:
    """A time on the asset's clock, in UTC as an order normalizes it
    (`ordering._utc`). A time the clock skips or shows twice, at a change of
    summer time, is refused: it names no one moment."""
    wall = datetime.fromisoformat(text)
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if first != wall.replace(tzinfo=zone, fold=1).astimezone(UTC):
        raise _RefusedError("not these hours", text)
    return first


# --- The people -----------------------------------------------------------------


def administrator_of(org: Organization, email: str) -> Member:
    """The active administrator with this email; LookupError otherwise."""
    found = list(
        Member.objects.of(org)
        .select_related("role")
        .filter(
            email__iexact=email.strip(),
            status=MemberStatus.ACTIVE,
            role__admin_access=True,
        )[:2]
    )
    if len(found) != 1:
        raise LookupError("No one active administrator has this email.")
    return found[0]


def _member(org: Organization, email: str) -> Member | str:
    found = list(
        Member.objects.of(org).filter(email__iexact=email, status=MemberStatus.ACTIVE)[
            :2
        ]
    )
    if not found:
        return "no member"
    return found[0] if len(found) == 1 else "several members"


# --- Bookings -------------------------------------------------------------------


class _RefusedError(Exception):
    def __init__(self, result: str, *detail: str) -> None:
        super().__init__(result)
        self.result = result
        self.detail = detail


def _captured(
    org: Organization,
    holder: Member,
    administrator: Member,
    piece: Piece,
    now: datetime,
) -> ordering._Captured:
    """One piece read as an order reads a booking (`ordering._capture`), with
    the categories the holder's or the administrator's role offers."""
    config = (
        BookingConfiguration.objects.of(org).filter(number=piece.configuration).first()
    )
    if config is None:
        raise _RefusedError("no container", str(piece.configuration))
    category = BookingCategory.objects.of(org).filter(name=piece.category).first()
    if category is None:
        raise _RefusedError("no category")
    zone = config.asset.zone
    request = ordering.BookingRequest(
        config.pk, category.pk, _at(piece.starts, zone), _at(piece.ends, zone)
    )
    if request.ends_at <= now:
        raise _RefusedError("past", piece.starts)
    if request.starts_at < now:
        # Under way: its rest is for an administrator to block by hand.
        raise _RefusedError("under way", piece.starts)
    try:
        price_data = pricing._read(config, category)
    except pricing.PriceUnavailableError:
        raise _RefusedError("not these hours", piece.starts, piece.ends) from None
    config = price_data.configuration
    # The holder's role first, then the administrator's: an administrator
    # brings in a category the holder's own role does not book.
    for role_id in dict.fromkeys((holder.role_id, administrator.role_id)):
        data = calendar._read(
            holder,
            config.asset,
            config.booking_type,
            request.starts_at.astimezone(zone).date(),
            request.ends_at.astimezone(zone).date() + timedelta(days=1),
            now,
            role_id=role_id,
        )
        try:
            asset, price = ordering._priced(price_data, data, request)
        except ordering.OrderUnavailableError:
            continue
        spots = data.spots.get(config.floor_plan_id or 0, []) if data else []
        return ordering._Captured(request, config, asset, price, spots, price_data)
    raise _RefusedError("not these hours", piece.starts, piece.ends)


def _in_already(
    org: Organization, holder: Member, captured: Sequence[ordering._Captured]
) -> int:
    """How many of the pieces are in already: a booking of this holder,
    container and times on an order made without conditions, as this makes
    them, in whatever state: one cancelled since is not brought in again."""
    return sum(
        Booking.objects.of(org)
        .filter(
            member=holder,
            order__conditions__isnull=True,
            booking_configuration=item.config,
            taken_time__starts_at=item.price.starts_at,
            taken_time__ends_at=item.price.ends_at,
        )
        .exists()
        for item in captured
    )


def _all_in_as_written(org: Organization, holder: Member, item: OldBooking) -> bool:
    """Every piece in already, read from the document before the container's
    rules: a re-run after go-live says "in already", not "past"."""
    for piece in item.pieces:
        config = (
            BookingConfiguration.objects.of(org)
            .select_related("asset")
            .filter(number=piece.configuration)
            .first()
        )
        if config is None:
            return False
        try:
            starts = _at(piece.starts, config.asset.zone)
            ends = _at(piece.ends, config.asset.zone)
        except _RefusedError:
            return False
        if not (
            Booking.objects.of(org)
            .filter(
                member=holder,
                order__conditions__isnull=True,
                booking_configuration=config,
                taken_time__starts_at=starts,
                taken_time__ends_at=ends,
            )
            .exists()
        ):
            return False
    return True


def _book(
    org: Organization, administrator: Member, item: OldBooking, now: datetime
) -> Outcome:
    holder = _member(org, item.holder)
    if isinstance(holder, str):
        return Outcome(item.key, "booking", holder)
    if _all_in_as_written(org, holder, item):
        return Outcome(item.key, "booking", "in already")
    try:
        captured = [
            _captured(org, holder, administrator, piece, now) for piece in item.pieces
        ]
    except _RefusedError as refused:
        return Outcome(item.key, "booking", refused.result, refused.detail)
    there = _in_already(org, holder, captured)
    if there == len(captured):
        return Outcome(item.key, "booking", "in already")
    if there:
        return Outcome(item.key, "booking", "partly in")
    try:
        placed = ordering._place(org, captured, keeping=False)
    except ordering.OrderUnavailableError:
        return Outcome(item.key, "booking", "taken")
    order = Order.objects.create(
        organization=org,
        member_id=holder.pk,
        created_at=now,
        hold_expires_at=None,
        total=0,
        # Settled before it moved: no confirmation is due, as for an order
        # from before the email existed.
        confirmation_due_at=None,
    )
    prices = [
        replace(
            entry.price,
            lines=tuple(replace(line, amount=0) for line in entry.price.lines),
        )
        for entry in captured
    ]
    ordering._book(org, holder, order, captured, prices, placed, BookingState.CONFIRMED)
    if not item.remind:
        Booking.objects.of(org).filter(order=order).update(reminded_at=now)
    record_event(
        actor=administrator, action=AuditAction.BROUGHT_IN_BOOKING, target=order
    )
    return Outcome(item.key, "booking", "brought in", (order.reference,))


# --- Blocks and events ------------------------------------------------------------


def _asset(org: Organization, number: int) -> Asset:
    asset = Asset.objects.of(org).filter(number=number).first()
    if asset is None:
        raise _RefusedError("no asset", str(number))
    return asset


def _block_note(item: OldBlock) -> str:
    return f"{item.key}: {item.note}"[: block_admin.LONGEST_NOTE]


def _block_in_already(
    org: Organization, asset: Asset, item: OldBlock, starts: datetime, ends: datetime
) -> bool:
    return (
        Block.objects.of(org)
        .filter(
            note=_block_note(item),
            taken_time__asset=asset,
            taken_time__starts_at__gte=starts,
            taken_time__ends_at__lte=ends,
        )
        .exists()
    )


def _block(
    org: Organization,
    administrator: Member,
    item: OldBlock,
    now: datetime,
    make: Callable[[Member, int, datetime, datetime, str], list[Block]],
) -> Outcome:
    try:
        asset = _asset(org, item.asset)
        starts, ends = _at(item.starts, asset.zone), _at(item.ends, asset.zone)
    except _RefusedError as refused:
        return Outcome(item.key, "block", refused.result, refused.detail)
    if _block_in_already(org, asset, item, starts, ends):
        return Outcome(item.key, "block", "in already")
    if ends <= now:
        return Outcome(item.key, "block", "past", (item.starts,))
    if starts < now:
        return Outcome(item.key, "block", "under way", (item.starts,))
    try:
        made = make(administrator, asset.pk, starts, ends, _block_note(item))
    except block_admin.BlockRefusedError as refused:
        return Outcome(
            item.key,
            "block",
            "taken" if refused.code != "busy" else "busy",
            (refused.code,),
        )
    except ValueError:
        return Outcome(item.key, "block", "not these hours", (item.starts, item.ends))
    return Outcome(item.key, "block", "made", (f"{len(made)} pieces",))


def _spec(org: Organization, item: OldEvent) -> event_admin.EventSpec | Outcome:
    contact = _member(org, item.contact)
    if isinstance(contact, str):
        return Outcome(item.key, "event", contact)
    asset_id = None
    zone = ZoneInfo(org.time_zone)
    if item.asset is not None:
        try:
            asset = _asset(org, item.asset)
        except _RefusedError as refused:
            return Outcome(item.key, "event", refused.result, refused.detail)
        asset_id, zone = asset.pk, asset.zone
    when: event_admin.OneOff | event_admin.Repeat
    if item.weekdays:
        when = event_admin.Repeat(
            weekdays=frozenset(item.weekdays),
            first_date=date.fromisoformat(item.first_date),
            end_date=date.fromisoformat(item.end_date) if item.end_date else None,
            start_minute=_minute(item.start_time),
            end_minute=_minute(item.end_time),
        )
    else:
        # An event takes wall times (`event_admin.OneOff`); `_at` only refuses
        # one that summer time skips or repeats.
        try:
            _at(item.starts, zone), _at(item.ends, zone)
        except _RefusedError as refused:
            return Outcome(item.key, "event", refused.result, refused.detail)
        when = event_admin.OneOff(
            starts=datetime.fromisoformat(item.starts),
            ends=datetime.fromisoformat(item.ends),
        )
    return event_admin.EventSpec(
        category=item.category,
        title=item.title,
        description=item.description,
        contact_id=contact.pk,
        when=when,
        asset_id=asset_id,
    )


def _event_in_already(org: Organization, spec: event_admin.EventSpec) -> bool:
    same = Event.objects.of(org).filter(title=spec.title, category=spec.category)
    if isinstance(spec.when, event_admin.Repeat):
        return same.filter(
            repeat__weekdays=event_plan.mask(spec.when.weekdays),
            repeat__first_date=spec.when.first_date,
            repeat__end_date=spec.when.end_date,
            repeat__start_minute=spec.when.start_minute,
            repeat__end_minute=spec.when.end_minute,
        ).exists()
    asset = (
        None
        if spec.asset_id is None
        else Asset.objects.of(org).filter(pk=spec.asset_id).first()
    )
    zone = asset.zone if asset else ZoneInfo(org.time_zone)
    # A wall time `_spec` has checked names one moment.
    starts = spec.when.starts.replace(tzinfo=zone).astimezone(UTC)
    return (
        EventDay.objects.of(org)
        .filter(event__in=same, starts_at=starts, removed_at=None)
        .exists()
    )


def _skipped(preview: event_admin.Preview) -> tuple[str, ...]:
    return tuple(
        f"{day.day.isoformat()} {day.reason}"
        for day in preview.days
        if day.reason is not None
    )


def _event_refused(item: OldEvent, refused: event_admin.EventRefusedError) -> Outcome:
    """An event's refusal by its code (`event_admin.EventRefusedError`)."""
    code = refused.code
    if code in ("closed", "booked", "blocked", "event"):
        result = "taken"
    elif code in ("past", "busy"):
        result = code
    elif code in ("dates", "weekdays", "hours"):
        result = "not these hours"
    else:
        result = "not this event"
    return Outcome(item.key, "event", result, (code,))


def _event(
    org: Organization,
    administrator: Member,
    item: OldEvent,
    now: datetime,
    make: Callable[[event_admin.EventSpec], object],
) -> Outcome:
    """The event, previewed for what it skips, then made by `make`."""
    spec = _spec(org, item)
    if isinstance(spec, Outcome):
        return spec
    if _event_in_already(org, spec):
        return Outcome(item.key, "event", "in already")
    try:
        preview = event_admin.preview(administrator, spec, now=now)
    except event_admin.EventRefusedError as refused:
        return _event_refused(item, refused)
    except ValueError:
        return Outcome(item.key, "event", "not these hours")
    if preview.over:
        return Outcome(item.key, "event", "past")
    skipped = _skipped(preview)
    if not preview.repeating and skipped:
        return Outcome(item.key, "event", "taken", skipped)
    try:
        make(spec)
    except event_admin.EventRefusedError as refused:
        return _event_refused(item, refused)
    return Outcome(item.key, "event", "made", skipped)


# --- Running it ---------------------------------------------------------------------


def _own_transaction[T](org_id: int, work: Callable[[Organization], T], busy: T) -> T:
    """`work` in its own serializable transaction with the organization's row
    locked, as an order's (`ordering.create_order`); `busy` when five fresh
    attempts all meet concurrent writes."""
    for attempt in range(ordering.MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"
                    )
                org = Organization.objects.select_for_update(no_key=True).get(pk=org_id)
                return work(org)
        except (IntegrityError, OperationalError) as error:
            if not ordering._retryable(error):
                raise
            if attempt == ordering.MAX_ATTEMPTS - 1:
                return busy
    raise AssertionError("Unreachable: each attempt returns, retries or raises.")


def _make_block_inside(
    administrator: Member, asset_id: int, start: datetime, end: datetime, note: str
) -> list[Block]:
    block_admin._check_stretch(start, end)
    return block_admin._attempt(administrator, asset_id, start, end, note)


class _RollBackError(Exception):
    pass


def check(
    org: Organization,
    administrator: Member,
    document: Document,
    now: datetime | None = None,
) -> list[Outcome]:
    """What `bring_in` would do, entry by entry, in one transaction that is
    rolled back: an entry sees the ones before it. Saves nothing."""
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Checking an import requires its own transaction.")
    moment = now or timezone.now()
    outcomes: list[Outcome] = []
    try:
        with transaction.atomic():
            locked = Organization.objects.select_for_update(no_key=True).get(pk=org.pk)
            for booking in document.bookings:
                with transaction.atomic():
                    outcomes.append(_book(locked, administrator, booking, moment))
            for block in document.blocks:
                with transaction.atomic():
                    outcomes.append(
                        _block(locked, administrator, block, moment, _make_block_inside)
                    )
            current = event_admin._staff(locked, administrator)

            def make(spec: event_admin.EventSpec) -> object:
                with transaction.atomic():
                    return event_admin.make_inside(locked, current, spec, {}, moment)

            for event in document.events:
                outcomes.append(_event(locked, administrator, event, moment, make))
            raise _RollBackError
    except _RollBackError:
        pass
    return outcomes


def bring_in(
    org: Organization,
    administrator: Member,
    document: Document,
    now: datetime | None = None,
) -> list[Outcome]:
    """Save every entry the rules allow, each in its own transaction, with an
    audit event of the administrator for each: the bookings, then the blocks,
    then the events."""
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Bringing in an import requires its own transaction.")
    moment = now or timezone.now()
    outcomes: list[Outcome] = []
    for booking in document.bookings:

        def book(locked: Organization, booking: OldBooking = booking) -> Outcome:
            return _book(locked, administrator, booking, moment)

        busy = Outcome(booking.key, "booking", "busy")
        outcomes.append(_own_transaction(org.pk, book, busy))
    for block in document.blocks:

        def take(locked: Organization, block: OldBlock = block) -> Outcome:
            return _block(locked, administrator, block, moment, _make_block_inside)

        busy = Outcome(block.key, "block", "busy")
        outcomes.append(_own_transaction(org.pk, take, busy))

    def make(spec: event_admin.EventSpec) -> object:
        return event_admin.make_event(administrator, spec, now=moment)

    for event in document.events:
        outcomes.append(_event(org, administrator, event, moment, make))
    return outcomes
