# SPDX-License-Identifier: AGPL-3.0-only
"""Complete-list reconciliation on PostgreSQL, using fictional people only."""

import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from randirandi.core import member_import
from randirandi.core.member_admin import first_administrator
from randirandi.core.models import (
    AccessEvent,
    AuditEvent,
    ImportOutcome,
    LoginCode,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    MemberStatus,
    Organization,
    Origin,
    Role,
)
from randirandi.environment import Switches
from randirandi.member_file import MemberTable, read_csv
from tests import made_up
from tests.capacity_app import application_login
from tests.made_up import import_members

pytestmark = pytest.mark.django_db
COLUMNS = ("membership", "first_name", "last_name", "email", "phone", "address")
DUMMY = Path(__file__).resolve().parent.parent / "dummy-members.csv"


def row(number: int = 1, /, **change: str) -> tuple[str, ...]:
    values = dict(
        zip(
            COLUMNS,
            (
                f"M{number}",
                "Member",
                f"Example{number}",
                f"member{number}@example.com",
                f"+479{number:07d}",
                "",
            ),
            strict=True,
        )
    )
    values.update(change)
    return tuple(values[name] for name in COLUMNS)


def table(*rows: tuple[str, ...]) -> MemberTable:
    return MemberTable(COLUMNS, rows)


def source_for(org: Organization) -> made_up.Source:
    return made_up.Source(org)


@pytest.fixture
def source() -> made_up.Source:
    org = made_up.organization()
    role = Role.objects.create(organization=org, name="Members")
    org.standard_role = role
    org.save(update_fields=["standard_role"])
    return source_for(org)


def member(source: made_up.Source, number: int = 1) -> Member:
    return Member.objects.of(source.organization_id).get(
        email=f"member{number}@example.com"
    )


def codes(run: MemberImport) -> set[str]:
    return {item["code"] for item in run.report["findings"]}


def test_csv_reader_keeps_quoted_columns_bom_and_privacy() -> None:
    parsed = read_csv('\ufeffa,b\n"one,two",three\n')
    assert parsed == MemberTable(("a", "b"), (("one,two", "three"),))
    assert "one,two" not in repr(parsed)
    assert read_csv('a,b\n"unfinished').problem == "invalid_csv"


def test_dummy_groups_memberships_and_never_imports_roles(
    source: made_up.Source,
) -> None:
    run = import_members(source, read_csv(DUMMY.read_text()))
    assert (run.created, run.granted, run.changed, run.revoked, run.skipped) == (
        10,
        10,
        0,
        0,
        0,
    )
    assert run.outcome == ImportOutcome.APPLIED
    members = Member.objects.of(source.organization_id)
    assert members.count() == 10
    assert set(members.values_list("role_id", flat=True)) == {
        source.organization.standard_role_id
    }
    nils = members.get(email="nils.fiktiv@example.com")
    assert (
        MemberMembership.objects.of(source.organization_id).filter(member=nils).count()
        == 2
    )
    assert Membership.objects.of(source.organization_id).count() == 8
    assert (
        AccessEvent.objects.of(source.organization_id).filter(member_import=run).count()
        == 10
    )
    assert nils.phone == "+4700000010"
    assert nils.full_name == "Nils Fiktiv"
    flat = Membership.objects.of(source.organization_id).get(
        identifier="H0101", address="Omsens gate 22"
    )
    assert (flat.address_unit, flat.section) == ("Flat 1", "North")
    repeated = import_members(source, read_csv(DUMMY.read_text()))
    assert (repeated.created, repeated.changed, repeated.granted, repeated.revoked) == (
        0,
        0,
        0,
        0,
    )
    assert AccessEvent.objects.of(source.organization_id).count() == 10


def test_normalizes_contact_details_and_deduplicates_rows(
    source: made_up.Source,
) -> None:
    data = row(email=" MEMBER1@EXAMPLE.COM ", phone="+47 900 00 001")
    run = import_members(source, table(data, data, row(membership="SECOND")))
    assert run.created == 1
    assert member(source).phone == "+4790000001"
    assert MemberMembership.objects.of(source.organization_id).count() == 2


def test_updates_details_and_memberships_but_keeps_assigned_role(
    source: made_up.Source,
) -> None:
    import_members(source, table(row(), row(membership="SECOND")))
    person = member(source)
    role = Role.objects.create(organization_id=source.organization_id, name="Other")
    person.role = role
    person.save(update_fields=["role"])
    run = import_members(
        source, table(row(first_name="Updated", phone="+4799999999", membership="NEW"))
    )
    person.refresh_from_db()
    assert run.changed == 1
    assert person.first_name == "Updated"
    assert person.role == role
    assert person.phone == "+4799999999"
    assert list(person.memberships.values_list("identifier", flat=True)) == ["NEW"]
    assert Membership.objects.of(source.organization_id).count() == 3


@pytest.mark.parametrize(
    "size,removed,stopped", [(5, 1, False), (10, 2, False), (4, 1, True), (10, 3, True)]
)
def test_twenty_percent_is_allowed_more_stops_every_change(
    source: made_up.Source, size: int, removed: int, stopped: bool
) -> None:
    import_members(source, table(*(row(i) for i in range(1, size + 1))))
    run = import_members(
        source,
        table(
            *(row(i, first_name="Updated") for i in range(removed + 1, size + 1)),
            row(100),
        ),
    )
    assert run.outcome == (ImportOutcome.STOPPED if stopped else ImportOutcome.APPLIED)
    assert run.revoked == (0 if stopped else removed)
    assert run.created == (0 if stopped else 1)
    assert member(source, size).first_name == ("Member" if stopped else "Updated")
    assert run.report["reason"] == ("revocation_limit" if stopped else "")


def test_revocation_voids_codes_retains_history_and_regrant_reuses_member(
    source: made_up.Source,
) -> None:
    data = [row(i) for i in range(1, 6)]
    import_members(source, table(*data))
    person = member(source)
    code = LoginCode.objects.create(
        organization_id=source.organization_id,
        member=person,
        scrambled_code="unusable-test-value",
        expires_at=made_up.at(made_up.A_DAY, 10),
    )
    revoked = import_members(source, table(*data[1:]))
    person.refresh_from_db()
    code.refresh_from_db()
    assert person.status == MemberStatus.DISABLED and not person.is_active
    assert code.used_at is not None
    assert revoked.revoked == 1
    assert Membership.objects.of(source.organization_id).count() == 5
    assert MemberMembership.objects.of(source.organization_id).count() == 5
    granted = import_members(source, table(*data))
    assert granted.granted == 1 and granted.created == 0
    assert member(source).pk == person.pk
    assert list(
        AccessEvent.objects.of(source.organization_id)
        .filter(member=person)
        .order_by("pk")
        .values_list("change", flat=True)
    ) == ["active", "disabled", "active"]
    code.refresh_from_db()
    assert code.used_at is not None


@pytest.mark.parametrize(
    "change",
    [
        {"email": "bad"},
        {"phone": "004790000001"},
        {"phone": "+0"},
        {"first_name": "", "last_name": ""},
        {"last_name": "x" * 151},
        {"membership": ""},
        {"address": "x" * 201},
        {"first_name": "bad\x00name"},
    ],
)
def test_bad_rows_never_revoke_or_remove_memberships(
    source: made_up.Source, change: dict[str, str]
) -> None:
    data = [row(i) for i in range(1, 6)]
    import_members(source, table(*data, row(2, membership="SECOND")))
    run = import_members(source, table(row(1, **change), *data[1:4]))
    assert run.outcome == ImportOutcome.APPLIED and run.skipped == 1
    assert run.report["removals_deferred"]
    assert run.revoked == 0
    assert MemberMembership.objects.of(source.organization_id).count() == 6
    assert set(
        Member.objects.of(source.organization_id).values_list("status", flat=True)
    ) == {"active"}


@pytest.mark.parametrize(
    "change",
    [{"phone": "+4790000002"}, {"first_name": "Different"}, {"last_name": "Different"}],
)
def test_inconsistent_duplicate_person_skips_every_row(
    source: made_up.Source, change: dict[str, str]
) -> None:
    run = import_members(
        source, table(row(), row(membership="SECOND", **change), row(3))
    )
    assert run.created == 1 and run.skipped == 2
    assert "inconsistent_person" in codes(run)
    assert (
        not Member.objects.of(source.organization_id)
        .filter(email="member1@example.com")
        .exists()
    )


def test_duplicate_phone_is_order_independent(source: made_up.Source) -> None:
    for incoming in ((row(), row(2, phone=row()[4])), (row(2, phone=row()[4]), row())):
        run = import_members(source, table(*incoming))
        assert run.skipped == 2 and run.created == 0
        assert "phone_in_multiple_rows" in codes(run)


def test_email_phone_pointing_to_different_members_is_skipped(
    source: made_up.Source,
) -> None:
    import_members(source, table(row(), row(2)))
    run = import_members(source, table(row(phone=row(2)[4])))
    assert run.skipped == 1 and run.changed == 0 and run.revoked == 0
    assert codes(run) == {"identity_conflict"}
    assert member(source).phone == row()[4]


@pytest.mark.parametrize("by_phone", [True, False])
def test_changed_email_waits_for_admin_and_does_not_change_other_details(
    source: made_up.Source, by_phone: bool
) -> None:
    import_members(source, table(row()))
    data = row(email="new@example.com", phone=row()[4] if by_phone else "+4799999999")
    run = import_members(source, table(data))
    assert codes(run) == {"email_change_requires_administrator"}
    assert run.created == run.changed == run.granted == run.revoked == 0
    assert run.skipped == 1
    assert member(source).phone == row()[4]
    assert run.report["findings"][0]["members"] == [member(source).pk]
    assert "new@example.com" not in json.dumps(run.report)


def test_ambiguous_name_and_membership_never_creates_a_third_identity(
    source: made_up.Source,
) -> None:
    import_members(source, table(row(), row(2, membership="M1", last_name="Example1")))
    run = import_members(source, table(row(email="new@example.com", phone="")))
    assert codes(run) == {"ambiguous_member"}
    assert run.created == run.revoked == 0 and run.skipped == 1


def test_two_email_groups_cannot_update_the_same_member(source: made_up.Source) -> None:
    import_members(source, table(row()))
    run = import_members(source, table(row(), row(email="new@example.com", phone="")))
    assert run.skipped == 2 and run.created == run.changed == 0
    assert "member_in_multiple_groups" in codes(run)


def test_admin_managed_members_are_never_changed_or_revoked(
    source: made_up.Source,
) -> None:
    import_members(source, table(*(row(i) for i in range(1, 7))))
    person = member(source)
    person.origin = Origin.ADMINISTRATOR
    person.save(update_fields=["origin"])
    run = import_members(
        source, table(row(first_name="Replacement"), *(row(i) for i in range(3, 7)))
    )
    assert run.revoked == 1
    assert member(source).first_name == "Member"
    assert codes(run) == {"administrator_managed"}
    run = import_members(source, table(*(row(i) for i in range(3, 7))))
    assert run.revoked == 0
    assert member(source).status == MemberStatus.ACTIVE


@pytest.mark.parametrize(
    "incoming,reason",
    [
        (MemberTable((), ()), "invalid_columns"),
        (MemberTable(COLUMNS, ()), "empty_list"),
        (MemberTable(COLUMNS[:-1], (row(),)), "invalid_row_width"),
        (
            MemberTable((*COLUMNS[:3], *COLUMNS[4:]), (row()[:3] + row()[4:],)),
            "missing_column",
        ),
        (
            MemberTable((*COLUMNS, "email"), ((*row(), "new@example.com"),)),
            "invalid_columns",
        ),
        (
            MemberTable((" membership", *COLUMNS), (("x", *row()),)),
            "invalid_columns",
        ),
        (read_csv('email\n"bad'), "invalid_csv"),
    ],
)
def test_structural_errors_stop_without_member_changes(
    source: made_up.Source, incoming: MemberTable, reason: str
) -> None:
    import_members(source, table(row()))
    run = import_members(source, incoming)
    # A list with nobody on it is refused; a file the parser cannot read fails.
    assert run.outcome == (
        ImportOutcome.STOPPED if reason == "empty_list" else ImportOutcome.FAILED
    )
    assert run.report["reason"] == reason
    assert member(source).status == MemberStatus.ACTIVE
    assert AccessEvent.objects.of(source.organization_id).count() == 1


def test_optional_phone_and_address_values_and_address_column(
    source: made_up.Source,
) -> None:
    run = import_members(source, MemberTable(COLUMNS[:-1], (row(phone="")[:-1],)))
    assert run.created == 1
    assert member(source).phone == ""


def test_the_parsers_column_map_is_used(source: made_up.Source) -> None:
    source.column_map = {name: f"column-{i}" for i, name in enumerate(COLUMNS)}
    columns = tuple(source.column_map.values())
    run = import_members(source, MemberTable(columns, (row(),)))
    assert run.created == 1


@pytest.mark.parametrize(
    "mapping",
    [
        {"bad": "column"},
        {"email": "phone"},
        {"email": 42},
        {"address": "missing"},
        [],
        None,
    ],
)
def test_a_bad_column_map_fails(source: made_up.Source, mapping: Any) -> None:
    # JSON null is SQL non-null; use a list to exercise the non-dict branch.
    source.column_map = mapping if mapping is not None else [None]
    run = import_members(source, table(row()))
    assert run.outcome == ImportOutcome.FAILED
    assert run.report["reason"] in {"invalid_column_map", "missing_column"}


@pytest.mark.parametrize("admin", [False, True])
def test_missing_or_admin_standard_role_stops(
    source: made_up.Source, admin: bool
) -> None:
    org = source.organization
    if admin:
        assert org.standard_role_id is not None
        Role.objects.of(org).filter(pk=org.standard_role_id).update(admin_access=True)
    else:
        org.standard_role = None
        org.save(update_fields=["standard_role"])
    run = import_members(source, table(row()))
    assert run.report["reason"] == "standard_role_must_be_non_admin"
    assert run.outcome == ImportOutcome.STOPPED


def test_import_is_organization_scoped_and_actor_is_rechecked(
    source: made_up.Source,
) -> None:
    theirs = made_up.everything("other")
    import_members(source, table(row()))
    other_source = source_for(theirs.organization)
    other_run = import_members(
        other_source, table(row()), started_by=theirs.administrator
    )
    assert other_run.created == 0  # The other organization owns this phone too.
    assert other_run.outcome == ImportOutcome.APPLIED
    assert codes(other_run) == {"email_change_requires_administrator"}
    assert theirs.member.email != member(source).email
    for actor in (theirs.administrator, member(source)):
        actor.role = theirs.board  # A stale/forged caller does not confer permission.
        with pytest.raises(PermissionDenied):
            import_members(source, table(row()), started_by=actor)
    assert MemberImport.objects.of(source.organization_id).count() == 1


def test_future_bookings_are_reported_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: made_up.at(made_up.A_DAY, 8))
    made = made_up.everything()
    source = source_for(made.organization)
    baseline = row(
        first_name=made.member.first_name,
        last_name=made.member.last_name,
        email=made.member.email,
        phone=made.member.phone,
        membership="H0201",
    )
    import_members(source, table(baseline, *(row(i) for i in range(10, 14))))
    run = import_members(source, table(*(row(i) for i in range(10, 14))))
    assert run.revoked == 1
    assert run.report["future_bookings"] == [made.booking.pk]
    made.booking.taken_time.refresh_from_db()
    assert made.booking.taken_time.state == "confirmed"


def test_write_failure_rolls_back_every_member_change_and_records_failure(
    source: made_up.Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = member_import._memberships
    calls = 0

    def fail(person: member_import.Person, member: Member, *, remove: bool) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            # A real native unique violation breaks this savepoint after the
            # first person's rows have already been written.
            Member.objects.create(
                organization_id=member.organization_id,
                role_id=member.role_id,
                email=member.email,
                status=MemberStatus.ACTIVE,
                origin=Origin.MEMBER_IMPORT,
            )
        return original(person, member, remove=remove)

    monkeypatch.setattr(member_import, "_memberships", fail)
    run = import_members(source, table(row(), row(2)))
    assert run.outcome == ImportOutcome.FAILED
    assert run.created == run.granted == 0
    assert run.report["reason"] == "write_conflict"
    assert Member.objects.of(source.organization_id).count() == 0
    assert AccessEvent.objects.of(source.organization_id).count() == 0
    assert Membership.objects.of(source.organization_id).count() == 0
    run.refresh_from_db()
    assert run.outcome == ImportOutcome.FAILED


def test_report_does_not_copy_personal_fields(source: made_up.Source) -> None:
    run = import_members(
        source, table(row(email="invalid-private-address", phone="secret-phone"))
    )
    report = json.dumps(run.report)
    for value in ("invalid-private-address", "secret-phone", "Example1", "M1"):
        assert value not in report


def test_dummy_command_is_explicit_repeatable_and_role_neutral(
    source: made_up.Source,
) -> None:
    assert Member.objects.of(source.organization_id).count() == 0
    output = StringIO()
    for _ in range(2):
        call_command(
            "import_dummy_members", source.organization.subdomain, stdout=output
        )
    assert "created=10" in output.getvalue() and "created=0" in output.getvalue()
    assert "example.com" not in output.getvalue()
    assert (
        not Member.objects.of(source.organization_id)
        .filter(role__admin_access=True)
        .exists()
    )
    runs = MemberImport.objects.of(source.organization_id)
    assert [(r.parser, r.file_name, r.started_by_id) for r in runs] == [
        ("made-up-members", "dummy-members.csv", None)
    ] * 2


@pytest.mark.parametrize(
    "environment,enabled",
    [("production", False), ("production", True), ("sandbox", False)],
)
def test_dummy_command_refuses_production_or_disabled_switch_before_reads(
    environment: str, enabled: bool
) -> None:
    switches = Switches(True, True, enabled, False)
    with (
        override_settings(ENVIRONMENT=environment, SWITCHES=switches),
        pytest.raises(CommandError, match=r"only.*sandbox"),
    ):
        call_command("import_dummy_members", "does-not-exist")


def test_first_administrator_is_explicit_idempotent_and_kept_by_import(
    source: made_up.Source,
) -> None:
    import_members(source, table(row(), row(2)))
    org = source.organization
    Role.objects.create(organization=org, name="Board", admin_access=True)
    person = member(source)
    out = StringIO()
    for _ in range(2):
        call_command(
            "first_administrator", org.subdomain, person.pk, "Board", stdout=out
        )
    person.refresh_from_db()
    assert person.is_staff and person.origin == Origin.ADMINISTRATOR
    event = AuditEvent.objects.of(org).get(action="operator named first administrator")
    # The gain of admin access is its own event, once.
    gave = AuditEvent.objects.of(org).get(action="gave admin access")
    assert gave.administrator == person and gave.object_id == person.pk
    assert event.administrator == person and event.object_id == person.pk
    assert event.action == "operator named first administrator"
    assert person.email not in out.getvalue()
    run = import_members(source, table(row(2)))
    assert run.revoked == 0
    with pytest.raises(ValidationError, match="already exists"):
        first_administrator(org, member(source, 2).pk, "Board")


def test_bootstrap_refuses_revoked_foreign_member_and_non_admin_role(
    source: made_up.Source,
) -> None:
    import_members(source, table(row()))
    org = source.organization
    person = member(source)
    theirs = made_up.everything("other")
    Role.objects.create(organization=org, name="Board", admin_access=True)
    for pk, role in (
        (theirs.member.pk, "Board"),
        (person.pk, "Members"),
        (person.pk, "Foreign"),
    ):
        with pytest.raises(CommandError, match="not found"):
            call_command("first_administrator", org.subdomain, pk, role)
    person.status = MemberStatus.DISABLED
    person.save(update_fields=["status"])
    with pytest.raises(ValidationError, match="already be active"):
        first_administrator(org, person.pk, "Board")
    assert AuditEvent.objects.of(org).count() == 0


@pytest.mark.django_db(transaction=True)
def test_real_application_login_import_and_bootstrap(source: made_up.Source) -> None:
    Role.objects.create(
        organization_id=source.organization_id, name="Board", admin_access=True
    )
    with application_login():
        run = import_members(source, table(row()))
        person = first_administrator(source.organization, member(source).pk, "Board")
        assert run.created == 1 and person.is_staff


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_imports_have_one_creation_and_complete_history(
    source: made_up.Source,
) -> None:
    barrier = Barrier(2)

    def run() -> tuple[int, int, str]:
        try:
            barrier.wait(timeout=10)
            result = import_members(source, table(*(row(i) for i in range(1, 6))))
            return result.created, result.granted, result.outcome
        finally:
            connection.close()

    with application_login(), ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert sorted(results) == [(0, 0, "applied"), (5, 5, "applied")]
    assert Member.objects.of(source.organization_id).count() == 5
    assert AccessEvent.objects.of(source.organization_id).count() == 5
    assert MemberImport.objects.of(source.organization_id).count() == 2


def test_identity_can_exist_independently_in_two_organizations(
    source: made_up.Source,
) -> None:
    other = made_up.organization("other")
    role = Role.objects.create(organization=other, name="Members")
    other.standard_role = role
    other.save(update_fields=["standard_role"])
    run = import_members(source, table(row()))
    second_source = source_for(other)
    second_run = import_members(second_source, table(row()))
    assert run.created == second_run.created == 1
    assert member(source).pk != member(second_source).pk
    assert member(source).phone == member(second_source).phone


@pytest.mark.django_db(transaction=True)
def test_report_migration_preserves_history_and_refuses_report_loss(
    source: made_up.Source,
) -> None:
    from django.db.migrations.executor import MigrationExecutor

    previous = [("core", "0002_capacity")]
    executor = MigrationExecutor(connection)
    try:
        # Empty reports can reverse. A run created at the previous schema gets
        # its empty report on upgrade without losing its original fields.
        executor.migrate(previous)
        legacy_apps = executor.loader.project_state(previous).apps
        legacy_source = legacy_apps.get_model(
            "core", "MemberSource"
        )._base_manager.create(
            organization_id=source.organization_id, kind="file upload"
        )
        legacy_run = legacy_apps.get_model("core", "MemberImport")._base_manager.create(
            organization_id=source.organization_id,
            member_source_id=legacy_source.pk,
            outcome="applied",
            created=7,
        )
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())
        run = MemberImport.objects.of(source.organization_id).get(pk=legacy_run.pk)
        assert run.report == {} and run.created == 7
        checked = import_members(source, table(row(email="invalid")))
        # A made-up run goes back past 0040 with a bare source; its report is
        # what the older schema cannot hold.
        with pytest.raises(RuntimeError, match="cannot be represented"):
            MigrationExecutor(connection).migrate(previous)
        # The steps before it were undone; forward again, nothing is lost.
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())
        checked.refresh_from_db()
        assert codes(checked) == {"invalid_member"}
    finally:
        current = MigrationExecutor(connection)
        current.migrate(current.loader.graph.leaf_nodes())


@pytest.mark.parametrize("reverse", [False, True])
def test_phone_conflicts_include_every_row_of_an_inconsistent_group(
    source: made_up.Source, reverse: bool
) -> None:
    rows = [row(), row(phone=row(2)[4]), row(2)]
    if reverse:
        rows.reverse()
    result = import_members(source, table(*rows))
    assert result.created == 0 and result.skipped == 3
    assert "phone_in_multiple_rows" in codes(result)
