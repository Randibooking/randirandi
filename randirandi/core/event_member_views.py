# SPDX-License-Identifier: AGPL-3.0-only
"""The members' events call : the signed-in members of the
organization read its events, never who made one or what only an
administrator sees. A signed-out visitor, and a member of another
organization, get 403; nothing is written."""

import re
from datetime import date, datetime

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from randirandi.core import event_members
from randirandi.core.booking_views import _signed_in
from randirandi.core.event_members import MembersQuery

BAD_REQUEST = {"error": "bad-request"}


def _now() -> datetime:
    return timezone.now()


DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
ID = re.compile(r"[1-9][0-9]{0,8}")


def _query(request: HttpRequest) -> MembersQuery:
    """`when` (coming or past), or a period (`since` and `until`, both);
    `category` (keys, comma-separated); `asset` (an id or none); `event` (one
    event). Anything else is a ValueError."""
    get = request.GET
    when = get.get("when", "coming")
    if when not in event_members.WHEN:
        raise ValueError("when")
    since, until = get.get("since"), get.get("until")
    if (since is None) != (until is None):
        raise ValueError("period")
    first = last = None
    if since is not None and until is not None:
        if not DAY.fullmatch(since) or not DAY.fullmatch(until):
            raise ValueError("period")
        first, last = date.fromisoformat(since), date.fromisoformat(until)
        if last < first or (last - first).days > 400:
            raise ValueError("period")
    keys = tuple(key for key in get.get("category", "").split(",") if key)
    if len(keys) > 4 or not event_members.valid_categories(keys):
        raise ValueError("category")
    asset = get.get("asset", "all")
    if asset not in ("all", "none") and not ID.fullmatch(asset):
        raise ValueError("asset")
    only = get.get("event")
    if only is not None and not ID.fullmatch(only):
        raise ValueError("event")
    return MembersQuery(
        when,
        first,
        last,
        keys,
        asset if asset in ("all", "none") else int(asset),
        None if only is None else int(only),
    )


@never_cache
@require_GET
def events(request: HttpRequest) -> HttpResponse:
    """The organization's events for the Events page and its Filter."""
    signed_in = _signed_in(request)
    if signed_in is None:
        return HttpResponse(status=403)
    try:
        query = _query(request)
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(
        event_members.listing(signed_in[1], query, _now()),
        json_dumps_params={"separators": (",", ":")},
    )
