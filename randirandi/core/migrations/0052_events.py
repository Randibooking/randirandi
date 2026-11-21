# SPDX-License-Identifier: AGPL-3.0-only
# the event on the server . Five new tables, empty: the
# event, its weekly repeat, its days, the days a repeat skipped and the emails
# about them. A claim on an asset gains the state "event" (the check
# `takentime_state_is_known` and the rule against double booking
# `capacity_no_double_booking` name it), the closure reason "event" goes (a
# closure that has it keeps no reason), and the audit vocabulary gains five
# actions and the kinds "event" and "event_day".
# Reversing is refused while any event, day, skipped day or notice exists, a
# claim is in the state "event" or an audit event uses the new actions or
# kinds: the reverse would drop the record. A reverse with none of them is
# clean, and the graph's leaf nodes are the caller's to restore. The closures
# that lost the reason "event" keep none.

import datetime
import importlib
from typing import Any

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import randirandi.core.models.base

# The vocabulary of the migrations before this part's, so that what other parts
# added survives forward and back.
OLD_ACTIONS = importlib.import_module(
    "randirandi.core.migrations.0051_payouts_and_fees"
).NEW_ACTIONS
NEW_ONLY = [
    ("made event", "Made Event"),
    ("changed event", "Changed Event"),
    ("removed event", "Removed Event"),
    ("removed event day", "Removed Event Day"),
    ("changed event day", "Changed Event Day"),
]
NEW_ACTIONS = [*OLD_ACTIONS, *NEW_ONLY]
OLD_KINDS = importlib.import_module(
    "randirandi.core.migrations.0048_availability_closures"
).NEW_KINDS
NEW_KINDS = (*OLD_KINDS, "event", "event_day")


def closures_keep_no_event_reason(apps: Any, schema_editor: Any) -> None:
    """A closure that had the reason "event" keeps no reason : an
    organization's own event is an Event."""
    apps.get_model("core", "AvailabilityClosure")._default_manager.using(
        schema_editor.connection.alias
    ).filter(reason="event").update(reason="")


def refuse_a_reverse_that_loses_records(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias

    def count(model: str, **where: Any) -> int:
        found: int = (
            apps.get_model("core", model)
            ._default_manager.using(alias)
            .filter(**where)
            .count()
        )
        return found

    held = {
        "event": count("Event"),
        "event day": count("EventDay"),
        "event repeat": count("EventRepeat"),
        "skipped event day": count("EventSkippedDay"),
        "event notice": count("EventNotice"),
        "claim in the state event": count("TakenTime", state="event"),
        "event audit event": count(
            "AuditEvent",
            action__in=[action for action, _ in NEW_ONLY],
        )
        + count("AuditEvent", object_kind__in=["event", "event_day"]),
    }
    held = {name: number for name, number in held.items() if number}
    if held:
        raise RuntimeError(
            f"Cannot reverse while the event records exist (retained records): {held}."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0051_payouts_and_fees"),
    ]

    operations = [
        migrations.CreateModel(
            name="Event",
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
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("social", "Social event"),
                            ("volunteer_work", "Volunteer work"),
                            ("organization", "The organization's own"),
                            ("open_house", "Open house"),
                        ],
                        max_length=14,
                    ),
                ),
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("show_email", models.BooleanField(default=True)),
                ("show_phone", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="EventDay",
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
                ("day", models.DateField()),
                ("starts_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField()),
                ("own_start", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("own_end", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="EventNotice",
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
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="EventRepeat",
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
                ("weekdays", models.PositiveSmallIntegerField()),
                ("first_date", models.DateField()),
                ("end_date", models.DateField(blank=True, null=True)),
                ("start_minute", models.PositiveSmallIntegerField()),
                ("end_minute", models.PositiveSmallIntegerField()),
                ("made_through", models.DateField(blank=True, null=True)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="EventSkippedDay",
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
                ("day", models.DateField()),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("closed", "Closed"),
                            ("booked", "Booked"),
                            ("blocked", "Blocked"),
                            ("event", "Event"),
                        ],
                        max_length=7,
                    ),
                ),
                ("in_the_way", models.PositiveBigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="auditevent",
            name="audit_fixed_kind",
        ),
        migrations.RemoveConstraint(
            model_name="auditevent",
            name="audit_fixed_action",
        ),
        migrations.RemoveConstraint(
            model_name="availabilityclosure",
            name="availabilityclosure_reason",
        ),
        migrations.RemoveConstraint(
            model_name="capacityallocation",
            name="capacity_no_double_booking",
        ),
        migrations.RemoveConstraint(
            model_name="takentime",
            name="takentime_state_is_known",
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=NEW_ACTIONS, max_length=100),
        ),
        migrations.AlterField(
            model_name="availabilityclosure",
            name="reason",
            field=models.CharField(
                blank=True,
                choices=[("holiday", "Holiday"), ("repair", "Repair")],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="takentime",
            name="state",
            field=models.CharField(
                choices=[
                    ("held", "Held"),
                    ("confirmed", "Confirmed"),
                    ("cancelled", "Cancelled"),
                    ("expired", "Expired"),
                    ("blocked", "Blocked"),
                    ("event", "Event"),
                ],
                max_length=9,
            ),
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
        migrations.RunPython(closures_keep_no_event_reason, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reason__in", ["holiday", "repair"]),
                    ("reason", ""),
                    _connector="OR",
                ),
                name="availabilityclosure_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="capacityallocation",
            constraint=randirandi.core.models.base.DatabaseRule(
                definition=(
                    "EXCLUDE USING gist (int8range(organization_id, organization_id, "
                    "'[]') WITH &&, int8range(scope_key, scope_key, '[]') WITH &&, "
                    "(CASE WHEN whole_space THEN int8range(NULL, NULL) ELSE "
                    "int8range(asset_id, asset_id, '[]') END) WITH &&, (CASE WHEN mode "
                    "= 'exclusive' OR NOT whole_space THEN int8range(NULL, NULL) ELSE "
                    "int8range(spot_id, spot_id, '[]') END) WITH &&, (CASE WHEN mode = "
                    "'exclusive' OR whole_space THEN int8range(NULL, NULL) ELSE "
                    "int8range(spot_id, spot_id, '[]') END) WITH &&, "
                    "tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (state IN "
                    "('held','confirmed','blocked','event')) DEFERRABLE INITIALLY "
                    "IMMEDIATE"
                ),
                name="capacity_no_double_booking",
            ),
        ),
        migrations.AddConstraint(
            model_name="takentime",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        [
                            "held",
                            "confirmed",
                            "cancelled",
                            "expired",
                            "blocked",
                            "event",
                        ],
                    )
                ),
                name="takentime_state_is_known",
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="asset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.asset",
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="contact",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="made_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="eventday",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="days",
                to="core.event",
            ),
        ),
        migrations.AddField(
            model_name="eventday",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="eventday",
            name="taken_time",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="event_day",
                to="core.takentime",
            ),
        ),
        migrations.AddField(
            model_name="eventnotice",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="eventnotice",
            name="recipient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="eventrepeat",
            name="event",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="repeat",
                to="core.event",
            ),
        ),
        migrations.AddField(
            model_name="eventrepeat",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="eventskippedday",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="skipped_days",
                to="core.event",
            ),
        ),
        migrations.AddField(
            model_name="eventskippedday",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="eventnotice",
            name="skipped_day",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notices",
                to="core.eventskippedday",
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="event_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=randirandi.core.models.base.SameOrganization(
                link="asset", name="event_asset_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=randirandi.core.models.base.SameOrganization(
                link="contact", name="event_contact_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=randirandi.core.models.base.SameOrganization(
                link="made_by", name="event_made_by_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "category__in",
                        ["social", "volunteer_work", "organization", "open_house"],
                    )
                ),
                name="event_category_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=models.Q(("title", ""), _negated=True),
                name="event_has_a_title",
            ),
        ),
        migrations.AddIndex(
            model_name="eventday",
            index=models.Index(fields=["organization", "day"], name="eventday_day"),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="eventday_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=randirandi.core.models.base.SameOrganization(
                link="event", name="eventday_event_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=randirandi.core.models.base.SameOrganization(
                link="taken_time", name="eventday_taken_time_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=models.UniqueConstraint(
                fields=("event", "day"), name="eventday_once_a_day"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=models.CheckConstraint(
                condition=models.Q(("starts_at__lt", models.F("ends_at"))),
                name="eventday_ends_after_it_starts",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("own_end__isnull", True), ("own_start__isnull", True)),
                    models.Q(
                        ("own_start__gte", 0),
                        ("own_start__lt", models.F("own_end")),
                        ("own_end__lte", 1440),
                    ),
                    _connector="OR",
                ),
                name="eventday_own_hours",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventday",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("removed_at__isnull", True),
                    ("taken_time__isnull", True),
                    _connector="OR",
                ),
                name="eventday_removed_holds_nothing",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventrepeat",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="eventrepeat_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventrepeat",
            constraint=randirandi.core.models.base.SameOrganization(
                link="event", name="eventrepeat_event_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventrepeat",
            constraint=models.CheckConstraint(
                condition=models.Q(("weekdays__gte", 1), ("weekdays__lte", 127)),
                name="eventrepeat_weekdays",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventrepeat",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("start_minute__gte", 0),
                    ("start_minute__lt", models.F("end_minute")),
                    ("end_minute__lte", 1440),
                ),
                name="eventrepeat_hours",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventrepeat",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("first_date__gte", datetime.date(2000, 1, 1)),
                    ("first_date__lte", datetime.date(2100, 12, 31)),
                    models.Q(
                        ("end_date__isnull", True),
                        models.Q(
                            ("end_date__gte", models.F("first_date")),
                            ("end_date__lte", datetime.date(2100, 12, 31)),
                        ),
                        _connector="OR",
                    ),
                ),
                name="eventrepeat_dates",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventskippedday",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="eventskippedday_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventskippedday",
            constraint=randirandi.core.models.base.SameOrganization(
                link="event", name="eventskippedday_event_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventskippedday",
            constraint=models.UniqueConstraint(
                fields=("event", "day"), name="eventskippedday_once_a_day"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventskippedday",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reason__in", ["closed", "booked", "blocked", "event"])
                ),
                name="eventskippedday_reason_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventnotice",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="eventnotice_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventnotice",
            constraint=randirandi.core.models.base.SameOrganization(
                link="skipped_day", name="eventnotice_skipped_day_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventnotice",
            constraint=randirandi.core.models.base.SameOrganization(
                link="recipient", name="eventnotice_recipient_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="eventnotice",
            constraint=models.UniqueConstraint(
                fields=("skipped_day", "recipient"), name="eventnotice_recipient_once"
            ),
        ),
        # Listed last, so reversing runs it first, before any table changes.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_records
        ),
    ]
