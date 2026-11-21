# SPDX-License-Identifier: AGPL-3.0-only
# a price book entry may name a group of spots , and
# a booking line records the group it is for. Two nullable columns, the one
# price for each group, and the rule that keeps each inside the organization;
# no rows change. Reversing is refused while a price or a line names a group,
# since the columns would take that with them.

from typing import Any

import django.db.models.deletion
from django.db import migrations, models

import randirandi.core.models.base


def refuse_a_reverse_that_loses_groups(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    for name in ("PriceBookEntry", "BookingLine"):
        rows = apps.get_model("core", name)._default_manager.using(alias)
        if rows.filter(spot_group__isnull=False).exists():
            raise RuntimeError(
                "Cannot reverse while a price or a booking line names a group "
                "of spots: remove those first."
            )


class Migration(migrations.Migration):
    dependencies = [("core", "0018_payment_yes_or_no")]

    operations = [
        migrations.AddField(
            model_name="pricebookentry",
            name="spot_group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="core.spotgroup",
            ),
        ),
        migrations.AddField(
            model_name="bookingline",
            name="spot_group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="core.spotgroup",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="pricebookentry", name="pricebookentry_once"
        ),
        migrations.AddConstraint(
            model_name="pricebookentry",
            constraint=models.UniqueConstraint(
                fields=(
                    "price_book",
                    "booking_configuration",
                    "booking_category",
                    "spot_group",
                ),
                name="pricebookentry_once",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="pricebookentry",
            constraint=randirandi.core.models.base.SameOrganization(
                link="spot_group", name="pricebookentry_spot_group_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingline",
            constraint=randirandi.core.models.base.SameOrganization(
                link="spot_group", name="bookingline_spot_group_same_org"
            ),
        ),
        # Last, so that reversing checks before the columns go.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_groups
        ),
    ]
