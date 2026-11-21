# SPDX-License-Identifier: AGPL-3.0-only
"""Member data and retention contracts on disposable PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connection, connections, transaction

from randirandi.core.member_data import (
    correct_member,
    export_member,
    operator_erase_member,
)
from randirandi.core.models import (
    AccessEvent,
    AuditEvent,
    Booking,
    BookingLine,
    CapacityAllocation,
    LoginCode,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    Order,
    Payment,
    Refund,
    SpotGroup,
    TakenTime,
)
from randirandi.core.retention import after_months, purge_organization
from randirandi.ops.jobs import JOBS
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.made_up import import_members
from tests.operator_erasure import erase, settle_coming

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2028, 3, 1, 12, tzinfo=UTC)


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def revoke(made: made_up.MadeUp, at: datetime) -> None:
    made.member.status = "disabled"
    made.member.listed = False
    made.member.unlisted_at = at
    made.member.save(update_fields=["status", "listed", "unlisted_at"])
    AccessEvent.objects.of(made.organization).filter(member=made.member).delete()
    AccessEvent.objects.create(
        organization=made.organization,
        member=made.member,
        change="disabled",
        cause="administrator",
        at=at,
    )


def test_export_is_scoped_complete_json_and_contains_no_login_secret(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("other")
    tried = datetime(2027, 2, 27, 9, 0, tzinfo=UTC)
    Payment.objects.of(made.organization).filter(pk=made.payment.pk).update(
        currency="DKK",
        redirect_url="https://apitest.vipps.no/checkout?token=private-test-token",
        create_phone="4700000000",
        create_description="Booking 1",
        create_receipt='{"orderLines": []}',
        details_sent_at=tried,
        captured_at=tried,
    )
    Order.objects.of(made.organization).filter(pk=made.order.pk).update(
        confirmation_due_at=tried, confirmation_tried_at=tried
    )
    Booking.objects.of(made.organization).filter(pk=made.booking.pk).update(
        reminder_tried_at=tried
    )
    Membership.objects.of(made.organization).filter(pk=made.membership.pk).update(
        address_unit="Flat 2",
        section="North",
        postal_code="0001",
        city="Harbourview",
        country="NO",
    )
    with application_login():
        result = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    assert result["member"]["email"] == made.member.email
    assert result["orders"][0]["id"] == made.order.pk
    assert result["orders"][0]["reference"] == made.order.reference
    # When its confirmation and a reminder were sent .
    assert result["orders"][0]["confirmation_sent_at"] is None
    assert result["bookings"][0]["reminded_at"] is None
    assert result["bookings"][0]["taken_time__ends_at"] == "2027-03-01T12:00:00Z"
    # The container it was booked under, by the number the member sees.
    assert result["bookings"][0]["booking_configuration__number"] == 1
    assert result["booking_lines"][0]["amount"] == 25000
    assert result["booking_lines"][0]["spot_group__number"] is None
    assert result["payments"][0]["amount"] == 25000
    assert result["payments"][0]["captured_at"] == "2027-02-27T09:00:00Z"
    assert result["payments"][0]["currency"] == "DKK"
    # Delivery and conditions fields: when mail was due and tried, and what the member
    # saw in the Vipps app. The phone's copy stays out .
    assert (
        result["orders"][0]["confirmation_due_at"],
        result["orders"][0]["confirmation_tried_at"],
        result["bookings"][0]["reminder_tried_at"],
        result["payments"][0]["details_sent_at"],
    ) == ("2027-02-27T09:00:00Z",) * 4
    assert (
        result["payments"][0]["create_description"],
        result["payments"][0]["create_receipt"],
    ) == ("Booking 1", '{"orderLines": []}')
    assert "create_phone" not in result["payments"][0]
    # The payment check's trouble record is not the member's data (it holds
    # fixed words about the payment, never a person): it stays out.
    assert not [k for k in result["payments"][0] if k.startswith("trouble")]
    assert "troubles" not in result
    assert "4700000000" not in json.dumps(result)
    assert result["refunds"][0]["amount"] == 10000
    assert result["memberships"][0]["identifier"] == "H0201"
    assert result["member"]["full_name"] == "Alex Rivers"
    assert result["memberships"][0]["address_unit"] == "Flat 2"
    assert result["memberships"][0]["section"] == "North"
    assert result["memberships"][0]["postal_code"] == "0001"
    assert (result["memberships"][0]["city"], result["memberships"][0]["country"]) == (
        "Harbourview",
        "NO",
    )
    encoded = json.dumps(result)
    for forbidden in (
        other.member.email,
        made.administrator.email,
        "scrambled",
        "scrambled_code",
        "private-test-token",
        "redirect_url",
    ):
        assert forbidden not in encoded


def test_export_names_the_last_booking_category_and_erasure_empties_it(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        before = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    assert before["member"]["last_booking_category_name"] is None
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        last_booking_category=made.private
    )
    with application_login():
        chosen = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
        erase(made, made.member.pk)
        erased = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    assert chosen["member"]["last_booking_category_name"] == "private"
    stored = Member.objects.of(made.organization).get(pk=made.member.pk)
    assert stored.last_booking_category_id is None
    assert erased["member"]["last_booking_category_name"] is None


def test_export_names_the_group_a_booking_line_was_priced_for(
    made: made_up.MadeUp,
) -> None:
    group = SpotGroup(
        organization=made.organization,
        floor_plan=made_up.floor_plan_of(made.asset),
        name="Window",
    )
    group.save()
    lines = BookingLine.objects.of(made.organization).filter(
        booking__member=made.member
    )
    assert lines.update(spot_group=group) == 1
    with application_login():
        result = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    assert result["booking_lines"][0]["spot_group__number"] == group.number


def test_export_gives_each_booking_its_spot_numbers_in_number_order(
    made: made_up.MadeUp,
) -> None:
    sauna = made.shared("Sauna", 4)
    assert sauna.floor_plan is not None
    # Picked as 3 and 2: the export gives them in number order.
    benches = made.book(
        16, 18, configuration=sauna, units=made_up.spots_of(sauna.floor_plan, 3, 2)
    )
    with application_login():
        result = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    spots = {row["id"]: row["spot_numbers"] for row in result["bookings"]}
    assert spots[benches.pk] == [2, 3]
    # An exclusive booking holds the whole asset, no spot.
    assert spots[made.booking.pk] == []


@pytest.mark.parametrize("operation", ["export", "correct"])
@pytest.mark.parametrize("actor_kind", ["member", "revoked", "foreign"])
def test_current_administrator_authority_is_required(
    made: made_up.MadeUp, operation: str, actor_kind: str
) -> None:
    actor = made.administrator
    if actor_kind == "member":
        actor = made.member
    elif actor_kind == "revoked":
        Member.objects.of(made.organization).filter(pk=actor.pk).update(
            status="disabled"
        )
    else:
        actor = made_up.everything("other").administrator
    with pytest.raises((PermissionDenied, Member.DoesNotExist)):
        if operation == "export":
            export_member(actor=actor, member_id=made.member.pk, **fresh(actor))
        else:
            correct_member(
                actor=actor,
                member_id=made.member.pk,
                changes={"first_name": "Wrong"},
                **fresh(actor),
            )
    made.member.refresh_from_db()
    assert made.member.first_name == "Alex"


def test_correction_normalizes_phone_invalidates_codes_and_keeps_identity(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        changed = correct_member(
            actor=made.administrator,
            member_id=made.member.pk,
            changes={"first_name": "Taylor", "phone": "+47 90000003"},
            **fresh(made.administrator),
        )
    assert changed.first_name == "Taylor" and changed.phone == "+4790000003"
    assert changed.email == made.member.email
    assert (
        not LoginCode.objects.of(made.organization).filter(member=made.member).exists()
    )
    assert made.booking.member_id == changed.pk


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "new@example.com"},
        {"role_id": "123"},
        {"status": "active"},
        {"phone": "invalid"},
        {"phone": "+4790000002"},
        {},
    ],
)
def test_invalid_correction_rolls_back_every_field(
    made: made_up.MadeUp, changes: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        correct_member(
            actor=made.administrator,
            member_id=made.member.pk,
            changes=changes,
            **fresh(made.administrator),
        )
    made.member.refresh_from_db()
    assert made.member.phone == "+4790000001"
    assert LoginCode.objects.of(made.organization).filter(member=made.member).exists()


@pytest.mark.parametrize("shared", [False, True])
def test_erasure_removes_identity_links_and_codes_but_keeps_transactions(
    made: made_up.MadeUp, shared: bool
) -> None:
    org = made.organization
    if shared:
        MemberMembership.objects.create(
            organization=org, member=made.administrator, membership=made.membership
        )
    with application_login():
        erase(made, made.member.pk)
        with pytest.raises(PermissionDenied, match="already erased"):
            erase(made, made.member.pk)
        result = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    made.member.refresh_from_db()
    assert made.member.status == "disabled"
    assert all(
        not getattr(made.member, name)
        for name in ("full_name", "first_name", "last_name", "email", "phone")
    )
    assert result["memberships"] == result["access_events"] == []
    assert Membership.objects.of(org).filter(pk=made.membership.pk).exists() == shared
    assert not LoginCode.objects.of(org).filter(member=made.member).exists()
    assert Booking.objects.of(org).get(pk=made.booking.pk).member_id == made.member.pk
    assert Payment.objects.of(org).filter(pk=made.payment.pk).exists()
    assert (
        CapacityAllocation.objects.of(org)
        .filter(taken_time=made.booking.taken_time)
        .exists()
    )
    for value in ("Alex", "Rivers", "alex@harbourview.example", "+4790000001"):
        assert value not in json.dumps(result)
    with pytest.raises(ValidationError):
        correct_member(
            actor=made.administrator,
            member_id=made.member.pk,
            changes={"first_name": "Restore"},
            **fresh(made.administrator),
        )


@pytest.mark.parametrize(
    "months,start,expected",
    [
        (1, "2027-01-31T12:00:00+00:00", "2027-02-28T12:00:00+00:00"),
        (12, "2028-02-29T12:00:00+00:00", "2029-02-28T12:00:00+00:00"),
        (36, "2027-03-01T12:00:00+00:00", "2030-03-01T12:00:00+00:00"),
    ],
)
def test_retention_uses_clamped_calendar_months(
    months: int, start: str, expected: str
) -> None:
    assert after_months(
        datetime.fromisoformat(start), months
    ) == datetime.fromisoformat(expected)


@pytest.mark.parametrize("delta,erased", [(-1, False), (0, True), (1, True)])
def test_revoked_identity_expires_at_exact_anniversary(
    made: made_up.MadeUp, delta: int, erased: bool
) -> None:
    revoke(made, NOW.replace(year=2027))
    with application_login():
        result = purge_organization(
            made.organization, now=NOW + timedelta(microseconds=delta)
        )
    made.member.refresh_from_db()
    assert (made.member.email == "") == erased
    assert result["members"] == int(erased)
    assert Booking.objects.of(made.organization).filter(pk=made.booking.pk).exists()


@pytest.mark.parametrize("case", ["unknown", "regranted"])
def test_missing_or_reset_revocation_clock_keeps_identity(
    made: made_up.MadeUp, case: str
) -> None:
    revoke(made, NOW.replace(year=2020))
    if case == "unknown":
        made.member.unlisted_at = None
        made.member.save(update_fields=["unlisted_at"])
    else:
        made.member.status = "active"
        made.member.listed = True
        made.member.unlisted_at = None
        made.member.save(update_fields=["status", "listed", "unlisted_at"])
    assert purge_organization(made.organization, now=NOW)["members"] == 0
    made.member.refresh_from_db()
    assert made.member.email


def test_due_booking_removes_its_complete_graph_but_not_another_organization(
    made: made_up.MadeUp,
) -> None:
    other = made_up.everything("other")
    taken_id = made.booking.taken_time_id
    deadline = datetime(2030, 3, 1, 12, tzinfo=UTC)
    with application_login():
        assert (
            purge_organization(
                made.organization, now=deadline - timedelta(microseconds=1)
            )["bookings"]
            == 0
        )
        assert purge_organization(made.organization, now=deadline)["bookings"] == 1
        assert purge_organization(made.organization, now=deadline)["bookings"] == 0
    for model in (Booking, BookingLine, Payment, Refund):
        assert model.objects.of(made.organization).count() == 0
        assert model.objects.of(other.organization).exists()
    assert not TakenTime.objects.of(made.organization).filter(pk=taken_id).exists()
    assert (
        not CapacityAllocation.objects.of(made.organization)
        .filter(taken_time_id=taken_id)
        .exists()
    )
    assert not Order.objects.of(made.organization).filter(pk=made.order.pk).exists()


def test_mixed_age_order_keeps_payment_until_last_booking_expires(
    made: made_up.MadeUp,
) -> None:
    made.day = made.day.replace(year=2028)
    later = made.book(10, 12, order=made.order)
    with application_login():
        purge_organization(made.organization, now=datetime(2030, 3, 1, 12, tzinfo=UTC))
        assert Payment.objects.of(made.organization).filter(pk=made.payment.pk).exists()
        assert list(
            Booking.objects.of(made.organization).values_list("pk", flat=True)
        ) == [later.pk]
        purge_organization(made.organization, now=datetime(2031, 3, 1, 12, tzinfo=UTC))
        assert not Order.objects.of(made.organization).filter(pk=made.order.pk).exists()
        assert Payment.objects.of(made.organization).count() == 0


def test_due_import_detaches_retained_access_history_and_cleans_expired_codes(
    made: made_up.MadeUp,
) -> None:
    org = made.organization
    run = MemberImport.objects.of(org).get()
    run.started_at = NOW.replace(year=2020)
    run.save(update_fields=["started_at"])
    event = AccessEvent.objects.of(org).get(member=made.member)
    event.member_import = run
    event.save(update_fields=["member_import"])
    LoginCode.objects.of(org).all().delete()
    for expires, used in [
        (NOW, None),
        (NOW + timedelta(days=1), NOW),
        (NOW + timedelta(days=1), None),
    ]:
        LoginCode.objects.create(
            organization=org,
            member=made.member,
            scrambled_code="fictional",
            expires_at=expires,
            used_at=used,
        )
    AuditEvent.objects.of(org).update(at=NOW.replace(year=2020))
    with application_login():
        counts = purge_organization(org, now=NOW)
    assert counts["member_imports"] == counts["audit_events"] == 1
    assert counts["login_codes"] == 2
    event.refresh_from_db()
    assert event.member_import_id is None
    assert LoginCode.objects.of(org).count() == 1


@pytest.mark.parametrize("manual", [False, True])
def test_ambient_transaction_refusal_preserves_callers_work(
    made: made_up.MadeUp, manual: bool
) -> None:
    if manual:
        connection.set_autocommit(False)
    try:
        with transaction.atomic():
            Member.objects.of(made.organization).filter(pk=made.member.pk).update(
                first_name="Pending"
            )
            with pytest.raises(RuntimeError, match="own transaction"):
                operator_erase_member(
                    organization=made.organization, member_id=made.member.pk
                )
            assert (
                Member.objects.of(made.organization).get(pk=made.member.pk).first_name
                == "Pending"
            )
    finally:
        if manual:
            connection.rollback()
            connection.set_autocommit(True)


def test_concurrent_export_and_erasure_never_return_mixed_identity(
    made: made_up.MadeUp,
) -> None:
    barrier = Barrier(2)

    def work(erase: bool) -> dict[str, object] | None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if erase:
                operator_erase_member(
                    organization=made.organization, member_id=made.member.pk
                )
                return None
            return export_member(
                actor=made.administrator,
                member_id=made.member.pk,
                **fresh(made.administrator),
            )
        finally:
            connections.close_all()

    settle_coming(made, made.member.pk)
    with application_login(), ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(work, False)
        erased = pool.submit(work, True)
        result = future.result(timeout=20)
        erased.result(timeout=20)
    assert result is not None
    details = result["member"]
    assert isinstance(details, dict)
    assert (
        details["first_name"],
        details["last_name"],
        details["email"],
        details["phone"],
    ) in [("Alex", "Rivers", made.member.email, "+4790000001"), ("", "", "", "")]


def test_actual_scheduled_job_and_restore_command_run_retention(
    made: made_up.MadeUp,
) -> None:
    revoke(made, datetime(2000, 1, 1, tzinfo=UTC))
    job = next(job for job in JOBS if job.name == "retention")
    assert job.every == timedelta(hours=1)
    with application_login():
        job.run()
        call_command("purge_expired", stdout=StringIO())
    made.member.refresh_from_db()
    assert made.member.email == ""


@pytest.mark.parametrize("operation", ["erase", "purge"])
def test_former_imported_address_is_removed_without_harming_shared_entitlements(
    made: made_up.MadeUp, operation: str
) -> None:
    from randirandi.member_file import MemberTable

    other = made_up.everything("other")
    source = made_up.Source(made.organization)
    source.column_map = {
        key: key
        for key in (
            "membership",
            "first_name",
            "last_name",
            "email",
            "phone",
            "address",
        )
    }
    shared = Membership.objects.create(
        organization=made.organization, identifier="shared", address="Still used"
    )
    MemberMembership.objects.create(
        organization=made.organization, member=made.administrator, membership=shared
    )
    table = MemberTable(
        tuple(source.column_map),
        (
            (
                "H0301",
                "Alex",
                "Rivers",
                made.member.email,
                made.member.phone,
                "New address",
            ),
        ),
    )
    with application_login():
        assert (
            import_members(source, table, started_by=made.administrator).outcome
            == "applied"
        )
        if operation == "erase":
            erase(made, made.member.pk)
        else:
            purge_organization(made.organization, now=NOW)
    assert (
        not Membership.objects.of(made.organization)
        .filter(pk=made.membership.pk)
        .exists()
    )
    assert Membership.objects.of(made.organization).filter(pk=shared.pk).exists()
    assert (
        Membership.objects.of(other.organization)
        .filter(pk=other.membership.pk)
        .exists()
    )


def test_export_covers_transaction_audit_targets_and_direct_actor_links(
    made: made_up.MadeUp,
) -> None:
    from randirandi.core.models import Block

    org = made.organization
    expected = []
    for kind, row in (
        ("booking", made.booking),
        ("order", made.order),
        ("payment", made.payment),
    ):
        expected.append(
            AuditEvent.objects.create(
                organization=org,
                administrator=made.administrator,
                action="inspected",
                object_kind=kind,
                object_id=row.pk,
            ).pk
        )
    run = MemberImport.objects.of(org).get()
    run.started_by = made.administrator
    run.save(update_fields=["started_by"])
    event = AccessEvent.objects.of(org).get()
    event.administrator = made.administrator
    event.save(update_fields=["administrator"])
    made.booking.cancelled_by = made.administrator
    made.booking.save(update_fields=["cancelled_by"])
    Block.objects.of(org).update(created_at=NOW)
    with application_login():
        subject = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
        admin = export_member(
            actor=made.administrator,
            member_id=made.administrator.pk,
            **fresh(made.administrator),
        )
    assert set(expected) <= {row["id"] for row in subject["audit_events"]}
    assert admin["administered_access_events"][0]["id"] == event.pk
    assert admin["started_imports"][0]["id"] == run.pk
    assert admin["cancelled_bookings"][0]["id"] == made.booking.pk
    assert admin["made_refunds"][0]["id"] == Refund.objects.of(org).get().pk
    assert admin["made_blocks"][0]["id"] == Block.objects.of(org).get().pk
    assert admin["made_blocks"][0]["created_at"] == "2028-03-01T12:00:00Z"
    encoded = json.dumps(admin)
    for value in (
        made.member.email,
        made.member.phone,
        "Floor sanding",
        "25000",
        "report",
    ):
        assert value not in encoded


def test_full_name_is_corrected_and_a_name_only_member_is_purged(
    made: made_up.MadeUp,
) -> None:
    with application_login():
        changed = correct_member(
            actor=made.administrator,
            member_id=made.member.pk,
            changes={"full_name": "Alexandra Rivers"},
            **fresh(made.administrator),
        )
    assert changed.full_name == "Alexandra Rivers"
    # Retention counts the full name: it alone keeps the member unerased.
    revoke(made, datetime(2000, 1, 1, tzinfo=UTC))
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        first_name="", last_name="", email="", phone=""
    )
    with application_login():
        assert purge_organization(made.organization, now=NOW)["members"] == 1
    made.member.refresh_from_db()
    assert made.member.full_name == ""


def _named_run(made: made_up.MadeUp) -> MemberImport:
    other = made.administrator
    return MemberImport.objects.create(
        organization=made.organization,
        outcome="applied",
        report={
            "disabled": [made.member.pk, other.pk],
            "proposed_revocations": [made.member.pk],
            "findings": [
                {
                    "code": "ambiguous_member",
                    "rows": [3],
                    "members": [made.member.pk, other.pk],
                }
            ],
            "future_bookings": [],
        },
    )


def test_export_lists_the_runs_whose_report_names_the_member(
    made: made_up.MadeUp,
) -> None:
    run = _named_run(made)
    unrelated = MemberImport.objects.create(
        organization=made.organization, outcome="applied", report={"disabled": []}
    )
    with application_login():
        data = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    ids = [row["id"] for row in data["named_in_imports"]]
    assert run.pk in ids and unrelated.pk not in ids


def test_erasure_takes_the_member_out_of_every_report(made: made_up.MadeUp) -> None:
    run = _named_run(made)
    settle_coming(made, made.member.pk)
    with application_login():
        operator_erase_member(organization=made.organization, member_id=made.member.pk)
    run.refresh_from_db()
    report = run.report
    assert made.member.pk not in report["disabled"] and len(report["disabled"]) == 1
    assert report["proposed_revocations"] == []
    assert made.member.pk not in report["findings"][0]["members"]
    assert len(report["findings"][0]["members"]) == 1
