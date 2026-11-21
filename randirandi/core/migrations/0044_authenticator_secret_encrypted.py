# SPDX-License-Identifier: AGPL-3.0-only
# the authenticator secret is stored encrypted (v1: AES-256-GCM
# under RANDIRANDI_AUTHENTICATOR_KEY). Existing plain rows are encrypted
# forward with the key from the environment at migrate time. Reversing is
# refused while a row exists: it would write every administrator's second step
# back as plain hex.


from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from randirandi.core import authenticator_secret


def encrypt_forward(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = apps.get_model("core", "Authenticator")._base_manager.using(
        schema_editor.connection.alias
    )
    plain = rows.filter(secret__regex=r"^[0-9a-f]{40}$")  # noqa: S106 — pattern
    if not plain.exists():
        return
    key = settings.AUTHENTICATOR_KEY
    if not key:
        raise RuntimeError(
            "RANDIRANDI_AUTHENTICATOR_KEY is not set, and authenticator secrets "
            "are stored in plain form that this migration encrypts. Set it "
            "(32 random bytes as 64 hex characters: openssl rand -hex 32) and "
            "run the migration again; nothing was changed."
        )
    for row in plain.select_for_update():
        row.secret = authenticator_secret.seal(
            row.secret,
            key=key,
            organization_id=row.organization_id,
            member_id=row.member_id,
        )
        row.save(update_fields=["secret"])


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = apps.get_model("core", "Authenticator")._base_manager.using(
        schema_editor.connection.alias
    )
    if not rows.exists():
        return
    raise RuntimeError(
        "Reversing 0044 is refused while an authenticator exists: it would "
        "store every administrator's secret as plain hex again."
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0043_session_limits")]

    operations = [
        migrations.RemoveConstraint(
            model_name="authenticator", name="authenticator_secret_valid"
        ),
        migrations.AlterField(
            model_name="authenticator",
            name="secret",
            field=models.CharField(max_length=80),
        ),
        migrations.RunPython(encrypt_forward, refuse_reverse),
        migrations.AddConstraint(
            model_name="authenticator",
            constraint=models.CheckConstraint(
                condition=models.Q(("secret__regex", "^v1:[A-Za-z0-9_-]{64}$")),
                name="authenticator_secret_valid",
            ),
        ),
    ]
