# SPDX-License-Identifier: AGPL-3.0-only
# an administrator changes a member's email. The audit vocabulary
# gains "changed member email", and a security notice may name the address it
# goes to (the member's old address, which the member no longer has). The
# address is emptied when the notice is delivered. Reversing is refused while an
# event uses the action or a notice still holds an address: the reverse would
# drop the record, or the only place the old address is kept.

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
    ("removed block", "Removed Block"),
]
NEW_ACTIONS = [
    *OLD_ACTIONS[:3],
    ("changed member email", "Change Member Email"),
    *OLD_ACTIONS[3:],
]
NEW_ONLY = "changed member email"


def refuse_a_reverse_that_loses_history(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    if (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action=NEW_ONLY)
        .exists()
    ):
        raise RuntimeError("Cannot reverse retained member email changes.")
    if (
        apps.get_model("core", "SecurityNotice")
        ._default_manager.using(alias)
        .exclude(address="")
        .exists()
    ):
        raise RuntimeError("Cannot reverse while a notice holds an old address.")


class Migration(migrations.Migration):
    dependencies = [("core", "0035_booking_limits")]

    operations = [
        migrations.AddField(
            model_name="securitynotice",
            name="address",
            field=models.EmailField(blank=True, max_length=254),
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
