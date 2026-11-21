# SPDX-License-Identifier: AGPL-3.0-only
# a booking configuration may be without payment (W12). It
# then has no price book; the name holds. Reversing is refused while a
# configuration has none, since nothing would say which book it takes.

from typing import Any

import django.db.models.deletion
from django.db import migrations, models


def refuse_without_payment_on_reverse(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    configurations = apps.get_model("core", "BookingConfiguration")
    if (
        configurations._default_manager.using(alias)
        .filter(price_book__isnull=True)
        .exists()
    ):
        raise RuntimeError(
            "Cannot reverse while a configuration is without payment: give "
            "each one a price book first."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0017_rule_by_spot")]

    operations = [
        migrations.AlterField(
            model_name="bookingconfiguration",
            name="price_book",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="core.pricebook",
            ),
        ),
        # Last, so that reversing checks before the column is required again.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_without_payment_on_reverse
        ),
    ]
