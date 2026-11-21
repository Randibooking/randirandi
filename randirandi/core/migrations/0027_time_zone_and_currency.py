# SPDX-License-Identifier: AGPL-3.0-only
# a currency on the price book, a time zone on the asset.
# Every price book gets its organization's currency and every asset its
# organization's time zone, so nothing a member sees changes. Every entry
# keeps a copy of its book's currency and every claim (booking or block) of
# its asset's time zone, by a key the database checks: a book with an entry
# and an asset with a claim cannot change theirs. The currencies are the
# ones the payment client takes, and a payment no longer defaults to NOK
# (its rows keep their values).
# Reversing is refused, naming the rows, while any price book or asset has
# a currency or a time zone that is not its organization's: the old schema
# has only the organization's, and reversing would read those rows in
# another one.

import importlib
from typing import Any

from django.db import migrations, models
from django.db.models import F

import randirandi.core.models.base

RULE_MIGRATION = importlib.import_module("randirandi.core.migrations.0017_rule_by_spot")

CURRENCIES = [("NOK", "NOK"), ("DKK", "DKK"), ("EUR", "EUR")]

# The columns the app's user may change on the asset, one more than 0017's.
APP_UPDATES: dict[str, tuple[str, ...]] = {
    "core_asset": (*RULE_MIGRATION.APP_UPDATES["core_asset"], "time_zone"),
}

# Each list names at most this many rows; the count is always whole.
NAMED = 10


def backfill(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute("""
        UPDATE core_asset a SET time_zone = o.time_zone
        FROM core_organization o WHERE o.id = a.organization_id;
        UPDATE core_pricebook b SET currency = o.currency
        FROM core_organization o WHERE o.id = b.organization_id;
        UPDATE core_pricebookentry e SET currency = b.currency
        FROM core_pricebook b WHERE b.id = e.price_book_id;
        UPDATE core_takentime t SET time_zone = a.time_zone
        FROM core_asset a WHERE a.id = t.asset_id;
    """)


def install_permissions(apps: Any, schema_editor: Any) -> None:
    RULE_MIGRATION.grant(schema_editor, APP_UPDATES)


def restore_old_permissions(apps: Any, schema_editor: Any) -> None:
    RULE_MIGRATION.grant(
        schema_editor, {"core_asset": RULE_MIGRATION.APP_UPDATES["core_asset"]}
    )


def refuse_a_reverse_that_loses_a_choice(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    books = (
        apps.get_model("core", "PriceBook")
        ._default_manager.using(alias)
        .exclude(currency=F("organization__currency"))
    )
    assets = (
        apps.get_model("core", "Asset")
        ._default_manager.using(alias)
        .exclude(time_zone=F("organization__time_zone"))
    )
    listed = []
    for kind, rows, field in (
        ("price books", books, "currency"),
        ("assets", assets, "time_zone"),
    ):
        count = rows.count()
        if count:
            named = [
                f"{row.organization.subdomain}: {row.name} ({getattr(row, field)})"
                for row in rows.select_related("organization").order_by(
                    "organization_id", "number"
                )[:NAMED]
            ]
            listed.append(f"{kind} with their own {field}: {count}, {'; '.join(named)}")
    if listed:
        raise RuntimeError(
            "Cannot reverse while a price book has a currency or an asset a "
            "time zone other than its organization's: change them back first.\n"
            + "\n".join(listed)
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0026_booking_emails")]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="time_zone",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="pricebook",
            name="currency",
            field=models.CharField(blank=True, choices=CURRENCIES, max_length=3),
        ),
        migrations.AddField(
            model_name="pricebookentry",
            name="currency",
            field=models.CharField(default="", editable=False, max_length=3),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="takentime",
            name="time_zone",
            field=models.CharField(default="", editable=False, max_length=64),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="organization",
            name="currency",
            field=models.CharField(choices=CURRENCIES, max_length=3),
        ),
        migrations.AlterField(
            model_name="payment",
            name="currency",
            field=models.CharField(choices=CURRENCIES, max_length=3),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                condition=models.Q(("time_zone", ""), _negated=True),
                name="asset_has_a_time_zone",
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.UniqueConstraint(
                fields=("organization", "id", "time_zone"), name="asset_time_zone_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("currency", "NOK"),
                    ("currency", "DKK"),
                    ("currency", "EUR"),
                    _connector="OR",
                ),
                name="payment_currency_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricebook",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("currency", "NOK"),
                    ("currency", "DKK"),
                    ("currency", "EUR"),
                    _connector="OR",
                ),
                name="pricebook_currency_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricebook",
            constraint=models.UniqueConstraint(
                fields=("organization", "id", "currency"),
                name="pricebook_currency_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricebookentry",
            constraint=randirandi.core.models.base.DatabaseRule(
                definition=(
                    "FOREIGN KEY (organization_id, price_book_id, currency) "
                    "REFERENCES core_pricebook (organization_id, id, currency)"
                ),
                name="pricebookentry_book_currency",
            ),
        ),
        migrations.AddConstraint(
            model_name="takentime",
            constraint=randirandi.core.models.base.DatabaseRule(
                definition=(
                    "FOREIGN KEY (organization_id, asset_id, time_zone) "
                    "REFERENCES core_asset (organization_id, id, time_zone)"
                ),
                name="takentime_asset_time_zone",
            ),
        ),
        migrations.RunPython(install_permissions, restore_old_permissions),
        # Last, so that reversing checks before anything is dropped.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_a_choice
        ),
    ]
