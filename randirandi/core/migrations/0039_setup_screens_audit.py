# SPDX-License-Identifier: AGPL-3.0-only
# the audit vocabulary gains the setup screens' nine actions
# (made, changed and removed a booking type, a booking category and a role).
# No table, column or grant changes. Reversing is refused while an event uses
# one of them: the reverse would drop the record.

from typing import Any

from django.db import migrations, models

OLD_ACTIONS = [
    ("operator named first administrator", "Bootstrap"),
    ("exported member", "Export Member"),
    ("corrected member", "Correct Member"),
    ("changed member email", "Change Member Email"),
    ("gave admin access", "Gave Admin Access"),
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
NEW_ONLY = [
    ("made booking type", "Made Booking Type"),
    ("changed booking type", "Changed Booking Type"),
    ("removed booking type", "Removed Booking Type"),
    ("made booking category", "Made Booking Category"),
    ("changed booking category", "Changed Booking Category"),
    ("removed booking category", "Removed Booking Category"),
    ("made role", "Made Role"),
    ("changed role", "Changed Role"),
    ("removed role", "Removed Role"),
]
NEW_ACTIONS = [*OLD_ACTIONS, *NEW_ONLY]


def refuse_a_reverse_that_loses_history(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    held = (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action__in=[action for action, _ in NEW_ONLY])
        .count()
    )
    if held:
        raise RuntimeError(
            f"Cannot reverse while {held} setup-screen audit event(s) exist "
            "(retained setup-screen events)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0038_category_icon")]

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
