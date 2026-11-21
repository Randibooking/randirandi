# SPDX-License-Identifier: AGPL-3.0-only
# payouts and the Vipps fee. A payment gains the fee and the payout
# the report showed for it, a refund the payout it was deducted from, and an
# account the moments of its daily report read; three tables are new: the
# ledger, its entries (the application's user cannot UPDATE an entry) and the
# payouts. The audit vocabulary gains "listed payouts".
# Reversing is refused while any ledger, entry, payout, fee or payout link
# exists, or an event uses the new action: the reverse would drop the record of
# money. A reverse with none of them is clean, and the graph's leaf nodes are
# the caller's to restore.

import importlib
from typing import Any

import django.db.models.deletion
import django.db.models.expressions
import django.db.models.manager
from django.db import migrations, models

import randirandi.core.models.base

OLD_ACTIONS = importlib.import_module(
    "randirandi.core.migrations.0049_organization_audit"
).NEW_ACTIONS
NEW_ONLY = [("listed payouts", "Listed Payouts")]
NEW_ACTIONS = [*OLD_ACTIONS, *NEW_ONLY]


def _app_exists(cursor: Any) -> bool:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='randirandi_app'")
    return cursor.fetchone() is not None


def no_update_on_entries(apps: Any, schema_editor: Any) -> None:
    """A ledger entry is immutable: the application's user may insert, read and
    delete it, never update it. Unit-test databases without the login skip it."""
    with schema_editor.connection.cursor() as cursor:
        if not _app_exists(cursor):
            return
        schema_editor.execute("REVOKE UPDATE ON core_ledgerentry FROM randirandi_app")
        cursor.execute(
            """SELECT a.attname,
            has_column_privilege('randirandi_app',c.oid,a.attname,'UPDATE')
            FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
            WHERE c.oid='core_ledgerentry'::regclass AND a.attnum>0
            AND NOT a.attisdropped"""
        )
        if any(allowed for _, allowed in cursor.fetchall()):
            raise RuntimeError("Unexpected effective ledger entry UPDATE permission")


def refuse_a_reverse_that_loses_money_records(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias

    def count(model: str, **where: Any) -> int:
        found: int = (
            apps.get_model("core", model)
            ._default_manager.using(alias)
            .filter(**where)
            .count()
        )
        return found

    held = {
        "ledger": count("PaymentLedger"),
        "ledger entry": count("LedgerEntry"),
        "payout": count("Payout"),
        "payment fee": count("Payment", fee_amount__isnull=False),
        "payment payout link": count("Payment", payout__isnull=False),
        "refund payout link": count("Refund", payout__isnull=False),
        "refund reported moment": count("Refund", reported_at__isnull=False),
        "account report moment": count(
            "PaymentProviderAccount", report_attempted_at__isnull=False
        )
        + count("PaymentProviderAccount", report_completed_at__isnull=False),
        "payout audit event": count(
            "AuditEvent", action__in=[action for action, _ in NEW_ONLY]
        ),
    }
    held = {name: number for name, number in held.items() if number}
    if held:
        raise RuntimeError(
            "Cannot reverse while the payout records exist (retained payout "
            f"records): {held}."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0050_payment_trouble"),
    ]

    operations = [
        migrations.CreateModel(
            name="LedgerEntry",
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
                (
                    "topic",
                    models.CharField(
                        choices=[("funds", "Funds"), ("fees", "Fees")], max_length=5
                    ),
                ),
                ("entry_type", models.CharField(max_length=64)),
                ("psp_reference", models.CharField(max_length=100)),
                ("reference", models.CharField(blank=True, max_length=200)),
                ("ledger_date", models.DateField()),
                ("at", models.DateTimeField()),
                ("amount", models.BigIntegerField()),
                ("balance_before", models.BigIntegerField()),
                ("balance_after", models.BigIntegerField()),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="PaymentLedger",
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
                ("ledger_id", models.CharField(max_length=64)),
                (
                    "currency",
                    models.CharField(
                        choices=[("NOK", "NOK"), ("DKK", "DKK"), ("EUR", "EUR")],
                        max_length=3,
                    ),
                ),
                ("funds_through", models.DateField(blank=True, null=True)),
                ("fees_through", models.DateField(blank=True, null=True)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="Payout",
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
                ("psp_reference", models.CharField(max_length=100)),
                ("ledger_date", models.DateField()),
                ("scheduled_at", models.DateTimeField()),
                ("aborted_at", models.DateTimeField(null=True)),
                (
                    "currency",
                    models.CharField(
                        choices=[("NOK", "NOK"), ("DKK", "DKK"), ("EUR", "EUR")],
                        max_length=3,
                    ),
                ),
                ("amount", models.BigIntegerField()),
                ("statement_text", models.CharField(blank=True, max_length=200)),
                ("captured_total", models.BigIntegerField()),
                ("refunded_total", models.BigIntegerField()),
                ("fee_total", models.BigIntegerField()),
                ("other_total", models.BigIntegerField()),
                ("captured_count", models.PositiveIntegerField(default=0)),
                ("refunded_count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "abstract": False,
                "default_manager_name": "across_organizations",
            },
            managers=[
                ("across_organizations", django.db.models.manager.Manager()),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="auditevent",
            name="audit_fixed_action",
        ),
        migrations.AddField(
            model_name="payment",
            name="fee_amount",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paymentprovideraccount",
            name="report_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paymentprovideraccount",
            name="report_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=NEW_ACTIONS, max_length=100),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", [a for a, _ in NEW_ACTIONS])),
                name="audit_fixed_action",
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="paymentledger",
            name="account",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.paymentprovideraccount",
            ),
        ),
        migrations.AddField(
            model_name="paymentledger",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="ledger",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="entries",
                to="core.paymentledger",
            ),
        ),
        migrations.AddField(
            model_name="payout",
            name="ledger",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payouts",
                to="core.paymentledger",
            ),
        ),
        migrations.AddField(
            model_name="payout",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="payout",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="core.payout",
            ),
        ),
        migrations.AddField(
            model_name="refund",
            name="reported_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="refund",
            name="payout",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="refunds",
                to="core.payout",
            ),
        ),
        migrations.AddConstraint(
            model_name="paymentledger",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="paymentledger_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="paymentledger",
            constraint=randirandi.core.models.base.SameOrganization(
                link="account", name="paymentledger_account_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="paymentledger",
            constraint=models.UniqueConstraint(
                fields=("organization", "ledger_id"), name="paymentledger_once"
            ),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(
                fields=["ledger", "topic", "ledger_date"], name="ledgerentry_day"
            ),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(
                fields=["organization", "reference"], name="ledgerentry_ref"
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="ledgerentry_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=randirandi.core.models.base.SameOrganization(
                link="ledger", name="ledgerentry_ledger_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.UniqueConstraint(
                fields=(
                    "ledger",
                    "topic",
                    "entry_type",
                    "psp_reference",
                    "at",
                    "amount",
                    "balance_before",
                ),
                name="ledgerentry_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("topic", "funds"), ("topic", "fees"), _connector="OR"
                ),
                name="ledgerentry_topic_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="payout",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="payout_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="payout",
            constraint=randirandi.core.models.base.SameOrganization(
                link="ledger", name="payout_ledger_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="payout",
            constraint=models.UniqueConstraint(
                fields=("ledger", "psp_reference"), name="payout_once"
            ),
        ),
        migrations.AddConstraint(
            model_name="payout",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("amount__gte", 0),
                    (
                        "amount",
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                django.db.models.expressions.CombinedExpression(
                                    models.F("captured_total"),
                                    "-",
                                    models.F("refunded_total"),
                                ),
                                "-",
                                models.F("fee_total"),
                            ),
                            "+",
                            models.F("other_total"),
                        ),
                    ),
                ),
                name="payout_adds_up",
            ),
        ),
        migrations.AddConstraint(
            model_name="payout",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("currency", "NOK"),
                    ("currency", "DKK"),
                    ("currency", "EUR"),
                    _connector="OR",
                ),
                name="payout_currency_is_known",
            ),
        ),
        # Last of the tables' constraints, so a reverse takes them off first: they
        # point at the payout table's unique (organization, id).
        migrations.AddConstraint(
            model_name="payment",
            constraint=randirandi.core.models.base.SameOrganization(
                link="payout", name="payment_payout_same_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=randirandi.core.models.base.SameOrganization(
                link="payout", name="refund_payout_same_org"
            ),
        ),
        migrations.RunPython(no_update_on_entries, migrations.RunPython.noop),
        # Listed last, so reversing runs it first, before any table changes.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_money_records
        ),
    ]
