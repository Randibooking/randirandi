# SPDX-License-Identifier: AGPL-3.0-only
"""The step that takes several schema changes together: it leaves the models as
the same changes one by one would, renders them fewer times, and refuses what
its database side cannot give the right states to."""

from collections.abc import Callable
from typing import Any

import pytest
from django.db import migrations, models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState, ProjectState, StateApps

from randirandi.core.migrations._together import Together


def rule(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(condition=models.Q(number__gte=1), name=name)


def floor_plan() -> ProjectState:
    """A made-up plan of rooms, seats, bookings and payments, its models
    rendered."""
    state = ProjectState()
    state.add_model(
        ModelState(
            "plan",
            "Room",
            [
                ("id", models.AutoField(primary_key=True)),
                ("name", models.TextField()),
                ("size", models.IntegerField(null=True)),
            ],
        )
    )
    state.add_model(
        ModelState(
            "plan",
            "Seat",
            [
                ("id", models.AutoField(primary_key=True)),
                (
                    "room",
                    models.ForeignKey(
                        "plan.Room", models.CASCADE, related_name="seats"
                    ),
                ),
                ("number", models.IntegerField()),
            ],
            options={"ordering": ["number"], "constraints": [rule("seat_number")]},
        )
    )
    state.add_model(
        ModelState(
            "plan",
            "Booking",
            [
                ("id", models.AutoField(primary_key=True)),
                ("seat", models.ForeignKey("plan.Seat", models.CASCADE)),
            ],
        )
    )
    # Two steps from the seat, out of reach of a render that leaves out
    # what is related to what is related.
    state.add_model(
        ModelState(
            "plan",
            "Payment",
            [
                ("id", models.AutoField(primary_key=True)),
                ("booking", models.ForeignKey("plan.Booking", models.CASCADE)),
            ],
        )
    )
    state.apps  # noqa: B018 - render the models, as a migration run has them
    return state


def field_changes() -> list[Operation]:
    # Fields with and without a relation, so models are rendered both with
    # and without the models related to them; the seat gets both.
    return [
        migrations.AddField("room", "floor", models.IntegerField(null=True)),
        migrations.AddField(
            "booking",
            "room",
            models.ForeignKey(
                "plan.Room", models.CASCADE, null=True, related_name="bookings"
            ),
        ),
        migrations.AlterField(
            "seat",
            "room",
            models.ForeignKey("plan.Room", models.CASCADE, related_name="places"),
        ),
        migrations.AddField("seat", "label", models.TextField(default="")),
        migrations.RemoveField("room", "size"),
        migrations.RemoveField("room", "name"),
        migrations.AddField(
            "room", "name", models.CharField(max_length=40, default="")
        ),
    ]


def rule_changes() -> list[Operation]:
    return [
        migrations.RemoveConstraint("seat", "seat_number"),
        migrations.AddConstraint(
            "seat",
            models.CheckConstraint(
                condition=models.Q(number__gte=0), name="seat_number"
            ),
        ),
        migrations.AddConstraint(
            "booking", models.UniqueConstraint(fields=["seat"], name="one_booking")
        ),
    ]


def rendered(state: ProjectState) -> dict[str, dict[str, Any]]:
    """What the rendered models hold."""
    found = {}
    for model in state.apps.get_models():
        meta = model._meta
        found[meta.label] = {
            "fields": [
                repr(field.deconstruct())
                for field in [*meta.local_fields, *meta.local_many_to_many]
            ],
            "rules": [
                repr(constraint.deconstruct()) for constraint in meta.constraints
            ],
            "options": {
                name: repr(value)
                for name, value in meta.original_attrs.items()
                if name != "apps"
            },
            "pointed at from": sorted(
                f"{relation.related_model._meta.label}.{relation.field.name}"
                for relation in meta.related_objects
            ),
        }
    return found


def pointing_at_the_model_now(state: ProjectState) -> set[tuple[str, str]]:
    """The relations, both ways, that point at the model as rendered now."""
    apps = state.apps

    def now(model: Any) -> bool:
        return model is apps.get_model(model._meta.label)

    found = set()
    for model in apps.get_models():
        meta = model._meta
        for field in meta.local_fields:
            if field.remote_field and now(field.remote_field.model):
                found.add((meta.label, field.name))
        for relation in meta.related_objects:
            if now(relation.related_model):
                found.add((meta.label, relation.name))
    return found


@pytest.mark.parametrize("changes", [field_changes, rule_changes])
def test_a_step_leaves_the_models_as_one_by_one(
    changes: Callable[[], list[Operation]],
) -> None:
    one_by_one = floor_plan()
    for operation in changes():
        operation.state_forwards("plan", one_by_one)
    together = floor_plan()
    Together(*changes()).state_forwards("plan", together)

    assert rendered(together) == rendered(one_by_one)
    assert together.is_delayed == one_by_one.is_delayed
    # One by one, a change that renders only the models near it can leave a
    # farther one pointing at a model rendered before; the payment does,
    # after the field changes. Rendering the touched models at once leaves
    # no more of those.
    assert pointing_at_the_model_now(together) >= pointing_at_the_model_now(one_by_one)


def test_a_step_renders_its_models_fewer_times_than_one_by_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one_by_one, together = floor_plan(), floor_plan()
    renders: list[StateApps] = []
    render = StateApps.render_multiple

    def counted(apps: StateApps, model_states: Any) -> None:
        renders.append(apps)
        render(apps, model_states)

    monkeypatch.setattr(StateApps, "render_multiple", counted)
    changes = field_changes()
    for operation in changes:
        operation.state_forwards("plan", one_by_one)
    Together(*changes).state_forwards("plan", together)

    # One by one, Django renders after each change. Together renders once
    # for the models rendered with those related to them, once for the rest.
    assert renders.count(one_by_one.apps) == len(changes)
    assert renders.count(together.apps) == 2


def test_a_step_that_fails_leaves_the_state_rendering_as_before() -> None:
    state = floor_plan()
    step = Together(
        migrations.AddField("room", "floor", models.IntegerField(null=True)),
        migrations.RemoveField("hall", "size"),
    )
    with pytest.raises(KeyError):
        step.state_forwards("plan", state)

    assert "reload_model" not in vars(state)
    assert state.reload_model.__func__ is ProjectState.reload_model  # type: ignore[attr-defined]


def test_rules_and_fields_are_not_taken_together() -> None:
    with pytest.raises(ValueError, match="only rules or only fields"):
        Together(
            migrations.AddConstraint(model_name="asset", constraint=rule("a")),
            migrations.RemoveField(model_name="asset", name="named_unit"),
        )


def test_a_field_changed_twice_is_refused() -> None:
    with pytest.raises(ValueError, match=r"names asset\.number twice"):
        Together(
            migrations.AlterField(
                model_name="asset", name="number", field=models.IntegerField()
            ),
            migrations.AlterField(
                model_name="asset", name="number", field=models.BigIntegerField()
            ),
        )


def test_a_rule_added_and_taken_away_again_is_refused() -> None:
    with pytest.raises(ValueError, match=r"names asset\.a twice"):
        Together(
            migrations.AddConstraint(model_name="asset", constraint=rule("a")),
            migrations.RemoveConstraint(model_name="asset", name="a"),
        )


def test_what_is_taken_away_may_be_added_again() -> None:
    step = Together(
        migrations.RemoveConstraint(model_name="asset", name="a"),
        migrations.AddConstraint(model_name="asset", constraint=rule("a")),
        migrations.AddConstraint(model_name="asset", constraint=rule("b")),
    )
    assert step.reversible
    assert step.describe() == (
        "Remove constraint a from model asset; "
        "Create constraint a on model asset; "
        "Create constraint b on model asset"
    )


def test_nothing_is_not_a_step() -> None:
    with pytest.raises(ValueError, match="only rules or only fields"):
        Together()
