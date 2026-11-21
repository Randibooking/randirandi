# SPDX-License-Identifier: AGPL-3.0-only
# a booking reference people can read, not the order's count.
# Every order gets six random characters from an alphabet without 0/O and
# 1/I/L, unique in its organization . The number stays the internal count. Orders have
# no column
# privilege contract, so no grant changes. Reversing drops the references; a
# reference already sent in an email is then gone, and a later forward draws
# new ones.

import secrets
from typing import Any

from django.db import migrations, models
from django.db.models import Q

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
PATTERN = r"^[23-9A-HJKMNP-Z]{6}$"


def give_every_order_one(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    orders = apps.get_model("core", "Order")._default_manager.using(alias)
    taken: set[tuple[int, str]] = set()
    for pk, organization in orders.order_by("pk").values_list("pk", "organization_id"):
        while True:
            code = "".join(secrets.choice(ALPHABET) for _ in range(6))
            if (organization, code) not in taken:
                break
        taken.add((organization, code))
        orders.filter(pk=pk).update(reference=code)


class Migration(migrations.Migration):
    dependencies = [("core", "0033_member_last_login_at")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="reference",
            field=models.CharField(default="", editable=False, max_length=6),
            preserve_default=False,
        ),
        migrations.RunPython(give_every_order_one, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                fields=("organization", "reference"), name="order_reference_once"
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=Q(reference__regex=PATTERN), name="order_reference_is_a_code"
            ),
        ),
    ]
