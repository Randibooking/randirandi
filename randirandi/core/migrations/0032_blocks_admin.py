# SPDX-License-Identifier: AGPL-3.0-only
# a block records when it was made (none for a block from
# before), and the audit vocabulary gains "removed block". Reversing is
# refused while an event uses the action: the reverse would drop the record
# that a block was removed.

from typing import Any

from django.db import migrations, models

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
    ("cancelled booking line", "Cancel Booking Line"),
]
NEW_ACTIONS = [*OLD_ACTIONS, ("removed block", "Removed Block")]
NEW_ONLY = "removed block"


def refuse_a_reverse_that_loses_history(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    if (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action=NEW_ONLY)
        .exists()
    ):
        raise RuntimeError("Cannot reverse retained block removals.")


class Migration(migrations.Migration):
    dependencies = [("core", "0031_cancel_booking_line")]

    operations = [
        migrations.AddField(
            model_name="block",
            name="created_at",
            field=models.DateTimeField(blank=True, null=True),
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
