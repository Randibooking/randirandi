# SPDX-License-Identifier: AGPL-3.0-only
# S1.3a: the language setting . An organization gains its language
# (`en` for every one that exists).
# Reversing is refused while any organization has a language other than
# English: the reverse would drop it. A reverse without one is clean. The
# graph's leaf nodes are the caller's to restore.

from typing import Any

from django.db import migrations, models

DEFAULT_LANGUAGE = "en"


def refuse_a_reverse_that_loses_a_language(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    organization = apps.get_model("core", "Organization")
    lost = sorted(
        organization._default_manager.using(alias)
        .exclude(language=DEFAULT_LANGUAGE)
        .values_list("subdomain", flat=True)
    )
    if lost:
        raise RuntimeError(
            "Cannot reverse while an organization has a language other than "
            f"English (it would be lost): {lost}."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0053_booking_conditions"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="language",
            field=models.CharField(default=DEFAULT_LANGUAGE, max_length=16),
        ),
        # Last, so a reverse runs it first: refused before anything is dropped.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_a_language
        ),
    ]
