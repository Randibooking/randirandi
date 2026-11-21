# SPDX-License-Identifier: AGPL-3.0-only
# the moment a payment was first seen captured. Reversing is
# refused while a payment holds one, as it would be lost.

from typing import Any

from django.db import migrations, models


def refuse_a_reverse_that_loses_a_moment(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    held = (
        apps.get_model("core", "Payment")
        ._default_manager.using(alias)
        .filter(captured_at__isnull=False)
        .count()
    )
    if held:
        raise RuntimeError(
            f"Cannot reverse while {held} payment(s) hold their capture moment "
            "(retained payment capture moments)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0029_audit_log_index")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="captured_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_a_moment
        ),
    ]
