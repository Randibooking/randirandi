# SPDX-License-Identifier: AGPL-3.0-only
"""A third closure reason, `vacation`: the organization's own holiday closure,
a different thing from a public holiday . The check constraint widens.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0054_organization_language"),
    ]

    operations = [
        migrations.AlterField(
            model_name="availabilityclosure",
            name="reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("holiday", "Holiday"),
                    ("repair", "Repair"),
                    ("vacation", "Vacation"),
                ],
                max_length=10,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="availabilityclosure",
            name="availabilityclosure_reason",
        ),
        migrations.AddConstraint(
            model_name="availabilityclosure",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reason__in", ["holiday", "repair", "vacation"]),
                    ("reason", ""),
                    _connector="OR",
                ),
                name="availabilityclosure_reason",
            ),
        ),
    ]
