# SPDX-License-Identifier: AGPL-3.0-only
"""Preserve each payment's provider environment, including pre-production rows."""

from typing import Any

from django.db import migrations, models


def refuse_production_reversal(apps: Any, schema_editor: Any) -> None:
    payment = apps.get_model("core", "Payment")
    # This atomic migration holds the same lock through the later DROP COLUMN.
    # A writer already in progress finishes before the check; another cannot
    # insert a production intent between the check and removal of its mode.
    schema_editor.execute("LOCK TABLE core_payment IN ACCESS EXCLUSIVE MODE")
    if (
        payment._base_manager.using(schema_editor.connection.alias)
        .filter(test_mode=False)
        .exists()
    ):
        raise RuntimeError("Reversing 0056 is refused while production payments exist.")


class Migration(migrations.Migration):
    dependencies = [("core", "0055_closure_vacation_reason")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="test_mode",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_production_reversal),
    ]
