# SPDX-License-Identifier: AGPL-3.0-only
"""A payment that keeps failing its provider check says which, why and since when.

The worker checks every unsettled payment every few seconds (payment_
reconciliation.py). A failed check is recorded as a spell (`PaymentTrouble`):
since when checks have failed with no success between, the step that failed,
the kind and the number of tries. A success ends the spell; the next failure
starts a new one, and every spell is kept. Nothing a provider said is kept: no
text, no body, no redirect, no exception message.

The log gets one line when a spell starts and then at most one a minute, and
the same line again under another name once the spell has lasted `LASTING`:
the alert rule reads that one (the service repository's
sandbox-payments rule file), so a check that fails and recovers inside 15
minutes mails nobody. The words in the line
come from the fixed lists below, so the mail reads in plain words.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import Error as DatabaseError
from django.db.models import F

from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    Organization,
    Payment,
    PaymentTrouble,
    TroubleKind,
    TroubleStep,
)
from randirandi.vipps import VippsError, VippsResponseError

logger = logging.getLogger(__name__)

# How long a spell must last before an alert rule or the admin's state sees it.
LASTING = timedelta(minutes=15)
# At most one line a minute for a spell, however many tries it has. A try can
# take 25 seconds (a token and a read, each bounded at 10), so the gap between
# lines stays well inside the alert's ten-minute window.
LOG_GAP = timedelta(seconds=60)

STEP_WORDS = {
    TroubleStep.READ.value: "reading the payment",
    TroubleStep.CAPTURE.value: "taking the money",
    TroubleStep.REFUND.value: "refunding",
    TroubleStep.CANCEL.value: "cancelling",
    TroubleStep.SAVE.value: "saving the answer",
}
KIND_WORDS = {
    TroubleKind.UNANSWERED.value: "Vipps does not answer",
    TroubleKind.UNACCEPTED.value: "Vipps answered something we do not accept",
    TroubleKind.DATABASE.value: "our database failed",
    TroubleKind.OTHER.value: "our own code failed",
}
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
OSLO = ZoneInfo("Europe/Oslo")
# Statuses that mean Vipps could not answer now, rather than refused the call.
_NO_ANSWER = frozenset({408, 429})
# When a spell last had a line, by its id. Per process: a restart may add one
# extra line, which does not change an alert.
_last_logged: dict[int, datetime] = {}


def sentence(words: str) -> str:
    return words[:1].upper() + words[1:]


def oslo(moment: datetime) -> str:
    """ "23 Oct 03:31" on Oslo's clock, whatever the machine's language."""
    local = moment.astimezone(OSLO)
    return f"{local.day} {_MONTHS[local.month - 1]} {local:%H:%M}"


@contextmanager
def step(name: TroubleStep) -> Iterator[None]:
    """Marks the step that raises; the innermost step wins. Untagged: `save`."""
    try:
        yield
    except Exception as error:
        if not hasattr(error, "trouble_step"):
            # An exception that takes no attributes stays untagged: `save`.
            with suppress(AttributeError):
                error.trouble_step = name.value  # type: ignore[attr-defined]
        raise


def classify(error: Exception) -> tuple[str, str]:
    """(step, kind) of a failure, from its type and status only."""
    named = getattr(error, "trouble_step", TroubleStep.SAVE.value)
    where = named if named in STEP_WORDS else TroubleStep.SAVE.value
    if isinstance(error, VippsResponseError):
        kind = TroubleKind.UNACCEPTED
    elif isinstance(error, VippsError):
        silent = error.status == 0 or error.status >= 500 or error.status in _NO_ANSWER
        kind = TroubleKind.UNANSWERED if silent else TroubleKind.UNACCEPTED
    elif isinstance(error, DatabaseError):
        kind = TroubleKind.DATABASE
    else:
        kind = TroubleKind.OTHER
    return where, kind.value


@dataclass(frozen=True)
class Spell:
    """One spell of trouble, with what its log line names: the step and kind the
    spell began with, so a spell's lines are one alert however its latest
    failure changes."""

    pk: int
    organization_id: int
    name: str
    payment_id: int
    reference: str
    link: str
    step: str
    kind: str
    tries: int
    since: datetime

    def log(self, level: int, message: str) -> None:
        # Every id carries its type: a bare eight-digit number is struck out as a
        # phone number by the log formatter (randirandi/logs.py). The name is the
        # organization's address name, the reference the booking's six-character
        # code. The words are quoted and `when` is Oslo's clock.
        logger.log(
            level,
            "%s: organization=org-%d payment=payment-%d reference=%s name=%s "
            'step="%s" kind="%s" tries=%d since=%s when="%s" link=%s',
            message,
            self.organization_id,
            self.payment_id,
            self.reference,
            self.name,
            STEP_WORDS[self.step],
            KIND_WORDS[self.kind],
            self.tries,
            self.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            oslo(self.since),
            self.link,
        )


def site(org: Organization) -> str:
    """The organization's own address, from settings and never from a request."""
    return f"https://{org.subdomain}.{settings.ORGANIZATION_DOMAIN}"


def _spell(org: Organization, row: PaymentTrouble, payment: Payment) -> Spell:
    order = payment.order
    return Spell(
        row.pk,
        org.pk,
        org.subdomain.capitalize(),
        payment.pk,
        order.reference,
        f"{site(org)}/admin/bookings/{order.number}/",
        row.first_step,
        row.first_kind,
        row.tries,
        row.since,
    )


def _fail(
    org: Organization, order_id: int, step_name: str, kind: str, moment: datetime
) -> Spell | None:
    payment = (
        Payment.objects.of(org)
        .filter(order_id=order_id, create_key__isnull=False)
        .select_related("order")
        .first()
    )
    if payment is None:
        return None
    spells = PaymentTrouble.objects.of(org).filter(payment=payment)
    row = spells.filter(ended_at__isnull=True).first()
    if row is None:
        row = PaymentTrouble.objects.create(
            organization=org,
            payment=payment,
            since=moment,
            step=step_name,
            kind=kind,
            first_step=step_name,
            first_kind=kind,
            tries=1,
        )
    else:
        spells.filter(pk=row.pk).update(step=step_name, kind=kind, tries=F("tries") + 1)
        row.refresh_from_db()
    return _spell(org, row, payment)


def failed(
    organization_id: int,
    order_id: int,
    error: Exception,
    *,
    now: datetime | None = None,
) -> None:
    """Records one failed check. Never raises: the caller re-raises the error."""
    step_name, kind = classify(error)
    moment = utc_now(now)
    try:
        spell = organization_transaction(
            organization_id,
            lambda org: _fail(org, order_id, step_name, kind, moment),
        )
    except Exception:  # noqa: BLE001 - the record is best effort, the error is not
        logger.error(
            "payment trouble not saved: organization=org-%d order=order-%d",
            organization_id,
            order_id,
        )
        return
    if spell is None:
        return
    before = _last_logged.get(spell.pk)
    if before is not None and moment - before < LOG_GAP:
        return
    _last_logged[spell.pk] = moment
    spell.log(logging.ERROR, "payment reconciliation failed")
    if moment - spell.since >= LASTING:
        spell.log(logging.ERROR, "payment trouble lasting")


def cleared(
    organization_id: int, order_id: int, *, now: datetime | None = None
) -> None:
    """A check went through: ends the payment's open spell, if it has one."""
    if not (
        PaymentTrouble.objects.of(organization_id)
        .filter(payment__order_id=order_id, ended_at__isnull=True)
        .exists()
    ):  # the usual case costs one read, no transaction
        return
    moment = utc_now(now)

    def end(org: Organization) -> Spell | None:
        row = (
            PaymentTrouble.objects.of(org)
            .filter(payment__order_id=order_id, ended_at__isnull=True)
            .select_related("payment__order")
            .first()
        )
        if row is None:
            return None
        PaymentTrouble.objects.of(org).filter(pk=row.pk).update(ended_at=moment)
        return _spell(org, row, row.payment)

    try:
        spell = organization_transaction(organization_id, end)
    except Exception:  # noqa: BLE001 - the next success ends it
        return
    if spell is not None:
        _last_logged.pop(spell.pk, None)
        spell.log(logging.INFO, "payment reconciliation recovered")


def spells(
    org: Organization, payment_ids: list[int], *, open_only: bool = False
) -> dict[int, list[PaymentTrouble]]:
    """The spells of these payments, oldest first, for the admin: one query."""
    found: dict[int, list[PaymentTrouble]] = {pk: [] for pk in payment_ids}
    rows = PaymentTrouble.objects.of(org).filter(payment_id__in=payment_ids)
    if open_only:
        rows = rows.filter(ended_at__isnull=True)
    for row in rows.order_by("since", "pk"):
        found[row.payment_id].append(row)
    return found
