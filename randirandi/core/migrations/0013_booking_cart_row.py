# SPDX-License-Identifier: AGPL-3.0-only
# the cart row each booking was ordered from.
# Reversing drops it: the earlier code does not read it, and a booking's
# time, state and price do not depend on it.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0012_administrator_payment_adjustments")]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="cart_row",
            field=models.CharField(
                blank=True, default="", editable=False, max_length=16
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(fields=["cart_row"], name="booking_cart_row"),
        ),
    ]
