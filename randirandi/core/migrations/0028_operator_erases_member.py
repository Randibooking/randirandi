# SPDX-License-Identifier: AGPL-3.0-only
# the operator erases a member on the member's written request.
# The audit vocabulary gains "operator erased member" (the action the
# administrator form of erasure had no counterpart for). Reversing is refused
# while an event uses it: the old vocabulary cannot hold that event.

from typing import Any

from django.db import migrations, models

OLD_ACTIONS = [
    ("operator named first administrator", "Bootstrap"),
    ("exported member", "Export Member"),
    ("corrected member", "Correct Member"),
    ("erased member", "Erase Member"),
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
NEW_ACTIONS = [
    *OLD_ACTIONS[:4],
    ("operator erased member", "Operator Erase Member"),
    *OLD_ACTIONS[4:],
]
NEW_ONLY = "operator erased member"


def refuse_a_reverse_that_loses_history(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    if (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action=NEW_ONLY)
        .exists()
    ):
        raise RuntimeError("Cannot reverse retained operator erasure history.")


class Migration(migrations.Migration):
    dependencies = [("core", "0027_time_zone_and_currency")]

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
