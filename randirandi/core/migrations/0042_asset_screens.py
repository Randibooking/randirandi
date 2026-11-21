# SPDX-License-Identifier: AGPL-3.0-only
# the audit vocabulary gains the assets and spaces screens' fifteen
# actions (made, changed and removed a space, an asset, a floor plan, a spot and a
# group of spots), and the application's user may change a spot's place: removing
# a spot moves the last spot into its place, so the places stay 1 to the
# capacity. No table or column changes. Reversing is refused while an event uses
# one of the actions: the reverse would drop the record. The permission goes back
# to a spot's name and group.

import importlib
from typing import Any

from django.db import migrations, models

# The newest migration's vocabulary, so what other parts added (the member
# import's, "added member") survives forward and back.
PREVIOUS = importlib.import_module("randirandi.core.migrations.0041_added_member")
RULE_MIGRATION = importlib.import_module("randirandi.core.migrations.0017_rule_by_spot")
FLOOR_PLAN_MIGRATION = importlib.import_module(
    "randirandi.core.migrations.0016_floor_plans"
)

OLD_ACTIONS = PREVIOUS.NEW_ACTIONS
NEW_ONLY = [
    ("made space", "Made Space"),
    ("changed space", "Changed Space"),
    ("removed space", "Removed Space"),
    ("made asset", "Made Asset"),
    ("changed asset", "Changed Asset"),
    ("removed asset", "Removed Asset"),
    ("made floor plan", "Made Floor Plan"),
    ("changed floor plan", "Changed Floor Plan"),
    ("removed floor plan", "Removed Floor Plan"),
    ("made spot", "Made Spot"),
    ("changed spot", "Changed Spot"),
    ("removed spot", "Removed Spot"),
    ("made spot group", "Made Spot Group"),
    ("changed spot group", "Changed Spot Group"),
    ("removed spot group", "Removed Spot Group"),
]
NEW_ACTIONS = [*OLD_ACTIONS, *NEW_ONLY]

# The columns the app's user may change on a spot: 0016's, and its place.
APP_UPDATES: dict[str, tuple[str, ...]] = {
    "core_spot": (*FLOOR_PLAN_MIGRATION.APP_UPDATES["core_spot"], "place"),
}


def install_permissions(apps: Any, schema_editor: Any) -> None:
    RULE_MIGRATION.grant(schema_editor, APP_UPDATES)


def restore_old_permissions(apps: Any, schema_editor: Any) -> None:
    RULE_MIGRATION.grant(
        schema_editor, {"core_spot": FLOOR_PLAN_MIGRATION.APP_UPDATES["core_spot"]}
    )


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
            f"Cannot reverse while {held} assets-and-spaces audit event(s) exist "
            "(retained setup-screen events)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0041_added_member")]

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
        migrations.RunPython(install_permissions, restore_old_permissions),
        # Listed last, so reversing runs it first, before any table changes.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_history
        ),
    ]
