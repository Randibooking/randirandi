# SPDX-License-Identifier: AGPL-3.0-only
# the database refuses dates that are not whole calendar weeks,
# and every N weeks without a first week or above 52. In a migration of its own, after
# 0014 has moved the stored dates.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0014_calendar_weeks")]

    operations = [
        migrations.AddConstraint(
            model_name="bookingconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(start_date__isnull=True)
                | models.Q(start_date__iso_week_day=1),
                name="bookingconfiguration_starts_on_a_monday",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(stop_date__isnull=True)
                | models.Q(stop_date__iso_week_day=7),
                name="bookingconfiguration_stops_on_a_sunday",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(every_weeks__gte=1, every_weeks__lte=52)
                & (models.Q(every_weeks=1) | models.Q(start_date__isnull=False)),
                name="bookingconfiguration_counts_weeks_from_the_first",
            ),
        ),
    ]
