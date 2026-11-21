# SPDX-License-Identifier: AGPL-3.0-only
# the rule against double booking by spot .
# A claim holds spots by their ids, each for its whole time (R3). A shared
# claim's unit becomes the spot of that number on its floor plan; an
# exclusive claim holds the whole asset and names no spot. The rule keys on
# the asset's own space key and mark, which every claim copies and follows
# by cascade. A floor plan's own space goes: an asset's space is the only
# one. Floor plans no asset is on are deleted with their spots and groups.
# Every booking and block has its container from here on.
# It counts first. When anything does not map, it stops before it changes
# anything and lists what, with counts, for a person to decide. Reversing is
# refused while the rows hold what the rule by unit cannot.

import importlib
import logging
from typing import Any

import django.db.models.deletion
import django.db.models.expressions
import django.db.models.functions.comparison
from django.db import migrations, models

import randirandi.core.models.base
from randirandi.core.migrations._together import Together

logger = logging.getLogger(__name__)

CAPACITY_MIGRATION = importlib.import_module("randirandi.core.migrations.0002_capacity")
FLOOR_PLAN_MIGRATION = importlib.import_module(
    "randirandi.core.migrations.0016_floor_plans"
)

# The columns the app's user may change, for each table this part changes,
# all of them from here on. A claim's spots are replaced, never changed.
APP_UPDATES: dict[str, tuple[str, ...]] = {
    "core_assetinventory": (
        "capacity",
        "unit",
        "name",
        "last_spot_number",
        "last_group_number",
    ),
    "core_asset": (
        "name",
        "description",
        "space_id",
        "whole_space",
        "maximum_occupancy",
    ),
    "core_capacityallocation": (),
}

# Each list names at most this many rows; the count is always whole.
NAMED = 10


def _rows(cursor: Any, query: str) -> list[tuple[Any, ...]]:
    cursor.execute(query)
    return list(cursor.fetchall())


def _listed(faults: dict[str, list[tuple[Any, ...]]]) -> list[str]:
    return [
        f"{kind}: {len(rows)} ("
        + "; ".join(str(name) for (name,) in rows[:NAMED])
        + ("; …" if len(rows) > NAMED else "")
        + ")"
        for kind, rows in faults.items()
        if rows
    ]


def count_first(apps: Any, schema_editor: Any) -> None:
    """Stop, changing nothing, when a row does not map."""
    with schema_editor.connection.cursor() as cursor:
        faults = {
            "named-unit assets (an asset that is one unit of an inventory)": _rows(
                cursor,
                "SELECT 'organization ' || organization_id || ', asset ' || "
                "lpad(number::text, 3, '0') FROM core_asset WHERE named_unit <> 0 "
                "ORDER BY organization_id, number",
            ),
            # Slices of one claim on one unit are that one spot, held
            # throughout; only a unit that changes does not map.
            "claims whose unit changes during the booking": _rows(
                cursor,
                "SELECT 'taken time ' || taken_time_id FROM (SELECT DISTINCT "
                "taken_time_id FROM core_capacityallocation GROUP BY "
                "taken_time_id, ordinal HAVING count(DISTINCT unit) > 1) changing "
                "ORDER BY taken_time_id",
            ),
            "claims on a unit their floor plan has no spot for": _rows(
                cursor,
                "SELECT 'taken time ' || taken_time_id FROM (SELECT DISTINCT "
                "a.taken_time_id FROM core_capacityallocation a WHERE a.mode = "
                "'shared' AND NOT EXISTS (SELECT 1 FROM core_spot s WHERE "
                "s.floor_plan_id = a.inventory_id AND s.number = a.unit)) missing "
                "ORDER BY taken_time_id",
            ),
            "claims whose inventory is not their asset's floor plan": _rows(
                cursor,
                "SELECT 'taken time ' || t.id FROM core_takentime t JOIN "
                "core_assetinventory i ON i.id = t.inventory_id WHERE i.asset_id "
                "IS DISTINCT FROM t.asset_id ORDER BY t.id",
            ),
            "bookings without a container": _rows(
                cursor,
                "SELECT 'booking ' || id FROM core_booking "
                "WHERE booking_configuration_id IS NULL ORDER BY id",
            ),
            "blocks without a container": _rows(
                cursor,
                "SELECT 'block ' || id FROM core_block "
                "WHERE booking_configuration_id IS NULL ORDER BY id",
            ),
            # The rule by unit let a shared claim on an asset that covers its
            # whole space stand beside a claim on another asset of the space.
            "claims beside a shared claim on an asset that covers its whole "
            "space": _rows(
                cursor,
                "SELECT 'taken times ' || one || ' and ' || other FROM (SELECT "
                "DISTINCT least(a.taken_time_id, b.taken_time_id) one, "
                "greatest(a.taken_time_id, b.taken_time_id) other FROM "
                "core_capacityallocation a JOIN core_capacityallocation b ON "
                "b.organization_id = a.organization_id AND b.scope_key = "
                "a.scope_key AND b.inventory_id <> a.inventory_id AND "
                "tstzrange(b.starts_at, b.ends_at) && tstzrange(a.starts_at, "
                "a.ends_at) WHERE a.whole_space AND a.mode = 'shared' AND a.state "
                "IN ('held','confirmed','blocked') AND b.state IN "
                "('held','confirmed','blocked')) pairs ORDER BY one, other",
            ),
        }
    listed = _listed(faults)
    if listed:
        raise RuntimeError(
            "The rule-by-spot migration (M6 part 2.5f2) changed nothing: these "
            "rows do not map to spots and containers.\n" + "\n".join(listed)
        )


def fill(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        counts = {
            "floor plans no asset is on, deleted": _rows(
                cursor,
                "SELECT count(*) FROM core_assetinventory WHERE asset_id IS NULL",
            ),
            "slices joined into the one allocation of their spot": _rows(
                cursor,
                "SELECT count(*) - count(DISTINCT (taken_time_id, ordinal)) "
                "FROM core_capacityallocation",
            ),
            "claims given their spots": _rows(
                cursor,
                "SELECT count(DISTINCT taken_time_id) FROM core_capacityallocation "
                "WHERE mode = 'shared'",
            ),
            "claims on the whole asset": _rows(
                cursor,
                "SELECT count(*) FROM core_capacityallocation WHERE mode = 'exclusive'",
            ),
        }
        cursor.execute("""
            DELETE FROM core_capacityallocation c USING core_capacityallocation d
            WHERE d.taken_time_id = c.taken_time_id AND d.ordinal = c.ordinal
            AND d.id < c.id;
            DELETE FROM core_spot WHERE floor_plan_id IN
                (SELECT id FROM core_assetinventory WHERE asset_id IS NULL);
            DELETE FROM core_spotgroup WHERE floor_plan_id IN
                (SELECT id FROM core_assetinventory WHERE asset_id IS NULL);
            DELETE FROM core_assetinventory WHERE asset_id IS NULL;
            UPDATE core_takentime t SET scope_key = coalesce(a.space_id, -a.id)
            FROM core_asset a WHERE a.id = t.asset_id;
            UPDATE core_capacityallocation c SET asset_id = t.asset_id,
                scope_key = t.scope_key, starts_at = t.starts_at,
                ends_at = t.ends_at
            FROM core_takentime t WHERE t.id = c.taken_time_id;
            UPDATE core_capacityallocation c SET spot_id = s.id
            FROM core_spot s WHERE c.mode = 'shared'
            AND s.floor_plan_id = c.inventory_id AND s.number = c.unit;
            UPDATE core_capacityallocation SET inventory_id = NULL
            WHERE mode = 'exclusive';
        """)
    for name, ((count,),) in counts.items():
        logger.info("rule-by-spot migration: %s: %d", name, count)


def unfill(apps: Any, schema_editor: Any) -> None:
    """The rule by unit again, refused while the rows hold what it cannot."""
    with schema_editor.connection.cursor() as cursor:
        lost = {
            "assets without exactly one floor plan": (
                "SELECT count(*) FROM core_asset a WHERE (SELECT count(*) FROM "
                "core_assetinventory i WHERE i.asset_id = a.id) <> 1"
            ),
            "spaces with two or more assets that cover their whole space": (
                "SELECT count(*) FROM (SELECT space_id FROM core_asset WHERE "
                "whole_space GROUP BY space_id HAVING count(*) > 1) spaces"
            ),
            "claims on a spot numbered above its floor plan's capacity": (
                "SELECT count(*) FROM core_capacityallocation c JOIN core_spot s "
                "ON s.id = c.spot_id JOIN core_assetinventory i ON i.id = "
                "s.floor_plan_id WHERE s.number > i.capacity"
            ),
        }
        listed = []
        for kind, query in lost.items():
            ((count,),) = _rows(cursor, query)
            if count:
                listed.append(f"{kind}: {count}")
        if listed:
            raise RuntimeError(
                "Cannot reverse the rule-by-spot migration while the rows hold "
                "what the rule by unit cannot:\n" + "\n".join(listed)
            )
        cursor.execute("""
            UPDATE core_assetinventory i SET space_id = a.space_id
            FROM core_asset a WHERE a.id = i.asset_id;
            UPDATE core_asset a SET inventory_id = i.id,
                inventory_capacity = i.capacity, named_unit = 0,
                scope_key = i.scope_key
            FROM core_assetinventory i WHERE i.asset_id = a.id;
            UPDATE core_takentime t SET inventory_id = a.inventory_id,
                inventory_capacity = a.inventory_capacity, named_unit = 0,
                scope_key = a.scope_key,
                represented_space = CASE WHEN a.whole_space THEN a.scope_key
                    ELSE 0 END
            FROM core_asset a WHERE a.id = t.asset_id;
            UPDATE core_capacityallocation c SET inventory_id = t.inventory_id,
                inventory_capacity = t.inventory_capacity, named_unit = 0,
                scope_key = t.scope_key, booking_start = t.starts_at,
                booking_end = t.ends_at, unit = 0
            FROM core_takentime t WHERE t.id = c.taken_time_id;
            UPDATE core_capacityallocation c SET unit = s.number
            FROM core_spot s WHERE s.id = c.spot_id;
        """)


def grant(schema_editor: Any, updates: dict[str, tuple[str, ...]]) -> None:
    """Let the app's user change these columns of each table, and no other."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='randirandi_app'")
        if cursor.fetchone() is None:
            # Unit-test databases without a deployed role have no app login.
            return
        for table, columns in updates.items():
            schema_editor.execute(f"REVOKE UPDATE ON {table} FROM randirandi_app")
            if columns:
                schema_editor.execute(
                    f"GRANT UPDATE ({', '.join(columns)}) ON {table} TO randirandi_app"
                )
            cursor.execute(
                """SELECT a.attname,
                has_column_privilege('randirandi_app',c.oid,a.attname,'UPDATE')
                FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
                WHERE c.oid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped""",
                [table],
            )
            if any(
                allowed != (column in columns) for column, allowed in cursor.fetchall()
            ):
                raise RuntimeError("Unexpected effective application UPDATE privileges")


def install_permissions(apps: Any, schema_editor: Any) -> None:
    grant(schema_editor, APP_UPDATES)


def restore_old_permissions(apps: Any, schema_editor: Any) -> None:
    FLOOR_PLAN_MIGRATION.install_permissions(apps, schema_editor)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='randirandi_app'")
        if cursor.fetchone() is None:
            return
    columns = CAPACITY_MIGRATION.APP_UPDATES["core_capacityallocation"]
    schema_editor.execute(
        f"GRANT UPDATE ({', '.join(columns)}) ON core_capacityallocation TO "
        "randirandi_app"
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0016_floor_plans")]

    operations = [
        migrations.RunPython(count_first, migrations.RunPython.noop),
        # Reversed last, once the old columns are back.
        migrations.RunPython(migrations.RunPython.noop, restore_old_permissions),
        Together(
            # What points at a key goes before the key.
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_header",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_final_slice",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_previous_claim",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_previous_slice",
            ),
            migrations.RemoveConstraint(
                model_name="takentime",
                name="takentime_complete_allocation",
            ),
            migrations.RemoveConstraint(
                model_name="takentime",
                name="takentime_asset_identity",
            ),
            migrations.RemoveConstraint(
                model_name="takentime",
                name="takentime_current_bound",
            ),
            migrations.RemoveConstraint(
                model_name="takentime",
                name="takentime_inventory_same_org",
            ),
            migrations.RemoveConstraint(
                model_name="takentime",
                name="takentime_demand",
            ),
            migrations.RemoveConstraint(
                model_name="bookingconfiguration",
                name="configuration_floor_plan_of_its_asset",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_space_is_its_floor_plans",
            ),
            migrations.RemoveConstraint(
                model_name="assetinventory",
                name="floor_plan_is_its_assets_inventory",
            ),
            migrations.RemoveConstraint(
                model_name="assetinventory",
                name="floor_plan_space_key",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_inventory_bound",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_inventory_same_org",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_slice_key",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_slice_bounds",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_claim_number",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_unit",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_fixed_identity_interval",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="allocation_one_slice_at_a_time",
            ),
            migrations.RemoveConstraint(
                model_name="capacityallocation",
                name="capacity_no_double_booking",
            ),
            migrations.RemoveConstraint(
                model_name="takentime",
                name="takentime_allocation_key",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_inventory_unit_once",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_whole_space_once",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_identity_key",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_current_bound_key",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_capacity_selection",
            ),
            migrations.RemoveConstraint(
                model_name="asset",
                name="asset_inventory_key",
            ),
            migrations.RemoveConstraint(
                model_name="assetinventory",
                name="inventory_witness_key",
            ),
            migrations.RemoveConstraint(
                model_name="assetinventory",
                name="assetinventory_space_same_org",
            ),
            migrations.RemoveConstraint(
                model_name="assetinventory",
                name="floor_plan_one_per_asset",
            ),
        ),
        Together(
            # The retired columns may be empty while they go, so that a reverse
            # can bring them back before it fills them.
            migrations.AlterField(
                model_name="takentime",
                name="inventory",
                field=models.ForeignKey(
                    editable=False,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="core.assetinventory",
                ),
            ),
            migrations.AlterField(
                model_name="takentime",
                name="inventory_capacity",
                field=models.PositiveIntegerField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="takentime",
                name="named_unit",
                field=models.PositiveIntegerField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="takentime",
                name="represented_space",
                field=models.BigIntegerField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="inventory_capacity",
                field=models.PositiveIntegerField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="named_unit",
                field=models.PositiveIntegerField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="booking_start",
                field=models.DateTimeField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="booking_end",
                field=models.DateTimeField(editable=False, null=True),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="unit",
                field=models.PositiveIntegerField(null=True),
            ),
            migrations.AddField(
                model_name="capacityallocation",
                name="asset",
                field=models.ForeignKey(
                    editable=False,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="core.asset",
                ),
            ),
            migrations.AddField(
                model_name="capacityallocation",
                name="spot",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="core.spot",
                ),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="inventory",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="core.assetinventory",
                ),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="starts_at",
                field=models.DateTimeField(editable=False),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="ends_at",
                field=models.DateTimeField(editable=False),
            ),
        ),
        # Rows queued for deferred checks would hold the tables' changes
        # back.
        migrations.RunSQL(
            sql=migrations.RunSQL.noop, reverse_sql="SET CONSTRAINTS ALL IMMEDIATE"
        ),
        migrations.RunPython(fill, unfill),
        migrations.RunSQL(
            sql="SET CONSTRAINTS ALL IMMEDIATE", reverse_sql=migrations.RunSQL.noop
        ),
        Together(
            migrations.RemoveField(
                model_name="asset",
                name="space_scope",
            ),
            migrations.RemoveField(
                model_name="asset",
                name="inventory",
            ),
            migrations.RemoveField(
                model_name="asset",
                name="inventory_capacity",
            ),
            migrations.RemoveField(
                model_name="asset",
                name="named_unit",
            ),
            migrations.RemoveField(
                model_name="asset",
                name="represented_space",
            ),
            migrations.RemoveField(
                model_name="assetinventory",
                name="scope_key",
            ),
            migrations.RemoveField(
                model_name="assetinventory",
                name="space",
            ),
            migrations.RemoveField(
                model_name="capacityallocation",
                name="booking_end",
            ),
            migrations.RemoveField(
                model_name="capacityallocation",
                name="inventory_capacity",
            ),
            migrations.RemoveField(
                model_name="capacityallocation",
                name="named_unit",
            ),
            migrations.RemoveField(
                model_name="capacityallocation",
                name="previous_end",
            ),
            migrations.RemoveField(
                model_name="capacityallocation",
                name="unit",
            ),
            migrations.RemoveField(
                model_name="takentime",
                name="inventory",
            ),
            migrations.RemoveField(
                model_name="takentime",
                name="inventory_capacity",
            ),
            migrations.RemoveField(
                model_name="takentime",
                name="named_unit",
            ),
            migrations.RemoveField(
                model_name="takentime",
                name="represented_space",
            ),
            migrations.RemoveField(
                model_name="capacityallocation",
                name="booking_start",
            ),
            # The space key is the asset's own from here on: its space, or the
            # asset alone without one.
            migrations.RemoveField(model_name="asset", name="scope_key"),
            migrations.AddField(
                model_name="asset",
                name="scope_key",
                field=models.GeneratedField(
                    db_persist=True,
                    expression=django.db.models.functions.comparison.Coalesce(
                        models.F("space_id"),
                        django.db.models.expressions.CombinedExpression(
                            models.F("id"), "*", models.Value(-1)
                        ),
                    ),
                    output_field=models.BigIntegerField(),
                ),
            ),
            migrations.AlterField(
                model_name="capacityallocation",
                name="asset",
                field=models.ForeignKey(
                    editable=False,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="core.asset",
                ),
            ),
            migrations.AlterField(
                model_name="assetinventory",
                name="asset",
                field=models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="floor_plans",
                    to="core.asset",
                ),
            ),
            migrations.AlterField(
                model_name="block",
                name="booking_configuration",
                field=models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="blocks",
                    to="core.bookingconfiguration",
                ),
            ),
            migrations.AlterField(
                model_name="booking",
                name="booking_configuration",
                field=models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="bookings",
                    to="core.bookingconfiguration",
                ),
            ),
        ),
        Together(
            # Keys before what points at them.
            migrations.AddConstraint(
                model_name="asset",
                constraint=models.UniqueConstraint(
                    fields=("organization", "id", "scope_key", "whole_space"),
                    name="asset_placement_key",
                ),
            ),
            # One asset per space carries the mark, now on the asset's own space.
            migrations.AddConstraint(
                model_name="asset",
                constraint=models.UniqueConstraint(
                    condition=models.Q(("whole_space", True)),
                    fields=("space",),
                    name="asset_whole_space_once",
                ),
            ),
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=models.UniqueConstraint(
                    fields=("organization", "asset", "id"),
                    name="floor_plan_of_asset_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="spot",
                constraint=models.UniqueConstraint(
                    fields=("organization", "floor_plan", "id"),
                    name="spot_floor_plan_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="takentime",
                constraint=models.UniqueConstraint(
                    fields=(
                        "id",
                        "organization",
                        "asset",
                        "scope_key",
                        "whole_space",
                        "mode",
                        "quantity",
                        "state",
                        "starts_at",
                        "ends_at",
                    ),
                    name="takentime_claim_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=models.UniqueConstraint(
                    fields=("taken_time", "ordinal"), name="allocation_ordinal_once"
                ),
            ),
            migrations.AddConstraint(
                model_name="bookingconfiguration",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, asset_id, floor_plan_id) "
                        "REFERENCES core_assetinventory (organization_id, asset_id, id)"
                    ),
                    name="configuration_floor_plan_of_its_asset",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="asset", name="capacityallocation_asset_same_org"
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="spot", name="capacityallocation_spot_same_org"
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "CHECK (ordinal >= 1 AND ordinal <= CASE WHEN mode = "
                        "'exclusive' THEN 1 ELSE quantity END)"
                    ),
                    name="allocation_ordinal_in_claim",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "CHECK ((mode = 'exclusive' AND spot_id IS NULL AND "
                        "inventory_id IS NULL) OR (mode = 'shared' AND spot_id IS NOT "
                        "NULL AND inventory_id IS NOT NULL))"
                    ),
                    name="allocation_spot_when_shared",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (taken_time_id, organization_id, asset_id, "
                        "scope_key, whole_space, mode, quantity, state, starts_at, "
                        "ends_at) REFERENCES core_takentime (id, organization_id, "
                        "asset_id, scope_key, whole_space, mode, quantity, state, "
                        "starts_at, ends_at) ON UPDATE CASCADE"
                    ),
                    name="allocation_claim_header",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (taken_time_id, previous_ordinal) REFERENCES "
                        "core_capacityallocation (taken_time_id, ordinal) DEFERRABLE "
                        "INITIALLY DEFERRED"
                    ),
                    name="allocation_previous_claim",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, inventory_id, spot_id) "
                        "REFERENCES core_spot (organization_id, floor_plan_id, id)"
                    ),
                    name="allocation_spot_on_its_floor_plan",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, asset_id, inventory_id) "
                        "REFERENCES core_assetinventory (organization_id, asset_id, id)"
                    ),
                    name="allocation_floor_plan_of_its_asset",
                ),
            ),
            migrations.AddConstraint(
                model_name="capacityallocation",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "EXCLUDE USING gist (int8range(organization_id, "
                        "organization_id, '[]') WITH &&, int8range(scope_key, "
                        "scope_key, '[]') WITH &&, (CASE WHEN whole_space THEN "
                        "int8range(NULL, NULL) ELSE int8range(asset_id, asset_id, "
                        "'[]') END) WITH &&, (CASE WHEN mode = 'exclusive' OR NOT "
                        "whole_space THEN int8range(NULL, NULL) ELSE "
                        "int8range(spot_id, spot_id, '[]') END) WITH &&, (CASE WHEN "
                        "mode = 'exclusive' OR whole_space THEN int8range(NULL, NULL) "
                        "ELSE int8range(spot_id, spot_id, '[]') END) WITH &&, "
                        "tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (state IN "
                        "('held','confirmed','blocked')) DEFERRABLE INITIALLY IMMEDIATE"
                    ),
                    name="capacity_no_double_booking",
                ),
            ),
            migrations.AddConstraint(
                model_name="takentime",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "CHECK (mode IN ('shared','exclusive') AND quantity > 0 AND "
                        "(mode <> 'exclusive' OR quantity = 1))"
                    ),
                    name="takentime_quantity_by_mode",
                ),
            ),
            migrations.AddConstraint(
                model_name="takentime",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, asset_id, scope_key, "
                        "whole_space) REFERENCES core_asset (organization_id, id, "
                        "scope_key, whole_space) ON UPDATE CASCADE"
                    ),
                    name="takentime_asset_placement",
                ),
            ),
            migrations.AddConstraint(
                model_name="takentime",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (id, required_claims) REFERENCES "
                        "core_capacityallocation (taken_time_id, ordinal) DEFERRABLE "
                        "INITIALLY DEFERRED"
                    ),
                    name="takentime_every_spot_held",
                ),
            ),
        ),
        migrations.RunPython(install_permissions, migrations.RunPython.noop),
    ]
