# SPDX-License-Identifier: AGPL-3.0-only
# two optional limits on a member's bookings, set by the
# organization : the most bookings a member may
# make in one calendar day, and how far ahead a booking may start, in whole
# calendar months. Empty is no limit. Organizations have no column privilege
# contract, so no grant changes. Reversing is refused while an organization
# sets either, as the setting would be lost.

from typing import Any

from django.db import migrations, models
from django.db.models import Q


def refuse_a_reverse_that_loses_a_limit(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    organizations = apps.get_model("core", "Organization")._default_manager.using(alias)
    held = organizations.filter(
        Q(max_bookings_a_day__isnull=False) | Q(book_ahead_months__isnull=False)
    ).count()
    if held:
        raise RuntimeError(
            f"Cannot reverse while {held} organization(s) set a limit on "
            "bookings (retained booking limits)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0034_order_reference")]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="max_bookings_a_day",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="book_ahead_months",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=Q(max_bookings_a_day__isnull=True)
                | Q(max_bookings_a_day__gte=1),
                name="organization_bookings_a_day_one_or_more",
            ),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=Q(book_ahead_months__isnull=True)
                | Q(book_ahead_months__gte=1),
                name="organization_book_ahead_one_month_or_more",
            ),
        ),
        # Last, so a reverse runs it first: refused before anything is dropped.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_a_limit
        ),
    ]
