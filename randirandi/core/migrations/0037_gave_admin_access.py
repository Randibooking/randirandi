# SPDX-License-Identifier: AGPL-3.0-only
# the audit vocabulary gains "gave admin access", written once
# for every gain of admin access; the administrators' notices hang on it.
# Reversing is refused while an event uses the action: the reverse would drop
# the record.

from typing import Any

from django.db import migrations, models

OLD_ACTIONS = [
    ("operator named first administrator", "Bootstrap"),
    ("exported member", "Export Member"),
    ("corrected member", "Correct Member"),
    ("changed member email", "Change Member Email"),
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
    ("removed block", "Removed Block"),
]
NEW_ACTIONS = [
    *OLD_ACTIONS[:4],
    ("gave admin access", "Gave Admin Access"),
    *OLD_ACTIONS[4:],
]
NEW_ONLY = "gave admin access"


def refuse_a_reverse_that_loses_history(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    if (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action=NEW_ONLY)
        .exists()
    ):
        raise RuntimeError("Cannot reverse retained gave-admin-access events.")


class Migration(migrations.Migration):
    dependencies = [("core", "0036_member_data_pages")]

    operations = [
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
