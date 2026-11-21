# SPDX-License-Identifier: AGPL-3.0-only
# a booking configuration's dates become calendar weeks, and
# it may start in every Nth week from the first.
# A start date moves back to its Monday and a stop date forward to its
# Sunday. That adds days, and is done only when none of them is a weekday a
# period starts on, so that no period is added. Otherwise nothing moves:
# the migration stops and lists the configurations, for a person to decide.
# Reversing keeps the whole weeks and drops `every_weeks`, which it refuses
# while a configuration starts in other than every week.

from datetime import timedelta
from typing import Any

from django.db import migrations, models

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def move_to_whole_weeks(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    configurations = apps.get_model("core", "BookingConfiguration")
    rows = configurations._default_manager.using(alias).exclude(
        start_date=None, stop_date=None
    )
    moves, refused = [], []
    for row in rows.order_by("organization_id", "number"):
        start, stop = row.start_date, row.stop_date
        added = []
        if start is not None and start.weekday() != 0:
            added += [
                start - timedelta(days=back) for back in range(1, start.weekday() + 1)
            ]
            start -= timedelta(days=start.weekday())
        if stop is not None and stop.weekday() != 6:
            added += [stop + timedelta(days=on) for on in range(1, 7 - stop.weekday())]
            stop += timedelta(days=6 - stop.weekday())
        if not added:
            continue
        starts = sorted(
            {day.weekday() for day in added} & set(row.period_start_weekdays)
        )
        if starts:
            refused.append(
                f"organization {row.organization_id}, configuration "
                f"{row.number:03d}: whole weeks would add a start on "
                + ", ".join(DAYS[day] for day in starts)
            )
        else:
            moves.append((row.pk, start, stop))
    if refused:
        raise RuntimeError(
            "These booking configurations cannot move to whole calendar weeks "
            "without adding periods. Change their dates first:\n" + "\n".join(refused)
        )
    for pk, start, stop in moves:
        rows.filter(pk=pk).update(start_date=start, stop_date=stop)


def refuse_cycles_on_reverse(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    configurations = apps.get_model("core", "BookingConfiguration")
    if configurations._default_manager.using(alias).exclude(every_weeks=1).exists():
        raise RuntimeError(
            "Cannot reverse while a configuration starts in other than every week."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0013_booking_cart_row")]

    operations = [
        migrations.AddField(
            model_name="bookingconfiguration",
            name="every_weeks",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RunPython(move_to_whole_weeks, refuse_cycles_on_reverse),
    ]
