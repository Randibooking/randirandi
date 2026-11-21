# SPDX-License-Identifier: AGPL-3.0-only
# a booking category's icon, one of a short list the member's
# page maps to an icon of its own . Blank on every
# existing category. Reversing is refused while a category has an icon, as the
# icon would be lost.

from typing import Any

from django.db import migrations, models
from django.db.models import Q

ICONS = ["person", "work", "building", "home", "group"]


def refuse_a_reverse_that_loses_an_icon(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    categories = apps.get_model("core", "BookingCategory")._default_manager.using(alias)
    held = categories.exclude(icon="").count()
    if held:
        raise RuntimeError(
            f"Cannot reverse while {held} booking categor(ies) have an icon "
            "(retained category icons)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0037_gave_admin_access")]

    operations = [
        migrations.AddField(
            model_name="bookingcategory",
            name="icon",
            field=models.CharField(
                blank=True,
                choices=[(key, key) for key in ICONS],
                default="",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingcategory",
            constraint=models.CheckConstraint(
                condition=Q(icon="") | Q(icon__in=ICONS),
                name="bookingcategory_icon_is_known",
            ),
        ),
        # Last, so a reverse runs it first: refused before anything is dropped.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_an_icon
        ),
    ]
