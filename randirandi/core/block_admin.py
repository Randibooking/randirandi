# SPDX-License-Identifier: AGPL-3.0-only
"""The admin's Blocks : time an administrator takes out of
booking on an asset.

One stretch entered is laid as the blocks it takes (`lay`): per container of
the asset, the container's cells wholly inside the stretch, cells that touch
as one block. The same laying answers the preview and the save, and the
save takes the locks of a booking's (`ordering.create_order`) so that a
booking and a block for the same time end with exactly one.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone

from randirandi.core import audit_log, booking_admin, calendar, ordering
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    Asset,
    Block,
    Booking,
    BookingConfiguration,
    BookingState,
    ConsumptionMode,
    EventDay,
    Member,
    Organization,
    Spot,
    Status,
    TakenTime,
)
from randirandi.core.models.orders import BLOCKED, EVENT_STATE

# The longest stretch one block entry may cover, and the longest note.
MOST_DAYS = 366
LONGEST_NOTE = 500
# The codes of a refusal, in the order the page reports them.
CODES = ("nothing", "booked", "event", "blocked")
WHEN = ("coming", "past", "day", "period")


class BlockNotFoundError(LookupError):
    """The asset or the block is not in this organization."""


class BlockRefusedError(Exception):
    """The block is refused; nothing was saved. `code` is one of `CODES`,
    "past" (the block is over) or "busy"."""

    def __init__(
        self,
        code: str,
        booked: list[int] | None = None,
        events: list[tuple[int, str]] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.booked = booked or []
        # The events (id and title) holding the time, for "event".
        self.events = events or []


@dataclass(frozen=True)
class Piece:
    """One block a stretch is laid as."""

    configuration: BookingConfiguration
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class Laid:
    pieces: list[Piece]
    # The numbers of the bookings holding any of the time, lowest first.
    booked: list[int]
    # Their references (what people read), in the same order.
    booked_references: list[str]
    # Whether a block holds any of the time.
    blocked: bool
    # The events (id and title) holding any of the time : a block is
    # refused over an event's time, naming the event.
    events: list[tuple[int, str]] = field(default_factory=list)
    # The same, one entry for each day of an event holding the time, with its
    # moments: (event id, title, starts, ends), by start.
    event_days: list[tuple[int, str, datetime, datetime]] = field(default_factory=list)

    @property
    def refusal(self) -> str | None:
        """The first reason the pieces cannot be saved."""
        if not self.pieces:
            return "nothing"
        if self.booked:
            return "booked"
        if self.events:
            return "event"
        return "blocked" if self.blocked else None


def _overlap(first: calendar.Span, second: calendar.Span) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _joined(cells: list[calendar.Span]) -> list[calendar.Span]:
    """Cells by start, those that touch as one."""
    joined: list[calendar.Span] = []
    for start, end in sorted(cells):
        if joined and joined[-1][1] == start:
            joined[-1] = (joined[-1][0], end)
        else:
            joined.append((start, end))
    return joined


def lay(asset: Asset, start: datetime, end: datetime) -> Laid:
    """What a stretch on the asset is laid as, and what is in its way.

    Stacked containers  never take the same time twice: where two
    containers' cells overlap, the lower-numbered container keeps its cells
    and the other's overlapping cells are skipped. A block on a shared
    container takes every spot of its floor plan, the whole asset for that
    time: no spot is chosen (`_attempt`).
    """
    org = asset.organization_id
    zone = asset.zone
    kept: list[calendar.Span] = []
    pieces: list[Piece] = []
    for config in (
        BookingConfiguration.objects.of(org)
        .filter(asset=asset, status=Status.ENABLED)
        .select_related("booking_type")
        .order_by("number")
    ):
        # A shared container whose floor plan has no spot sells nothing.
        if (
            config.consumption_mode == ConsumptionMode.SHARED
            and not Spot.objects.of(org)
            .filter(floor_plan_id=config.floor_plan_id)
            .exists()
        ):
            continue
        cells = calendar.cells_within(config, start, end, zone)
        mine = [cell for cell in cells if not any(_overlap(cell, k) for k in kept)]
        kept.extend(mine)
        pieces.extend(
            Piece(config, first, last)
            for first, last in _joined(mine)
            # A piece across a clock change may not be whole cells.
            if calendar.on_its_grid(config, first, last, zone)
        )
    pieces.sort(key=lambda piece: (piece.starts_at, piece.configuration.number))
    booked: dict[int, str] = {}
    blocked = False
    events: list[tuple[int, str]] = []
    event_days: list[tuple[int, str, datetime, datetime]] = []
    if pieces:
        # What the database rule keeps apart: the claims of the asset's space.
        holding = list(
            TakenTime.objects.of(org)
            .filter(
                scope_key=asset.scope_key,
                state__in=[
                    BookingState.HELD,
                    BookingState.CONFIRMED,
                    BLOCKED,
                    EVENT_STATE,
                ],
                starts_at__lt=max(piece.ends_at for piece in pieces),
                ends_at__gt=min(piece.starts_at for piece in pieces),
            )
            .values_list(
                "pk", "starts_at", "ends_at", "state", "asset_id", "whole_space"
            )
        )
        in_way = [
            (pk, state)
            for pk, first, last, state, claim_asset, claim_whole in holding
            if any(_overlap((first, last), (p.starts_at, p.ends_at)) for p in pieces)
            # An event holds its whole asset, and the space when either
            # covers it (`capacity_no_double_booking`): one on another asset
            # of the space is no hindrance.
            and (
                state != EVENT_STATE
                or claim_asset == asset.pk
                or asset.whole_space
                or claim_whole
            )
        ]
        blocked = any(state == BLOCKED for _, state in in_way)
        booked = dict(
            Booking.objects.of(org)
            .filter(
                taken_time__in=[
                    pk
                    for pk, state in in_way
                    if state in (BookingState.HELD, BookingState.CONFIRMED)
                ]
            )
            .values_list("order__number", "order__reference")
        )
        event_days = list(
            EventDay.objects.of(org)
            .filter(taken_time__in=[pk for pk, state in in_way if state == EVENT_STATE])
            .order_by("starts_at", "pk")
            .values_list("event_id", "event__title", "starts_at", "ends_at")
        )
        events = sorted({(pk, title) for pk, title, _, _ in event_days})
    numbers = sorted(booked)
    return Laid(
        pieces, numbers, [booked[n] for n in numbers], blocked, events, event_days
    )


def _check_stretch(start: datetime, end: datetime) -> None:
    if end - start > timedelta(days=MOST_DAYS):
        raise ValueError("A stretch is at most 366 days.")


def preview(asset: Asset, start: datetime, end: datetime) -> Laid:
    """What a stretch would be laid as, written nowhere. ValueError when the
    stretch is too long."""
    _check_stretch(start, end)
    return lay(asset, start, end)


def _attempt(
    administrator: Member, asset_id: int, start: datetime, end: datetime, note: str
) -> list[Block]:
    # The organization's row serialises this with the orders (see
    # `ordering._attempt`).
    org = Organization.objects.select_for_update(no_key=True).get(
        pk=administrator.organization_id
    )
    asset = Asset.objects.of(org).filter(pk=asset_id).first()
    if asset is None:
        raise BlockNotFoundError
    laid = lay(asset, start, end)
    if (refusal := laid.refusal) is not None:
        raise BlockRefusedError(refusal, laid.booked, laid.events)
    now = timezone.now()
    made = []
    for piece in laid.pieces:
        config = piece.configuration
        # A shared container's block holds all of its floor plan's spots.
        units = (
            list(
                Spot.objects.of(org)
                .filter(floor_plan_id=config.floor_plan_id)
                .order_by("number")
            )
            if config.consumption_mode == ConsumptionMode.SHARED
            else None
        )
        block = Block.objects.take(
            organization=org,
            asset=asset,
            booking_configuration=config,
            units=units,
            starts_at=piece.starts_at,
            ends_at=piece.ends_at,
            note=note,
            made_by=administrator,
            created_at=now,
        )
        record_event(actor=administrator, action=AuditAction.MADE_BLOCK, target=block)
        made.append(block)
    return made


def make(
    administrator: Member, asset_id: int, start: datetime, end: datetime, note: str
) -> list[Block]:
    """Lay the stretch and save every block of it, or none: with one audit
    event for each block, in the same transaction. A booking or a block in the
    way refuses it (`BlockRefusedError`); a double submit finds its own blocks
    in the way. Five fresh attempts meet concurrent bookings, as an order's
    (`ordering.create_order`); all five failing is "busy".
    """
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Making blocks requires its own transaction.")
    _check_stretch(start, end)
    for attempt in range(ordering.MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"
                    )
                return _attempt(administrator, asset_id, start, end, note)
        except (IntegrityError, OperationalError) as error:
            if not ordering._retryable(error):
                raise
            if attempt == ordering.MAX_ATTEMPTS - 1:
                raise BlockRefusedError("busy") from None
    raise AssertionError("Unreachable: each attempt returns, retries or raises.")


def remove(administrator: Member, block_id: int) -> None:
    """Remove a block that has not ended (one under way too) and free its
    time. The event is written before the delete, in the same transaction. The
    organization's lock makes a second removal of it wait, then not find it."""
    with transaction.atomic():
        org = Organization.objects.select_for_update(no_key=True).get(
            pk=administrator.organization_id
        )
        block = (
            Block.objects.of(org)
            .select_related("taken_time")
            .filter(pk=block_id)
            .first()
        )
        if block is None:
            raise BlockNotFoundError
        if block.taken_time.ends_at <= booking_admin._now():
            raise BlockRefusedError("past")
        record_event(
            actor=administrator, action=AuditAction.REMOVED_BLOCK, target=block
        )
        block.delete()


def piece_words(asset: Asset, piece: Piece) -> dict[str, object]:
    """A piece as the page shows it, on the asset's clock."""
    return {
        **booking_admin._times(piece.starts_at, piece.ends_at, asset.time_zone),
        "container": piece.configuration.number,
        "booking_type": piece.configuration.booking_type.name,
    }


def laid_words(asset: Asset, laid: Laid) -> dict[str, object]:
    return {
        "pieces": [piece_words(asset, piece) for piece in laid.pieces],
        "booked": laid.booked,
        "booked_references": laid.booked_references,
        "blocked": laid.blocked,
        "events": [{"id": pk, "title": title} for pk, title in laid.events],
        "event_days": [
            {
                "event": pk,
                "title": title,
                "start": first.astimezone(asset.zone).strftime("%Y-%m-%dT%H:%M"),
                "end": last.astimezone(asset.zone).strftime("%Y-%m-%dT%H:%M"),
            }
            for pk, title, first, last in laid.event_days
        ],
        "refusal": laid.refusal,
    }


def blocks(org: Organization, query: booking_admin.ScheduleQuery) -> dict[str, object]:
    """The blocks by their start, as the Blocks lists them. A day is the date a
    block starts on the clock of its own asset."""
    now = booking_admin._now()
    zone = ZoneInfo(org.time_zone)
    found = Block.objects.of(org).annotate(
        starts=F("taken_time__starts_at"),
        ends=F("taken_time__ends_at"),
        clock=F("taken_time__time_zone"),
        day=booking_admin.clock_day("taken_time__"),
        who=audit_log.member_shown("made_by__"),
    )
    match query.when:
        case "coming":
            found = found.filter(ends__gt=now)
        case "past":
            found = found.filter(ends__lte=now)
        case "day":
            found = found.filter(day=query.first)
        case "period":
            found = found.filter(day__gte=query.first, day__lte=query.last)
    count = found.count()
    key = ("starts", "pk")
    order = key if query.earliest_first else tuple(f"-{name}" for name in key)
    start = (query.page - 1) * booking_admin.PER_PAGE
    page = found.order_by(*order).values(
        "pk",
        "starts",
        "ends",
        "clock",
        "taken_time__asset__name",
        "booking_configuration__booking_type__name",
        "note",
        "who",
        "created_at",
    )[start : start + booking_admin.PER_PAGE]
    return {
        **booking_admin._paging(count, query.page),
        # What a new block can be on.
        "assets": [
            {"id": pk, "name": name}
            for pk, name in Asset.objects.of(org)
            .order_by("name", "pk")
            .values_list("pk", "name")
        ],
        "blocks": [
            {
                "id": row["pk"],
                **booking_admin._times(row["starts"], row["ends"], row["clock"]),
                "asset": row["taken_time__asset__name"],
                "booking_type": row["booking_configuration__booking_type__name"],
                "note": row["note"],
                "made_by": row["who"],
                "created": None
                if row["created_at"] is None
                else booking_admin._stamp(row["created_at"], zone),
                # What a removal can take: not over yet.
                "coming": row["ends"] > now,
            }
            for row in page
        ],
    }
