# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only, scoped inspection registration. No public route until M4/M7."""

from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import models
from django.http import HttpRequest, HttpResponse

from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import (
    Authenticator,
    LoginCode,
    LoginRequest,
    Member,
    Organization,
    Owned,
    Payment,
)


def administrator(request: HttpRequest) -> Member | None:
    supplied = request.user
    if not isinstance(supplied, Member):
        return None
    current = (
        Member.objects.of(supplied.organization_id)
        .select_related("role")
        .filter(pk=supplied.pk)
        .first()
    )
    return current if current is not None and current.is_staff else None


class InspectionSite(admin.AdminSite):
    site_header = "RandiRandi inspection"

    def has_permission(self, request: HttpRequest) -> bool:
        return administrator(request) is not None

    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        # The passwordless, organization-aware login belongs to M4.
        raise PermissionDenied("Member login is not available yet.")


if TYPE_CHECKING:
    InspectionBase = admin.ModelAdmin[models.Model]
else:
    InspectionBase = admin.ModelAdmin


class InspectionAdmin(InspectionBase):
    actions = None
    list_display: tuple[str, ...] = ("id",)
    view_on_site = False

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[models.Model]:
        actor = administrator(request)
        org_id = actor.organization_id if actor is not None else -1
        if self.model is Organization:
            return Organization.objects.filter(pk=org_id)
        if issubclass(self.model, Owned):
            return self.model.objects.of(org_id)
        raise PermissionDenied("Unregistered organization boundary.")

    def has_module_permission(self, request: HttpRequest) -> bool:
        return administrator(request) is not None

    def has_view_permission(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> bool:
        actor = administrator(request)
        if actor is None:
            return False
        if obj is None:
            return True
        return (
            obj.pk
            if isinstance(obj, Organization)
            else getattr(obj, "organization_id", None)
        ) == actor.organization_id

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> bool:
        return False

    def get_readonly_fields(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> tuple[str, ...]:
        return tuple(
            field.name
            for field in self.model._meta.concrete_fields
            if not (
                self.model in (LoginCode, LoginRequest, Authenticator)
                and field.name
                in {
                    "scrambled_code",
                    "secret",
                    "browser_digest",
                    "challenge",
                    "identifier_digest",
                }
            )
            and not (
                self.model is Payment and field.name in {"redirect_url", "create_phone"}
            )
        )

    def get_fields(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> tuple[str, ...]:
        return self.get_readonly_fields(request, obj)

    def lookup_allowed(self, lookup: str, value: str, request: HttpRequest) -> bool:
        # No arbitrary relation traversal or probing hidden login hashes.
        return lookup in {"id", "id__exact"}

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        actor = administrator(request)
        if actor is None:
            raise PermissionDenied
        # A collection inspection names the organization, with no query text.
        record_event(actor=actor, action=AuditAction.INSPECT, target=actor.organization)
        return super().changelist_view(request, extra_context)

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        actor = administrator(request)
        if actor is None or request.method not in {"GET", "HEAD"} or object_id is None:
            raise PermissionDenied
        obj = self.get_object(request, object_id)
        if obj is None or not self.has_view_permission(request, obj):
            raise PermissionDenied
        record_event(actor=actor, action=AuditAction.INSPECT, target=obj)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def history_view(
        self,
        request: HttpRequest,
        object_id: str,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        # The product's scoped AuditEvent is the history, not Django LogEntry.
        raise PermissionDenied


site = InspectionSite(name="inspection")
for model in apps.get_app_config("core").get_models():
    site.register(model, InspectionAdmin)
