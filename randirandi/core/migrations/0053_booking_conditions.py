# SPDX-License-Identifier: AGPL-3.0-only
# the booking conditions . An
# organization gains its days of free cancellation (5 for every one that
# exists), an order the booking conditions it accepted and when (empty on every
# order that exists: "not recorded"), and a table is new: the frozen texts. The
# application's user cannot UPDATE a frozen text.
# Reversing is refused while any frozen text, any order's acceptance or any
# organization's own days exist: the reverse would drop what a member accepted.
# A reverse with none of them is clean, and the graph's leaf nodes are the
# caller's to restore.

from typing import Any

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models

import randirandi.core.models.base


def _app_exists(cursor: Any) -> bool:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='randirandi_app'")
    return cursor.fetchone() is not None


def no_update_on_frozen_texts(apps: Any, schema_editor: Any) -> None:
    """A frozen text is never changed: the application's user may insert, read
    and delete it, never update it. Unit-test databases without the login skip
    it."""
    with schema_editor.connection.cursor() as cursor:
        if not _app_exists(cursor):
            return
        schema_editor.execute(
            "REVOKE UPDATE ON core_bookingconditions FROM randirandi_app"
        )
        cursor.execute(
            """SELECT a.attname,
            has_column_privilege('randirandi_app',c.oid,a.attname,'UPDATE')
            FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
            WHERE c.oid='core_bookingconditions'::regclass AND a.attnum>0
            AND NOT a.attisdropped"""
        )
        if any(allowed for _, allowed in cursor.fetchall()):
            raise RuntimeError("Unexpected effective frozen text UPDATE permission")


def refuse_a_reverse_that_loses_acceptances(apps: Any, schema_editor: Any) -> None:
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
        "frozen text": count("BookingConditions"),
        "order that accepted conditions": count("Order", conditions__isnull=False),
        "organization with its own days": count(
            "Organization", free_cancellation_days__lt=5
        )
        + count("Organization", free_cancellation_days__gt=5),
    }
    held = {name: number for name, number in held.items() if number}
    if held:
        raise RuntimeError(
            "Cannot reverse while booking conditions are recorded (retained "
            f"acceptances): {held}."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_events"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingConditions",
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
                ("standard_version", models.PositiveSmallIntegerField()),
                ("digest", models.CharField(max_length=64)),
                ("organization_name", models.CharField(max_length=200)),
                ("contact_email", models.EmailField(max_length=254)),
                ("free_cancellation_days", models.PositiveSmallIntegerField()),
                ("title", models.CharField(max_length=200)),
                ("intro", models.TextField()),
                ("sections", models.JSONField()),
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
        migrations.AddField(
            model_name="order",
            name="conditions_accepted_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="free_cancellation_days",
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("free_cancellation_days__gte", 0),
                    ("free_cancellation_days__lte", 365),
                ),
                name="organization_free_cancellation_days_in_range",
            ),
        ),
        migrations.AddField(
            model_name="bookingconditions",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        # The text's own keys first: the order's same-organization rule rests on
        # `bookingconditions_org_id`, so a reverse drops it last.
        migrations.AddConstraint(
            model_name="bookingconditions",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="bookingconditions_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingconditions",
            constraint=models.UniqueConstraint(
                fields=("organization", "digest"), name="bookingconditions_text_once"
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingconditions",
            constraint=models.CheckConstraint(
                condition=models.Q(("digest__regex", "^[0-9a-f]{64}$")),
                name="bookingconditions_digest_is_sha256",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="conditions",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.bookingconditions",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=randirandi.core.models.base.SameOrganization(
                link="conditions", name="order_conditions_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("conditions__isnull", True), ("conditions_accepted_at", None)
                    ),
                    models.Q(
                        ("conditions__isnull", False),
                        ("conditions_accepted_at__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="order_conditions_both_or_neither",
            ),
        ),
        migrations.RunPython(no_update_on_frozen_texts, migrations.RunPython.noop),
        # Last, so a reverse runs it first: refused before anything is dropped.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_acceptances
        ),
    ]
