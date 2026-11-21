# SPDX-License-Identifier: AGPL-3.0-only
"""The Members place's Imports tab as the admin pages read it .

The runs of an organization's member lists, newest first, and one run's page:
its outcome, its five counts, the note, the members it disabled with any
booking still to come, the rows to check and the rows skipped. Every answer is
read only. A run's number is its place among the organization's runs, oldest
first, as the member pages count them. The report holds row numbers, ids and
reason codes only; the page adds the names of the members it disabled, and
nothing from the file.
"""

from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from randirandi.core import audit_log
from randirandi.core.booking_admin import PER_PAGE, _paging, _stamp
from randirandi.core.models import (
    Booking,
    BookingState,
    ImportOutcome,
    Member,
    MemberImport,
    Organization,
)
from randirandi.member_parsers import PARSERS, parser_named

OUTCOME_WORDS = {
    ImportOutcome.APPLIED.value: "Applied",
    ImportOutcome.STOPPED.value: "Stopped",
    ImportOutcome.FAILED.value: "Failed",
}
# A finding that does not skip its row: the row was applied, and the
# administrator is asked to look at it. Every other finding skipped its row.
CHECK_CODES = frozenset({"people_count_differs"})


def parsers() -> list[dict[str, str]]:
    return [
        {"id": p.id, "name": p.name, "reads": p.reads, "accept": p.accept}
        for p in PARSERS
    ]


def run_numbers(org: Organization) -> dict[int, int]:
    return {
        pk: number
        for number, pk in enumerate(
            MemberImport.objects.of(org).order_by("pk").values_list("pk", flat=True),
            start=1,
        )
    }


def _parser_name(identifier: str) -> str:
    found = parser_named(identifier)
    return found.name if found is not None else ""


def _counts(run: MemberImport) -> list[int]:
    # "Active again" is the members who were disabled and are active again; a
    # new member also counts as granted access, and is counted as new.
    return [
        run.created,
        run.changed,
        max(run.granted - run.created, 0),
        run.revoked,
        run.skipped,
    ]


def _names(org: Organization, ids: list[int]) -> dict[int, str]:
    return dict(
        Member.objects.of(org)
        .annotate(shown=audit_log.member_shown())
        .filter(pk__in=ids)
        .values_list("pk", "shown")
    )


def _row(
    run: MemberImport, number: int, zone: ZoneInfo, who: dict[int, str]
) -> dict[str, Any]:
    return {
        "number": number,
        "started": _stamp(run.started_at, zone),
        "by": who.get(run.started_by_id or 0),
        "parser": run.parser,
        "parser_name": _parser_name(run.parser),
        "file": run.file_name,
        "outcome": run.outcome,
        "outcome_label": OUTCOME_WORDS[run.outcome],
        "counts": _counts(run),
    }


def imports(org: Organization, page: int) -> dict[str, Any]:
    """One page of the runs, newest first, with the parsers an administrator
    can pick."""
    zone = ZoneInfo(org.time_zone)
    numbers = run_numbers(org)
    start = (page - 1) * PER_PAGE
    runs = list(MemberImport.objects.of(org).order_by("-pk")[start : start + PER_PAGE])
    who = _names(org, [r.started_by_id for r in runs if r.started_by_id])
    return {
        **_paging(len(numbers), page),
        "parsers": parsers(),
        "imports": [_row(run, numbers[run.pk], zone, who) for run in runs],
    }


def _findings(report: dict[str, Any]) -> tuple[list[dict[str, Any]], ...]:
    """The rows to check and the rows skipped, each a row with its reason
    code, by row number."""
    seen: set[tuple[int, str]] = set()
    for item in report.get("findings", []):
        code = item.get("code") if isinstance(item, dict) else None
        if not isinstance(code, str):
            continue
        for number in item.get("rows", []):
            if isinstance(number, int):
                seen.add((number, code))
    ordered = [{"row": row, "code": code} for row, code in sorted(seen)]
    return (
        [item for item in ordered if item["code"] in CHECK_CODES],
        [item for item in ordered if item["code"] not in CHECK_CODES],
    )


def _disabled(
    org: Organization, report: dict[str, Any], ids: list[int]
) -> list[dict[str, Any]]:
    """The members the run disabled, each with the bookings still to come and
    not cancelled (read now, not as the run saw them)."""
    names = _names(org, ids)
    # At most one page of them (step 9: no page of the member list is larger
    # than the screens need); the run keeps the rest, and the Members list
    # filtered to Disabled pages through them all.
    ids = sorted(
        ids, key=lambda pk: (str(names.get(pk, f"Member {pk}")).casefold(), pk)
    )[:PER_PAGE]
    coming: dict[int, list[dict[str, Any]]] = {pk: [] for pk in ids}
    for member_id, number, reference in (
        Booking.objects.of(org)
        .filter(
            pk__in=report.get("future_bookings", []),
            member_id__in=ids,
            taken_time__state__in=[BookingState.HELD, BookingState.CONFIRMED],
            taken_time__ends_at__gt=timezone.now(),
        )
        .order_by("order__number", "pk")
        .values_list("member_id", "order__number", "order__reference")
    ):
        if all(item["number"] != number for item in coming[member_id]):
            coming[member_id].append({"number": number, "reference": reference})
    return sorted(
        (
            {"id": pk, "name": names.get(pk, f"Member {pk}"), "bookings": coming[pk]}
            for pk in ids
        ),
        key=lambda item: (str(item["name"]).casefold(), item["id"]),
    )


def run_page(org: Organization, number: int) -> dict[str, Any] | None:
    """One run's page, or None when the organization has no such run."""
    zone = ZoneInfo(org.time_zone)
    pks = list(MemberImport.objects.of(org).order_by("pk").values_list("pk", flat=True))
    if not 1 <= number <= len(pks):
        return None
    run = MemberImport.objects.of(org).get(pk=pks[number - 1])
    report: dict[str, Any] = run.report if isinstance(run.report, dict) else {}
    who = _names(org, [run.started_by_id] if run.started_by_id else [])
    check, skipped = _findings(report)
    disabled_ids = [pk for pk in report.get("disabled", []) if isinstance(pk, int)]
    return {
        **_row(run, number, zone, who),
        "id": run.pk,
        "file_size": run.file_size,
        "reason": report.get("reason", ""),
        "column": report.get("column", ""),
        "active": report.get("active"),
        "proposed": len(report.get("proposed_revocations", [])),
        "deferred": bool(report.get("removals_deferred")),
        "disabled": _disabled(org, report, disabled_ids),
        "disabled_total": len(disabled_ids),
        "check": check,
        "skipped": skipped,
    }
