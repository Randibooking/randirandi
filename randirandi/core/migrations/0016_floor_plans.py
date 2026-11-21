# SPDX-License-Identifier: AGPL-3.0-only
# floor plans, spots, groups and spaces stored .
# A floor plan is stored in the inventory's table: every inventory becomes a
# floor plan with spots 1 to its capacity, without names, and an inventory
# with one asset becomes that asset's. An asset takes its inventory's space
# and keeps its mark; a shared container takes its asset's floor plan. Every
# booking takes the container its booking lines name, and every block the
# container whose time it is on.
# It counts first. When anything does not map, it stops before it changes
# anything and lists what, with counts, for a person to decide. Reversing is
# refused while the floor plans hold what an inventory cannot.

import importlib
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import django.db.models.deletion
import django.db.models.functions.comparison
import django.db.models.manager
from django.db import migrations, models

import randirandi.core.models.base
from randirandi.core.migrations._together import Together

logger = logging.getLogger(__name__)

CAPACITY_MIGRATION = importlib.import_module("randirandi.core.migrations.0002_capacity")

# The columns the app's user may change, for each table this part adds to,
# all of them from here on. 0002's lists hold for the other tables.
APP_UPDATES = {
    "core_space": ("name", "description", "booking_page"),
    "core_assetinventory": (
        "space_id",
        "capacity",
        "unit",
        "asset_id",
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
    "core_spotgroup": ("name", "word"),
    "core_spot": ("name", "group_id"),
}

# Each list names at most this many rows; the count is always whole.
NAMED = 10


def _rows(cursor: Any, query: str) -> list[tuple[Any, ...]]:
    cursor.execute(query)
    return list(cursor.fetchall())


def _starts_on(row: dict[str, Any], day: date) -> bool:
    """Whether a date period of the container starts on the day, as
    `BookingConfiguration.starts_on` said it when this was written."""
    if day.weekday() not in row["period_start_weekdays"]:
        return False
    start, stop = row["start_date"], row["stop_date"]
    if start is not None and day < start:
        return False
    if stop is not None and stop < day:
        return False
    return start is None or (
        (day.toordinal() - start.toordinal()) // 7 % row["every_weeks"] == 0
    )


def _shares_time(row: dict[str, Any], begins: datetime, ends: datetime) -> bool:
    """Whether a date period of the container shares time with the wall
    moments from `begins` to `ends`."""
    first = begins.date() - timedelta(days=row["end_day"] + 1)
    for ordinal in range(first.toordinal(), ends.date().toordinal() + 1):
        day = date.fromordinal(ordinal)
        if not _starts_on(row, day):
            continue
        midnight = datetime.combine(day, time())
        start = midnight + timedelta(minutes=row["period_start"])
        end = midnight + timedelta(minutes=row["end_day"] * 24 * 60 + row["period_end"])
        if start < ends and begins < end:
            return True
    return False


def _block_containers(cursor: Any) -> dict[int, list[int]]:
    """For each block, the containers on its asset whose time it is on."""
    columns = (
        "id",
        "asset_id",
        "period_start",
        "period_end",
        "end_day",
        "period_start_weekdays",
        "start_date",
        "stop_date",
        "every_weeks",
    )
    containers: dict[int, list[dict[str, Any]]] = {}
    for values in _rows(
        cursor,
        "SELECT id, asset_id, period_start, period_end, end_day, "
        "period_start_weekdays, start_date, stop_date, every_weeks "
        "FROM core_bookingconfiguration ORDER BY id",
    ):
        row = dict(zip(columns, values, strict=True))
        containers.setdefault(row["asset_id"], []).append(row)
    found: dict[int, list[int]] = {}
    for block, asset, starts_at, ends_at, zone in _rows(
        cursor,
        """SELECT b.id, t.asset_id, t.starts_at, t.ends_at, o.time_zone
        FROM core_block b JOIN core_takentime t ON t.id = b.taken_time_id
        JOIN core_organization o ON o.id = b.organization_id ORDER BY b.id""",
    ):
        clock = ZoneInfo(zone)
        begins = starts_at.astimezone(clock).replace(tzinfo=None)
        ends = ends_at.astimezone(clock).replace(tzinfo=None)
        found[block] = [
            row["id"]
            for row in containers.get(asset, [])
            if _shares_time(row, begins, ends)
        ]
    return found


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
            "inventories with several assets": _rows(
                cursor,
                "SELECT 'organization ' || organization_id || ', inventory ' || "
                "inventory_id FROM core_asset GROUP BY organization_id, "
                "inventory_id HAVING count(*) > 1 ORDER BY 1",
            ),
            "bookings without a booking line": _rows(
                cursor,
                "SELECT 'booking ' || b.id FROM core_booking b WHERE NOT EXISTS "
                "(SELECT 1 FROM core_bookingline l WHERE l.booking_id = b.id) "
                "ORDER BY b.id",
            ),
            "bookings whose lines name two or more containers": _rows(
                cursor,
                "SELECT 'booking ' || booking_id FROM core_bookingline GROUP BY "
                "booking_id HAVING count(DISTINCT booking_configuration_id) > 1 "
                "ORDER BY booking_id",
            ),
            "bookings whose line names a container on another asset": _rows(
                cursor,
                "SELECT DISTINCT 'booking ' || b.id FROM core_booking b "
                "JOIN core_takentime t ON t.id = b.taken_time_id "
                "JOIN core_bookingline l ON l.booking_id = b.id "
                "JOIN core_bookingconfiguration c ON c.id = "
                "l.booking_configuration_id WHERE c.asset_id <> t.asset_id ORDER BY 1",
            ),
        }
        blocks = _block_containers(cursor)
        faults["blocks no container covers"] = [
            (f"block {block}",) for block, found in blocks.items() if not found
        ]
        faults["blocks over several containers"] = [
            (f"block {block}",) for block, found in blocks.items() if len(found) > 1
        ]
    listed = [
        f"{kind}: {len(rows)} ("
        + "; ".join(name for (name,) in rows[:NAMED])
        + ("; …" if len(rows) > NAMED else "")
        + ")"
        for kind, rows in faults.items()
        if rows
    ]
    if listed:
        raise RuntimeError(
            "The floor plans migration (M6 part 2.5f1) changed nothing: these "
            "rows do not map to floor plans and containers.\n" + "\n".join(listed)
        )


def fill(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        blocks = _block_containers(cursor)
        counts = {
            "floor plans": _rows(cursor, "SELECT count(*) FROM core_assetinventory"),
            "of them default floor plans, with one spot": _rows(
                cursor,
                "SELECT count(*) FROM core_assetinventory i WHERE capacity = 1 "
                "AND unit = 'booking' AND NOT EXISTS (SELECT 1 FROM "
                "core_bookingconfiguration c JOIN core_asset a ON a.id = c.asset_id "
                "WHERE a.inventory_id = i.id AND c.consumption_mode = 'shared')",
            ),
            "of them stock no asset is on": _rows(
                cursor,
                "SELECT count(*) FROM core_assetinventory i WHERE NOT EXISTS "
                "(SELECT 1 FROM core_asset a WHERE a.inventory_id = i.id)",
            ),
            "spots": _rows(
                cursor, "SELECT coalesce(sum(capacity), 0) FROM core_assetinventory"
            ),
            "assets in a space": _rows(
                cursor,
                "SELECT count(*) FROM core_asset a JOIN core_assetinventory i "
                "ON i.id = a.inventory_id WHERE i.space_id IS NOT NULL",
            ),
            "of them covering their whole space": _rows(
                cursor, "SELECT count(*) FROM core_asset WHERE whole_space"
            ),
            "shared containers given their floor plan": _rows(
                cursor,
                "SELECT count(*) FROM core_bookingconfiguration "
                "WHERE consumption_mode = 'shared'",
            ),
            "bookings given their container": _rows(
                cursor, "SELECT count(*) FROM core_booking"
            ),
        }
        cursor.execute("""
            UPDATE core_assetinventory i SET asset_id = a.id
            FROM core_asset a WHERE a.inventory_id = i.id;
            INSERT INTO core_spot (organization_id, floor_plan_id, number, place,
                floor_plan_capacity, name)
            SELECT organization_id, id, number, number, capacity, ''
            FROM core_assetinventory, generate_series(1, capacity) number;
            UPDATE core_assetinventory SET last_spot_number = capacity;
            UPDATE core_asset a SET space_id = i.space_id
            FROM core_assetinventory i WHERE i.id = a.inventory_id;
            UPDATE core_bookingconfiguration c SET floor_plan_id = a.inventory_id
            FROM core_asset a WHERE a.id = c.asset_id
            AND c.consumption_mode = 'shared';
            UPDATE core_booking b SET booking_configuration_id =
                l.booking_configuration_id
            FROM core_bookingline l WHERE l.booking_id = b.id;
        """)
        for block, (container,) in blocks.items():
            cursor.execute(
                "UPDATE core_block SET booking_configuration_id = %s WHERE id = %s",
                [container, block],
            )
    for name, ((count,),) in counts.items():
        logger.info("floor plans migration: %s: %d", name, count)
    logger.info("floor plans migration: blocks given their container: %d", len(blocks))


def refuse_a_lossy_reverse(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        lost = {
            "groups of spots": "SELECT count(*) FROM core_spotgroup",
            "spots with a name": "SELECT count(*) FROM core_spot WHERE name <> ''",
            "floor plans with a name": (
                "SELECT count(*) FROM core_assetinventory WHERE name <> ''"
            ),
            "floor plans whose spots are not numbered 1 to their capacity": (
                "SELECT count(*) FROM core_assetinventory i WHERE "
                "last_spot_number <> capacity OR EXISTS (SELECT 1 FROM core_spot s "
                "WHERE s.floor_plan_id = i.id AND s.number > i.capacity)"
            ),
            "assets with a maximum occupancy": (
                "SELECT count(*) FROM core_asset WHERE maximum_occupancy IS NOT NULL"
            ),
            "assets without an inventory": (
                "SELECT count(*) FROM core_asset WHERE inventory_id IS NULL"
            ),
            "spaces whose booking page shows the whole space": (
                "SELECT count(*) FROM core_space WHERE booking_page <> 'one_asset'"
            ),
            "audit events on a spot or a group": (
                "SELECT count(*) FROM core_auditevent "
                "WHERE object_kind IN ('spot', 'spot_group')"
            ),
        }
        listed = []
        for kind, query in lost.items():
            ((count,),) = _rows(cursor, query)
            if count:
                listed.append(f"{kind}: {count}")
    if listed:
        raise RuntimeError(
            "Cannot reverse the floor plans migration while they hold what an "
            "inventory cannot:\n" + "\n".join(listed)
        )


def install_permissions(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='randirandi_app'")
        if cursor.fetchone() is None:
            # Unit-test databases without a deployed role have no app login.
            return
        for table, columns in APP_UPDATES.items():
            schema_editor.execute(f"REVOKE UPDATE ON {table} FROM randirandi_app")
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


def restore_old_permissions(apps: Any, schema_editor: Any) -> None:
    CAPACITY_MIGRATION.install_permissions(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("core", "0015_calendar_week_rules")]

    operations = [
        migrations.RunPython(count_first, migrations.RunPython.noop),
        migrations.CreateModel(
            name="SpotGroup",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("number", models.PositiveIntegerField(editable=False)),
                ("name", models.CharField(max_length=200)),
                ("word", models.CharField(default="group", max_length=80)),
                (
                    "floor_plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="groups",
                        to="core.assetinventory",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.organization",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="spotgroup_org_id"
                    ),
                    randirandi.core.models.base.SameOrganization(
                        link="floor_plan", name="spotgroup_floor_plan_same_org"
                    ),
                    models.UniqueConstraint(
                        fields=("floor_plan", "number"), name="spotgroup_number_once"
                    ),
                    models.UniqueConstraint(
                        fields=("floor_plan", "name"), name="spotgroup_name_once"
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "floor_plan", "id"),
                        name="spotgroup_floor_plan_key",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("number__gte", 1),
                            models.Q(("name", ""), _negated=True),
                            models.Q(("word", ""), _negated=True),
                        ),
                        name="spotgroup_numbered_and_named",
                    ),
                ],
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="Spot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("number", models.PositiveIntegerField(editable=False)),
                ("name", models.CharField(blank=True, max_length=200)),
                (
                    "floor_plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="spots",
                        to="core.assetinventory",
                    ),
                ),
                (
                    "group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="spots",
                        to="core.spotgroup",
                    ),
                ),
                ("place", models.PositiveIntegerField(editable=False)),
                (
                    "previous_place",
                    models.GeneratedField(
                        db_persist=True,
                        expression=django.db.models.functions.comparison.Greatest(
                            models.Value(1), models.F("place") - 1
                        ),
                        output_field=models.IntegerField(),
                    ),
                ),
                ("floor_plan_capacity", models.PositiveIntegerField(editable=False)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.organization",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="spot_org_id"
                    ),
                    randirandi.core.models.base.SameOrganization(
                        link="floor_plan", name="spot_floor_plan_same_org"
                    ),
                    randirandi.core.models.base.SameOrganization(
                        link="group", name="spot_group_same_org"
                    ),
                    models.UniqueConstraint(
                        fields=("floor_plan", "number"), name="spot_number_once"
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("name", ""), _negated=True),
                        fields=("floor_plan", "name"),
                        name="spot_name_once",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("number__gte", 1)),
                        name="spot_numbered_from_one",
                    ),
                    randirandi.core.models.base.DatabaseRule(
                        definition=(
                            "FOREIGN KEY (organization_id, floor_plan_id, group_id) "
                            "REFERENCES core_spotgroup "
                            "(organization_id, floor_plan_id, id)"
                        ),
                        name="spot_group_on_its_floor_plan",
                    ),
                ],
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        Together(
            migrations.AddField(
                model_name="asset",
                name="maximum_occupancy",
                field=models.PositiveIntegerField(blank=True, null=True),
            ),
            migrations.AddField(
                model_name="asset",
                name="space",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="assets",
                    to="core.space",
                ),
            ),
            migrations.AddField(
                model_name="assetinventory",
                name="asset",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="floor_plans",
                    to="core.asset",
                ),
            ),
            migrations.AddField(
                model_name="assetinventory",
                name="last_group_number",
                field=models.PositiveIntegerField(default=0, editable=False),
            ),
            migrations.AddField(
                model_name="assetinventory",
                name="last_spot_number",
                field=models.PositiveIntegerField(default=0, editable=False),
            ),
            migrations.AddField(
                model_name="assetinventory",
                name="name",
                field=models.CharField(blank=True, max_length=200),
            ),
            migrations.AddField(
                model_name="block",
                name="booking_configuration",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="blocks",
                    to="core.bookingconfiguration",
                ),
            ),
            migrations.AddField(
                model_name="booking",
                name="booking_configuration",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="bookings",
                    to="core.bookingconfiguration",
                ),
            ),
            migrations.AddField(
                model_name="bookingconfiguration",
                name="floor_plan",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="booking_configurations",
                    to="core.assetinventory",
                ),
            ),
            migrations.AddField(
                model_name="space",
                name="booking_page",
                field=models.CharField(
                    choices=[
                        ("one_asset", "One asset at a time"),
                        ("whole_space", "The whole space"),
                    ],
                    default="one_asset",
                    max_length=11,
                ),
            ),
            migrations.AlterField(
                model_name="asset",
                name="inventory",
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="assets",
                    to="core.assetinventory",
                ),
            ),
            migrations.AddField(
                model_name="asset",
                name="space_scope",
                field=models.GeneratedField(
                    db_persist=True,
                    expression=django.db.models.functions.comparison.Coalesce(
                        models.F("space_id"), -models.F("inventory_id")
                    ),
                    output_field=models.BigIntegerField(),
                ),
            ),
        ),
        migrations.RunPython(fill, refuse_a_lossy_reverse),
        # The filled rows are checked now, so the tables can take new rules.
        migrations.RunSQL(sql="SET CONSTRAINTS ALL IMMEDIATE", reverse_sql=""),
        Together(
            migrations.AddConstraint(
                model_name="asset",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="space", name="asset_space_same_org"
                ),
            ),
            migrations.AddConstraint(
                model_name="asset",
                constraint=models.UniqueConstraint(
                    fields=("organization", "id", "inventory"),
                    name="asset_inventory_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="asset",
                constraint=models.CheckConstraint(
                    condition=models.Q(
                        ("whole_space", False),
                        ("space__isnull", False),
                        _connector="OR",
                    ),
                    name="asset_mark_needs_a_space",
                ),
            ),
            migrations.AddConstraint(
                model_name="asset",
                constraint=models.CheckConstraint(
                    condition=models.Q(
                        ("maximum_occupancy__isnull", True),
                        ("maximum_occupancy__gte", 1),
                        _connector="OR",
                    ),
                    name="asset_occupancy_above_zero",
                ),
            ),
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="asset", name="assetinventory_asset_same_org"
                ),
            ),
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=models.UniqueConstraint(
                    fields=("asset",), name="floor_plan_one_per_asset"
                ),
            ),
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, asset_id, id) REFERENCES "
                        "core_asset (organization_id, id, inventory_id)"
                    ),
                    name="floor_plan_is_its_assets_inventory",
                ),
            ),
            migrations.RemoveConstraint(
                model_name="auditevent",
                name="audit_fixed_kind",
            ),
            migrations.AddConstraint(
                model_name="auditevent",
                constraint=models.CheckConstraint(
                    condition=models.Q(
                        (
                            "object_kind__in",
                            (
                                "organization",
                                "space",
                                "asset_inventory",
                                "spot_group",
                                "spot",
                                "asset",
                                "availability_calendar",
                                "available_span",
                                "booking_type",
                                "booking_category",
                                "booking_configuration",
                                "configuration_category",
                                "price_book",
                                "price_book_entry",
                                "price_tier",
                                "role",
                                "role_category",
                                "membership",
                                "member",
                                "member_membership",
                                "member_source",
                                "member_import",
                                "access_event",
                                "login_code",
                                "login_attempt",
                                "login_request",
                                "authenticator",
                                "security_notice",
                                "order",
                                "taken_time",
                                "capacity_allocation",
                                "booking",
                                "booking_line",
                                "payment",
                                "refund",
                                "block",
                                "payment_provider_account",
                                "audit_event",
                            ),
                        )
                    ),
                    name="audit_fixed_kind",
                ),
            ),
            migrations.AddConstraint(
                model_name="block",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="booking_configuration",
                    name="block_booking_configuration_same_org",
                ),
            ),
            migrations.AddConstraint(
                model_name="booking",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="booking_configuration",
                    name="booking_booking_configuration_same_org",
                ),
            ),
            migrations.AddConstraint(
                model_name="bookingconfiguration",
                constraint=randirandi.core.models.base.SameOrganization(
                    link="floor_plan", name="bookingconfiguration_floor_plan_same_org"
                ),
            ),
            migrations.AddConstraint(
                model_name="bookingconfiguration",
                constraint=models.CheckConstraint(
                    condition=models.Q(
                        models.Q(
                            ("consumption_mode", "shared"),
                            ("floor_plan__isnull", False),
                        ),
                        models.Q(
                            ("consumption_mode", "exclusive"),
                            ("floor_plan__isnull", True),
                        ),
                        _connector="OR",
                    ),
                    name="configuration_floor_plan_when_shared",
                ),
            ),
            migrations.AddConstraint(
                model_name="bookingconfiguration",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, asset_id, floor_plan_id) "
                        "REFERENCES core_asset (organization_id, id, inventory_id)"
                    ),
                    name="configuration_floor_plan_of_its_asset",
                ),
            ),
            migrations.AddConstraint(
                model_name="space",
                constraint=models.CheckConstraint(
                    condition=models.Q(
                        ("booking_page__in", ["one_asset", "whole_space"])
                    ),
                    name="space_booking_page_known",
                ),
            ),
            # The floor plan's capacity is its number of spots, and an asset's
            # space its floor plan's, whatever writes them. Native rules only:
            # no trigger, nothing on an extension.
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=models.UniqueConstraint(
                    fields=("organization", "id", "capacity"),
                    name="floor_plan_capacity_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=models.UniqueConstraint(
                    fields=("organization", "id", "scope_key"),
                    name="floor_plan_space_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="spot",
                constraint=models.UniqueConstraint(
                    fields=("organization", "floor_plan", "place"),
                    name="spot_place_key",
                ),
            ),
            migrations.AddConstraint(
                model_name="spot",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition="CHECK (place >= 1 AND place <= floor_plan_capacity)",
                    name="spot_place_within_capacity",
                ),
            ),
            migrations.AddConstraint(
                model_name="spot",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, floor_plan_id, "
                        "floor_plan_capacity) REFERENCES core_assetinventory "
                        "(organization_id, id, capacity) ON UPDATE CASCADE"
                    ),
                    name="spot_capacity_bound",
                ),
            ),
            migrations.AddConstraint(
                model_name="spot",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, floor_plan_id, previous_place) "
                        "REFERENCES core_spot (organization_id, floor_plan_id, place) "
                        "DEFERRABLE INITIALLY DEFERRED"
                    ),
                    name="spot_previous_place",
                ),
            ),
            migrations.AddConstraint(
                model_name="assetinventory",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, id, capacity) REFERENCES "
                        "core_spot (organization_id, floor_plan_id, place) "
                        "DEFERRABLE INITIALLY DEFERRED"
                    ),
                    name="floor_plan_counts_its_spots",
                ),
            ),
            migrations.AddConstraint(
                model_name="asset",
                constraint=randirandi.core.models.base.DatabaseRule(
                    definition=(
                        "FOREIGN KEY (organization_id, inventory_id, space_scope) "
                        "REFERENCES core_assetinventory (organization_id, id, "
                        "scope_key) DEFERRABLE INITIALLY DEFERRED"
                    ),
                    name="asset_space_is_its_floor_plans",
                ),
            ),
        ),
        migrations.RunPython(install_permissions, restore_old_permissions),
    ]
