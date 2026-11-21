# SPDX-License-Identifier: AGPL-3.0-only
"""The daily read of the provider's report: payouts and the Vipps fee .

Once a day for each account that is not a test account (the report API is not in
Vipps's test environment), the job lists the account's ledgers and reads each
topic day by day from where it stopped. A day is kept whole only when Vipps says
nothing more will come and the day is over; every entry is stored once, so a
rerun changes nothing and a stopped run resumes. The entries make the payouts
(`payout-scheduled`) and tie the fee and the payout to the payments and refunds
by the payment reference. Nothing is estimated: what the report has not shown
stays empty.
"""

import logging
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Count, F, Max, Min, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from randirandi import vipps_report
from randirandi.core.member_data import organization_transaction, utc_now
from randirandi.core.models import (
    LedgerEntry,
    LedgerTopic,
    Organization,
    Payment,
    PaymentLedger,
    PaymentProvider,
    PaymentProviderAccount,
    Payout,
    Refund,
)
from randirandi.core.payment_accounts import client_for as client_for
from randirandi.vipps import Client, VippsResponseError

logger = logging.getLogger(__name__)

# An account is read once a day; one that failed or stopped early is tried
# again after the retry gap. A run takes at most this many accounts, those
# tried longest ago first.
DAILY = timedelta(days=1)
RETRY = timedelta(minutes=15)
ACCOUNTS_PER_RUN = 5
# Refunds paired again in one run, the newest completed first.
RETIE_LIMIT = 500
# Bounds of one run for one ledger topic: days, and pages of one day.
MAX_DAYS = 31
MAX_PAGES = 100
# Advisory locks with two keys: this class and the account. The one-key space is
# taken (a checkout per member, a payment per order).
LOCK_CLASS = 5_100_001

CAPTURE, REFUND = "capture", "refund"
CAPTURE_FEE, FEES_RETAINED, PAYOUT = "capture-fee", "fees-retained", "payout-scheduled"
ABORTED = "payout-aborted"


def _lock(account_id: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_CLASS, account_id])
        return bool(cursor.fetchone()[0])


def _unlock(account_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_CLASS, account_id])


def read_reports(*, now: datetime | None = None) -> None:
    """The job: read the accounts that are due, a few each time. Only the
    production deployment reads, and only live accounts: the report API is not
    in Vipps's test environment, and a live account's keys are not in the
    sandbox, so the sandbox reads nothing and logs nothing."""
    if settings.ENVIRONMENT != "production":
        return
    moment = utc_now(now)
    due = (
        PaymentProviderAccount.across_organizations.filter(
            payment_provider=PaymentProvider.VIPPS, test_mode=False
        )
        .filter(
            Q(report_completed_at__isnull=True)
            | Q(report_completed_at__lte=moment - DAILY)
        )
        .filter(
            Q(report_attempted_at__isnull=True)
            | Q(report_attempted_at__lte=moment - RETRY)
        )
        .order_by(F("report_attempted_at").asc(nulls_first=True), "pk")
    )
    attempted = 0
    # The batch limit counts only accounts this worker got hold of.
    for account in due.iterator():
        if not _lock(account.pk):
            continue
        try:
            _read_account(account, moment)
        finally:
            _unlock(account.pk)
        attempted += 1
        if attempted == ACCOUNTS_PER_RUN:
            break


def _read_account(account: PaymentProviderAccount, moment: datetime) -> None:
    org_id = account.organization_id

    def stamp(**fields: datetime) -> None:
        organization_transaction(
            org_id,
            lambda org: (
                PaymentProviderAccount.objects.of(org)
                .filter(pk=account.pk)
                .update(**fields)
            ),
        )

    stamp(report_attempted_at=moment)
    try:
        complete = _read(account, moment)
    except Exception as error:  # noqa: BLE001 - one account must not stop the others
        # Neither the provider's reply nor a credential goes in the log: the
        # ids, and the kind of the failure.
        logger.error(
            "payout report failed: organization=org-%d account=account-%d error=%s",
            org_id,
            account.pk,
            type(error).__name__,
            extra={"require_write": True},
        )
        return
    if complete:
        stamp(report_completed_at=moment)


def _first_day(org_id: int) -> date | None:
    """The day before the earliest money captured, or None with none."""
    first = (
        Payment.objects.of(org_id)
        .filter(captured_amount__gt=0)
        .aggregate(at=Min(Coalesce("captured_at", "order__created_at")))["at"]
    )
    return None if first is None else first.date() - timedelta(days=1)


def _read(account: PaymentProviderAccount, moment: datetime) -> bool:
    """True when every ledger of the account is read up to today."""
    client = client_for(account)
    ledgers = [
        ledger
        for ledger in vipps_report.read_ledgers(client)
        if ledger.settles(account.merchant_number)
    ]
    start = _first_day(account.organization_id)
    if not ledgers:
        if start is None:
            return True
        # Money was captured and the provider lists no ledger for the account.
        raise VippsResponseError()
    complete = True
    for ledger in ledgers:
        row = _ledger_row(account, ledger)
        for topic in vipps_report.TOPICS:
            complete = _advance(client, row, topic, start, moment) and complete
        _retie_refunds(account.organization_id, row)
    return complete


def _unreported(org: Organization) -> list[str]:
    """The payment references of the completed refunds the report has not been
    shown to hold, those completed most recently first, at most RETIE_LIMIT
    refunds."""
    found = (
        Refund.objects.of(org)
        .filter(completed_at__isnull=False, reported_at__isnull=True)
        .order_by("-completed_at", "-pk")
        .values_list("payment__provider_reference", flat=True)[:RETIE_LIMIT]
    )
    return sorted(set(found))


def _retie_refunds(org_id: int, ledger: PaymentLedger) -> None:
    """Pair again the completed refunds the report has not been shown to hold:
    a refund can complete after its entry was read."""

    def go(org: Organization) -> None:
        for reference in _unreported(org):
            _tie_reference(org, ledger, reference)

    organization_transaction(org_id, go)


def _ledger_row(
    account: PaymentProviderAccount, ledger: vipps_report.Ledger
) -> PaymentLedger:
    def get(org: Organization) -> PaymentLedger:
        row = PaymentLedger.objects.of(org).filter(ledger_id=ledger.ledger_id).first()
        if row is None:
            return PaymentLedger.objects.create(
                organization=org,
                account=account,
                ledger_id=ledger.ledger_id,
                currency=ledger.currency,
            )
        if row.account_id != account.pk or row.currency != ledger.currency:
            raise VippsResponseError()
        return row

    return organization_transaction(account.organization_id, get)


def _advance(
    client: Client,
    ledger: PaymentLedger,
    topic: str,
    start: date | None,
    moment: datetime,
) -> bool:
    through = ledger.funds_through if topic == "funds" else ledger.fees_through
    day = through + timedelta(days=1) if through else start
    if day is None:
        return True
    today = moment.date()
    read = 0
    while day <= today:
        if read == MAX_DAYS:
            return False
        read += 1

        def keep(page: vipps_report.Page) -> None:
            _store(ledger, topic, page.items)

        closed = vipps_report.read_day(
            client, ledger.ledger_id, topic, day, max_pages=MAX_PAGES, keep=keep
        )
        if day == today:
            # Today is open whatever Vipps says: nothing to keep whole yet.
            return True
        if not closed:
            return False
        _keep_through(ledger, topic, day)
        day += timedelta(days=1)
    return True


def _keep_through(ledger: PaymentLedger, topic: str, day: date) -> None:
    column = f"{topic}_through"

    def move(org: Organization) -> None:
        PaymentLedger.objects.of(org).filter(pk=ledger.pk).filter(
            Q(**{column + "__isnull": True}) | Q(**{column + "__lt": day})
        ).update(**{column: day})

    organization_transaction(ledger.organization_id, move)
    setattr(ledger, column, day)


def _store(
    ledger: PaymentLedger, topic: str, entries: tuple[vipps_report.Entry, ...]
) -> None:
    """Keep a page's entries once and tie them to what they belong to."""
    if not entries:
        return
    if any(entry.currency != ledger.currency for entry in entries):
        raise VippsResponseError()

    def keep(org: Organization) -> None:
        LedgerEntry.objects.of(org).bulk_create(
            [
                LedgerEntry(
                    organization=org,
                    ledger_id=ledger.pk,
                    topic=topic,
                    entry_type=entry.entry_type,
                    psp_reference=entry.psp_reference,
                    reference=entry.reference,
                    ledger_date=entry.ledger_date,
                    at=entry.at,
                    amount=entry.amount,
                    balance_before=entry.balance_before,
                    balance_after=entry.balance_after,
                )
                for entry in entries
            ],
            ignore_conflicts=True,
        )
        _tie(org, ledger, topic, entries)

    organization_transaction(ledger.organization_id, keep)


def _window(org: Organization, payout: Payout) -> QuerySet[LedgerEntry]:
    """The funds entries a payout settles: since the previous payout of its
    ledger, up to its own day (its entry is the last of that day)."""
    previous = (
        Payout.objects.of(org)
        .filter(
            ledger_id=payout.ledger_id,
            ledger_date__lt=payout.ledger_date,
            aborted_at__isnull=True,
        )
        .aggregate(day=Max("ledger_date"))["day"]
    )
    found = LedgerEntry.objects.of(org).filter(
        ledger_id=payout.ledger_id,
        topic=LedgerTopic.FUNDS,
        ledger_date__lte=payout.ledger_date,
    )
    return found.filter(ledger_date__gt=previous) if previous else found


def _totals(org: Organization, payout: Payout) -> dict[str, int]:
    sums = (
        _window(org, payout)
        .exclude(entry_type=PAYOUT)
        .aggregate(
            captured=Sum("amount", filter=Q(entry_type=CAPTURE)),
            refunded=Sum("amount", filter=Q(entry_type=REFUND)),
            fees=Sum("amount", filter=Q(entry_type=FEES_RETAINED)),
            captures=Count("pk", filter=Q(entry_type=CAPTURE)),
            refunds=Count("pk", filter=Q(entry_type=REFUND)),
        )
    )
    captured = sums["captured"] or 0
    refunded = -(sums["refunded"] or 0)
    fees = -(sums["fees"] or 0)
    return {
        "captured_total": captured,
        "refunded_total": refunded,
        "fee_total": fees,
        "other_total": payout.amount - (captured - refunded - fees),
        "captured_count": sums["captures"],
        "refunded_count": sums["refunds"],
    }


def _tie(
    org: Organization,
    ledger: PaymentLedger,
    topic: str,
    entries: tuple[vipps_report.Entry, ...],
) -> None:
    references = {
        entry.reference
        for entry in entries
        if topic == LedgerTopic.FUNDS
        and entry.entry_type in {CAPTURE, REFUND}
        and entry.reference
    }
    if topic == LedgerTopic.FEES:
        fees = {e.psp_reference for e in entries if e.entry_type == CAPTURE_FEE}
        references |= set(
            LedgerEntry.objects.of(org)
            .filter(
                ledger_id=ledger.pk,
                topic=LedgerTopic.FUNDS,
                entry_type=CAPTURE,
                psp_reference__in=fees,
            )
            .values_list("reference", flat=True)
        )
    if topic == LedgerTopic.FUNDS:
        references |= _payouts(org, ledger, entries)
    for reference in sorted(references):
        _tie_reference(org, ledger, reference)


def _schedule(
    org: Organization, ledger: PaymentLedger, entry: vipps_report.Entry
) -> None:
    if entry.amount > 0:
        raise VippsResponseError()
    # The window of a payout runs by ledger day: its entry is the last of its
    # day. A second live payout on the same day is not in the documentation,
    # so it is refused rather than counted twice.
    if (
        Payout.objects.of(org)
        .filter(ledger=ledger, ledger_date=entry.ledger_date, aborted_at__isnull=True)
        .exclude(psp_reference=entry.psp_reference)
        .exists()
    ):
        raise VippsResponseError()
    Payout.objects.of(org).get_or_create(
        ledger=ledger,
        psp_reference=entry.psp_reference,
        defaults={
            "organization": org,
            "ledger_date": entry.ledger_date,
            "scheduled_at": entry.at,
            "currency": ledger.currency,
            "amount": -entry.amount,
            "statement_text": entry.reference[:200],
            "captured_total": 0,
            "refunded_total": 0,
            "fee_total": 0,
            "other_total": -entry.amount,
        },
    )


def _aborted(
    org: Organization, ledger: PaymentLedger, entry: vipps_report.Entry
) -> tuple[set[str], date]:
    """A `payout-aborted` entry puts a payout's money back on the ledger: the
    payout is voided, and its payments wait for the next payout. The entry names
    the payout by its Vipps reference; one that names no payout of the ledger is
    an unexpected reply (the documentation gives no other link, so none is
    guessed). Returns the payment references the payout held, and its day."""
    payouts = Payout.objects.of(org).filter(ledger=ledger)
    target = payouts.filter(psp_reference=entry.psp_reference).first()
    if target is None:
        raise VippsResponseError()
    if target.aborted_at is not None:
        return set(), target.ledger_date
    held = set(
        _window(org, target)
        .filter(entry_type__in=[CAPTURE, REFUND])
        .exclude(reference="")
        .values_list("reference", flat=True)
    )
    payouts.filter(pk=target.pk).update(aborted_at=entry.at)
    return held, target.ledger_date


def _payouts(
    org: Organization,
    ledger: PaymentLedger,
    entries: tuple[vipps_report.Entry, ...],
) -> set[str]:
    """Make the payouts these entries announce, void those they abort, and bring
    the totals of those they touch up to date. Returns the payment references of
    the payouts that moved."""
    scheduled = [entry for entry in entries if entry.entry_type == PAYOUT]
    moved: set[str] = set()
    days = [entry.ledger_date for entry in entries]
    for entry in entries:
        if entry.entry_type == PAYOUT:
            _schedule(org, ledger, entry)
        elif entry.entry_type == ABORTED:
            held, day = _aborted(org, ledger, entry)
            moved |= held
            days.append(day)
    first = min(days, default=None)
    if first is None:
        return moved
    for payout in Payout.objects.of(org).filter(
        ledger=ledger, ledger_date__gte=first, aborted_at__isnull=True
    ):
        wanted = _totals(org, payout)
        new = any(entry.psp_reference == payout.psp_reference for entry in scheduled)
        if new or any(getattr(payout, name) != value for name, value in wanted.items()):
            Payout.objects.of(org).filter(pk=payout.pk).update(**wanted)
            moved |= set(
                _window(org, payout)
                .filter(entry_type__in=[CAPTURE, REFUND])
                .exclude(reference="")
                .values_list("reference", flat=True)
            )
    return moved


def _payout_on_or_after(
    org: Organization, ledger: PaymentLedger, day: date
) -> Payout | None:
    return (
        Payout.objects.of(org)
        .filter(ledger=ledger, ledger_date__gte=day, aborted_at__isnull=True)
        .order_by("ledger_date", "pk")
        .first()
    )


def _tie_reference(org: Organization, ledger: PaymentLedger, reference: str) -> None:
    """Set the fee and the payout on the payment with this reference, and the
    payout on its refunds. A reference with no payment on record (erased, or not
    ours) is left; its entries stay."""
    payment = Payment.objects.of(org).filter(provider_reference=reference).first()
    if payment is None:
        return
    mine = LedgerEntry.objects.of(org).filter(
        ledger=ledger, topic=LedgerTopic.FUNDS, reference=reference
    )
    captures = list(mine.filter(entry_type=CAPTURE))
    if captures:
        fees = list(
            LedgerEntry.objects.of(org)
            .filter(
                ledger=ledger,
                topic=LedgerTopic.FEES,
                entry_type=CAPTURE_FEE,
                psp_reference__in={entry.psp_reference for entry in captures},
            )
            .values_list("amount", flat=True)
        )
        payout = _payout_on_or_after(
            org, ledger, max(entry.ledger_date for entry in captures)
        )
        pay: dict[str, int | Payout | None] = {}
        if fees and payment.fee_amount != abs(sum(fees)):
            pay["fee_amount"] = abs(sum(fees))
        if payment.payout_id != (None if payout is None else payout.pk):
            pay["payout"] = payout
        if pay:
            Payment.objects.of(org).filter(pk=payment.pk).update(**pay)
    entries = list(mine.filter(entry_type=REFUND).order_by("at", "pk"))
    refunds = list(
        Refund.objects.of(org).filter(payment=payment).order_by("made_at", "pk")
    )
    # By the provider's reference when the two agree, then by amount in order.
    taken: set[int] = set()
    paired: list[tuple[LedgerEntry, Refund]] = []
    rest: list[LedgerEntry] = []
    for entry in entries:
        match = next(
            (
                r
                for r in refunds
                if r.pk not in taken and r.provider_reference == entry.psp_reference
            ),
            None,
        )
        if match is None:
            rest.append(entry)
        else:
            taken.add(match.pk)
            paired.append((entry, match))
    for entry in rest:
        match = next(
            (r for r in refunds if r.pk not in taken and r.amount == abs(entry.amount)),
            None,
        )
        if match is not None:
            taken.add(match.pk)
            paired.append((entry, match))
    for entry, refund in paired:
        payout = _payout_on_or_after(org, ledger, entry.ledger_date)
        changes: dict[str, datetime | Payout | None] = {}
        if refund.payout_id != (None if payout is None else payout.pk):
            changes["payout"] = payout
        if refund.reported_at != entry.at:
            changes["reported_at"] = entry.at
        if changes:
            Refund.objects.of(org).filter(pk=refund.pk).update(**changes)
