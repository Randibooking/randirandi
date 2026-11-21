# SPDX-License-Identifier: AGPL-3.0-only
# what the member sees in the Vipps app. The organization gets a
# tax rate (hundredths of a percent, 0 by default). A payment freezes its
# description and receipt with the create intent, as it does the phone, and
# records when Vipps accepted the link back to the site. No rows change.
# Reversing is refused while a pending intent holds a receipt or description,
# since a retry would then send a different body, and while an organization
# has a tax rate, which the reverse would drop.

from typing import Any

from django.db import migrations, models
from django.db.models import Q


def refuse_a_reverse_that_loses_an_intent(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    rows = apps.get_model("core", "Payment")._default_manager.using(alias)
    pending = rows.filter(create_key__isnull=False, create_completed_at__isnull=True)
    if pending.exclude(create_description="").exists():
        raise RuntimeError(
            "Cannot reverse while a payment create intent holds a description: "
            "let the create finish first."
        )
    organizations = apps.get_model("core", "Organization")._default_manager
    if organizations.using(alias).exclude(tax_rate=0).exists():
        raise RuntimeError(
            "Cannot reverse while an organization has a tax rate: set it to 0 first."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0023_member_last_category")]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="tax_rate",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=Q(tax_rate__lte=10000),
                name="organization_tax_rate_at_most_all",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="create_description",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="payment",
            name="create_receipt",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="details_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Last, so that reversing checks before the columns are removed.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_an_intent
        ),
    ]
