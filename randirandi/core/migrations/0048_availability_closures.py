# SPDX-License-Identifier: AGPL-3.0-only
# closures by date. One new table, empty (a date, or a run of dates,
# closed beside an asset's weekly availability), and the audit vocabulary gains
# seven actions (made, changed and removed a weekly availability and a closure,
# and a refund left by hand) and the kind "availability_closure". No existing
# table changes. Reversing is refused while a closure exists or an audit event
# uses one of the new actions or the new kind: the reverse would drop the record.

import datetime
import importlib
from typing import Any

import django.db.models.deletion
import django.db.models.manager
from django.db import migrations, models

import randirandi.core.models.base

# The vocabulary of the migration before this part's, so that what other parts
# added survives forward and back. At the port this names the newest one.
PREVIOUS = importlib.import_module("randirandi.core.migrations.0047_list_lookups")

OLD_ACTIONS = PREVIOUS.NEW_ACTIONS
NEW_ONLY = [
    ("made availability", "Made Availability"),
    ("changed availability", "Changed Availability"),
    ("removed availability", "Removed Availability"),
    ("made closure", "Made Closure"),
    ("changed closure", "Changed Closure"),
    ("removed closure", "Removed Closure"),
    ("left refund by hand", "Left Refund By Hand"),
]
NEW_ACTIONS = [*OLD_ACTIONS, *NEW_ONLY]

OLD_KINDS = (
    "organization",
    "space",
    "asset_inventory",
    "spot_group",
    "spot",
    "asset",
    "availability_calendar",
    "available_span",
    "booking_type",
    "booking_category",
    "booking_configuration",
    "configuration_category",
    "price_book",
    "price_book_entry",
    "price_tier",
    "role",
    "role_category",
    "membership",
    "member",
    "member_membership",
    "member_source",
    "member_import",
    "access_event",
    "login_code",
    "login_attempt",
    "login_request",
    "authenticator",
    "security_notice",
    "order",
    "taken_time",
    "capacity_allocation",
    "booking",
    "booking_line",
    "payment",
    "refund",
    "block",
    "payment_provider_account",
    "audit_event",
)
NEW_KINDS = (*OLD_KINDS[:8], "availability_closure", *OLD_KINDS[8:])


def refuse_a_reverse_that_loses_records(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    closures = apps.get_model("core", "AvailabilityClosure")._default_manager.using(
        alias
    )
    events = apps.get_model("core", "AuditEvent")._default_manager.using(alias)
    if closures.exists():
        raise RuntimeError(
            "Cannot reverse while a closure by date exists: remove the closures first."
        )
    held = events.filter(
        models.Q(action__in=[action for action, _ in NEW_ONLY])
        | models.Q(object_kind="availability_closure")
    ).count()
    if held:
        raise RuntimeError(
            f"Cannot reverse while {held} availability audit event(s) exist "
            "(retained history)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0047_list_lookups")]

    operations = [
        migrations.CreateModel(
            name="AvailabilityClosure",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("first_date", models.DateField()),
                ("last_date", models.DateField()),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("holiday", "Holiday"),
                            ("event", "Event"),
                            ("repair", "Repair"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "asset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="core.asset"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.organization",
                    ),
                ),
            ],
            options={
                "ordering": ["first_date", "last_date", "id"],
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="availabilityclosure_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=randirandi.core.models.base.SameOrganization(
                link="asset", name="availabilityclosure_asset_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=models.CheckConstraint(
                condition=models.Q(("first_date__lte", models.F("last_date"))),
                name="availabilityclosure_ends_after_it_starts",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("first_date__gte", datetime.date(2000, 1, 1)),
                    ("last_date__lte", datetime.date(2100, 12, 31)),
                ),
                name="availabilityclosure_in_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reason__in", ["holiday", "event", "repair"]),
                    ("reason", ""),
                    _connector="OR",
                ),
                name="availabilityclosure_reason",
            ),
        ),
        migrations.RemoveConstraint(model_name="auditevent", name="audit_fixed_action"),
        migrations.RemoveConstraint(model_name="auditevent", name="audit_fixed_kind"),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=NEW_ACTIONS, max_length=100),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", [a for a, _ in NEW_ACTIONS])),
                name="audit_fixed_action",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("object_kind__in", NEW_KINDS)),
                name="audit_fixed_kind",
            ),
        ),
        # Listed last, so reversing runs it first, before any table changes.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_records
        ),
    ]
