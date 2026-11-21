# SPDX-License-Identifier: AGPL-3.0-only
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def refuse_report_loss(apps: StateApps, editor: BaseDatabaseSchemaEditor) -> None:
    imports = apps.get_model("core", "MemberImport")._base_manager.using(
        editor.connection.alias
    )
    if imports.exclude(report={}).exists():
        raise RuntimeError(
            "Import reports cannot be represented before migration 0003."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_capacity")]

    operations = [
        migrations.AddField(
            model_name="memberimport",
            name="report",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_report_loss),
    ]
