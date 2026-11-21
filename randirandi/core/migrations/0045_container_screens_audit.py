# SPDX-License-Identifier: AGPL-3.0-only
# the audit vocabulary gains the container screens' three actions
# (made, changed and removed a container). No table, column or grant changes.
# Reversing is refused while an event uses one of them: the reverse would drop
# the record.
# OLD_ACTIONS is the vocabulary of 0042_asset_screens, the newest that changed
# it (0043 and 0044 leave it as it is): every action of 7.93, 8.2 and 7.95 survives,
# forward and back. The migration adds the three container actions and takes
# only them away.

import importlib
from typing import Any

from django.db import migrations, models

OLD_ACTIONS = importlib.import_module(
    "randirandi.core.migrations.0042_asset_screens"
).NEW_ACTIONS
NEW_ONLY = [
    ("made container", "Made Booking Configuration"),
    ("changed container", "Changed Booking Configuration"),
    ("removed container", "Removed Booking Configuration"),
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
            f"Cannot reverse while {held} container-screen audit event(s) exist "
            "(retained container-screen events)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0044_authenticator_secret_encrypted")]

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
