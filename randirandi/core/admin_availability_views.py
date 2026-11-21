# SPDX-License-Identifier: AGPL-3.0-only
"""The calls of an asset's availability page . Administrators of
their own organization only (the gate of `admin_views`); a change needs the
page's CSRF token. See `availability_admin` for how a change is previewed,
saved and its bookings cancelled.

* GET data/: the page. GET month/?month=2026-11: the days of a month. GET
  holidays/?year=2026: a year's public holidays.
* POST apply/ {"kind": ..., fields, "preview": true}: what the change would
  cancel, nothing kept: {"preview": {bookings, members, refund, token, listed}}.
* POST apply/ {"kind": ..., fields, "expect": token}: save it and cancel and
  refund the bookings it strands, in the same transaction: {"saved": true,
  "cancelled": n, "unfinished": [{number, code}], "page": the page}; `page.owed`
  lists the refunds queued and not yet taken by the provider. "unfinished" is
  an order cancelled whose paid money could not be recorded as owed. A refusal
  is 409 with its code; "changed" carries the fresh "preview" because the
  bookings are not those of the token. A preview that lists bookings writes one
  "listed bookings" event (a look-up of who booked), the other answers none.
"""

import re
from datetime import date

from django.contrib.auth.decorators import login_required
from django.db import OperationalError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from randirandi.core import availability_admin
from randirandi.core.admin_setup_views import LARGEST_ID, _body, _gate
from randirandi.core.admin_views import BAD_REQUEST, NOT_FOUND
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import Asset, Member
from randirandi.core.setup_admin import SetupNotFoundError, SetupRefusedError

MONTH = re.compile(r"[0-9]{4}-[0-9]{2}")


def _asset(request: HttpRequest, pk: int) -> tuple[Member, Asset] | HttpResponse:
    member = _gate(request)
    if not 1 <= pk <= LARGEST_ID:
        return JsonResponse(NOT_FOUND, status=404)
    asset = Asset.objects.of(member.organization_id).filter(pk=pk).first()
    if asset is None:
        return JsonResponse(NOT_FOUND, status=404)
    return member, asset


@never_cache
@login_required
@require_GET
def data(request: HttpRequest, asset: int) -> HttpResponse:
    """The availability page of one asset."""
    found = _asset(request, asset)
    if isinstance(found, HttpResponse):
        return found
    member, row = found
    return JsonResponse(availability_admin.page(member.organization, row))


@never_cache
@login_required
@require_GET
def month(request: HttpRequest, asset: int) -> HttpResponse:
    """The days of a month ("month=2026-11"): open time, closed, week starts."""
    found = _asset(request, asset)
    if isinstance(found, HttpResponse):
        return found
    member, row = found
    text = request.GET.get("month", "")
    try:
        if not MONTH.fullmatch(text):
            raise ValueError("A month is YYYY-MM.")
        first = date.fromisoformat(f"{text}-01")
        if not 2000 <= first.year <= 2100:
            raise ValueError("The month is outside 2000 to 2100.")
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(availability_admin.month(member.organization, row, first))


@never_cache
@login_required
@require_GET
def holidays(request: HttpRequest, asset: int) -> HttpResponse:
    """A year's public holidays ("year=2026"), this year or the next."""
    found = _asset(request, asset)
    if isinstance(found, HttpResponse):
        return found
    member, row = found
    try:
        year = int(request.GET.get("year", ""))
        answer = availability_admin.holidays(member.organization, row, year)
    except ValueError:
        return JsonResponse(BAD_REQUEST, status=400)
    return JsonResponse(answer)


def _looked_up(administrator: Member, preview: dict[str, object]) -> bool:
    """A preview that names bookings and their members is a look-up of who
    booked: one "listed bookings" event on the organization ,
    written after the preview's own transaction was rolled back. A preview of
    no booking shows nobody and writes nothing. False when the database was
    busy: the preview is then not shown (it is not recorded)."""
    if not preview["listed"]:
        return True
    try:
        with transaction.atomic():
            record_event(
                actor=administrator,
                action=AuditAction.LISTED_BOOKINGS,
                target=administrator.organization,
            )
    except OperationalError:
        return False
    return True


@never_cache
@login_required
@require_POST
def apply(request: HttpRequest, asset: int) -> HttpResponse:
    """Preview or save a change. Refusals: "changed" (with the fresh
    "preview"), "past", "not-coming", "over", "date-taken", "ends-first",
    "invalid-span", "invalid-reason", "invalid", "busy"; a row of another
    asset or organization is a 404."""
    found = _asset(request, asset)
    if isinstance(found, HttpResponse):
        return found
    member, row = found
    try:
        body = _body(request)
        saved = availability_admin.apply(member, row.pk, body)
    except availability_admin.PreviewedError as previewed:
        if not _looked_up(member, previewed.preview):
            return JsonResponse({"error": "busy"}, status=409)
        return JsonResponse({"preview": previewed.preview})
    except availability_admin.ChangedSincePreviewError as changed:
        if not _looked_up(member, changed.preview):
            return JsonResponse({"error": "busy"}, status=409)
        return JsonResponse(
            {"error": changed.code, "preview": changed.preview}, status=409
        )
    except ValueError, UnicodeDecodeError, RecursionError:
        return JsonResponse(BAD_REQUEST, status=400)
    except SetupNotFoundError:
        return JsonResponse(NOT_FOUND, status=404)
    except SetupRefusedError as refused:
        refusal: dict[str, object] = {"error": refused.code}
        message = getattr(refused, "message", "")
        if message:
            refusal["message"] = message
        return JsonResponse(refusal, status=409)
    except OperationalError:
        return JsonResponse({"error": "busy"}, status=409)
    return JsonResponse(
        {
            "saved": True,
            "cancelled": saved.cancelled,
            "unfinished": list(saved.unfinished),
            "page": availability_admin.page(member.organization, row),
        }
    )
