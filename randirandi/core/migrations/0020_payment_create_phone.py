# SPDX-License-Identifier: AGPL-3.0-only
# the member's phone, as Vipps takes it, is frozen with a payment's
# create intent so a retry sends the same body. One column, empty by default;
# no rows change. Reversing is refused while a pending intent still holds one,
# since the column would take it and a retry would send a different body.

from typing import Any

from django.db import migrations, models


def refuse_a_reverse_that_loses_phones(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    rows = apps.get_model("core", "Payment")._default_manager.using(alias)
    if rows.exclude(create_phone="").exists():
        raise RuntimeError(
            "Cannot reverse while a payment create intent holds a phone: "
            "let the create finish first."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0019_group_prices")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="create_phone",
            field=models.CharField(blank=True, max_length=15),
        ),
        # Last, so that reversing checks before the column is removed.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_phones
        ),
    ]
