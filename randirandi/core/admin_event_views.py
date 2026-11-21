# SPDX-License-Identifier: AGPL-3.0-only
"""The calls of admin's events .
Administrators of their own organization only (the gate of `admin_views`);
a change needs the page's CSRF token. Reading writes no event; the preview
is written nowhere. A refusal is 409 with its code (`event_admin`'s: a
field, "past", a reason, "busy", "settle"), and with what is in the way for a
reason; an event, day, asset or person of another organization is a 404."""

import re
from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.db import OperationalError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import admin_views, event_admin, event_pages
from randirandi.core.admin_setup_views import LARGEST_ID, _body, _gate
from randirandi.core.admin_views import BAD_REQUEST, NOT_FOUND
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.event_admin import EventSpec, OneOff, Repeat, Settle
from randirandi.core.models import Asset, EventDay, Member

LOCAL_MOMENT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}")
DAY_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CLOCK = re.compile(r"([0-9]{2}):([0-9]{2})")
FIELDS = {
    "category",
    "title",
    "description",
    "asset",
    "contact",
    "show_email",
    "show_phone",
    "when",
}
# The most dates one in-the-way read takes (the public holidays sheet).
MOST_DATES = 20


def _now() -> datetime:
    return timezone.now()


# --- What the body says ---------------------------------------------------------------


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("A text.")
    return value


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("A flag is true or false.")
    return value


def _id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= LARGEST_ID:
        raise ValueError("An id.")
    return value


def _moment(value: object) -> datetime:
    """A wall time, "2027-03-06T10:00", without a zone."""
    if not isinstance(value, str) or not LOCAL_MOMENT.fullmatch(value):
        raise ValueError("A moment is YYYY-MM-DDTHH:MM.")
    return datetime.fromisoformat(value)


def _date(value: object) -> date:
    if not isinstance(value, str) or not DAY_TEXT.fullmatch(value):
        raise ValueError("A date is YYYY-MM-DD.")
    return date.fromisoformat(value)


def _minutes(value: object) -> int:
    """A time of day, "HH:MM", as minutes; "24:00" is the end of the day."""
    found = CLOCK.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        raise ValueError("A time is HH:MM.")
    minutes = int(found[1]) * 60 + int(found[2])
    if int(found[2]) > 59 or minutes > 24 * 60:
        raise ValueError("A time of day.")
    return minutes


def _when(value: object) -> OneOff | Repeat:
    if not isinstance(value, dict):
        raise ValueError("When is an object.")
    if value.get("repeat") is False:
        if set(value) != {"repeat", "start", "end"}:
            raise ValueError("A one-off is a start and an end.")
        return OneOff(_moment(value["start"]), _moment(value["end"]))
    if value.get("repeat") is not True or set(value) != {
        "repeat",
        "weekdays",
        "first",
        "until",
        "from",
        "to",
    }:
        raise ValueError("A repeat is weekdays, dates and hours.")
    weekdays = value["weekdays"]
    if not isinstance(weekdays, list) or len(weekdays) > 7:
        raise ValueError("Weekdays are a list.")
    if not all(type(day) is int for day in weekdays):
        raise ValueError("A weekday is a number.")
    until = value["until"]
    return Repeat(
        frozenset(weekdays),
        _date(value["first"]),
        None if until is None else _date(until),
        _minutes(value["from"]),
        _minutes(value["to"]),
    )


def _settle(value: object) -> dict[int, Settle]:
    """What to do with other events' repeat days in the way, by the day's id:
    "remove", or {"from", "to"} for other hours."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > event_pages.MOST_PEOPLE * 100:
        raise ValueError("Settle is an object.")
    found: dict[int, Settle] = {}
    for key, how in value.items():
        if not isinstance(key, str) or not key.isdigit():
            raise ValueError("A day is its id.")
        pk = _id(int(key))
        if how == "remove":
            found[pk] = event_admin.REMOVE
        elif isinstance(how, dict) and set(how) == {"from", "to"}:
            found[pk] = Settle(_minutes(how["from"]), _minutes(how["to"]))
        else:
            raise ValueError("Remove the day or give it hours.")
    return found


def _spec(
    body: dict[str, object], *, preview: bool, administrator: Member
) -> tuple[EventSpec, dict[int, Settle]]:
    """The event asked for. A preview needs no title, category or contact
    yet: it fills in what it does not read."""
    extra = set(body) - FIELDS - {"settle", "event"}
    if extra or "when" not in body:
        raise ValueError("The body is wrong.")
    required = FIELDS - {"when"}
    if not preview and required - set(body):
        raise ValueError("The body is wrong.")
    asset = body.get("asset")
    given = body.get("contact")
    contact_id = 0 if given is None else _id(given)
    spec = EventSpec(
        category=_text(body.get("category", "social")),
        title=_text(body.get("title") or ("-" if preview else "")),
        description=_text(body.get("description", "")),
        contact_id=administrator.pk if preview else contact_id,
        when=_when(body["when"]),
        asset_id=None if asset is None else _id(asset),
        show_email=_flag(body.get("show_email", True)),
        show_phone=_flag(body.get("show_phone", False)),
    )
    return spec, _settle(body.get("settle"))


def _looked_up(member: Member, answer: dict[str, object]) -> bool:
    """An answer that names who booked is a look-up: one "listed bookings"
    event on the organization . False when the database was busy: nothing is shown."""
    if not event_pages.names_members(answer):
        return True
    try:
        with transaction.atomic():
            record_event(
                actor=member,
                action=AuditAction.LISTED_BOOKINGS,
                target=member.organization,
            )
    except OperationalError:
        return False
    return True


def _refused(member: Member, refused: event_admin.EventRefusedError) -> JsonResponse:
    answer: dict[str, object] = {"error": refused.code}
    if refused.in_way:
        answer["way"] = event_pages.refused_way(member.organization, refused.in_way)
        if not _looked_up(member, answer):
            return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(answer, status=409)


def _not_big(pk: int) -> bool:
    return not 1 <= pk <= LARGEST_ID


# --- Reading --------------------------------------------------------------------------


def _list_query(request: HttpRequest) -> event_pages.EventsQuery:
    """The filters of the Blocks' list (`admin_views._schedule_query`: `when`,
    `from`, `to`, `q`, `order` earliest or latest, `page` from 1) and the
    event's own: `category` (a key) and `asset` (an id or none)."""
    base = admin_views._schedule_query(request, event_pages.WHEN, "coming")
    asked = request.GET.get("asset", "all")
    asset: str | int = asked
    if asked not in {"all", "none"}:
        if not asked.isdigit() or _not_big(int(asked)):
            raise ValueError("asset is wrong.")
        asset = int(asked)
    return event_pages.EventsQuery(
        base.when,
        base.first,
        base.last or base.first,
        request.GET.get("category", "all"),
        asset,
        base.search,
        not base.earliest_first,
        base.page - 1,
    )


@never_cache
@login_required
@require_GET
def events_list(request: HttpRequest) -> HttpResponse:
    """One page of the events (25), each once, by its next day (`order` is
    `earliest` or `latest`): `when` coming, past, day or period (`from`, `to`),
    `category` (a key), `asset` (an id or none) and `q` (a title). An event
    with no contact person has `contact` null. Also the categories with the
    organization's words and the assets."""
    member = _gate(request)
    try:
        query = _list_query(request)
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    if query.category != "all" and query.category not in {
        row["key"] for row in event_pages.categories(member.organization)
    }:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(event_pages.listing(member.organization, query, _now()))


@never_cache
@login_required
@require_GET
def event_data(request: HttpRequest, event: int) -> HttpResponse:
    """One event: its fields, contact, maker, a repeat's rule with every day it
    has, and the days it skipped. Another organization's event is a 404."""
    member = _gate(request)
    if _not_big(event):
        return JsonResponse(NOT_FOUND, status=404)
    try:
        return JsonResponse(event_pages.event_data(member.organization, event, _now()))
    except event_admin.EventNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)


@never_cache
@login_required
@require_GET
def contacts(request: HttpRequest) -> HttpResponse:
    """The active members whose name holds `q`, at most twenty: who an event's
    contact can be."""
    member = _gate(request)
    search = request.GET.get("q", "").strip()
    if len(search) > admin_views.LONGEST_SEARCH:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse({"people": event_pages.people(member.organization, search)})


@never_cache
@login_required
@require_GET
def in_the_way(request: HttpRequest) -> HttpResponse:
    """The event days on an asset that a closure would be refused for: `asset`
    and either `first` and `last` or `dates` (comma-separated, at most 20, the
    public holidays). Each says whether it is a repeat's day, which can be
    removed in the same save."""
    member = _gate(request)
    org = member.organization
    try:
        asset_id = request.GET.get("asset", "")
        if not asset_id.isdigit() or _not_big(int(asset_id)):
            raise ValueError("asset is wrong.")
        if "dates" in request.GET:
            dates = [_date(item) for item in request.GET["dates"].split(",") if item]
            if not dates or len(dates) > MOST_DATES:
                raise ValueError("dates are wrong.")
            spans = [(item, item) for item in dates]
        else:
            first, last = (
                _date(request.GET.get("first")),
                _date(request.GET.get("last")),
            )
            if last < first or last - first > timedelta(days=366 * 2):
                raise ValueError("The dates are wrong.")
            spans = [(first, last)]
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    asset = Asset.objects.of(org).filter(pk=int(asset_id)).first()
    if asset is None:
        return JsonResponse(NOT_FOUND, status=404)
    return JsonResponse(
        {"days": event_pages.days_in_the_way(org, asset, spans, _now())}
    )


# --- Changing -------------------------------------------------------------------------


def _event_id(body: dict[str, object]) -> int | None:
    value = body.get("event")
    return None if value is None else _id(value)


@never_cache
@login_required
@require_POST
def preview(request: HttpRequest) -> HttpResponse:
    """What a make or a change would do, written nowhere: the body is the
    event as `make` takes it (a title, category and contact may still be
    missing), `event` the id of the event being changed, `settle` the ways
    out chosen so far. The answer: its time, what it leaves, what is in its
    way with the ways out (a one-off), or the days it takes and skips, with
    their reasons (a repeat)."""
    member = _gate(request)
    try:
        body = _body(request)
        spec, settle = _spec(body, preview=True, administrator=member)
        shown = event_admin.preview(
            member, spec, event_id=_event_id(body), settle=settle, now=_now()
        )
    except ValueError, UnicodeDecodeError, RecursionError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    except event_admin.EventNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except event_admin.EventRefusedError as refused:
        return _refused(member, refused)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    answer = event_pages.preview_data(member.organization, shown)
    if not _looked_up(member, answer):
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(answer)


@never_cache
@login_required
@require_POST
def make(request: HttpRequest) -> HttpResponse:
    """Make an event: the body is {"category", "title", "description",
    "asset" (an id or null), "contact" (a member's id), "show_email",
    "show_phone", "when", "settle"?}. The answer carries "made", its id. A
    one-off is refused for what is in its way (409 with the code and "way");
    a repeat skips such days. A contact of another organization is refused
    "contact"; an asset of another organization "asset"."""
    member = _gate(request)
    try:
        spec, settle = _spec(_body(request), preview=False, administrator=member)
        made = event_admin.make_event(member, spec, settle=settle, now=_now())
    except ValueError, UnicodeDecodeError, RecursionError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    except event_admin.EventRefusedError as refused:
        return _refused(member, refused)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse({"made": made.pk})


@never_cache
@login_required
@require_POST
def change(request: HttpRequest, event: int) -> HttpResponse:
    """Change an event: every field is given, as `make` takes them. The answer
    is the event as `event_data` reads it. Another organization's event is a
    404."""
    member = _gate(request)
    if _not_big(event):
        return JsonResponse(NOT_FOUND, status=404)
    try:
        spec, settle = _spec(_body(request), preview=False, administrator=member)
        event_admin.change_event(member, event, spec, settle=settle, now=_now())
    except ValueError, UnicodeDecodeError, RecursionError, OverflowError:
        return JsonResponse(BAD_REQUEST, status=400)
    except event_admin.EventNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except event_admin.EventRefusedError as refused:
        return _refused(member, refused)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(event_pages.event_data(member.organization, event, _now()))


@never_cache
@login_required
@require_POST
def remove(request: HttpRequest, event: int) -> HttpResponse:
    """Remove an event and free its time. Another organization's event is a
    404."""
    member = _gate(request)
    if _not_big(event):
        return JsonResponse(NOT_FOUND, status=404)
    try:
        event_admin.remove_event(member, event)
    except event_admin.EventNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except event_admin.EventRefusedError as refused:
        return _refused(member, refused)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse({"removed": event})


def _event_of_day(member: Member, day: int) -> int | None:
    return (
        EventDay.objects.of(member.organization)
        .filter(pk=day)
        .values_list("event_id", flat=True)
        .first()
    )


@never_cache
@login_required
@require_POST
def remove_day(request: HttpRequest, day: int) -> HttpResponse:
    """Remove one day of a repeat; it stays removed. The answer is the event.
    A one-off's day is 409 "one-off", one removed or over "removed" or
    "past"; another organization's day is a 404."""
    member = _gate(request)
    if _not_big(day) or (event := _event_of_day(member, day)) is None:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        _body(request)
        event_admin.remove_day(member, day, now=_now())
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    except event_admin.EventNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except event_admin.EventRefusedError as refused:
        return _refused(member, refused)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(event_pages.event_data(member.organization, event, _now()))


@never_cache
@login_required
@require_POST
def day_hours(request: HttpRequest, day: int) -> HttpResponse:
    """Give one day of a repeat hours of its own: {"from": "HH:MM", "to":
    "HH:MM"}, checked like any time. The answer is the event."""
    member = _gate(request)
    if _not_big(day) or (event := _event_of_day(member, day)) is None:
        return JsonResponse(NOT_FOUND, status=404)
    try:
        body = _body(request)
        if set(body) != {"from", "to"}:
            raise ValueError("The body is a start and an end.")
        event_admin.give_day_other_hours(
            member, day, _minutes(body["from"]), _minutes(body["to"]), now=_now()
        )
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    except event_admin.EventNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except event_admin.EventRefusedError as refused:
        return _refused(member, refused)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(event_pages.event_data(member.organization, event, _now()))
