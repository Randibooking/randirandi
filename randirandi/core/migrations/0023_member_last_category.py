# SPDX-License-Identifier: AGPL-3.0-only
# the booking category a member last chose in the Filter.
# One new column, empty: a member who has not chosen yet starts on the first
# of their categories, so no member changes.

import django.db.models.deletion
from django.db import migrations, models

import randirandi.core.models.base


class Migration(migrations.Migration):
    dependencies = [("core", "0022_category_hours")]

    operations = [
        migrations.AddField(
            model_name="member",
            name="last_booking_category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="core.bookingcategory",
            ),
        ),
        migrations.AddConstraint(
            model_name="member",
            constraint=randirandi.core.models.base.SameOrganization(
                link="last_booking_category",
                name="member_last_booking_category_same_org",
            ),
        ),
    ]
