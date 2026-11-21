# SPDX-License-Identifier: AGPL-3.0-only
# the audit vocabulary gains one action, "changed organization"
# (an administrator saved the organization's own details). No table, column or
# grant changes. Reversing is refused while an event uses it: the reverse would
# drop the record.
# OLD_ACTIONS is the vocabulary of 0048_availability_closures, the newest that
# changed it: every earlier action survives, forward and back. The migration
# adds the one action and takes only it away.

import importlib
from typing import Any

from django.db import migrations, models

OLD_ACTIONS = importlib.import_module(
    "randirandi.core.migrations.0048_availability_closures"
).NEW_ACTIONS
NEW_ONLY = [("changed organization", "Changed Organization")]
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
            f"Cannot reverse while {held} organization audit event(s) exist "
            "(retained organization events)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0048_availability_closures")]

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
