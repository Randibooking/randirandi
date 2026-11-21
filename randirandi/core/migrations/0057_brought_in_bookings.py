# SPDX-License-Identifier: AGPL-3.0-only
"""The audit vocabulary gains the action "brought in old booking"
(`old_booking_import`). Reversing is refused while an audit event uses it: the
reverse would drop the record. A reverse with none is clean."""

import importlib
from typing import Any

from django.db import migrations, models

# The vocabulary of the migrations before this one, so that what other parts
# added survives forward and back.
OLD_ACTIONS = importlib.import_module(
    "randirandi.core.migrations.0052_events"
).NEW_ACTIONS
NEW_ONLY = [("brought in old booking", "Brought In Booking")]
NEW_ACTIONS = [*OLD_ACTIONS, *NEW_ONLY]


def refuse_a_reverse_that_loses_records(apps: Any, schema_editor: Any) -> None:
    events = apps.get_model("core", "AuditEvent")._default_manager
    if (
        events.using(schema_editor.connection.alias)
        .filter(action__in=[action for action, _ in NEW_ONLY])
        .exists()
    ):
        raise RuntimeError(
            "Cannot reverse while an audit event records a booking brought in."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0056_payment_environment")]

    operations = [
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_records
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
    ]
