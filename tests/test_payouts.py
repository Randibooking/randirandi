# SPDX-License-Identifier: AGPL-3.0-only
"""The daily read of the report : payouts, and the Vipps fee on
the payments they tie to. A fictional report API stands in for Vipps's; the
real client and parser run. The read runs as the restricted application login,
which may insert a ledger entry and never update one."""

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import django.db.utils
import psycopg
import pytest
from django.db import connection
from django.db.models import Model
from pytest_django.fixtures import Settings

from randirandi import vipps_report
from randirandi.core import booking_admin, payout_admin, payouts, refund_check
from randirandi.core.models import (
    LedgerEntry,
    Payment,
    PaymentLedger,
    PaymentProviderAccount,
    PaymentTrouble,
    Payout,
    Refund,
)
from randirandi.core.retention import purge_organization
from randirandi.environment import Environment
from randirandi.logs import RequiredLogError
from randirandi.ops.jobs import JOBS
from randirandi.payment_configuration import PaymentConfigurationError
from randirandi.vipps import Client, Credentials
from tests import made_up, payouts_made
from tests.capacity_app import application_login
from tests.test_admin_payments import pay

pytestmark = pytest.mark.django_db(transaction=True)

NOW = datetime(2027, 3, 5, 12, 0, tzinfo=UTC)
CAPTURED_AT = datetime(2027, 2, 28, 10, 0, tzinfo=UTC)
LEDGER = "9001"
OPEN = {"items": [], "hasMore": False, "tryLater": True}
EMPTY = {"items": [], "hasMore": False, "tryLater": False}


def entry(
    psp: str,
    kind: str,
    amount: Any,
    before: Any,
    after: Any,
    *,
    day: str,
    reference: str = "",
    clock: str = "12:00:00.000000+0100",
) -> dict[str, Any]:
    return {
        "pspReference": psp,
        "time": f"{day}T{clock}",
        "ledgerDate": day,
        "entryType": kind,
        "reference": reference,
        "currency": "NOK",
        "amount": amount,
        "balanceBefore": before,
        "balanceAfter": after,
        "recipientHandle": "NO:123456",
    }


def page(*items: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {"items": list(items), "hasMore": False, "tryLater": False} | over


@dataclass
class Report:
    """A fictional report API. `days` maps (topic, day) to the pages Vipps
    would answer for it, the first for no cursor and then one for each."""

    merchant: str = "123456"
    days: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    refuse: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    today: str = "2027-03-05"

    def __call__(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        assert host == "api.vipps.no"
        if path == "/accesstoken/get":
            return 200, b'{"access_token":"fictional-token","expires_in":"3600"}'
        assert method == "GET" and body is None
        self.calls.append(path)
        assert "includeGDPRSensitiveData" not in path
        for part, status in self.refuse.items():
            if part in path:
                return status, b'{"detail":"fictional-private-reply"}'
        if path == "/settlement/v1/ledgers":
            reply: Any = {
                "items": [
                    {
                        "ledgerId": LEDGER,
                        "currency": "NOK",
                        "payoutBankAccount": {"scheme": "BBAN", "id": "12345678901"},
                        "salesUnits": [{"recipientHandle": f"NO:{self.merchant}"}],
                    }
                ],
                "cursor": "",
            }
            return 200, json.dumps(reply).encode()
        base, _, cursor = path.partition("?cursor=")
        parts = base.split("/")
        topic, day = parts[5], parts[7]
        assert parts[4] == LEDGER
        pages = self.days.get((topic, day))
        if pages is None:
            pages = [OPEN if day == self.today else EMPTY]
        index = int(cursor[1:]) if cursor else 0
        return 200, json.dumps(pages[index]).encode()

    def asked(self, topic: str, day: str) -> int:
        suffix = f"/{topic}/dates/{day}"
        return sum(1 for path in self.calls if path.partition("?")[0].endswith(suffix))


def march(report: Report) -> Report:
    """Two days of money and a payout, as Vipps's documentation lays it out."""
    f28, m01 = "2027-02-28", "2027-03-01"
    report.days = {
        # Amounts come as numbers and as strings; the second capture is not ours.
        ("funds", f28): [
            page(
                entry(
                    "C1",
                    "capture",
                    25000,
                    0,
                    "25000",
                    day=f28,
                    reference="harbourview-1",
                ),
                entry(
                    "C2",
                    "capture",
                    "5000",
                    25000,
                    30000,
                    day=f28,
                    reference="not-ours-1",
                ),
            )
        ],
        ("fees", f28): [
            page(
                entry(
                    "C1",
                    "capture-fee",
                    "300",
                    0,
                    300,
                    day=f28,
                    reference="harbourview-1",
                ),
                entry(
                    "C2", "capture-fee", 50, 300, 350, day=f28, reference="not-ours-1"
                ),
            )
        ],
        # An entry type nobody knows adds to what is paid out, and counts as other.
        ("funds", m01): [
            page(
                entry(
                    "R1",
                    "refund",
                    -10000,
                    30000,
                    20000,
                    day=m01,
                    reference="harbourview-1",
                ),
                entry("F1", "fees-retained", -350, 20000, 19650, day=m01),
                entry("N1", "a-new-entry-2031", 100, 19650, 19750, day=m01),
                entry(
                    "9001-2000023",
                    "payout-scheduled",
                    -19750,
                    19750,
                    0,
                    day=m01,
                    reference="Vipps utbet. 2000023 Vippsnr 123456",
                    clock="23:59:00.000000+0100",
                ),
            )
        ],
        ("fees", m01): [page(entry("F1", "fees-retained", 350, 0, 350, day=m01))],
    }
    return report


@pytest.fixture
def made(settings: Settings) -> made_up.MadeUp:
    settings.ENVIRONMENT = "production"
    made = made_up.everything()
    payouts_made.account(made)
    # Captured on the 28th; the read starts the day before.
    Payment.objects.of(made.organization).update(captured_at=CAPTURED_AT)
    return made


@pytest.fixture
def report(monkeypatch: pytest.MonkeyPatch) -> Report:
    wire = march(Report())

    def client_for(account: PaymentProviderAccount) -> Client:
        credentials = Credentials("id", "secret", "key", account.merchant_number)
        return Client(Environment.PRODUCTION, credentials, transport=wire)

    monkeypatch.setattr(payouts, "client_for", client_for)
    return wire


def read(moment: datetime = NOW) -> None:
    with application_login():
        payouts.read_reports(now=moment)


def table(model: type[Model]) -> list[dict[str, Any]]:
    return list(model.across_organizations.order_by("pk").values())  # type: ignore[attr-defined]


def account_of(made: made_up.MadeUp) -> PaymentProviderAccount:
    return PaymentProviderAccount.objects.of(made.organization).get()


def test_the_read_makes_the_payout_and_ties_the_fee_and_the_payout(
    made: made_up.MadeUp, report: Report
) -> None:
    read()
    (payout,) = Payout.objects.of(made.organization)
    assert (payout.psp_reference, payout.ledger_date) == (
        "9001-2000023",
        date(2027, 3, 1),
    )
    assert payout.statement_text == "Vipps utbet. 2000023 Vippsnr 123456"
    assert payout.scheduled_at == datetime(2027, 3, 1, 22, 59, tzinfo=UTC)
    # 300.00 captured (one capture is another seller's), 100.00 refunded,
    # 3.50 retained in fees, and 1.00 of an entry nobody knows.
    assert (
        payout.amount,
        payout.captured_total,
        payout.refunded_total,
        payout.fee_total,
        payout.other_total,
    ) == (19750, 30000, 10000, 350, 100)
    assert (payout.captured_count, payout.refunded_count) == (2, 1)
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    # The fee of this capture only, the other capture's 0.50 is not on it.
    assert (payment.fee_amount, payment.payout_id) == (300, payout.pk)
    refund = Refund.objects.of(made.organization).get()
    assert refund.payout_id == payout.pk
    # What was read stays: 9 funds-and-fees entries, one row each.
    assert LedgerEntry.objects.of(made.organization).count() == 9
    ledger = PaymentLedger.objects.of(made.organization).get()
    assert (ledger.funds_through, ledger.fees_through) == (date(2027, 3, 4),) * 2
    account = account_of(made)
    assert account.report_attempted_at == account.report_completed_at == NOW
    # Today is read but never kept whole; the days before it are read once.
    assert report.asked("funds", "2027-03-05") == 1
    assert report.asked("funds", "2027-02-27") == 1


def test_a_rerun_changes_nothing(made: made_up.MadeUp, report: Report) -> None:
    read()
    before = [
        table(model) for model in (LedgerEntry, Payout, Payment, Refund, PaymentLedger)
    ]
    calls = len(report.calls)
    report.today = "2027-03-06"
    # Not due again within a day: no call at all.
    read(NOW + timedelta(hours=23))
    assert len(report.calls) == calls
    # Due: today's open day and the next are read again, every entry once.
    read(NOW + timedelta(days=1, minutes=1))
    assert len(report.calls) > calls
    after = [
        table(model) for model in (LedgerEntry, Payout, Payment, Refund, PaymentLedger)
    ]
    ledger_before, ledger_after = before.pop(), after.pop()
    assert before == after
    assert ledger_after[0]["funds_through"] == date(2027, 3, 5)
    assert ledger_before[0]["funds_through"] == date(2027, 3, 4)
    assert LedgerEntry.objects.of(made.organization).count() == 9


def test_the_same_page_read_twice_is_stored_once(
    made: made_up.MadeUp, report: Report
) -> None:
    ledger = payouts_made.ledger(made)
    items = tuple(
        vipps_report.entry(item)
        for item in report.days[("funds", "2027-02-28")][0]["items"]
    )
    for _ in range(3):
        payouts._store(ledger, "funds", items)
    assert LedgerEntry.objects.of(made.organization).count() == 2


def test_an_account_with_test_keys_reads_nothing(
    made: made_up.MadeUp, report: Report
) -> None:
    PaymentProviderAccount.objects.of(made.organization).update(test_mode=True)
    read()
    read(NOW + timedelta(days=3))
    assert report.calls == []
    account = account_of(made)
    assert account.report_attempted_at is None and account.report_completed_at is None
    assert not LedgerEntry.objects.of(made.organization).exists()
    assert not Payout.objects.of(made.organization).exists()
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    assert (payment.fee_amount, payment.payout_id) == (None, None)


def test_the_job_is_in_the_runner() -> None:
    job = next(job for job in JOBS if job.name == "payouts")
    assert job.run is payouts.read_reports
    assert timedelta(minutes=1) <= job.every <= timedelta(hours=1)


def test_a_fee_that_comes_with_a_minus_sign_is_still_a_fee(
    made: made_up.MadeUp, report: Report
) -> None:
    fees = report.days[("fees", "2027-02-28")][0]["items"]
    for item in fees:
        item["amount"] = -abs(int(item["amount"]))
    read()
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    assert payment.fee_amount == 300


def test_a_payment_before_the_report_shows_a_fee_keeps_none(
    made: made_up.MadeUp, report: Report
) -> None:
    del report.days[("fees", "2027-02-28")]
    read()
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    assert payment.fee_amount is None
    assert payment.payout is not None


def test_a_payout_that_comes_after_its_payments_ties_them_then(
    made: made_up.MadeUp, report: Report
) -> None:
    held = report.days[("funds", "2027-03-01")]
    report.days[("funds", "2027-03-01")] = [page(*held[0]["items"][:-1], tryLater=True)]
    read()
    assert not Payout.objects.of(made.organization).exists()
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    assert payment.payout_id is None and payment.fee_amount == 300
    # The open past day is not kept whole: it is read again at the next try.
    assert PaymentLedger.objects.of(made.organization).get().funds_through == date(
        2027, 2, 28
    )
    report.days[("funds", "2027-03-01")] = held
    read(NOW + timedelta(minutes=16))
    (payout,) = Payout.objects.of(made.organization)
    payment.refresh_from_db()
    assert payment.payout_id == payout.pk
    assert Refund.objects.of(made.organization).get().payout_id == payout.pk


def test_a_stopped_read_resumes_where_it_stopped(
    made: made_up.MadeUp, report: Report, caplog: pytest.LogCaptureFixture
) -> None:
    report.refuse["/funds/dates/2027-03-01"] = 500
    with caplog.at_level(logging.ERROR):
        read()
    ledger = PaymentLedger.objects.of(made.organization).get()
    assert ledger.funds_through == date(2027, 2, 28)
    assert LedgerEntry.objects.of(made.organization).filter(topic="funds").count() == 2
    account = account_of(made)
    assert account.report_attempted_at == NOW and account.report_completed_at is None
    # The alert log names the organization and the account by id, and nothing
    # of the reply.
    lines = [r.getMessage() for r in caplog.records if "payout report failed" in r.msg]
    assert lines == [
        f"payout report failed: organization=org-{made.organization.pk} "
        f"account=account-{account.pk} error=VippsError"
    ]
    assert "fictional-private-reply" not in caplog.text
    # Not tried again before the retry gap...
    seen = len(report.calls)
    read(NOW + timedelta(minutes=14))
    assert len(report.calls) == seen
    # ...then it goes on from the 1st, not from the start.
    del report.refuse["/funds/dates/2027-03-01"]
    report.calls.clear()
    read(NOW + timedelta(minutes=16))
    assert report.asked("funds", "2027-02-28") == 0
    assert report.asked("funds", "2027-03-01") == 1
    assert Payout.objects.of(made.organization).count() == 1
    assert account_of(made).report_completed_at == NOW + timedelta(minutes=16)


def test_the_bounds_stop_a_run_and_the_next_one_goes_on(
    made: made_up.MadeUp, report: Report, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(payouts, "MAX_DAYS", 2)
    read()
    ledger = PaymentLedger.objects.of(made.organization).get()
    assert ledger.funds_through == date(2027, 2, 28)
    assert account_of(made).report_completed_at is None
    assert not Payout.objects.of(made.organization).exists()
    read(NOW + timedelta(minutes=16))
    assert PaymentLedger.objects.of(made.organization).get().funds_through == date(
        2027, 3, 2
    )
    assert Payout.objects.of(made.organization).count() == 1
    read(NOW + timedelta(minutes=32))
    assert PaymentLedger.objects.of(made.organization).get().funds_through == date(
        2027, 3, 4
    )
    assert account_of(made).report_completed_at is None
    read(NOW + timedelta(minutes=48))
    assert account_of(made).report_completed_at == NOW + timedelta(minutes=48)


def test_a_day_of_many_pages_is_bounded_and_read_in_turn(
    made: made_up.MadeUp, report: Report, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = "2027-02-28"
    first = report.days[("funds", day)][0]["items"]
    report.days[("funds", day)] = [
        page(first[0], hasMore=True, tryLater=True, cursor="p1"),
        page(first[1]),
    ]
    read()
    assert LedgerEntry.objects.of(made.organization).filter(topic="funds").count() == 6
    monkeypatch.setattr(payouts, "MAX_PAGES", 1)
    PaymentProviderAccount.objects.of(made.organization).update(
        report_completed_at=None
    )
    PaymentLedger.objects.of(made.organization).update(funds_through=None)
    read(NOW + timedelta(days=2))
    # The first quiet day is kept; the day with a second page is not closed.
    assert PaymentLedger.objects.of(made.organization).get().funds_through == date(
        2027, 2, 27
    )
    assert account_of(made).report_completed_at is None


def test_a_payout_stays_whole_when_its_bookings_are_erased(
    made: made_up.MadeUp, report: Report
) -> None:
    read()
    report.today = "2027-03-07"
    (payout,) = Payout.objects.of(made.organization)
    before = table(Payout)
    with application_login():
        assert purge_organization(
            made.organization, now=datetime(2032, 1, 1, tzinfo=UTC)
        )["orders"]
    assert (
        not Payment.objects.of(made.organization)
        .filter(provider_reference="harbourview-1")
        .exists()
    )
    assert not Refund.objects.of(made.organization).exists()
    assert table(Payout) == before
    assert LedgerEntry.objects.of(made.organization).count() == 9
    # Reading again finds nothing to tie and breaks nothing.
    PaymentProviderAccount.objects.of(made.organization).update(
        report_completed_at=None
    )
    read(NOW + timedelta(days=2))
    assert table(Payout) == before
    assert LedgerEntry.objects.of(made.organization).count() == 9
    assert payout.pk == Payout.objects.of(made.organization).get().pk


def test_another_organizations_payments_are_left_alone(
    made: made_up.MadeUp, report: Report
) -> None:
    elsewhere = made_up.everything("elsewhere")
    Payment.objects.of(elsewhere.organization).update(
        provider_reference="harbourview-1x"
    )
    report.days[("funds", "2027-02-28")][0]["items"][0]["reference"] = "harbourview-1x"
    read()
    other = Payment.objects.of(elsewhere.organization).get()
    assert (other.fee_amount, other.payout_id) == (None, None)
    assert not LedgerEntry.objects.of(elsewhere.organization).exists()
    assert not Payout.objects.of(elsewhere.organization).exists()
    assert Payout.objects.of(made.organization).count() == 1


def test_a_ledger_of_another_merchant_is_not_read(
    made: made_up.MadeUp, report: Report
) -> None:
    report.merchant = "654321"
    read()
    # Money was captured and Vipps lists no ledger for this merchant.
    assert account_of(made).report_completed_at is None
    assert not PaymentLedger.objects.of(made.organization).exists()


def test_with_nothing_captured_there_is_nothing_to_read(
    made: made_up.MadeUp, report: Report
) -> None:
    report.merchant = "654321"
    Payment.objects.of(made.organization).update(captured_amount=0, refunded_amount=0)
    read()
    assert account_of(made).report_completed_at == NOW
    assert not any("/report/" in path for path in report.calls)


def test_a_production_account_waits_for_m9_and_alerts(
    made: made_up.MadeUp, caplog: pytest.LogCaptureFixture
) -> None:
    # No monkeypatch: the real credential path refuses a production account in
    # the sandbox, and the failure is logged like any other.
    with caplog.at_level(logging.ERROR):
        read()
    assert "payout report failed" in caplog.text
    assert account_of(made).report_completed_at is None
    with pytest.raises(PaymentConfigurationError):
        payouts.client_for(account_of(made))


def test_a_log_that_cannot_be_written_keeps_the_attempt_and_the_retry(
    made: made_up.MadeUp, report: Report, monkeypatch: pytest.MonkeyPatch
) -> None:
    report.refuse["/settlement/v1/ledgers"] = 500

    def broken(*args: Any, **kwargs: Any) -> None:
        assert kwargs["extra"] == {"require_write": True}
        raise RequiredLogError("Required alert log could not be written.")

    working = payouts.logger.error
    monkeypatch.setattr(payouts.logger, "error", broken)
    with pytest.raises(RequiredLogError):
        read()
    account = account_of(made)
    assert account.report_attempted_at == NOW and account.report_completed_at is None
    monkeypatch.setattr(payouts.logger, "error", working)
    del report.refuse["/settlement/v1/ledgers"]
    read(NOW + timedelta(minutes=16))
    assert account_of(made).report_completed_at == NOW + timedelta(minutes=16)


@contextmanager
def other_session_holding(account_id: int) -> Iterator[None]:
    """Another worker's database session holds the account's lock."""
    config = connection.settings_dict
    with psycopg.connect(
        host=config["HOST"],
        port=config["PORT"],
        dbname=config["NAME"],
        user=config["USER"],
        password=config["PASSWORD"],
        autocommit=True,
    ) as other:
        held = other.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", [payouts.LOCK_CLASS, account_id]
        ).fetchone()
        assert held == (True,)
        yield


@pytest.fixture
def four(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[list[PaymentProviderAccount]]:
    settings.ENVIRONMENT = "production"
    accounts = []
    for number in range(4):
        made = made_up.everything(f"shop{number}")
        accounts.append(payouts_made.account(made))
        Payment.objects.of(made.organization).update(captured_at=CAPTURED_AT)
    monkeypatch.setattr(payouts, "ACCOUNTS_PER_RUN", 2)
    yield accounts


def test_failures_go_last_and_a_busy_account_does_not_hold_a_batch(
    four: list[PaymentProviderAccount], monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[int] = []

    def refuse(account: PaymentProviderAccount) -> Client:
        tried.append(account.pk)
        raise PaymentConfigurationError("fictional")

    monkeypatch.setattr(payouts, "client_for", refuse)
    first, second, third, fourth = (a.pk for a in four)
    # Never tried first; the batch is two accounts.
    read()
    assert tried == [first, second]
    # The two that failed wait: the other two go next, then the oldest again.
    tried.clear()
    read(NOW + timedelta(minutes=16))
    assert tried == [third, fourth]
    tried.clear()
    read(NOW + timedelta(minutes=32))
    assert tried == [first, second]
    # An account another worker holds is passed over, not counted.
    tried.clear()
    with other_session_holding(third):
        read(NOW + timedelta(minutes=64))
    assert tried == [fourth, first]


def test_the_application_cannot_change_a_ledger_entry(
    made: made_up.MadeUp, report: Report
) -> None:
    read()
    with application_login():
        assert LedgerEntry.objects.of(made.organization).count() == 9
        with pytest.raises(django.db.utils.ProgrammingError, match="permission denied"):
            LedgerEntry.objects.of(made.organization).update(amount=1)


ABORT = entry(
    "9001-2000023",
    "payout-aborted",
    19750,
    0,
    19750,
    day="2027-03-02",
    reference="Vipps utbet. 2000023 Vippsnr 123456",
)
SECOND = entry(
    "9001-2000024",
    "payout-scheduled",
    -19750,
    19750,
    0,
    day="2027-03-03",
    reference="Vipps utbet. 2000024 Vippsnr 123456",
    clock="23:59:00.000000+0100",
)


def aborted(report: Report, *, then: bool = False) -> None:
    report.days[("funds", "2027-03-02")] = [page(ABORT)]
    if then:
        report.days[("funds", "2027-03-03")] = [page(SECOND)]


def test_an_aborted_payout_is_void_and_its_payments_wait(
    made: made_up.MadeUp, report: Report
) -> None:
    aborted(report)
    read()
    (payout,) = Payout.objects.of(made.organization)
    assert payout.aborted_at == datetime(2027, 3, 2, 11, 0, tzinfo=UTC)
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    assert (payment.fee_amount, payment.payout_id) == (300, None)
    assert Refund.objects.of(made.organization).get().payout_id is None
    assert (
        payout_admin.payouts(made.organization, payout_admin.PayoutsQuery())["payouts"]
        == []
    )
    # A rerun a day later, and the abort read again, change nothing.
    before = [table(m) for m in (LedgerEntry, Payout, Payment, Refund)]
    report.today = "2027-03-06"
    read(NOW + timedelta(days=1, minutes=1))
    PaymentLedger.objects.of(made.organization).update(funds_through=date(2027, 2, 27))
    PaymentProviderAccount.objects.of(made.organization).update(
        report_completed_at=None, report_attempted_at=None
    )
    read(NOW + timedelta(days=2))
    assert before == [table(m) for m in (LedgerEntry, Payout, Payment, Refund)]


def test_the_next_payout_takes_the_money_of_an_aborted_one(
    made: made_up.MadeUp, report: Report
) -> None:
    aborted(report, then=True)
    read()
    voided, second = Payout.objects.of(made.organization).order_by("pk")
    assert voided.aborted_at is not None and second.aborted_at is None
    assert second.psp_reference == "9001-2000024"
    assert (second.captured_total, second.refunded_total, second.fee_total) == (
        30000,
        10000,
        350,
    )
    assert second.amount == 19750
    payment = Payment.objects.of(made.organization).get(
        provider_reference="harbourview-1"
    )
    assert payment.payout_id == second.pk
    assert Refund.objects.of(made.organization).get().payout_id == second.pk


def test_a_second_payout_on_one_ledger_day_is_an_unexpected_reply(
    made: made_up.MadeUp, report: Report, caplog: pytest.LogCaptureFixture
) -> None:
    twin = entry(
        "9001-2000025",
        "payout-scheduled",
        -1,
        100,
        99,
        day="2027-03-01",
        clock="23:59:30.000000+0100",
    )
    funds = report.days[("funds", "2027-03-01")][0]
    report.days[("funds", "2027-03-01")] = [page(*funds["items"], twin)]
    with caplog.at_level(logging.ERROR):
        read()
    assert "payout report failed" in caplog.text
    assert not Payout.objects.of(made.organization).exists()
    assert account_of(made).report_completed_at is None


def test_the_sandbox_reads_nothing_and_logs_nothing(
    made: made_up.MadeUp,
    report: Report,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.ENVIRONMENT = "sandbox"
    with caplog.at_level(logging.ERROR):
        read()
    assert caplog.text == ""
    assert report.calls == []
    assert account_of(made).report_attempted_at is None


def completed(made: made_up.MadeUp, at: datetime, **more: Any) -> Refund:
    Refund.objects.of(made.organization).update(completed_at=at, **more)
    return Refund.objects.of(made.organization).get()


def flagged(made: made_up.MadeUp) -> set[int]:
    return refund_check.attention(made.organization)


def test_a_refund_the_report_shows_is_paired_and_needs_no_attention(
    made: made_up.MadeUp, report: Report
) -> None:
    completed(made, datetime(2027, 2, 28, 10, 0, tzinfo=UTC))
    read()
    refund = Refund.objects.of(made.organization).get()
    assert refund.reported_at == datetime(2027, 3, 1, 11, 0, tzinfo=UTC)
    assert flagged(made) == set()


def test_a_refund_the_report_does_not_show_needs_attention_after_two_days(
    made: made_up.MadeUp, report: Report
) -> None:
    # Amount 777.00 matches no refund entry; the read reaches 4 March.
    completed(made, datetime(2027, 2, 28, 10, 0, tzinfo=UTC), amount=77700)
    read()
    payment = Payment.objects.of(made.organization).get()
    assert Refund.objects.of(made.organization).get().reported_at is None
    assert flagged(made) == {payment.pk}
    row = booking_admin.payments(made.organization, booking_admin.PaymentsQuery())
    assert [p["attention"] for p in row["payments"]] == [True]  # type: ignore[attr-defined]
    # The last day that is judged is two days before the read's end.
    completed(made, datetime(2027, 3, 2, 23, 59, tzinfo=UTC), amount=77700)
    assert flagged(made) == {payment.pk}
    completed(made, datetime(2027, 3, 3, 0, 0, tzinfo=UTC), amount=77700)
    assert flagged(made) == set()


def test_a_read_that_lags_does_not_raise_it(
    made: made_up.MadeUp, report: Report
) -> None:
    completed(made, datetime(2027, 2, 28, 10, 0, tzinfo=UTC), amount=77700)
    read()
    assert flagged(made) != set()
    PaymentLedger.objects.of(made.organization).update(funds_through=date(2027, 3, 1))
    assert flagged(made) == set()


def test_a_refund_not_yet_paired_is_paired_again_by_a_later_read(
    made: made_up.MadeUp, report: Report
) -> None:
    read()
    Refund.objects.of(made.organization).update(reported_at=None, payout=None)
    completed(made, datetime(2027, 3, 5, 9, 0, tzinfo=UTC))
    report.today = "2027-03-06"
    read(NOW + timedelta(days=1, minutes=1))
    assert Refund.objects.of(made.organization).get().reported_at is not None
    assert flagged(made) == set()


def test_an_erased_booking_and_another_organization_are_not_flagged(
    made: made_up.MadeUp, report: Report
) -> None:
    completed(made, datetime(2027, 2, 28, 10, 0, tzinfo=UTC), amount=77700)
    other = made_up.everything("lakeside")
    Refund.objects.of(other.organization).update(
        completed_at=datetime(2027, 2, 1, 10, 0, tzinfo=UTC)
    )
    read()
    assert flagged(made) != set()
    # The other organization has no ledger of its own: nothing to judge by.
    assert refund_check.attention(other.organization) == set()
    with application_login():
        assert purge_organization(
            made.organization, now=datetime(2032, 1, 1, tzinfo=UTC)
        )["orders"]
    assert flagged(made) == set()


def test_an_abort_voids_the_payout_it_names_when_two_have_one_amount(
    made: made_up.MadeUp, report: Report
) -> None:
    # 9001-2000023 (1 March) and 9001-2000024 (3 March) are both 197.50; the
    # abort on 4 March names the first.
    report.days[("funds", "2027-03-03")] = [page(SECOND)]
    report.days[("funds", "2027-03-04")] = [
        page(
            entry(
                "9001-2000023",
                "payout-aborted",
                19750,
                0,
                19750,
                day="2027-03-04",
                reference="Vipps utbet. 2000024 Vippsnr 123456",
            )
        )
    ]
    read()
    first, second = Payout.objects.of(made.organization).order_by("pk")
    assert (first.psp_reference, first.aborted_at is not None) == (
        "9001-2000023",
        True,
    )
    assert second.aborted_at is None


def test_an_abort_that_names_no_payout_is_an_unexpected_reply(
    made: made_up.MadeUp, report: Report, caplog: pytest.LogCaptureFixture
) -> None:
    report.days[("funds", "2027-03-02")] = [
        page(
            entry(
                "9001-2999999",
                "payout-aborted",
                19750,
                0,
                19750,
                day="2027-03-02",
                reference="Vipps utbet. 2000023 Vippsnr 123456",
            )
        )
    ]
    with caplog.at_level(logging.ERROR):
        read()
    assert "payout report failed" in caplog.text
    assert (
        not Payout.objects.of(made.organization)
        .filter(aborted_at__isnull=False)
        .exists()
    )


def test_the_newest_refunds_are_paired_first_when_there_are_too_many(
    made: made_up.MadeUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert payouts.RETIE_LIMIT == 500
    monkeypatch.setattr(payouts, "RETIE_LIMIT", 2)
    expected = []
    for day in range(4):
        order = pay(made, day=day, reference=f"old-{day}")
        refund = Refund.objects.create(
            organization=made.organization,
            payment=order.payment,
            amount=100,
            made_by=made.administrator,
            completed_at=datetime(2027, 2, 1, tzinfo=UTC) + timedelta(days=day),
        )
        expected.append((refund.completed_at, f"old-{day}"))
    newest = [reference for _, reference in sorted(expected, reverse=True)][:2]
    assert payouts._unreported(made.organization) == sorted(newest)


def test_a_refund_is_judged_by_the_ledger_of_its_own_payment(
    made: made_up.MadeUp, report: Report
) -> None:
    completed(made, datetime(2027, 2, 28, 10, 0, tzinfo=UTC), amount=77700)
    read()
    (own,) = PaymentLedger.objects.of(made.organization)
    PaymentLedger.objects.create(
        organization=made.organization,
        account=own.account,
        ledger_id="9002",
        currency="NOK",
        funds_through=date(2027, 12, 31),
    )
    assert flagged(made) != set()
    PaymentLedger.objects.of(made.organization).filter(pk=own.pk).update(
        funds_through=date(2027, 3, 1)
    )
    assert flagged(made) == set()


def listed(org: Any, **query: Any) -> dict[str, Any]:
    found: Any = booking_admin.payments(org, booking_admin.PaymentsQuery(**query))
    return dict(found)


def test_one_attention_state_with_both_reasons(
    made: made_up.MadeUp, report: Report, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed(made, datetime(2027, 2, 28, 10, 0, tzinfo=UTC), amount=77700)
    read()
    payment = Payment.objects.of(made.organization).get()
    org = made.organization
    monkeypatch.setattr(booking_admin, "_now", lambda: NOW)
    # The refund missing from the report alone.
    (row,) = listed(org, state="attention")["payments"]
    assert (row["attention"], row["trouble"], row["missing_refund"]) == (
        True,
        None,
        True,
    )
    card: Any = booking_admin.booking(org, payment.order)["payment"]
    assert (card["attention"], card["trouble"], card["missing_refund"]) == (
        True,
        None,
        True,
    )
    # A check that has failed for more than 15 minutes as well: both reasons.
    PaymentTrouble.objects.create(
        organization=org,
        payment=payment,
        since=NOW - timedelta(hours=1),
        step="capture",
        kind="unanswered",
        first_step="capture",
        first_kind="unanswered",
        tries=4,
    )
    found = listed(org, state="attention")
    assert found["count"] == 1
    (row,) = found["payments"]
    assert row["trouble"]["tries"] == 4 and row["missing_refund"] is True
    # The trouble alone: the refund is shown by the report.
    Refund.objects.of(org).update(reported_at=NOW)
    (row,) = listed(org)["payments"]
    assert (row["attention"], row["missing_refund"]) == (True, False)
    # Neither: no trouble spell open, the refund shown.
    PaymentTrouble.objects.of(org).update(ended_at=NOW)
    assert listed(org, state="attention")["count"] == 0
