# SPDX-License-Identifier: AGPL-3.0-only
"""The administrators' screens: the shell and its gate, and what the places ask
the server for. The places are empty until M7 parts 7.2 onward fill them."""

import json
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from randirandi.core import (
    audit_log,
    block_admin,
    booking_admin,
    calendar,
    member_admin,
    member_imports,
    member_pages,
    payment_adjustments,
    payout_admin,
)
from randirandi.core.admin import administrator
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import (
    add_member,
    change_member_email,
    correct_member,
    export_member,
)
from randirandi.core.member_import import import_members
from randirandi.core.models import (
    Asset,
    Booking,
    Member,
    MemberImport,
    Order,
    Organization,
    Payout,
)
from randirandi.core.network import client_address
from randirandi.core.organization_request import organization_for_request
from randirandi.core.payments import PaymentBusyError, PaymentUnavailableError
from randirandi.core.presentation import browser_context
from randirandi.core.second_factor import CodeRefusedError, reset_member_authenticator
from randirandi.member_parsers import MAX_UPLOAD, parser_by_id
from randirandi.payment_configuration import PaymentConfigurationError
from randirandi.vipps import VippsError

# The places in their groups; the keys and labels match
# frontend/src/admin/places.js.
PLACES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Daily",
        (
            ("bookings", "Bookings"),
            ("schedule", "Schedule"),
            ("blocks", "Blocks"),
            ("events", "Events"),
            ("members", "Members"),
            ("payments", "Payments"),
        ),
    ),
    (
        "Setup",
        (
            ("assets", "Assets and availability"),
            ("containers", "Containers"),
            ("types", "Booking types and categories"),
            ("roles", "Roles"),
            ("prices", "Price books"),
            ("organization", "Organization"),
        ),
    ),
    ("Records", (("audit", "Audit log"),)),
)
TITLES = {key: label for _, places in PLACES for key, label in places}


def confirmed_administrator(request: HttpRequest) -> Member | None:
    """The signed-in administrator of this address's organization who passed
    the second step; the role is read fresh from the database."""
    org = organization_for_request(request)
    member = administrator(request)
    if (
        member is None
        or member.organization_id != org.pk
        or request.session.get("admin_confirmed") is not True
    ):
        return None
    return member


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def index(request: HttpRequest) -> HttpResponse:
    return redirect("admin-place", place="bookings")


def _page(
    request: HttpRequest,
    place: str,
    booking: int | None = None,
    member: int | None = None,
    imports: bool = False,
    run: int | None = None,
    payouts: bool = False,
    payout: int | None = None,
    event: int | None = None,
) -> HttpResponse:
    """A place's page; with `booking` or `member`, the page of that booking or
    member (which the server finds when the page asks for it). With `imports`,
    the Members place's Imports tab, and with `run` one run's page. With
    `payouts`, the Payments place's Payouts tab, and with `payout` one payout's page.
    With `event`, the Events place with that event's page open."""
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    if place not in TITLES:
        raise Http404
    ui = {
        **browser_context(request, administrator.organization),
        "screen": "admin",
        "place": place,
        "booking": booking,
        "member": member,
        "imports": imports,
        "run": run,
        "payouts": payouts,
        "payout": payout,
        "event": event,
        "organization": administrator.organization.name,
        "time_zone": administrator.organization.time_zone,
        "name": administrator.full_name or administrator.first_name,
        "csrf": get_token(request),
    }
    return render(
        request,
        "core/admin.html",
        {"ui": ui, "title": TITLES[place], "places": PLACES, "place": place},
    )


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def place(request: HttpRequest, place: str) -> HttpResponse:
    return _page(request, place)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def event_page(request: HttpRequest, event: int) -> HttpResponse:
    return _page(request, "events", event=event)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def booking_page(request: HttpRequest, number: int) -> HttpResponse:
    return _page(request, "bookings", number)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def member_page(request: HttpRequest, member: int) -> HttpResponse:
    return _page(request, "members", member=member)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def imports_page(request: HttpRequest) -> HttpResponse:
    return _page(request, "members", imports=True)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def import_page(request: HttpRequest, number: int) -> HttpResponse:
    return _page(request, "members", imports=True, run=number)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def payouts_page(request: HttpRequest) -> HttpResponse:
    return _page(request, "payments", payouts=True)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def payout_page(request: HttpRequest, payout: int) -> HttpResponse:
    return _page(request, "payments", payouts=True, payout=payout)


DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NUMBER = re.compile(r"[1-9][0-9]{0,8}")
# The largest booking number asked for: nine digits, as NUMBER (an order's
# number is a 32-bit integer).
LAST_NUMBER = 999_999_999
BAD_REQUEST = {"error": "bad-request"}
NOT_FOUND = {"error": "not-found"}
EARLIEST, LATEST = date(2000, 1, 1), date(2100, 12, 31)
# The longest search; a name is at most 300 characters.
LONGEST_SEARCH = 100


def _day(request: HttpRequest, name: str) -> date | None:
    asked = request.GET
    if name not in asked:
        return None
    if not DATE.fullmatch(asked[name]):
        raise ValueError(f"{name} must be a date.")
    chosen = date.fromisoformat(asked[name])
    if not EARLIEST <= chosen <= LATEST:
        raise ValueError(f"{name} must be between 2000 and 2100.")
    return chosen


def _moment(
    request: HttpRequest, name: str, plus: int, zone: ZoneInfo
) -> datetime | None:
    """The start of the day `plus` days after the date asked for, in the zone."""
    chosen = _day(request, name)
    if chosen is None:
        return None
    return datetime.combine(chosen + timedelta(days=plus), time.min, tzinfo=zone)


def _whole(request: HttpRequest, name: str, default: str | None) -> int | None:
    asked = request.GET
    value = asked.get(name, default)
    if value is None:
        return None
    if not NUMBER.fullmatch(value):
        raise ValueError(f"{name} must be a positive whole number.")
    return int(value)


def _search(request: HttpRequest) -> str:
    asked = request.GET
    search = asked.get("q", "").strip()
    if len(search) > LONGEST_SEARCH:
        raise ValueError("q is too long.")
    return search


def _newest(request: HttpRequest) -> bool:
    """`order` is `newest` (left out) or `oldest`."""
    order = request.GET.get("order", "newest")
    if order not in {"newest", "oldest"}:
        raise ValueError("order must be newest or oldest.")
    return order == "newest"


def _members_query(request: HttpRequest) -> member_pages.MembersQuery:
    """The Members' filters from the query string; ValueError when one is
    wrong. `status` is one of the page's statuses; `role` a role's id; `q` the
    search; `sort` is `name` (left out), `unit` or `login`; `order` is `asc` or
    `desc` (left out: the sort's own first, `desc` for `login`); `page` counts
    from 1.
    """
    asked = request.GET
    status = asked.get("status")
    if status is not None and status not in member_pages.STATUS_WORDS:
        raise ValueError("status must be one of the page's statuses.")
    sort = asked.get("sort", "name")
    if sort not in member_pages.SORTS:
        raise ValueError("sort must be name, unit or login.")
    order = asked.get("order")
    if order not in {None, "asc", "desc"}:
        raise ValueError("order must be asc or desc.")
    return member_pages.MembersQuery(
        status=status,
        role=_whole(request, "role", None),
        search=_search(request),
        sort=sort,
        ascending=None if order is None else order == "asc",
        page=_whole(request, "page", "1") or 1,
    )


def _audit_query(request: HttpRequest, zone: ZoneInfo) -> audit_log.Query:
    """The log's filters from the query string; ValueError when one is wrong.

    `from` and `to` are dates in the organization's time zone (one day: both the
    same); `by` is an administrator's id; `action` one of the fixed actions;
    `q` the search; `order` is `newest` (left out) or `oldest`; `page` counts
    from 1.
    """
    asked = request.GET
    action = asked.get("action")
    if action is not None and action not in AuditAction.values:
        raise ValueError("action must be one of the fixed actions.")
    return audit_log.Query(
        since=_moment(request, "from", 0, zone),
        until=_moment(request, "to", 1, zone),
        by=_whole(request, "by", None),
        action=action,
        search=_search(request),
        newest_first=_newest(request),
        page=_whole(request, "page", "1") or 1,
    )


def _bookings_query(
    request: HttpRequest, zone: ZoneInfo
) -> booking_admin.BookingsQuery:
    """The Bookings' filters from the query string; ValueError when one is
    wrong. `from` and `to` are the dates the booking was created, in the
    organization's time zone (one day: both the same); `state` one of the
    page's states; `q` the member search; `member` one member's id (a member's
    page asks for their bookings); `sort` is `number` (left out) or `created`;
    `order` is `newest` (left out) or `oldest`; `page` counts from 1.
    """
    asked = request.GET
    state = asked.get("state")
    if state is not None and state not in booking_admin.STATE_WORDS:
        raise ValueError("state must be one of the page's states.")
    if asked.get("sort", "number") not in {"number", "created"}:
        raise ValueError("sort must be number or created.")
    return booking_admin.BookingsQuery(
        since=_moment(request, "from", 0, zone),
        until=_moment(request, "to", 1, zone),
        state=state,
        search=_search(request),
        by_number=asked.get("sort", "number") == "number",
        newest_first=_newest(request),
        page=_whole(request, "page", "1") or 1,
        member=_whole(request, "member", None),
    )


def _payments_query(
    request: HttpRequest, zone: ZoneInfo
) -> booking_admin.PaymentsQuery:
    """The Payments' filters from the query string; ValueError when one is
    wrong. `from` and `to` are the dates the booking was created, in the
    organization's time zone (one day: both the same); `state` one of the
    payment's states; `q` the member or reference search; `order` is `newest`
    (left out) or `oldest`; `page` counts from 1.
    """
    state = request.GET.get("state")
    if state is not None and state not in (
        *booking_admin.PAYMENT_WORDS,
        booking_admin.ATTENTION,
    ):
        raise ValueError("state must be one of the payment's states.")
    return booking_admin.PaymentsQuery(
        since=_moment(request, "from", 0, zone),
        until=_moment(request, "to", 1, zone),
        state=state,
        search=_search(request),
        newest_first=_newest(request),
        page=_whole(request, "page", "1") or 1,
    )


def _payouts_query(request: HttpRequest) -> payout_admin.PayoutsQuery:
    """The Payouts' filters from the query string; ValueError when one is
    wrong. `from` and `to` are the days paid out, both included (the wrong way
    round reads the same); `order` is `newest` (left out) or `oldest`; `page`
    counts from 1.
    """
    first, last = _day(request, "from"), _day(request, "to")
    if first is not None and last is not None:
        first, last = min(first, last), max(first, last)
    return payout_admin.PayoutsQuery(
        first=first,
        last=last,
        newest_first=_newest(request),
        page=_whole(request, "page", "1") or 1,
    )


def _schedule_query(
    request: HttpRequest,
    whens: tuple[str, ...] = booking_admin.WHEN,
    default: str = "week",
) -> booking_admin.ScheduleQuery:
    """The Schedule's filters from the query string; ValueError when one is
    wrong. `when` is one of the "Booked for" periods (`week` left out); `day`
    needs `from`, `period` needs `from` and `to` (the wrong way round reads
    the same), both dates on the booked asset's clock; `q` the member search;
    `order` is `earliest` or `latest` (left out: latest for `past`, else
    earliest); `page` counts from 1. The Blocks' list reads it with its own
    periods (`whens`, `default`).
    """
    asked = request.GET
    when = asked.get("when", default)
    if when not in whens:
        raise ValueError("when must be one of the periods.")
    first, last = _day(request, "from"), _day(request, "to")
    if when == "day" and first is None:
        raise ValueError("from is needed for one day.")
    if when == "period":
        if first is None or last is None:
            raise ValueError("from and to are needed for a period.")
        first, last = min(first, last), max(first, last)
    order = asked.get("order", "latest" if when == "past" else "earliest")
    if order not in {"earliest", "latest"}:
        raise ValueError("order must be earliest or latest.")
    return booking_admin.ScheduleQuery(
        when=when,
        first=first,
        last=last,
        search=_search(request),
        earliest_first=order == "earliest",
        page=_whole(request, "page", "1") or 1,
    )


def _listed(
    administrator: Member, action: AuditAction, page: Callable[[], dict[str, object]]
) -> HttpResponse:
    """A list that names who booked or a member's contact details: showing a
    page is a look-up, one event on the organization in the transaction of the
    read, so the page is not answered without it . The page is
    read first: the event takes the organization's lock, held only from there
    to the commit, so a list read does not wait out other admin work."""
    with transaction.atomic():
        found = page()
        record_event(
            actor=administrator, action=action, target=administrator.organization
        )
        return JsonResponse(found)


@never_cache
@login_required
@require_GET
def audit_events(request: HttpRequest) -> HttpResponse:
    """One page of the organization's audit events, filtered and sorted, with
    the administrators and actions the filters offer . Reading
    writes no event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        query = _audit_query(request, ZoneInfo(org.time_zone))
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(audit_log.answer(org, query))


@never_cache
@login_required
@require_GET
def bookings(request: HttpRequest) -> HttpResponse:
    """One page of the organization's bookings (the orders), filtered and
    sorted . Showing a page is a look-up: one "listed bookings" event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        query = _bookings_query(request, ZoneInfo(org.time_zone))
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return _listed(
        member, AuditAction.LISTED_BOOKINGS, lambda: booking_admin.bookings(org, query)
    )


@never_cache
@login_required
@require_GET
def payments(request: HttpRequest) -> HttpResponse:
    """One page of the organization's payments, one per order that has one,
    filtered and sorted . Showing a page is a look-up: one
    "listed payments" event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        query = _payments_query(request, ZoneInfo(org.time_zone))
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return _listed(
        member, AuditAction.LISTED_PAYMENTS, lambda: booking_admin.payments(org, query)
    )


@never_cache
@login_required
@require_GET
def payouts_list(request: HttpRequest) -> HttpResponse:
    """One page of the organization's payouts : days, counts and
    amounts, no member, so reading it writes no event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    try:
        query = _payouts_query(request)
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(payout_admin.payouts(member.organization, query))


@never_cache
@login_required
@require_GET
def payout_data(request: HttpRequest, payout: int) -> HttpResponse:
    """One payout, read for its page . The page names who booked
    each payment, so opening it is a look-up: one "listed payouts" event on the
    organization, in the transaction of the read. A payout of another
    organization is a 404, and a 404 writes no event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        payments_page = _whole(request, "payments_page", "1") or 1
        refunds_page = _whole(request, "refunds_page", "1") or 1
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    with transaction.atomic():
        found = (
            Payout.objects.of(org).filter(pk=payout, aborted_at__isnull=True).first()
            if 1 <= payout <= 2**62
            else None
        )
        if found is None:
            return JsonResponse(NOT_FOUND, status=404)
        answer = payout_admin.payout(
            org, found, payments_page=payments_page, refunds_page=refunds_page
        )
        record_event(actor=member, action=AuditAction.LISTED_PAYOUTS, target=org)
        return JsonResponse(answer)


@never_cache
@login_required
@require_GET
def schedule(request: HttpRequest) -> HttpResponse:
    """One page of the bookings that hold time, by their start .
    Showing a page is a look-up: one "listed schedule" event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    try:
        query = _schedule_query(request)
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return _listed(
        member,
        AuditAction.LISTED_SCHEDULE,
        lambda: booking_admin.schedule(member.organization, query),
    )


@never_cache
@login_required
@require_GET
def booking_data(request: HttpRequest, number: int) -> HttpResponse:
    """One booking, read for its page . Opening a page is
    looking someone up: it writes one "inspected" event on the order, in the
    same transaction as the read.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    with transaction.atomic():
        found = (
            Order.objects.of(org).filter(number=number).first()
            if 1 <= number <= LAST_NUMBER
            else None
        )
        if found is None:
            return JsonResponse(NOT_FOUND, status=404)
        record_event(actor=member, action=AuditAction.INSPECT, target=found)
        return JsonResponse(booking_admin.booking(org, found))


@never_cache
@login_required
@require_GET
def members(request: HttpRequest) -> HttpResponse:
    """One page of the organization's members, filtered and sorted . The list answers 25
    at a time and has no way to read them all.
    Showing a page is a look-up: one "listed members" event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    try:
        query = _members_query(request)
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return _listed(
        member,
        AuditAction.LISTED_MEMBERS,
        lambda: member_pages.members(member.organization, query),
    )


@never_cache
@login_required
@require_GET
def member_data(request: HttpRequest, member: int) -> HttpResponse:
    """One member, read for their page . Opening a page is
    looking someone up: it writes one "inspected" event on the member, in the
    same transaction as the read. A member of another organization is a 404.
    """
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    org = administrator.organization
    with transaction.atomic():
        found = (
            Member.objects.of(org).select_related("role").filter(pk=member).first()
            if 1 <= member <= 2**62
            else None
        )
        if found is None:
            return JsonResponse(NOT_FOUND, status=404)
        record_event(actor=administrator, action=AuditAction.INSPECT, target=found)
        return JsonResponse(member_pages.member(org, found, administrator))


@never_cache
@login_required
@require_GET
def member_imports_list(request: HttpRequest) -> HttpResponse:
    """One page of the organization's member imports, newest first, with the
    parsers an administrator can pick . Reading writes no event."""
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    try:
        page = _whole(request, "page", "1") or 1
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(member_imports.imports(member.organization, page))


@never_cache
@login_required
@require_GET
def member_import_data(request: HttpRequest, number: int) -> HttpResponse:
    """One run, read for its page . It names the members the run
    disabled, so opening it writes one "inspected" event on the run, in the
    same transaction as the read. A run of another organization is a 404."""
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    org = administrator.organization
    with transaction.atomic():
        found = member_imports.run_page(org, number) if 1 <= number <= 2**31 else None
        if found is None:
            return JsonResponse(NOT_FOUND, status=404)
        record_event(
            actor=administrator,
            action=AuditAction.INSPECT,
            target=MemberImport.objects.of(org).get(pk=found["id"]),
        )
        return JsonResponse(found)


def _file_name(raw: str) -> str:
    """The name of an uploaded file: no folder and no control character (the
    run keeps at most its first 255 characters)."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", raw.replace("\\", "/").rsplit("/", 1)[-1])
    return name.strip()


@never_cache
@login_required
@require_POST
def member_import_start(request: HttpRequest) -> HttpResponse:
    """Start an import . The body is the file itself, at most
    `MAX_UPLOAD` bytes; the query names the parser (`parser`, its id) and the
    file (`name`). The file is read in memory and never stored; the run keeps
    its name and size. Reading, reconciling and the safety stop are
    `import_members`; the answer is the run's page. A refusal before a run is
    400 "bad-request", "unknown-parser" or "wrong-file-type", or 413
    "file-too-large"; a run that cannot read the file is a failed run (200).
    """
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    org = administrator.organization
    parser = parser_by_id(request.GET.get("parser", ""))
    name = _file_name(request.GET.get("name", ""))
    if not name:
        return JsonResponse(BAD_REQUEST, status=400)
    if parser is None:
        return JsonResponse({"error": "unknown-parser"}, status=400)
    if not name.lower().endswith(parser.accept):
        return JsonResponse({"error": "wrong-file-type"}, status=400)
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    if declared > MAX_UPLOAD:
        return JsonResponse({"error": "file-too-large"}, status=413)
    data = request.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return JsonResponse({"error": "file-too-large"}, status=413)
    try:
        run = import_members(
            org,
            parser,
            parser.read(data),
            started_by=administrator,
            file_name=name[:255],
            file_size=len(data),
        )
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    number = MemberImport.objects.of(org).filter(pk__lte=run.pk).count()
    found = member_imports.run_page(org, number)
    return JsonResponse(found)


def _empty_body(request: HttpRequest) -> None:
    """ValueError unless the body is empty or an empty object."""
    if request.body.strip() and json.loads(request.body) != {}:
        raise ValueError("The body must be empty.")


def _change_status(
    request: HttpRequest,
    member: int,
    change: Callable[..., Member],
) -> HttpResponse:
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    org = administrator.organization
    try:
        _empty_body(request)
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    if not 1 <= member <= 2**62:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        changed = change(actor=administrator, member_id=member)
    except Member.DoesNotExist:
        return JsonResponse(NOT_FOUND, status=404)
    except ValidationError as refused:
        return JsonResponse({"error": refused.code}, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    changed = Member.objects.of(org).select_related("role").get(pk=changed.pk)
    return JsonResponse(member_pages.member(org, changed, administrator))


@never_cache
@login_required
@require_POST
def member_block(request: HttpRequest, member: int) -> HttpResponse:
    """Block a member , with no body. The answer is the member's
    page after the block (it writes no "inspected" event). A refusal is 409
    with its code ("self", "already-blocked" or "busy"); a member of another
    organization is a 404. A second press finds the member blocked, so it is
    refused and writes nothing.
    """
    return _change_status(request, member, member_admin.block_member)


@never_cache
@login_required
@require_POST
def member_unblock(request: HttpRequest, member: int) -> HttpResponse:
    """Unblock a member , as the block. A refusal is 409 with
    its code ("not-blocked" or "busy")."""
    return _change_status(request, member, member_admin.unblock_member)


def _fresh_body(
    request: HttpRequest, allowed: set[str], required: set[str]
) -> dict[str, object]:
    """The JSON object of a member call: the keys of `required`, any of the
    rest of `allowed`, and "code", the authenticator code (spaces left out).
    ValueError when the body is anything else."""
    body = json.loads(request.body)
    if (
        not isinstance(body, dict)
        or set(body) - allowed - {"code"}
        or (required | {"code"}) - set(body)
        or not isinstance(body["code"], str)
    ):
        raise ValueError("The body is wrong.")
    body["code"] = "".join(body["code"].split())
    return body


def _fresh_member_call(
    request: HttpRequest,
    member: int,
    allowed: set[str],
    required: set[str],
    run: Callable[[Member, dict[str, object], str], Member | dict[str, object]],
) -> HttpResponse:
    """The common part of the calls that ask for a fresh authenticator code
    (part 7.92): the gate, the body, the member (another organization's is a
    404), and the refusals as 409 with their codes ("code-refused" and
    "code-limited" for the code, the service's own for the rest, "busy" when
    the database could not serve the call)."""
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    org = administrator.organization
    try:
        body = _fresh_body(request, allowed, required)
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    if not 1 <= member <= 2**62:
        return JsonResponse(NOT_FOUND, status=404)
    if not Member.objects.of(org).filter(pk=member).exists():
        return JsonResponse(NOT_FOUND, status=404)
    try:
        done = run(administrator, body, client_address(request))
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    except Member.DoesNotExist:
        return JsonResponse(NOT_FOUND, status=404)
    except CodeRefusedError as refused:
        code = "code-limited" if refused.limited else "code-refused"
        return JsonResponse({"error": code}, status=409)
    except ValidationError as refused:
        return JsonResponse({"error": refused.code or "invalid"}, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    if isinstance(done, dict):
        response = JsonResponse(done, json_dumps_params={"indent": 2})
        response["Content-Disposition"] = f'attachment; filename="member-{member}.json"'
        return response
    changed = Member.objects.of(org).select_related("role").get(pk=done.pk)
    return JsonResponse(member_pages.member(org, changed, administrator))


@never_cache
@login_required
@require_POST
def member_add(request: HttpRequest) -> HttpResponse:
    """Add a member . The body is {"full_name": <text>, "email":
    <address>, "phone": <text, empty or left out for none>, "role": <a role's
    id>, "code": <the authenticator code>}. The answer is the new member's
    page (no "inspected" event), status 201. A refusal is 409 with its code:
    the code's ("code-refused", "code-limited"), "invalid-name",
    "invalid-email", "email-taken", "invalid-phone", "phone-taken",
    "unknown-role" or "busy".
    """
    administrator = confirmed_administrator(request)
    if administrator is None:
        raise PermissionDenied
    org = administrator.organization
    try:
        body = _fresh_body(
            request,
            {"full_name", "email", "phone", "role"},
            {"full_name", "email", "role"},
        )
        texts = (body.get(k, "") for k in ("full_name", "email", "phone"))
        if not all(isinstance(v, str) for v in texts) or type(body["role"]) is not int:
            raise ValueError("The body is wrong.")
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    try:
        added = add_member(
            actor=administrator,
            full_name=str(body["full_name"]),
            email=str(body["email"]),
            phone=str(body.get("phone", "")),
            role_id=int(str(body["role"])),
            code=str(body["code"]),
            network=client_address(request),
        )
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    except CodeRefusedError as refused:
        code = "code-limited" if refused.limited else "code-refused"
        return JsonResponse({"error": code}, status=409)
    except ValidationError as refused:
        return JsonResponse({"error": refused.code or "invalid"}, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    found = Member.objects.of(org).select_related("role").get(pk=added.pk)
    return JsonResponse(member_pages.member(org, found, administrator), status=201)


@never_cache
@login_required
@require_POST
def member_correct(request: HttpRequest, member: int) -> HttpResponse:
    """Correct a member's name, phone and role . The body is
    {"full_name": <text>, "phone": <text, empty for none>, "role": <a role's
    id>, "code": <the authenticator code>}, any of the first three left out
    to leave it, at least one given. The answer is the member's page after the
    change (no "inspected" event). A refusal is 409 with its code: the code's
    ("code-refused", "code-limited"), "invalid-name", "invalid-phone",
    "phone-taken", "unknown-role", "own-role", "last-administrator",
    "erased" or "busy".
    """

    def run(who: Member, body: dict[str, object], network: str) -> Member:
        changes = {key: body[key] for key in ("full_name", "phone") if key in body}
        role = body.get("role")
        if not all(isinstance(v, str) for v in changes.values()) or (
            role is not None and type(role) is not int
        ):
            raise ValueError("The body is wrong.")
        if not changes and role is None:
            raise ValueError("The body is wrong.")
        return correct_member(
            actor=who,
            member_id=member,
            changes={k: str(v) for k, v in changes.items()},
            role_id=role if type(role) is int else None,
            code=str(body["code"]),
            network=network,
        )

    return _fresh_member_call(
        request, member, {"full_name", "phone", "role"}, set(), run
    )


@never_cache
@login_required
@require_POST
def member_email(request: HttpRequest, member: int) -> HttpResponse:
    """Change a member's email . The body is {"email": <the new
    address>, "code": <the authenticator code>}. The old address is sent a
    notice with the next security notices. The answer is the member's page
    after the change. A refusal is 409 with its code: the code's
    ("code-refused", "code-limited"), "invalid-email", "email-taken",
    "unchanged", "erased", "blocked" or "busy".
    """

    def run(who: Member, body: dict[str, object], network: str) -> Member:
        if not isinstance(body["email"], str):
            raise ValueError("The body is wrong.")
        return change_member_email(
            actor=who,
            member_id=member,
            email=body["email"],
            code=str(body["code"]),
            network=network,
        )

    return _fresh_member_call(request, member, {"email"}, {"email"}, run)


@never_cache
@login_required
@require_POST
def member_reset(request: HttpRequest, member: int) -> HttpResponse:
    """Reset another administrator's authenticator . The body
    is {"code": <the authenticator code>}. The answer is the member's page
    after the reset. The member signs in next with the email code and sets the
    authenticator up again; every administrator is emailed. A refusal is 409
    with its code: the code's ("code-refused", "code-limited"), "self",
    "not-administrator" or "busy".
    """

    def run(who: Member, body: dict[str, object], network: str) -> Member:
        return reset_member_authenticator(
            actor=who, member_id=member, code=str(body["code"]), network=network
        )

    return _fresh_member_call(request, member, set(), set(), run)


@never_cache
@login_required
@require_POST
def member_export(request: HttpRequest, member: int) -> HttpResponse:
    """Export a member's data . The body is {"code": <the
    authenticator code>}. The answer is the export as a download,
    `member-<id>.json`. A refusal is 409 with the code's ("code-refused",
    "code-limited") or "busy".
    """

    def run(who: Member, body: dict[str, object], network: str) -> dict[str, object]:
        return export_member(
            actor=who, member_id=member, code=str(body["code"]), network=network
        )

    return _fresh_member_call(request, member, set(), set(), run)


def _cancel_body(request: HttpRequest) -> tuple[uuid.UUID, int | None, bool]:
    """The cancel's request key, the booking line asked for (none: the whole
    booking) and whether to refund; ValueError when the body is wrong."""
    body = json.loads(request.body)
    if not isinstance(body, dict) or set(body) - {"key", "line", "refund"}:
        raise ValueError("The body must be a request key, a line and a refund.")
    key, line, refund = body.get("key"), body.get("line"), body.get("refund")
    if not isinstance(key, str) or type(refund) is not bool:
        raise ValueError("key must be a text and refund yes or no.")
    if line is not None and (type(line) is not int or not 1 <= line <= 2**62):
        raise ValueError("line must be a whole number.")
    return uuid.UUID(key), line, refund


@never_cache
@login_required
@require_POST
def booking_cancel(request: HttpRequest, number: int) -> HttpResponse:
    """Cancel a whole booking or one of its lines, with or without the refund
    . The body is {"key": <a new request key for each press>,
    "line": <a line's id, or null for the booking>, "refund": true|false}; a
    repeat of a key answers as the first did. The answer is the booking's
    page, read after the cancel (it writes no "inspected" event). A refusal
    is 409 with its code (`payment_adjustments.CODES`, or "busy"); a
    booking or line of another organization is a 404.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        key, line, refund = _cancel_body(request)
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    found = (
        Order.objects.of(org).filter(number=number).first()
        if 1 <= number <= LAST_NUMBER
        else None
    )
    public_id = None
    if line is not None and found is not None:
        public_id = (
            Booking.objects.of(org)
            .filter(order=found, pk=line)
            .values_list("public_id", flat=True)
            .first()
        )
        if public_id is None:
            found = None
    if found is None:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        payment_adjustments.cancel(
            administrator=member,
            order_public_id=found.public_id,
            request_key=key,
            booking_public_id=public_id,
            refund=refund,
        )
    except PaymentBusyError:
        return JsonResponse({"error": "busy"}, status=409)
    except payment_adjustments.CancelRefusedError as refused:
        status = 404 if refused.code == "not-found" else 409
        return JsonResponse({"error": refused.code}, status=status)
    except PaymentUnavailableError:
        return JsonResponse({"error": "unavailable"}, status=409)
    except PaymentConfigurationError, VippsError:
        return JsonResponse({"error": "payment-unavailable"}, status=503)
    return JsonResponse(booking_admin.booking(org, found))


@never_cache
@login_required
@require_GET
def blocks(request: HttpRequest) -> HttpResponse:
    """One page of the blocks, by their start : `when` is one of
    the "Blocked for" periods (`coming` left out), the rest as the Schedule's.
    Reading writes no event.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    try:
        query = _schedule_query(request, block_admin.WHEN, "coming")
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(block_admin.blocks(member.organization, query))


# A stretch is typed on the asset's clock, as the browser's own picker writes it.
LOCAL_MOMENT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}")


def _local_moment(value: object, zone: ZoneInfo) -> datetime:
    """A moment on the asset's clock, "2027-03-01T10:00"; ValueError when it is
    not one, is outside 2000 to 2100 or falls in the hour a clock change
    skips. In the hour a change repeats it is the first."""
    if not isinstance(value, str) or not LOCAL_MOMENT.fullmatch(value):
        raise ValueError("A moment is YYYY-MM-DDTHH:MM.")
    wall = datetime.fromisoformat(value)
    if not EARLIEST <= wall.date() <= LATEST:
        raise ValueError("A moment must be between 2000 and 2100.")
    found = calendar._instants(wall, zone)
    if not found:
        raise ValueError("That time does not exist on the asset's clock.")
    return found[0]


def _asset_of(org: Organization, asset: object) -> Asset | None:
    if type(asset) is not int and not (
        isinstance(asset, str) and NUMBER.fullmatch(asset)
    ):
        raise ValueError("asset must be a positive whole number.")
    number = int(asset)
    if not 1 <= number <= 2**62:
        raise ValueError("asset must be a positive whole number.")
    return Asset.objects.of(org).filter(pk=number).first()


@never_cache
@login_required
@require_GET
def blocks_lay(request: HttpRequest) -> HttpResponse:
    """What a stretch would be laid as , written nowhere: the
    pieces with their times and booking types, the bookings holding any of the
    time, and whether a block does. `asset`, `start` and `end` (on the
    asset's clock, "2027-03-01T10:00") are asked for; an asset of another
    organization is a 404.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        asset = _asset_of(org, request.GET.get("asset"))
        if asset is None:
            return JsonResponse(NOT_FOUND, status=404)
        start = _local_moment(request.GET.get("start"), asset.zone)
        end = _local_moment(request.GET.get("end"), asset.zone)
        laid = block_admin.preview(asset, start, end)
    except ValueError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(block_admin.laid_words(asset, laid))


def _make_body(request: HttpRequest) -> tuple[dict[str, object], str]:
    """The body of a make and its note; ValueError when it is wrong."""
    body = json.loads(request.body)
    if not isinstance(body, dict) or set(body) - {"asset", "start", "end", "note"}:
        raise ValueError("The body must be an asset, a start, an end and a note.")
    note = body.get("note", "")
    if not isinstance(note, str) or len(note.strip()) > block_admin.LONGEST_NOTE:
        raise ValueError("note must be a text of at most 500 characters.")
    return body, note.strip()


@never_cache
@login_required
@require_POST
def blocks_make(request: HttpRequest) -> HttpResponse:
    """Make the blocks of a stretch . The body is {"asset": <id>,
    "start": "2027-03-01T10:00", "end": ..., "note": <text, left out for
    none>}, the moments on the asset's clock. All the blocks of the stretch
    are made, or none; the answer lists their ids. A refusal is
    409 with its code (`block_admin.CODES`, or "busy"), the bookings in
    the way for "booked" and the events for "event"; an asset of another
    organization is a 404.
    """
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    org = member.organization
    try:
        body, note = _make_body(request)
        asset = _asset_of(org, body.get("asset"))
        if asset is None:
            return JsonResponse(NOT_FOUND, status=404)
        start = _local_moment(body.get("start"), asset.zone)
        end = _local_moment(body.get("end"), asset.zone)
        made = block_admin.make(member, asset.pk, start, end, note)
    except ValueError, UnicodeDecodeError, RecursionError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    except block_admin.BlockNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except block_admin.BlockRefusedError as refused:
        return JsonResponse(
            {
                "error": refused.code,
                "booked": refused.booked,
                "events": [{"id": pk, "title": title} for pk, title in refused.events],
            },
            status=409,
        )
    return JsonResponse({"blocks": [block.pk for block in made]})


@never_cache
@login_required
@require_POST
def blocks_remove(request: HttpRequest, block: int) -> HttpResponse:
    """Remove a block that has not ended, one under way too ;
    its time is free at once. A block that is over is 409 "past"; a block of
    another organization is a 404."""
    member = confirmed_administrator(request)
    if member is None:
        raise PermissionDenied
    if not 1 <= block <= 2**62:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        block_admin.remove(member, block)
    except block_admin.BlockNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except block_admin.BlockRefusedError as refused:
        return JsonResponse({"error": refused.code}, status=409)
    return JsonResponse({"removed": block})
