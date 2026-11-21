# SPDX-License-Identifier: AGPL-3.0-only
# a container may give a booking category its own hours ().
# One new table, empty: a category without a row has the container's hours, so
# no container changes. Reversing is refused while a row exists, so that a
# downgrade never drops hours without a word.

from typing import Any

import django.db.models.deletion
import django.db.models.manager
from django.db import migrations, models

import randirandi.core.models.base


def refuse_a_reverse_that_loses_hours(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    rows = apps.get_model("core", "CategoryHoursSpan")._default_manager.using(alias)
    if rows.exists():
        raise RuntimeError(
            "Cannot reverse while a container gives a booking category its own "
            "hours: remove the category hours first."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0021_member_list_layouts")]

    operations = [
        migrations.CreateModel(
            name="CategoryHoursSpan",
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
                    "weekday",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (0, "Monday"),
                            (1, "Tuesday"),
                            (2, "Wednesday"),
                            (3, "Thursday"),
                            (4, "Friday"),
                            (5, "Saturday"),
                            (6, "Sunday"),
                        ]
                    ),
                ),
                ("start_time", models.PositiveSmallIntegerField()),
                ("end_time", models.PositiveSmallIntegerField()),
                (
                    "configuration_category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="hours",
                        to="core.configurationcategory",
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
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddConstraint(
            model_name="categoryhoursspan",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="categoryhoursspan_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="categoryhoursspan",
            constraint=randirandi.core.models.base.SameOrganization(
                link="configuration_category",
                name="categoryhoursspan_configuration_category_same_org",
            ),
        ),
        migrations.AddConstraint(
            model_name="categoryhoursspan",
            constraint=models.UniqueConstraint(
                fields=("configuration_category", "weekday", "start_time"),
                name="categoryhoursspan_start_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="categoryhoursspan",
            constraint=models.CheckConstraint(
                condition=models.Q(("weekday__lte", 6)),
                name="categoryhoursspan_weekday",
            ),
        ),
        migrations.AddConstraint(
            model_name="categoryhoursspan",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("start_time__lt", models.F("end_time")), ("end_time__lte", 1440)
                ),
                name="categoryhoursspan_ends_after_it_starts",
            ),
        ),
        # Last, so that reversing checks before the table is dropped.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_hours
        ),
    ]
