# SPDX-License-Identifier: AGPL-3.0-only
# a login code remembers the network it was asked for from (a new
# request voids only codes from the same network), and a login attempt may be
# a payment return (the anonymous return's per-network limit).

from typing import Any

from django.db import migrations, models


def keep(apps: Any, schema_editor: Any) -> None:
    """Forward: nothing to do."""


def drop_returns(apps: Any, schema_editor: Any) -> None:
    """Reverse: payment-return counters (no member, no record of anything) go,
    since the older constraint does not know the kind."""
    apps.get_model("core", "LoginAttempt")._base_manager.filter(kind="return").delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0042_asset_screens")]

    operations = [
        migrations.RemoveConstraint(
            model_name="loginattempt", name="loginattempt_outcome_kind"
        ),
        migrations.RunPython(keep, drop_returns),
        migrations.AddField(
            model_name="logincode",
            name="network",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="loginattempt",
            name="kind",
            field=models.CharField(
                choices=[
                    ("request", "Request"),
                    ("verify", "Verify"),
                    ("mfa", "Authenticator"),
                    ("return", "Payment return"),
                ],
                max_length=7,
            ),
        ),
        migrations.AddConstraint(
            model_name="loginattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kind", "request"),
                        ("outcome__in", ["issued", "limited", "unknown"]),
                    ),
                    models.Q(
                        ("kind", "verify"), ("outcome__in", ["accepted", "refused"])
                    ),
                    models.Q(
                        ("kind", "mfa"),
                        ("outcome__in", ["accepted", "refused", "limited"]),
                    ),
                    models.Q(("kind", "return"), ("outcome", "unknown")),
                    _connector="OR",
                ),
                name="loginattempt_outcome_kind",
            ),
        ),
    ]
