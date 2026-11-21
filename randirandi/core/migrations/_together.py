# SPDX-License-Identifier: AGPL-3.0-only
"""Several schema operations taken as one migration step.

After each operation Django renders again every model related to the one it
changed, both on the way forward and on the way back. Migrations changing
many fields and rules of connected models mean every
migration test that goes back past them paid that rendering for each of
them, twice. `Together` makes the same changes, operation by operation and in
the same order, to the database, and renders the models they touched once.

The database side gives each operation the step's first and last state, so a
step holds only rules or only fields, and names each field or rule once,
except one taken away and then added again. The step refuses anything else
when it is written.

It relies on Django 6.1: each operation asks for its models to be rendered
again through `ProjectState.reload_model`, and `ProjectState.reload_models`
renders several at once. `pyproject.toml` holds Django below 6.2, and a test
counts the renders, so a Django that renders some other way is caught.
"""

from typing import Any

from django.db import migrations
from django.db.migrations.operations.base import Operation

_RULES = (migrations.AddConstraint, migrations.RemoveConstraint)
_FIELDS = (migrations.AddField, migrations.AlterField, migrations.RemoveField)


def _named(operation: Any) -> tuple[str, str]:
    if isinstance(operation, migrations.AddConstraint):
        return operation.model_name_lower, operation.constraint.name
    return operation.model_name_lower, operation.name


def _taken_away(operation: Any) -> bool:
    return isinstance(operation, migrations.RemoveConstraint | migrations.RemoveField)


def _added(operation: Any) -> bool:
    return isinstance(operation, migrations.AddConstraint | migrations.AddField)


class Together(Operation):
    reduces_to_sql = True

    def __init__(self, *operations: Operation) -> None:
        kinds = [
            kind
            for kind in (_RULES, _FIELDS)
            if all(isinstance(operation, kind) for operation in operations)
        ]
        if not operations or not kinds:
            raise ValueError("A step together holds only rules or only fields.")
        seen: dict[tuple[str, str], Any] = {}
        for operation in operations:
            name = _named(operation)
            earlier = seen.get(name)
            if earlier is not None and not (_taken_away(earlier) and _added(operation)):
                raise ValueError(
                    f"A step together names {name[0]}.{name[1]} twice; only one "
                    "taken away and then added again may."
                )
            seen[name] = operation
        self.operations = list(operations)
        super().__init__()

    def deconstruct(self) -> tuple[str, list[Operation], dict[str, Any]]:
        return self.__class__.__qualname__, self.operations, {}

    @property
    def reversible(self) -> bool:  # type: ignore[override]
        return all(operation.reversible for operation in self.operations)

    def state_forwards(self, app_label: str, state: Any) -> None:
        touched: dict[tuple[str, str], bool] = {}

        def note(app: str, model: str, delay: bool = False) -> None:
            touched[app, model] = touched.get((app, model), True) and delay

        # Each operation asks for its models to be rendered again; note them
        # instead, and render them once at the end.
        state.reload_model = note
        try:
            for operation in self.operations:
                operation.state_forwards(app_label, state)
        finally:
            del state.reload_model
        for delay in (True, False):
            models = [model for model, kind in touched.items() if kind is delay]
            if models:
                state.reload_models(models, delay=delay)

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: Any, to_state: Any
    ) -> None:
        for operation in self.operations:
            operation.database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: Any, to_state: Any
    ) -> None:
        for operation in reversed(self.operations):
            operation.database_backwards(app_label, schema_editor, from_state, to_state)

    def describe(self) -> str:
        return "; ".join(operation.describe() for operation in self.operations)

    @property
    def migration_name_fragment(self) -> str:
        return "together"
