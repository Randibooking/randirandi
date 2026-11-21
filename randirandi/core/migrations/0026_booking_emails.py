# SPDX-License-Identifier: AGPL-3.0-only
# Booking email delivery: when an order's confirmation email is due, tried and
# sent, and when a booking's reminder was tried and sent. Every existing
# order and booking gets none, so nothing confirmed before the emails existed
# is sent a confirmation; a coming booking is sent its reminder when due.
# Reversing is refused while any of them holds a moment: the reverse would
# forget what was sent, and a later forward would send it again.

from typing import Any

from django.db import migrations, models
from django.db.models import Q

ORDER = ("confirmation_due_at", "confirmation_tried_at", "confirmation_sent_at")
BOOKING = ("reminder_tried_at", "reminded_at")


def refuse_when_used(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    for model, names, what in (
        ("Order", ORDER, "an order"),
        ("Booking", BOOKING, "a booking"),
    ):
        rows = apps.get_model("core", model)._default_manager.using(alias)
        used = Q()
        for name in names:
            used |= Q(**{f"{name}__isnull": False})
        if rows.filter(used).exists():
            raise RuntimeError(
                f"Not reversed: {what} records an email; reversing would send it again."
            )


class Migration(migrations.Migration):
    dependencies = [("core", "0025_member_status")]

    operations = [
        *(
            migrations.AddField(
                model_name="order",
                name=name,
                field=models.DateTimeField(blank=True, null=True),
            )
            for name in ORDER
        ),
        *(
            migrations.AddField(
                model_name="booking",
                name=name,
                field=models.DateTimeField(blank=True, null=True),
            )
            for name in BOOKING
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                condition=Q(
                    confirmation_sent_at=None, confirmation_due_at__isnull=False
                ),
                fields=["organization", "confirmation_due_at"],
                name="order_confirmation_due",
            ),
        ),
        # Reversed first, before a field or the index is dropped.
        migrations.RunPython(migrations.RunPython.noop, refuse_when_used),
    ]
