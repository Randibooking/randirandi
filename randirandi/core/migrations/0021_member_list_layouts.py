# SPDX-License-Identifier: AGPL-3.0-only
# a member's full name, a membership's address unit, section,
# postal code, city and country, and the layout a member source is read with.
# The full name of an existing member is made from the first and last name.
# Reversing is refused while any of the new values holds something the old
# columns cannot take back.

from typing import Any

from django.db import migrations, models
from django.db.models import Value
from django.db.models.functions import Concat, Trim


def _made() -> Any:
    return Trim(Concat("first_name", Value(" "), "last_name"))


def fill_full_names(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    members = apps.get_model("core", "Member")._default_manager.using(alias)
    members.update(full_name=_made())


def refuse_a_reverse_that_loses_values(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    members = apps.get_model("core", "Member")._default_manager.using(alias)
    memberships = apps.get_model("core", "Membership")._default_manager.using(alias)
    sources = apps.get_model("core", "MemberSource")._default_manager.using(alias)
    if (
        # A blank full name holds nothing the old columns would lose.
        members.exclude(full_name="").exclude(full_name=_made()).exists()
        or memberships.exclude(
            address_unit="", section="", postal_code="", city="", country=""
        ).exists()
        or sources.exclude(layout={}).exists()
    ):
        raise RuntimeError(
            "Cannot reverse while a full name differs from first and last "
            "name, or an address unit, section, postal code, city, country "
            "or a list layout is set."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0020_payment_create_phone")]

    operations = [
        migrations.AddField(
            model_name="member",
            name="full_name",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="membership",
            name="address_unit",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="membership",
            name="section",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="membership",
            name="postal_code",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="membership",
            name="city",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="membership",
            name="country",
            field=models.CharField(blank=True, max_length=2),
        ),
        migrations.AddField(
            model_name="membersource",
            name="layout",
            field=models.JSONField(default=dict),
        ),
        # Last, so that reversing checks before any column is removed.
        migrations.RunPython(fill_full_names, refuse_a_reverse_that_loses_values),
    ]
