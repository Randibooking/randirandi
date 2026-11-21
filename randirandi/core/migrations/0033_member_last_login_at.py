# SPDX-License-Identifier: AGPL-3.0-only
# the moment a member last signed in, kept beyond the thirty days
# the login attempts are. Members get the newest sign-in the kept attempts
# show: the accepted authenticator code of an administrator, the accepted login
# code of anyone else; the others read empty until they sign in. Reversing is
# refused while a member holds one, as it would be lost.

from typing import Any

from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def fill_from_the_kept_attempts(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    member = apps.get_model("core", "Member")._default_manager.using(alias)
    attempt = apps.get_model("core", "LoginAttempt")._default_manager.using(alias)
    for administrator, kind in ((True, "mfa"), (False, "verify")):
        newest = (
            attempt.filter(member=OuterRef("pk"), kind=kind, outcome="accepted")
            .order_by("-at")
            .values("at")[:1]
        )
        member.filter(role__admin_access=administrator).update(
            last_login_at=Subquery(newest)
        )


def refuse_a_reverse_that_loses_a_login(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    held = (
        apps.get_model("core", "Member")
        ._default_manager.using(alias)
        .filter(last_login_at__isnull=False)
        .count()
    )
    if held:
        raise RuntimeError(
            f"Cannot reverse while {held} member(s) hold their last login "
            "(retained last logins)."
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0032_blocks_admin")]

    operations = [
        migrations.AddField(
            model_name="member",
            name="last_login_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            fill_from_the_kept_attempts, refuse_a_reverse_that_loses_a_login
        ),
    ]
