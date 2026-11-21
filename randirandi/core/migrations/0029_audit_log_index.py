# SPDX-License-Identifier: AGPL-3.0-only
# the Audit log reads one organization's events by time.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0028_operator_erases_member")]

    operations = [
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["organization", "at"], name="audit_org_at"),
        ),
    ]
