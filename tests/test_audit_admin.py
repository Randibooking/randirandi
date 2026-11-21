# SPDX-License-Identifier: AGPL-3.0-only
"""Audit immutability and tenant-scoped inspection through the real app login."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, IntegrityError, connection, models, transaction
from django.http import HttpRequest
from django.template.response import SimpleTemplateResponse
from django.test import RequestFactory, override_settings
from django.urls import Resolver404, path, resolve
from django.utils import timezone

from randirandi.core.admin import InspectionAdmin, site
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.member_data import correct_member, export_member
from randirandi.core.models import AuditEvent, LoginCode, Member, Organization, Owned
from randirandi.core.retention import purge_expired, purge_organization
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.operator_erasure import erase

pytestmark = pytest.mark.django_db(transaction=True)
urlpatterns = [path("inspection/", site.urls)]


def request(actor: Member | None) -> HttpRequest:
    result = RequestFactory().get("/inspection/")
    result.user = actor if actor is not None else AnonymousUser()
    return result


def test_fixed_audit_writer_reloads_authority_and_target_and_rolls_back() -> None:
    made = made_up.everything()
    other = made_up.everything("other")
    org = made.organization
    before = AuditEvent.objects.of(org).count()
    with application_login():
        event = record_event(
            actor=made.administrator, action=AuditAction.INSPECT, target=made.booking
        )
        assert (event.object_kind, event.object_id, event.administrator_id) == (
            "booking",
            made.booking.pk,
            made.administrator.pk,
        )
        with pytest.raises(ValueError):
            record_event(
                actor=made.administrator,
                action="Alex alex@example.test",  # type: ignore[arg-type]
                target=made.member,
            )
        with pytest.raises(PermissionDenied):
            record_event(
                actor=made.administrator,
                action=AuditAction.INSPECT,
                target=other.member,
            )
        with pytest.raises(ValueError):
            record_event(
                actor=made.administrator, action=AuditAction.INSPECT, target=Member()
            )
        with pytest.raises(RuntimeError), transaction.atomic():
            record_event(
                actor=made.administrator, action=AuditAction.INSPECT, target=made.member
            )
            raise RuntimeError("rollback")
        Member.objects.of(org).filter(pk=made.administrator.pk).update(
            status="disabled"
        )
        with pytest.raises(PermissionDenied):
            record_event(
                actor=made.administrator, action=AuditAction.INSPECT, target=made.member
            )
    assert AuditEvent.objects.of(org).count() == before + 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE core_auditevent SET action='inspected' WHERE id=%s",
        (
            "INSERT INTO core_auditevent SELECT * FROM core_auditevent WHERE id=%s "
            "ON CONFLICT (id) DO UPDATE SET action='inspected'"
        ),
        "ALTER TABLE core_auditevent DISABLE ROW LEVEL SECURITY",
        "TRUNCATE core_auditevent",
    ],
)
def test_application_cannot_rewrite_audit_history(statement: str) -> None:
    made = made_up.everything()
    event = AuditEvent.objects.of(made.organization).get()
    with (
        application_login(),
        pytest.raises(DatabaseError),
        connection.cursor() as cursor,
    ):
        cursor.execute(statement, [event.pk] if "%s" in statement else None)
    event.refresh_from_db()
    assert event.action == "made block"


def test_deletion_replacement_is_refused_until_real_retention_anniversary() -> None:
    made = made_up.everything()
    org = made.organization
    event = AuditEvent.objects.of(org).get()
    old = AuditEvent.objects.create(
        organization=org,
        administrator=made.administrator,
        action="inspected",
        object_kind="member",
        object_id=made.member.pk,
        at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    with application_login():
        assert AuditEvent.objects.of(org).filter(pk=event.pk).delete()[0] == 0
        with pytest.raises(IntegrityError):
            AuditEvent.objects.create(
                id=event.pk,
                organization=org,
                administrator=made.administrator,
                action="inspected",
                object_kind="member",
                object_id=made.member.pk,
            )
        assert (
            purge_organization(org, now=datetime(2090, 1, 1, tzinfo=UTC))[
                "audit_events"
            ]
            == 1
        )
    assert AuditEvent.objects.of(org).filter(pk=event.pk).exists()
    assert not AuditEvent.objects.of(org).filter(pk=old.pk).exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "alex@example.test"},
        {"object_kind": "Alex"},
        {"object_id": 0},
    ],
)
def test_database_refuses_free_text_or_invalid_audit_target(
    fields: dict[str, object],
) -> None:
    made = made_up.everything()
    row = AuditEvent(
        organization=made.organization,
        administrator=made.administrator,
        action="inspected",
        object_kind="member",
        object_id=made.member.pk,
    )
    for field, value in fields.items():
        setattr(row, field, value)
    with application_login(), pytest.raises(IntegrityError):
        row.save(force_insert=True)


def test_member_mutations_export_and_self_erasure_have_atomic_audit_events() -> None:
    made = made_up.everything()
    org = made.organization
    with application_login():
        correct_member(
            actor=made.administrator,
            member_id=made.member.pk,
            changes={"first_name": "Taylor"},
            **fresh(made.administrator),
        )
        data = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
        assert {row["action"] for row in data["audit_events"]} == {
            "corrected member",
            "exported member",
        }
        with (
            patch(
                "randirandi.core.member_data.record_event",
                side_effect=RuntimeError("audit unavailable"),
            ),
            pytest.raises(RuntimeError),
        ):
            correct_member(
                actor=made.administrator,
                member_id=made.member.pk,
                changes={"first_name": "Should roll back"},
                **fresh(made.administrator),
            )
        made.member.refresh_from_db()
        assert made.member.first_name == "Taylor"
        erase(made, made.administrator.pk)
    assert (
        AuditEvent.objects.of(org)
        .filter(action="operator erased member", object_id=made.administrator.pk)
        .count()
        == 1
    )
    made.administrator.refresh_from_db()
    assert made.administrator.email == "" and not made.administrator.is_active


@pytest.mark.parametrize(
    "model",
    list(apps.get_app_config("core").get_models()),
    ids=lambda model: model.__name__,
)
def test_every_registered_model_is_scoped_and_read_only(
    model: type[models.Model],
) -> None:
    made = made_up.everything()
    other = made_up.everything("other")
    inspector = site.get_model_admin(model)
    assert isinstance(inspector, InspectionAdmin)
    own_request = request(made.administrator)
    with application_login():
        rows = inspector.get_queryset(own_request)
        for obj in rows:
            assert isinstance(obj, (Organization, Owned))
            assert (
                obj.pk if isinstance(obj, Organization) else obj.organization_id
            ) == made.organization.pk
        assert not inspector.get_queryset(request(other.member)).exists()
        assert not inspector.get_queryset(request(None)).exists()
        assert inspector.has_view_permission(own_request)
        assert not inspector.has_add_permission(own_request)
        assert not inspector.has_change_permission(own_request)
        assert not inspector.has_delete_permission(own_request)
        assert inspector.get_actions(own_request) == {}
        assert not inspector.has_view_permission(
            own_request, other.organization if model is Organization else other.member
        )
        Member.objects.of(made.organization).filter(pk=made.administrator.pk).update(
            status="disabled"
        )
        assert not inspector.get_queryset(own_request).exists()
        assert not inspector.has_view_permission(own_request)


@override_settings(
    ROOT_URLCONF=__name__,
    STORAGES={
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        }
    },
)
def test_inspection_renders_read_only_views_and_records_lookups() -> None:
    made = made_up.everything()
    other = made_up.everything("other")
    req = request(made.administrator)
    with application_login():
        for model in apps.get_app_config("core").get_models():
            inspector = site.get_model_admin(model)
            response = inspector.changelist_view(req)
            assert response.status_code == 200
            assert isinstance(response, SimpleTemplateResponse)
            response.render()
            obj = inspector.get_queryset(req).first()
            if obj is not None:
                response = inspector.changeform_view(req, str(obj.pk))
                assert response.status_code == 200
                assert isinstance(response, SimpleTemplateResponse)
                response.render()
        inspected = AuditEvent.objects.of(made.organization).filter(action="inspected")
        assert inspected.count() >= 34
        # Floor plans, spots and groups too .
        assert {"asset_inventory", "spot", "spot_group"} <= set(
            inspected.values_list("object_kind", flat=True)
        )
        with pytest.raises(PermissionDenied):
            site.get_model_admin(Member).changeform_view(req, str(other.member.pk))
        req.method = "POST"
        with pytest.raises(PermissionDenied):
            site.get_model_admin(Member).changeform_view(req, str(made.member.pk))
        code_admin = site.get_model_admin(LoginCode)
        assert "scrambled_code" not in code_admin.get_fields(req)
        assert not code_admin.lookup_allowed("scrambled_code__startswith", "x", req)


def test_framework_sessions_expire_and_admin_is_not_publicly_mounted() -> None:
    now = timezone.now()
    Session.objects.create(
        session_key="expired",
        session_data="fictional",
        expire_date=now - timedelta(days=1),
    )
    Session.objects.create(
        session_key="current",
        session_data="fictional",
        expire_date=now + timedelta(days=1),
    )
    with application_login():
        purge_expired()
    assert list(Session.objects.values_list("session_key", flat=True)) == ["current"]
    with pytest.raises(Resolver404):
        resolve("/inspection/")
    with pytest.raises(PermissionDenied):
        site.login(request(None))


@pytest.mark.parametrize("subject", ["member", "administrator"])
def test_export_follows_personal_inspection_targets_and_audit_inspections(
    subject: str,
) -> None:
    from randirandi.core.models import (
        AccessEvent,
        Block,
        CapacityAllocation,
        MemberImport,
        MemberMembership,
        Membership,
        Refund,
    )

    made = made_up.everything()
    other = made_up.everything("other")
    org = made.organization
    observer = Member.objects.create(
        organization=org,
        role=made.board,
        email="observer@example.test",
        status="active",
        origin="added by administrator",
    )
    who = made.member if subject == "member" else made.administrator
    targets: list[models.Model]
    if subject == "member":
        targets = [
            CapacityAllocation.objects.of(org)
            .filter(taken_time=made.booking.taken_time)
            .get(),
            AccessEvent.objects.of(org).get(member=who),
            LoginCode.objects.of(org).get(member=who),
            MemberMembership.objects.of(org).get(member=who),
            Membership.objects.of(org).get(pk=made.membership.pk),
        ]
    else:
        run = MemberImport.objects.of(org).get()
        run.started_by = who
        run.save(update_fields=["started_by"])
        event = AccessEvent.objects.of(org).get()
        event.administrator = who
        event.save(update_fields=["administrator"])
        made.booking.cancelled_by = who
        made.booking.save(update_fields=["cancelled_by"])
        targets = [
            run,
            event,
            made.booking,
            Refund.objects.of(org).get(),
            Block.objects.of(org).get(),
        ]
    with application_login():
        records = [
            record_event(actor=observer, action=AuditAction.INSPECT, target=target)
            for target in targets
        ]
        # Same object ID in another kind must not pull unrelated configuration.
        unrelated = record_event(
            actor=observer, action=AuditAction.INSPECT, target=made.asset
        )
        foreign = record_event(
            actor=other.administrator, action=AuditAction.INSPECT, target=other.member
        )
        for _ in range(3):
            records.append(
                record_event(
                    actor=observer, action=AuditAction.INSPECT, target=records[-1]
                )
            )
        data = export_member(actor=observer, member_id=who.pk, **fresh(observer))
    found = {row["id"] for row in data["audit_events"]}
    assert {record.pk for record in records} <= found
    assert not {unrelated.pk, foreign.pk} & found
    # Only the subject's own identity may appear; not the observer/co-member.
    import json

    encoded = json.dumps(data)
    assert observer.email not in encoded and other.member.email not in encoded
    if subject == "administrator":
        assert made.member.email not in encoded
        assert "25000" not in encoded and "Floor sanding" not in encoded
    assert "scrambled" not in encoded
