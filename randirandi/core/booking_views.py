# SPDX-License-Identifier: AGPL-3.0-only
"""What the member booking page asks the server for .

JSON for the logged-in member only. Every answer is computed from the member
the session names; nothing a browser sends widens what a member may see.
"""

import json
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import (
    booking_calendar,
    booking_cart,
    booking_checkout,
    booking_conditions,
    booking_outcome,
    booking_words,
    payments,
)
from randirandi.core.booking_choices import booking_choices, choose_category
from randirandi.core.booking_quote import Choice, QuoteInputError, quote
from randirandi.core.calendar import MAX_DAYS, CalendarLimitError
from randirandi.core.member_calendar import (
    ContainerPrices,
    GroupColumn,
    MemberCalendar,
    TierPrice,
    member_calendar,
)
from randirandi.core.models import (
    Asset,
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingType,
    Member,
    Order,
    Organization,
)
from randirandi.core.organization_request import organization_for_request
from randirandi.limits import MOST_SPOTS
from randirandi.payment_configuration import PaymentConfigurationError
from randirandi.vipps import VippsError

NUMBER = re.compile(r"[1-9][0-9]{0,8}")
NUMBERS = re.compile(r"[1-9][0-9]{0,8}(,[1-9][0-9]{0,8})*")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MOMENT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}"
)
ROW = re.compile(r"[0-9a-f]{16}")
LARGEST = 999_999_999


def _signed_in(request: HttpRequest) -> tuple[Member, Organization] | None:
    org = organization_for_request(request)
    user = request.user
    if not isinstance(user, Member) or user.organization_id != org.pk:
        return None
    return user, org


def _number(request: HttpRequest, name: str, default: str | None = None) -> int:
    value = request.GET.get(name, default)
    if value is None or not NUMBER.fullmatch(value):
        raise ValueError(f"{name} must be a positive whole number.")
    return int(value)


def _maybe(request: HttpRequest, name: str) -> int | None:
    """A positive whole number, or None when the query leaves it out."""
    return _number(request, name) if name in request.GET else None


def _pick(request: HttpRequest) -> tuple[int, ...]:
    """The spot numbers picked, comma-separated; none for the lowest free."""
    value = request.GET.get("spots")
    if value is None:
        return ()
    if not NUMBERS.fullmatch(value) or value.count(",") >= MOST_SPOTS:
        raise ValueError("spots must be spot numbers, comma-separated.")
    return tuple(int(number) for number in value.split(","))


def _date(request: HttpRequest, name: str) -> date:
    value = request.GET.get(name, "")
    if not DATE.fullmatch(value):
        raise ValueError(f"{name} must be a date.")
    return date.fromisoformat(value)


def _moment(request: HttpRequest, name: str) -> datetime:
    value = request.GET.get(name, "")
    if not MOMENT.fullmatch(value):
        raise ValueError(f"{name} must be a moment with its UTC offset.")
    return datetime.fromisoformat(value)


def _beside(request: HttpRequest, member: Member) -> list[Choice] | None:
    """The cart, less the booking `editing` names; None when that is gone."""
    editing = request.GET.get("editing")
    if editing is not None and not ROW.fullmatch(editing):
        raise ValueError("editing must name a booking in the cart.")
    return booking_cart.without(request.session, member, editing)


NOT_IN_CART = {"error": "not-in-cart"}


def choices_payload(member: Member) -> dict[str, object]:
    """What `/booking/choices/` answers for this member; the page embeds it too."""
    found = booking_choices(member)
    return {
        "categories": [
            {"id": row.id, "name": row.name, "icon": row.icon}
            for row in found.categories
        ],
        "category": found.category,
        "assets": [
            {
                "id": asset.id,
                "name": asset.name,
                "description": asset.description,
                "max_quantity": asset.max_quantity,
                "space": asset.space_id,
                "types": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "description": row.description,
                        "categories": list(row.category_ids),
                        "quantity_for": list(row.quantity_category_ids),
                        "containers": [
                            {
                                "id": container.id,
                                "number": container.number,
                                "with_payment": container.with_payment,
                                "categories": list(container.category_ids),
                                "max_quantity": container.max_quantity,
                                "period_type": container.period_type,
                                "consumption_mode": container.consumption_mode,
                                "spot_word": container.spot_word,
                                "interval": container.interval,
                                "minimum_length": container.minimum_length,
                                "maximum_length": container.maximum_length,
                                "groups": [
                                    {
                                        "id": group.id,
                                        "number": group.number,
                                        "name": group.name,
                                        "word": group.word,
                                        "spots": group.spots,
                                        "plan": group.plan,
                                    }
                                    for group in container.groups
                                ],
                            }
                            for container in row.containers
                        ],
                    }
                    for row in asset.types
                ],
            }
            for asset in found.assets
        ],
        "spaces": [
            {
                "id": space.id,
                "name": space.name,
                "booking_page": space.booking_page,
            }
            for space in found.spaces
        ],
    }


@never_cache
@require_GET
def choices(request: HttpRequest) -> HttpResponse:
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    return JsonResponse(choices_payload(signed_in[0]))


@never_cache
@require_POST
def choices_category(request: HttpRequest) -> HttpResponse:
    """Keep the category the member chose in the Filter (form field
    `category`). The page posts it only when the member changes the Filter,
    never when a link chooses a category. 404 "not-offered" for a category
    the member's role cannot book, 204 when it is kept.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    value = request.POST.get("category", "")
    if not NUMBER.fullmatch(value):
        return JsonResponse(BAD_REQUEST, status=400)
    if not choose_category(signed_in[0], int(value)):
        return JsonResponse({"error": "not-offered"}, status=404)
    return HttpResponse(status=204)


@never_cache
@require_GET
def calendar(request: HttpRequest) -> HttpResponse:
    """The booking calendar of one asset (`asset`) or one space (`space`),
    per container, for `days` local dates from `start`.

    `type` and `category` filter the containers; `quantity` (1 when left
    out) filters the shared ones; `group` asks for one group of spots, whose
    spots only are answered ; `length`, in minutes, left out, is each
    flexible container's own minimum. Moments carry
    the organization's UTC offset, so a row repeated when the clocks go back
    is told apart. Too many days for one answer is 422: the page asks for
    fewer. Times the cart needs are no bookable start; when the member
    changes a booking in the cart, `editing` names it. With `prices=1`  the answer is
    the organization's standard role's: its
    categories and prices, whatever the signed-in member's own role holds; no
    cart counts, and an organization without a standard role gets none.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    member, org = signed_in
    return calendar_answer(
        request, member, org, price_list=request.GET.get("prices") == "1"
    )


# The most days the public price list asks for: two weeks hold every weekday of
# every container (`frontend/src/book/PricesPage.jsx`).
PUBLIC_DAYS = 14


def calendar_answer(
    request: HttpRequest,
    member: Member,
    org: Organization,
    *,
    price_list: bool,
    public: bool = False,
) -> HttpResponse:
    """The calendar's answer for `member`. `public` : the price list read
    without signing in, for a stand-in member of the standard role
    (`public_prices`); nothing of any member is in it, no taken time, no name,
    no mark of an own booking, and it asks for at most `PUBLIC_DAYS` days."""
    if price_list and org.standard_role_id is None:
        return JsonResponse(
            _calendar(MemberCalendar(timezone.now(), ()), ZoneInfo(org.time_zone)),
            json_dumps_params={"separators": (",", ":")},
        )
    try:
        start = _date(request, "start")
        cart = [] if price_list else _beside(request, member)
        if cart is None:
            return JsonResponse(NOT_IN_CART, status=404)
        # Past the limit is 422 however far; a date past year 9999 is
        # never worked out.
        days = min(_number(request, "days"), MAX_DAYS + 1)
        if public and days > PUBLIC_DAYS:
            raise CalendarLimitError("The price list asks for fewer days.")
        found = member_calendar(
            member=member,
            asset_id=_maybe(request, "asset"),
            space_id=_maybe(request, "space"),
            booking_type_id=_maybe(request, "type"),
            booking_category_id=_maybe(request, "category"),
            quantity=_number(request, "quantity", "1"),
            group_id=_maybe(request, "group"),
            length=_maybe(request, "length"),
            start_date=start,
            end_date=start + timedelta(days=days),
            cart=cart,
            role_id=org.standard_role_id if price_list else None,
        )
    except CalendarLimitError:
        return JsonResponse({"error": "fewer-days"}, status=422)
    except ValueError, OverflowError:
        return JsonResponse({"error": "bad-request"}, status=400)
    return JsonResponse(
        # Every time in the zone of the asset or space asked for.
        _calendar(found, ZoneInfo(found.time_zone or org.time_zone), public=public),
        json_dumps_params={"separators": (",", ":")},
    )


def _group(group: GroupColumn) -> dict[str, Any]:
    return {
        "id": group.id,
        "number": group.number,
        "name": group.name,
        "word": group.word,
    }


def _tiers(tiers: tuple[TierPrice, ...]) -> list[dict[str, int]]:
    return [
        {"up_to": tier.up_to, "paid_per": tier.paid_per, "amount": tier.amount}
        for tier in tiers
    ]


def _prices(prices: ContainerPrices | None) -> dict[str, Any] | None:
    """What the container costs in each category, in the smallest unit of the
    currency; the page words it. None when the container takes no payment."""
    if prices is None:
        return None
    return {
        "currency": prices.currency,
        "categories": [
            {
                "category": row.category_id,
                "billing_basis": row.billing_basis,
                "tiers_go_by": row.tiers_go_by,
                "tiers": _tiers(row.tiers),
                "groups": [
                    {
                        "group": group.group,
                        "tiers_go_by": group.tiers_go_by,
                        "tiers": _tiers(group.tiers),
                    }
                    for group in row.groups
                ],
            }
            for row in prices.categories
        ],
    }


def _calendar(
    found: MemberCalendar, zone: ZoneInfo, *, public: bool = False
) -> dict[str, Any]:
    def local(moment: datetime) -> str:
        return moment.astimezone(zone).isoformat()

    return {
        "worked_out_at": local(found.worked_out_at),
        "containers": [
            {
                "id": row.id,
                "number": row.number,
                "asset": {
                    "id": row.asset_id,
                    "name": row.asset_name,
                    "maximum_occupancy": row.maximum_occupancy,
                },
                "type": {"id": row.booking_type_id, "name": row.booking_type_name},
                "categories": list(row.category_ids),
                "period_type": row.period_type,
                "consumption_mode": row.consumption_mode,
                "with_payment": row.with_payment,
                "prices": _prices(row.prices),
                "interval": row.interval,
                "minimum_length": row.minimum_length,
                "maximum_length": row.maximum_length,
                "floor_plan": None
                if row.floor_plan is None
                else {
                    "name": row.floor_plan.name,
                    "spot_word": booking_words.spot_word(row.floor_plan.spot_word),
                    "groups": [_group(group) for group in row.floor_plan.groups],
                    "spots": [
                        {
                            "id": spot.id,
                            "number": spot.number,
                            "name": spot.name,
                            "group": None if spot.group is None else spot.group.id,
                        }
                        for spot in row.floor_plan.spots
                    ],
                },
                "max_quantity": row.max_quantity,
                "periods": [
                    {
                        "day": period.day.isoformat(),
                        "rows": [local(moment) for moment in period.rows],
                        "end": local(period.ends_at),
                        "row_ends": [local(moment) for moment in period.row_ends],
                        "closed_rows": list(period.closed_rows),
                        "taken": [
                            {
                                "spot": span.spot,
                                "first": span.first,
                                "last": span.last,
                                "state": span.state,
                                "mine": span.mine,
                                # Another member's full name, only on a booking, and
                                # only when it has one.
                                **({"name": span.name} if span.name else {}),
                                # An event's title, S9: only for an event.
                                **(
                                    {
                                        "event": {
                                            "id": span.event.id,
                                            "title": span.event.title,
                                        }
                                    }
                                    if span.event is not None
                                    else {}
                                ),
                            }
                            for span in ([] if public else period.taken)
                        ],
                        "starts": [
                            {
                                "row": start.row,
                                "end": local(start.ends_at),
                                "spots": list(start.spots),
                                "free": [
                                    {"group": part.group, "spots": list(part.spots)}
                                    for part in start.free
                                ],
                            }
                            for start in period.starts
                        ],
                        # Only the categories with hours of their own on the
                        # container; the others have its hours ().
                        "category_hours": [
                            {
                                "category": item.category_id,
                                "spans": [
                                    [local(first), local(last)]
                                    for first, last in item.spans
                                ],
                            }
                            for item in period.category_hours
                        ],
                    }
                    for period in row.periods
                ],
            }
            for row in found.containers
        ],
        # What an empty day can offer instead of the category asked for.
        "other_categories": [
            {"day": item.day.isoformat(), "categories": list(item.category_ids)}
            for item in found.other_categories
        ],
        # The assets asked for and when each is closed: `by` is "closure"
        # (with the reason's key, "" for none) or "weekly" (its weekly
        # availability does not open that day). Nothing else of a closure.
        "assets": list(found.asset_ids),
        "closed_days": [
            {
                "asset": row.asset_id,
                "day": row.day.isoformat(),
                "by": row.by,
                "reason": row.reason,
            }
            for row in found.closed_days
        ],
    }


@never_cache
@require_GET
def price(request: HttpRequest) -> HttpResponse:
    """The price of one bookable start from the calendar, with its booking
    lines.

    The page sends the container the calendar answered it in, the category,
    the quantity, maybe the spots picked (`spots`, numbers comma-separated)
    or the group of spots asked for  and the moments
    exactly as the calendar gave them, URL-encoded (a bare "+" in an offset
    reads as a space). A time no longer free, a picked spot that is not, or a
    time that would not fit beside the cart, says so; nothing is reserved.
    When the member changes a booking in the cart, `editing` names it.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    try:
        container, category = (
            _number(request, name) for name in ("container", "category")
        )
        quantity = _number(request, "quantity", "1")
        start, end = _moment(request, "start"), _moment(request, "end")
        spots = _pick(request)
        group = _maybe(request, "group")
        cart = _beside(request, signed_in[0])
    except ValueError:
        return JsonResponse({"error": "bad-request"}, status=400)
    if cart is None:
        return JsonResponse(NOT_IN_CART, status=404)
    try:
        found = quote(
            member=signed_in[0],
            configuration_id=container,
            booking_category_id=category,
            quantity=quantity,
            starts_at=start,
            ends_at=end,
            cart=cart,
            spots=spots,
            group_id=group,
        )
    except QuoteInputError:
        return JsonResponse({"error": "bad-request"}, status=400)
    if found.reason is not None:
        refused: dict[str, str | int] = {
            "available": False,
            "reason": found.reason,
            "text": booking_words.text(found.reason, org=signed_in[1]),
        }
        if found.places_left is not None:
            refused["places_left"] = found.places_left
        return JsonResponse(refused)
    return JsonResponse(
        {
            "available": True,
            "container": found.configuration_id,
            "period_type": found.period_type,
            "consumption_mode": found.consumption_mode,
            "quantity": found.quantity,
            "tiers_go_by": found.tiers_go_by,
            "currency": found.currency,
            "total": found.total,
            "lines": [
                {
                    "up_to": line.up_to,
                    "paid_per": line.paid_per,
                    "times_paid": line.paid_units,
                    "amount": line.unit_price,
                    "capacity_quantity": line.times,
                    "capacity_unit": line.times_unit,
                    "billing_basis": line.billing_basis,
                    "group": line.group_id,
                    "total": line.total,
                }
                for line in found.lines
            ],
        }
    )


def _named(
    model: type[Asset | BookingType | BookingCategory], org: Organization, ids: set[int]
) -> dict[int, str]:
    return dict(model.objects.of(org).filter(pk__in=ids).values_list("pk", "name"))


def _cart(request: HttpRequest, member: Member, org: Organization) -> HttpResponse:
    found = booking_cart.look(request.session, member)
    choices = [item.row.choice for item in found.items]
    containers = {
        pk: (number, asset, kind, word, ZoneInfo(zone))
        for pk, number, asset, kind, word, zone in BookingConfiguration.objects.of(org)
        .filter(pk__in={choice.configuration_id for choice in choices})
        .values_list(
            "pk",
            "number",
            "asset_id",
            "booking_type_id",
            "floor_plan__unit",
            "asset__time_zone",
        )
    }
    assets = _named(Asset, org, {row[1] for row in containers.values()})
    kinds = _named(BookingType, org, {row[2] for row in containers.values()})
    categories = _named(
        BookingCategory, org, {choice.booking_category_id for choice in choices}
    )
    # The zone of a booking not found, and of an order without bookings.
    zone = ZoneInfo(org.time_zone)
    made = found.order
    held_bookings: list[dict[str, Any]] = []
    ordered: list[Booking] = []
    hold_on = zone
    if made is not None:
        # The member's own order (the cart finds it by the member's session
        # and rows), read by the same function as the order read.
        mine = (
            Order.objects.of(org)
            .filter(public_id=made.public_id, member_id=member.pk)
            .first()
        )
        if mine is not None:
            # Each booking as the order read gives it, with its price: the
            # cart's own rows are not priced while their order holds them.
            ordered = booking_outcome.ordered_bookings(mine)
            if ordered:
                hold_on = ZoneInfo(ordered[0].taken_time.time_zone)
            held_bookings = [
                booking_outcome.booking_item(org, booking, placed)
                for booking, placed in zip(
                    ordered,
                    booking_outcome.container_and_spots(org, ordered),
                    strict=True,
                )
            ]
    bookings = []
    for item, choice in zip(found.items, choices, strict=True):
        number, asset, kind, word, own_zone = containers.get(
            choice.configuration_id, (None, None, None, None, zone)
        )
        bookings.append(
            {
                "id": item.row.id,
                "container": {"id": choice.configuration_id, "number": number},
                "asset": {"id": asset, "name": assets.get(asset or 0)},
                "type": {"id": kind, "name": kinds.get(kind or 0)},
                "category": {
                    "id": choice.booking_category_id,
                    "name": categories.get(choice.booking_category_id),
                },
                "quantity": choice.quantity,
                "spots": list(choice.spots),
                # The floor plan's word for them ("seat", "bed"); null for a
                # container without a floor plan.
                "spot_word": booking_words.spot_word(word),
                "group": choice.group_id,
                "start": choice.starts_at.astimezone(own_zone).isoformat(),
                "end": choice.ends_at.astimezone(own_zone).isoformat(),
                "state": item.state,
                "text": booking_words.text(item.state, org=org),
                "total": item.quote.total if item.quote.reason is None else None,
                "seen": item.row.seen,
                "places_left": item.quote.places_left,
            }
        )
    return JsonResponse(
        {
            "count": len(found.items),
            "limit": booking_cart.MAX_ROWS,
            # The currency of the paid bookings, one for the cart; the
            # organization's for a cart of free ones.
            "currency": booking_outcome.currency_of(org, ordered)
            if held_bookings
            else found.currency or org.currency,
            "total": found.total,
            "ready": found.ready,
            "locked": found.locked,
            # The booking conditions in force: the identifier the order call
            # carries as `accepted_version` .
            "conditions": {"version": booking_conditions.current(org).digest},
            "order": None
            if made is None
            else {
                "id": str(made.public_id),
                "reference": made.reference,
                "state": made.state,
                "total": made.total,
                "hold_expires_at": made.hold_expires_at.astimezone(hold_on).isoformat()
                if made.hold_expires_at is not None
                else None,
                # Each booking with its container, the spots it holds and its
                # price; the cart's own rows answer only the member's pick.
                "bookings": held_bookings,
            },
            "bookings": bookings,
        }
    )


def _posted(request: HttpRequest) -> tuple[Choice, int]:
    """The booking a page posts as JSON, with the total it showed: the
    container, category, quantity, start and end, and maybe the spots
    picked."""
    body = json.loads(request.body)
    if not isinstance(body, dict):
        raise ValueError("The booking must be a JSON object.")

    def number(name: str, default: int | None = None, least: int = 1) -> int:
        value = body.get(name, default)
        if type(value) is not int or not least <= value <= LARGEST:
            raise ValueError(f"{name} must be a whole number.")
        return value

    def moment(name: str) -> datetime:
        value = body.get(name)
        if not isinstance(value, str) or not MOMENT.fullmatch(value):
            raise ValueError(f"{name} must be a moment with its UTC offset.")
        return datetime.fromisoformat(value)

    spots = body.get("spots", [])
    if (
        not isinstance(spots, list)
        or len(spots) > MOST_SPOTS
        or any(type(value) is not int or not 1 <= value <= LARGEST for value in spots)
    ):
        raise ValueError("spots must be spot numbers.")
    group = body.get("group")
    if group is not None and (type(group) is not int or not 1 <= group <= LARGEST):
        raise ValueError("group must be a whole number.")
    choice = Choice(
        number("container"),
        number("category"),
        number("quantity", 1),
        moment("start"),
        moment("end"),
        tuple(spots),
        group,
    )
    return choice, number("total", least=0)


def _refused(refusal: booking_cart.Refusal, org: Organization) -> HttpResponse:
    body: dict[str, str | int] = {
        "reason": refusal.reason,
        "text": booking_words.text(
            refusal.reason,
            org=org,
            **({} if refusal.left is None else {"left": refusal.left}),
            **({} if refusal.limit is None else {"limit": refusal.limit}),
            **({"in_cart": True} if refusal.in_cart else {}),
        ),
    }
    if refusal.total is not None:
        body["total"] = refusal.total
    if refusal.left is not None:
        body["left"] = refusal.left
    if refusal.limit is not None:
        body["limit"] = refusal.limit
    return JsonResponse(body, status=409)


BAD_REQUEST = {"error": "bad-request"}


@never_cache
@require_GET
def cart(request: HttpRequest) -> HttpResponse:
    """The member's cart, every booking priced again beside the others.

    A booking's state is "ok", "taken", "does-not-fit-beside-cart",
    "already-started", "not-offered", "other-currency" or "price-changed"; with
    each state but "ok" comes its default English text, `text`. "ready" is true
    when every booking is ok.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    return _cart(request, *signed_in)


@never_cache
@require_POST
def cart_add(request: HttpRequest) -> HttpResponse:
    """Put a time from the calendar in the cart, at the total the page showed.

    A refusal is 409 with its reason and the reason's default English text:
    "cart-full", "taken", "does-not-fit-beside-cart", "already-started",
    "not-offered", "other-currency", or "price-changed" with the total today.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    try:
        choice, seen = _posted(request)
        done = booking_cart.add(request.session, signed_in[0], choice, seen)
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    if isinstance(done, booking_cart.Refusal):
        return _refused(done, signed_in[1])
    return _cart(request, *signed_in)


@never_cache
@require_POST
def cart_change(request: HttpRequest, row_id: str) -> HttpResponse:
    """Replace one booking in the cart; a refusal leaves it as it was."""
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    if not ROW.fullmatch(row_id):
        return JsonResponse(NOT_IN_CART, status=404)
    try:
        choice, seen = _posted(request)
        done = booking_cart.change(request.session, signed_in[0], row_id, choice, seen)
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    if done is None:
        return JsonResponse(NOT_IN_CART, status=404)
    if isinstance(done, booking_cart.Refusal):
        return _refused(done, signed_in[1])
    return _cart(request, *signed_in)


@never_cache
@require_POST
def cart_remove(request: HttpRequest, row_id: str) -> HttpResponse:
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    if not ROW.fullmatch(row_id):
        return JsonResponse(NOT_IN_CART, status=404)
    done = booking_cart.remove(request.session, signed_in[0], row_id)
    if isinstance(done, booking_cart.Refusal):
        return _refused(done, signed_in[1])
    if not done:
        return JsonResponse(NOT_IN_CART, status=404)
    return _cart(request, *signed_in)


@never_cache
@require_POST
def cart_empty(request: HttpRequest) -> HttpResponse:
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    done = booking_cart.empty(request.session, signed_in[0])
    if done is not None:
        return _refused(done, signed_in[1])
    return _cart(request, *signed_in)


@never_cache
@require_POST
def cart_order(request: HttpRequest) -> HttpResponse:
    """Make one order from the cart at the total the page shows, then start
    its payment.

    The page posts {"total": ..., "accepted_version": ...}: the total it
    showed and the identifier of the booking conditions the cart showed
    . The server is the gate: an order without it, or with another than
    the text in force, is refused (409 "conditions-not-accepted" or
    "conditions-changed"), also an order with nothing to pay. A refusal is
    409 with its reason; the page looks at the cart again to see which
    bookings are marked, or, after "busy" (a second press) and "order-held",
    the order held; a "busy" from other orders made at the same moment
    leaves no order, and the member presses again. A free order
    is confirmed at once. A paid one answers with the address at Vipps; when
    Vipps cannot be reached the order stays held and the page tries again
    with the payment start (`/payments/start/<order>/`). Like every cart
    change, the page sends it alone; a request beside it that saves an older
    session cannot bring the ordered bookings back, because the cart finds
    its order from the bookings' cart rows.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    member = signed_in[0]
    try:
        body = json.loads(request.body)
        total = body.get("total") if isinstance(body, dict) else None
        if type(total) is not int or not 0 <= total <= LARGEST:
            raise ValueError("total must be a whole number.")
        accepted = body.get("accepted_version")
        if not isinstance(accepted, str) or len(accepted) > 200:
            accepted = None
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    # The order saves the session inside its lock, before Vipps is called,
    # so a request that dies there still leaves the held order on the cart
    # page and a second press sees the order made.
    done = booking_checkout.order(
        request.session, member, total, accepted_version=accepted
    )
    if isinstance(done, booking_cart.Refusal):
        return _refused(done, signed_in[1])
    made = {"order": str(done.public_id)}
    if not done.total:
        return JsonResponse({**made, "confirmed": True})
    try:
        started = payments.checkout(member=member, order_public_id=done.public_id)
    except payments.PaymentUnavailableError, payments.PaymentBusyError:
        return JsonResponse({**made, "reason": "payment-unavailable"}, status=409)
    except PaymentConfigurationError, VippsError:
        return JsonResponse({**made, "error": "payment-unavailable"}, status=503)
    if started is None:
        return JsonResponse({**made, "confirmed": True})
    return JsonResponse({**made, "redirect": started.redirect_url})


@never_cache
@require_POST
def order_cancel(request: HttpRequest, order_public_id: uuid.UUID) -> HttpResponse:
    """Let go of the member's order waiting for payment ; the answer is the cart, its
    times back
    in it. An order that does not exist and another member's are the same
    404. A refusal is 409: "confirmed" (paid first) or "settling" (try
    again).
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    done = booking_checkout.cancel(signed_in[0], order_public_id)
    if done is False:
        return JsonResponse({"error": "not-found"}, status=404)
    if isinstance(done, booking_cart.Refusal):
        return _refused(done, signed_in[1])
    return _cart(request, *signed_in)


@never_cache
@require_GET
def order_outcome(request: HttpRequest, order_public_id: uuid.UUID) -> HttpResponse:
    """What became of one order, for the member who made it .

    An order that does not exist and another member's are the same answer:
    404 with one fixed body. When the cart's order is this one, the read
    applies the cart's clean-up, so a confirmed order's rows leave the cart.
    """
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    member, org = signed_in
    found = booking_outcome.read(org, member, order_public_id)
    if found is None:
        return JsonResponse({"error": "not-found"}, status=404)
    booking_cart.settle(request.session, member, order_public_id)
    return JsonResponse(found)


@never_cache
@require_GET
def order_calendar(request: HttpRequest, order_public_id: uuid.UUID) -> HttpResponse:
    """The member's confirmed order as a calendar file . An order that does not exist,
    another
    member's and one that is not confirmed are the same 404."""
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    member, org = signed_in
    found = (
        Order.objects.of(org)
        .filter(public_id=order_public_id, member_id=member.pk)
        .first()
    )
    made = None
    if found is not None and booking_cart.order_state(found) == booking_cart.CONFIRMED:
        made = booking_calendar.calendar_file(org, found)
    if found is None or made is None:
        return JsonResponse({"error": "not-found"}, status=404)
    response = HttpResponse(made, content_type="text/calendar; charset=utf-8")
    name = booking_calendar.file_name(org, found)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@never_cache
@require_GET
def member_bookings(request: HttpRequest) -> HttpResponse:
    """The signed-in member's own bookings, the coming ones first . Nothing of another
    member appears."""
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    member, org = signed_in
    return JsonResponse(booking_outcome.bookings(org, member))
