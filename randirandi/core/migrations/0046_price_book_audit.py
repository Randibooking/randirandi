# SPDX-License-Identifier: AGPL-3.0-only
# the audit vocabulary gains the price screens' six actions
# (made, changed and removed a price book and a price). No table, column or
# grant changes. Reversing is refused while an event uses one of them: the
# reverse would drop the record.
# OLD_ACTIONS is the vocabulary of 0045_container_screens_audit, the newest
# that changed it: every earlier action survives, forward and back. The
# migration adds the six price actions and takes only them away.

import importlib
from typing import Any

from django.db import migrations, models

OLD_ACTIONS = importlib.import_module(
    "randirandi.core.migrations.0045_container_screens_audit"
).NEW_ACTIONS
NEW_ONLY = [
    ("made price book", "Made Price Book"),
    ("changed price book", "Changed Price Book"),
    ("removed price book", "Removed Price Book"),
    ("made price", "Made Price"),
    ("changed price", "Changed Price"),
    ("removed price", "Removed Price"),
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
            f"Cannot reverse while {held} price audit event(s) exist "
            "(retained price events)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0045_container_screens_audit")]

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
