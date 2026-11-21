# SPDX-License-Identifier: AGPL-3.0-only
"""The Bookings, the Schedule and the Payments as the admin pages read them
.

The pages' "booking" is the database's order : what one member made at
one time. Its "booking lines" are the database's bookings, the times it
holds; what a line is bought with (the price tiers) are the database's
booking lines, here "price lines". Every answer is read only. The filtering,
the sorting, the search and the paging are one database query each; the
names, categories, totals and currencies of the page asked for are looked up
for its 25 rows together, so the number of queries does not grow with them.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db.models import (
    Case,
    CharField,
    DateField,
    DateTimeField,
    Exists,
    F,
    Func,
    OuterRef,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Cast
from django.utils import timezone

from randirandi.core import audit_log, booking_conditions, refund_check
from randirandi.core.booking_emails import money_words
from randirandi.core.models import (
    Booking,
    BookingLine,
    BookingState,
    Member,
    Order,
    Organization,
    Payment,
    PaymentTrouble,
    Refund,
)
from randirandi.core.payment_trouble import (
    KIND_WORDS,
    LASTING,
    STEP_WORDS,
    sentence,
    spells,
)

PER_PAGE = audit_log.PER_PAGE
# An order's state, as the page words it. The order has none of its own: it
# follows its bookings' (DATA-MODEL, Order).
STATE_WORDS = {
    "paid": "Paid",
    "held": "Being paid",
    "cancelled": "Cancelled",
    "expired": "Not completed",
}
# A booking's state as the page words it.
BOOKING_STATE = {
    BookingState.CONFIRMED.value: "paid",
    BookingState.HELD.value: "held",
    BookingState.CANCELLED.value: "cancelled",
    BookingState.EXPIRED.value: "expired",
}
PAYMENT_WORDS = {
    "created": "Waiting",
    "authorized": "Reserved",
    "captured": "Captured",
    "cancelled": "Cancelled",
    "expired": "Expired",
    "refunded": "Refunded",
}
# The Payments' extra state: a payment whose provider check keeps failing.
ATTENTION = "attention"
ATTENTION_WORDS = "Needs attention"
# The periods of the Schedule's "Booked for" filter.
WHEN = ("today", "week", "coming", "past", "day", "period")


def state_words(state: str, amount: int) -> str:
    """Paid with nothing to pay reads "Confirmed"."""
    return "Confirmed" if state == "paid" and not amount else STATE_WORDS[state]


def _stamp(moment: datetime, zone: ZoneInfo) -> str:
    """When something happened : YYYY-MM-DD, HH:MM:SS."""
    return moment.astimezone(zone).strftime("%Y-%m-%d, %H:%M:%S")


def _flags(
    payment: Payment, spells_of: list[PaymentTrouble], zone: ZoneInfo
) -> dict[str, object]:
    """Why a payment needs attention, on its booking's page: its check keeps
    failing (`trouble`), a completed refund is not in the daily report
    (`missing_refund`), or both."""
    trouble = next((_trouble(s, zone) for s in spells_of if s.ended_at is None), None)
    missing = payment.pk in refund_check.attention(
        payment.organization_id, [payment.pk]
    )
    return {
        "trouble": trouble,
        "missing_refund": missing,
        "attention": trouble is not None or missing,
    }


def _trouble(row: PaymentTrouble, zone: ZoneInfo) -> dict[str, object]:
    """A payment's open trouble with the provider check, in fixed words."""
    return {
        "label": ATTENTION_WORDS,
        "since": _stamp(row.since, zone),
        "step": row.step,
        "step_label": sentence(STEP_WORDS[row.step]),
        "kind": row.kind,
        "kind_label": sentence(KIND_WORDS[row.kind]),
        "tries": row.tries,
    }


def _times(starts: datetime, ends: datetime, zone: str) -> dict[str, str]:
    """A booked time as four cells , on the asset's clock. A time that
    ends at midnight ends 00:00 on the next day."""
    start, end = starts.astimezone(ZoneInfo(zone)), ends.astimezone(ZoneInfo(zone))
    return {
        "start_date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%H:%M"),
        "end_date": end.strftime("%Y-%m-%d"),
        "end_time": end.strftime("%H:%M"),
    }


def _moment(moment: datetime, zone: str) -> dict[str, str]:
    """A moment as its date and time on a clock."""
    local = moment.astimezone(ZoneInfo(zone))
    return {"date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M")}


def _currencies(org: Organization, ids: list[int]) -> dict[int, str]:
    """The currency of each order: its payment's; with none, its first
    priced booking's price book; with none of those, the organization's (as
    the confirmation email, `booking_emails.confirmation_message`)."""
    found = dict(
        Payment.objects.of(org)
        .filter(order_id__in=ids)
        .values_list("order_id", "currency")
    )
    priced = (
        BookingLine.objects.of(org)
        .filter(booking__order_id__in=[pk for pk in ids if pk not in found])
        .order_by("booking__taken_time__starts_at", "booking_id", "pk")
        .values_list("booking__order_id", "price_book__currency")
    )
    for order_id, currency in priced:
        found.setdefault(order_id, currency)
    return {pk: found.get(pk, org.currency) for pk in ids}


def _totals(org: Organization, ids: list[int]) -> dict[int, int]:
    """What each booking costs: the sum of its price lines."""
    return dict(
        BookingLine.objects.of(org)
        .filter(booking_id__in=ids)
        .values("booking_id")
        .annotate(cost=Sum(F("quantity") * F("amount") * F("capacity_quantity")))
        .values_list("booking_id", "cost")
    )


def _paging(count: int, page: int) -> dict[str, int]:
    """The numbers every list answers with, beside its rows."""
    return {
        "count": count,
        "per_page": PER_PAGE,
        "page": page,
        "pages": -(-count // PER_PAGE),
    }


@dataclass(frozen=True)
class BookingsQuery:
    since: datetime | None = None
    until: datetime | None = None
    state: str | None = None
    search: str = ""
    by_number: bool = True
    newest_first: bool = True
    page: int = 1
    # One member's bookings, for the member's page .
    member: int | None = None


def bookings(org: Organization, query: BookingsQuery) -> dict[str, object]:
    zone = ZoneInfo(org.time_zone)

    def has(state: BookingState) -> Exists:
        return Exists(
            Booking.objects.of(org)
            .filter(order=OuterRef("pk"), taken_time__state=state)
            .values("pk")
        )

    orders = Order.objects.of(org).annotate(
        state=Case(
            When(has(BookingState.HELD), then=Value("held")),
            When(has(BookingState.CONFIRMED), then=Value("paid")),
            When(has(BookingState.CANCELLED), then=Value("cancelled")),
            default=Value("expired"),
            output_field=CharField(),
        ),
        who=audit_log.member_shown("member__"),
    )
    if query.since is not None:
        orders = orders.filter(created_at__gte=query.since)
    if query.until is not None:
        orders = orders.filter(created_at__lt=query.until)
    if query.state is not None:
        orders = orders.filter(state=query.state)
    if query.member is not None:
        orders = orders.filter(member=query.member)
    if query.search:
        orders = orders.filter(
            Q(who__icontains=query.search) | Q(reference__icontains=query.search)
        )
    count = orders.count()
    key = ("number",) if query.by_number else ("created_at", "number")
    order = key if not query.newest_first else tuple(f"-{name}" for name in key)
    start = (query.page - 1) * PER_PAGE
    page = list(
        orders.order_by(*order).values(
            "pk", "number", "reference", "created_at", "total", "state", "who"
        )[start : start + PER_PAGE]
    )
    ids = [row["pk"] for row in page]
    categories: dict[int, list[str]] = defaultdict(list)
    for order_id, name in (
        Booking.objects.of(org)
        .filter(order_id__in=ids)
        .order_by("taken_time__starts_at", "pk")
        .values_list("order_id", "booking_category_name")
    ):
        if name not in categories[order_id]:
            categories[order_id].append(name)
    currencies = _currencies(org, ids)
    return {
        **_paging(count, query.page),
        "bookings": [
            {
                "number": row["number"],
                "reference": row["reference"],
                "created": _stamp(row["created_at"], zone),
                "categories": ", ".join(categories[row["pk"]]),
                "member": row["who"],
                "state": row["state"],
                "state_label": state_words(row["state"], row["total"]),
                "amount": row["total"],
                "currency": currencies[row["pk"]],
            }
            for row in page
        ],
        "states": [{"value": k, "label": v} for k, v in STATE_WORDS.items()],
    }


@dataclass(frozen=True)
class PaymentsQuery:
    since: datetime | None = None
    until: datetime | None = None
    state: str | None = None
    search: str = ""
    newest_first: bool = True
    page: int = 1


def payments(org: Organization, query: PaymentsQuery) -> dict[str, object]:
    """One row per order that has a payment , from the payment's
    own record: its state, amounts and currency are not recomputed. "Created"
    is the order's, as in `bookings`; the search is the member's name or the
    Vipps reference."""
    zone = ZoneInfo(org.time_zone)
    found = Payment.objects.of(org).annotate(
        who=audit_log.member_shown("order__member__")
    )
    if query.since is not None:
        found = found.filter(order__created_at__gte=query.since)
    if query.until is not None:
        found = found.filter(order__created_at__lt=query.until)
    if query.state == ATTENTION:
        # One state, two reasons. A check that keeps failing, only once it has
        # lasted (payment_trouble.LASTING): a check that fails and recovers inside
        # 15 minutes is not the operator's business. Or a completed refund the
        # daily report does not show (refund_check).
        found = found.filter(
            Q(
                troubles__ended_at__isnull=True,
                troubles__since__lte=_now() - LASTING,
            )
            | Q(pk__in=refund_check.attention(org))
        ).distinct()
    elif query.state is not None:
        found = found.filter(state=query.state)
    if query.search:
        found = found.filter(
            Q(who__icontains=query.search)
            | Q(order__reference__icontains=query.search)
            | Q(provider_reference__icontains=query.search)
        )
    count = found.count()
    key = ("order__created_at", "order__number")
    order = tuple(f"-{name}" for name in key) if query.newest_first else key
    start = (query.page - 1) * PER_PAGE
    page = found.order_by(*order).values(
        "order__number",
        "order__reference",
        "order__created_at",
        "who",
        "state",
        "amount",
        "captured_amount",
        "refunded_amount",
        "currency",
        "provider_reference",
        "id",
    )[start : start + PER_PAGE]
    rows = list(page)
    open_spells = spells(org, [row["id"] for row in rows], open_only=True)
    cutoff = _now() - LASTING
    # A completed refund the daily report does not show .
    missing = refund_check.attention(org, [row["id"] for row in rows])
    troubles = {
        row["id"]: next(
            (
                _trouble(spell, zone)
                for spell in open_spells[row["id"]]
                if spell.since <= cutoff
            ),
            None,
        )
        for row in rows
    }
    return {
        **_paging(count, query.page),
        "payments": [
            {
                "number": row["order__number"],
                "order_reference": row["order__reference"],
                "created": _stamp(row["order__created_at"], zone),
                "member": row["who"],
                "state": row["state"],
                "state_label": PAYMENT_WORDS[row["state"]],
                "amount": row["amount"],
                "captured": row["captured_amount"],
                "refunded": row["refunded_amount"],
                "currency": row["currency"],
                "reference": row["provider_reference"],
                "trouble": troubles[row["id"]],
                "missing_refund": row["id"] in missing,
                "attention": troubles[row["id"]] is not None or row["id"] in missing,
            }
            for row in rows
        ],
        "states": [
            *({"value": k, "label": v} for k, v in PAYMENT_WORDS.items()),
            {"value": ATTENTION, "label": ATTENTION_WORDS},
        ],
    }


@dataclass(frozen=True)
class ScheduleQuery:
    when: str = "week"
    # The dates of "day" (first only) and "period".
    first: date | None = None
    last: date | None = None
    search: str = ""
    earliest_first: bool = True
    page: int = 1


def _now() -> datetime:
    return timezone.now()


def clock_day(prefix: str) -> Cast:
    """The date a taken time starts on, on the clock of its own asset;
    `prefix` reaches the taken time from another row, as in "taken_time__"."""
    return Cast(
        Func(
            F(prefix + "time_zone"),
            F(prefix + "starts_at"),
            function="timezone",
            output_field=DateTimeField(),
        ),
        DateField(),
    )


def schedule(org: Organization, query: ScheduleQuery) -> dict[str, object]:
    """The bookings that hold time, by their start. A day is the date a
    booking starts on the clock of its own asset; "today" is today on the
    organization's clock."""
    now = _now()
    today = now.astimezone(ZoneInfo(org.time_zone)).date()
    lines = (
        Booking.objects.of(org)
        .filter(
            taken_time__state__in=[BookingState.HELD, BookingState.CONFIRMED],
        )
        .annotate(
            starts=F("taken_time__starts_at"),
            ends=F("taken_time__ends_at"),
            clock=F("taken_time__time_zone"),
            day=clock_day("taken_time__"),
            who=audit_log.member_shown("member__"),
        )
    )
    match query.when:
        case "today":
            lines = lines.filter(day=today)
        case "week":
            lines = lines.filter(day__gte=today, day__lte=today + timedelta(days=6))
        case "coming":
            lines = lines.filter(ends__gt=now)
        case "past":
            lines = lines.filter(ends__lte=now)
        case "day":
            lines = lines.filter(day=query.first)
        case "period":
            lines = lines.filter(day__gte=query.first, day__lte=query.last)
    if query.search:
        lines = lines.filter(
            Q(who__icontains=query.search) | Q(order__reference__icontains=query.search)
        )
    count = lines.count()
    key = ("starts", "order__number", "pk")
    order = key if query.earliest_first else tuple(f"-{name}" for name in key)
    start = (query.page - 1) * PER_PAGE
    page = list(
        lines.order_by(*order).values(
            "pk",
            "starts",
            "ends",
            "clock",
            "who",
            "booking_category_name",
            "taken_time__state",
            "order__number",
            "order__reference",
        )[start : start + PER_PAGE]
    )
    totals = _totals(org, [row["pk"] for row in page])
    rows = []
    for row in page:
        state = BOOKING_STATE[row["taken_time__state"]]
        rows.append(
            {
                "id": row["pk"],
                **_times(row["starts"], row["ends"], row["clock"]),
                "member": row["who"],
                "category": row["booking_category_name"],
                "state": state,
                "state_label": state_words(state, totals.get(row["pk"], 0)),
                "booking": row["order__number"],
                "reference": row["order__reference"],
            }
        )
    return {**_paging(count, query.page), "lines": rows}


def _order_states(states: set[str]) -> str:
    """The order's state from its bookings' (the same rule as `bookings`)."""
    for state in ("held", "paid", "cancelled"):
        if state in states:
            return state
    return "expired"


def _price(line: BookingLine) -> dict[str, object]:
    """One price line as the record holds it. The record keeps the tier's
    "up to" but not whether tiers go by length or by time of day, nor how
    long one payment covers, so it does not word the tier itself."""
    return {
        "times": line.quantity,
        "amount": line.amount,
        "places": line.capacity_quantity,
        "unit": line.capacity_unit,
        "group": line.spot_group.name if line.spot_group is not None else None,
        "total": line.total,
    }


def booking(org: Organization, order: Order) -> dict[str, object]:
    """One booking's page: the member, the payment, the booking lines, the
    refunds and the history, from the records only."""
    zone = ZoneInfo(org.time_zone)
    held = (
        Booking.objects.of(org)
        .filter(order=order)
        .select_related("taken_time")
        .order_by("taken_time__starts_at", "pk")
    )
    times = list(held)
    prices: dict[int, list[BookingLine]] = defaultdict(list)
    for price in (
        BookingLine.objects.of(org)
        .filter(booking__order=order)
        .select_related("spot_group")
        .order_by("pk")
    ):
        prices[price.booking_id].append(price)
    payment = (
        Payment.objects.of(org).filter(order=order).select_related("payout").first()
    )
    refunds = (
        list(Refund.objects.of(org).filter(payment=payment).order_by("made_at", "pk"))
        if payment is not None
        else []
    )
    people = {
        pk: (name, email) for pk, name, email in _people(org, order, times, refunds)
    }
    currency = _currencies(org, [order.pk])[order.pk]
    states = {BOOKING_STATE[line.taken_time.state] for line in times}
    state = _order_states(states)
    by_id = {line.pk: line for line in times}

    def words(line: Booking) -> str:
        cells = _times(
            line.taken_time.starts_at,
            line.taken_time.ends_at,
            line.taken_time.time_zone,
        )
        return (
            f"{cells['start_date']} {cells['start_time']} to "
            f"{cells['end_date']} {cells['end_time']}"
        )

    def money(amount: int) -> str:
        return money_words(amount, currency)

    name = people[order.member_id][0]
    events: list[tuple[datetime, str]] = [
        (
            order.created_at,
            f"Booked by {name}: {len(times)} line{'' if len(times) == 1 else 's'}",
        )
    ]
    if state == "held":
        events.append((order.created_at, "Waiting for payment"))
    captured_at = None if payment is None else payment.captured_at
    if captured_at is not None:
        events.append((captured_at, f"Paid with Vipps, {money(order.total)}"))
    if state == "expired" and order.hold_expires_at is not None:
        stopped = payment is not None and payment.state == "cancelled"
        events.append(
            (
                order.hold_expires_at,
                "Payment stopped in Vipps"
                if stopped
                else "Not paid in time: the time was let go",
            )
        )
    refunded_for = {refund.refund_key for refund in refunds}
    mailed: dict[object, datetime] = {}
    for line in times:
        if line.cancelled_at is not None:
            by = people.get(line.cancelled_by_id or 0, ("an administrator",))[0]
            events.append((line.cancelled_at, f"{words(line)} cancelled by {by}"))
            # A cancel of an administrator (7.7) names its money: a paid line
            # it did not refund says so.
            if (
                line.cancellation_key is not None
                and captured_at is not None
                and any(price.total for price in prices[line.pk])
                and line.cancellation_key not in refunded_for
            ):
                events.append((line.cancelled_at, "Not refunded"))
        if line.cancel_mailed_at is not None:
            mailed[line.cancellation_key] = line.cancel_mailed_at
    for at in mailed.values():
        events.append((at, f"Cancellation email sent to {name}"))
    for refund in refunds:
        who = (
            "automatically"
            if refund.automatic
            else f"by {people[refund.made_by_id or 0][0]}"
        )
        events.append(
            (refund.made_at, f"Refund of {money(refund.amount)} requested {who}")
        )
        if refund.completed_at is not None:
            events.append(
                (refund.completed_at, f"Refunded {money(refund.amount)} through Vipps")
            )
    if order.confirmation_sent_at is not None:
        events.append(
            (order.confirmation_sent_at, f"Confirmation email sent to {name}")
        )
    # The payment check's trouble: every spell keeps a line when it began and,
    # once over, a line with its step, reason and tries; never one per try
    # (payment_trouble.py).
    spells_of = [] if payment is None else spells(org, [payment.pk])[payment.pk]
    for spell in spells_of:
        events.append((spell.since, "Payment check began failing"))
        if spell.ended_at is not None:
            events.append(
                (
                    spell.ended_at,
                    "Payment check works again "
                    f"({STEP_WORDS[spell.step]}, {KIND_WORDS[spell.kind]}, "
                    f"{spell.tries} {'try' if spell.tries == 1 else 'tries'})",
                )
            )
    events.sort(key=lambda event: event[0])

    now = _now()
    # The days of free cancellation the order accepted ; an order from
    # without recorded conditions has no acceptance moment either.
    accepted = order.conditions
    accepted_days = None if accepted is None else accepted.free_cancellation_days
    lines = []
    for line in times:
        line_state = BOOKING_STATE[line.taken_time.state]
        total = sum(price.total for price in prices[line.pk])
        lines.append(
            {
                "id": line.pk,
                # The moment free cancelling of this time ended: the days its
                # order accepted before the time starts, on the asset's clock,
                # to compare with the email asking to cancel it (the
                # administrator applies the rule; the product never does).
                "free_until": None
                if accepted_days is None
                else _moment(
                    booking_conditions.free_until(
                        line.taken_time.starts_at, accepted_days
                    ),
                    line.taken_time.time_zone,
                ),
                # Held or paid and not over: what a cancel can take (7.7).
                "coming": line_state in {"paid", "held"}
                and line.taken_time.ends_at > now,
                **_times(
                    line.taken_time.starts_at,
                    line.taken_time.ends_at,
                    line.taken_time.time_zone,
                ),
                "booking_type": line.booking_type_name,
                "category": line.booking_category_name,
                "state": line_state,
                "state_label": state_words(line_state, total),
                "amount": total,
                "prices": [_price(price) for price in prices[line.pk]],
            }
        )
    categories = list(dict.fromkeys(line.booking_category_name for line in times))
    return {
        "number": order.number,
        "reference": order.reference,
        "created": _stamp(order.created_at, zone),
        "state": state,
        "state_label": state_words(state, order.total),
        "amount": order.total,
        "currency": currency,
        "member": {
            "id": order.member_id,
            "name": name,
            "email": people[order.member_id][1],
            "booked_as": ", ".join(categories),
        },
        "payment": None
        if payment is None
        else {
            "state": payment.state,
            "state_label": PAYMENT_WORDS[payment.state],
            "amount": payment.amount,
            "captured": payment.captured_amount,
            "captured_at": None if captured_at is None else _stamp(captured_at, zone),
            "refunded": payment.refunded_amount,
            "reference": payment.provider_reference,
            **_flags(payment, spells_of, zone),
            # From the daily report; None until it has shown them .
            "fee": payment.fee_amount,
            "net": None
            if payment.fee_amount is None
            else payment.captured_amount - payment.fee_amount,
            "payout": None
            if payment.payout is None
            else {
                "id": payment.payout_id,
                "day": payment.payout.ledger_date.isoformat(),
            },
        },
        "lines": lines,
        "refunds": [
            {
                "id": refund.pk,
                "requested": _stamp(refund.made_at, zone),
                "completed": None
                if refund.completed_at is None
                else _stamp(refund.completed_at, zone),
                "amount": refund.amount,
                "line": None
                if refund.booking_id not in by_id
                else _times(
                    by_id[refund.booking_id].taken_time.starts_at,
                    by_id[refund.booking_id].taken_time.ends_at,
                    by_id[refund.booking_id].taken_time.time_zone,
                ),
                "by": None if refund.automatic else people[refund.made_by_id or 0][0],
                "automatic": refund.automatic,
                "reference": refund.provider_reference,
            }
            for refund in refunds
        ],
        "history": [{"at": _stamp(at, zone), "text": text} for at, text in events],
    }


def _people(
    org: Organization, order: Order, times: list[Booking], refunds: list[Refund]
) -> list[tuple[int, str, str]]:
    """The member, the administrators who cancelled and who refunded: id,
    name (or "Member 14" with none) and email, in one query."""
    ids = {order.member_id}
    ids.update(line.cancelled_by_id for line in times if line.cancelled_by_id)
    ids.update(refund.made_by_id for refund in refunds if refund.made_by_id)
    return list(
        Member.objects.of(org)
        .filter(pk__in=ids)
        .annotate(shown=audit_log.member_shown())
        .values_list("pk", "shown", "email")
    )
