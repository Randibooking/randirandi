# SPDX-License-Identifier: AGPL-3.0-only
# an administrator cancels a booking or one of its lines. A
# booking records the cancel that took it (its key) and the cancellation
# email of that cancel (due, tried, sent), and the audit vocabulary gains
# "cancelled booking line". Reversing is refused while a booking records a
# cancel or an event uses the action: the reverse would forget which cancel
# an email was sent for, and a later forward would send it again.

from typing import Any

from django.db import migrations, models
from django.db.models import Q

OLD_ACTIONS = [
    ("operator named first administrator", "Bootstrap"),
    ("exported member", "Export Member"),
    ("corrected member", "Correct Member"),
    ("erased member", "Erase Member"),
    ("operator erased member", "Operator Erase Member"),
    ("blocked member", "Block Member"),
    ("unblocked member", "Unblock Member"),
    ("requested member import", "Import Members"),
    ("inspected", "Inspect"),
    ("made block", "Made Block"),
    ("administrator signed in", "Admin Login"),
    ("enrolled authenticator", "Enroll Authenticator"),
    ("reset authenticator", "Reset Authenticator"),
    ("operator reset authenticator", "Operator Reset Authenticator"),
    ("requested refund", "Request Refund"),
    ("cancelled unpaid order", "Cancel Payment"),
]
NEW_ACTIONS = [*OLD_ACTIONS, ("cancelled booking line", "Cancel Booking Line")]
NEW_ONLY = "cancelled booking line"


def refuse_a_reverse_that_loses_history(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    if (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action=NEW_ONLY)
        .exists()
    ):
        raise RuntimeError("Cannot reverse retained booking line cancellations.")
    if (
        apps.get_model("core", "Booking")
        ._default_manager.using(alias)
        .filter(cancellation_key__isnull=False)
        .exists()
    ):
        raise RuntimeError(
            "Not reversed: a booking records its cancel and the email about it; "
            "reversing would send it again."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0030_payment_captured_at")]

    operations = [
        *(
            migrations.AddField(
                model_name="booking",
                name=name,
                field=field,
            )
            for name, field in (
                (
                    "cancellation_key",
                    models.UUIDField(blank=True, editable=False, null=True),
                ),
                ("cancel_mail_due_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_mail_tried_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_mailed_at", models.DateTimeField(blank=True, null=True)),
            )
        ),
        migrations.AddConstraint(
            model_name="booking",
            constraint=models.CheckConstraint(
                condition=Q(cancellation_key__isnull=False)
                | Q(
                    cancel_mail_due_at__isnull=True,
                    cancel_mail_tried_at__isnull=True,
                    cancel_mailed_at__isnull=True,
                ),
                name="booking_cancel_mail_needs_cancel",
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(
                condition=Q(
                    cancel_mailed_at__isnull=True, cancel_mail_due_at__isnull=False
                ),
                fields=["organization", "cancel_mail_due_at"],
                name="booking_cancel_mail_due",
            ),
        ),
        migrations.RemoveConstraint(model_name="auditevent", name="audit_fixed_action"),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=NEW_ACTIONS, max_length=100),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", [a for a, _ in NEW_ACTIONS])),
                name="audit_fixed_action",
            ),
        ),
        # Listed last, so reversing runs it first, before any table changes.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_history
        ),
    ]
