# SPDX-License-Identifier: AGPL-3.0-only
# the member import is built around parsers, not around one
# list's columns . A run keeps the id of the parser that read it and the
# uploaded file's name and size; the member source (kind, location, column
# map, layout, schedule) goes, because the parser in code holds the layout.
# An earlier run keeps its place: the sandbox's made-up members become the
# parser "made-up-members", any other earlier run has no parser id.
# Going forward drops the sources' configuration. Reversing is refused while
# a run holds a parser id other than the made-up members', a file name or a
# file size, which the old schema has no column for; other runs get a bare
# source back (the made-up members' the bundled one). The application's table
# privileges are unchanged (member imports have no column privilege contract).

from typing import Any

from django.db import migrations, models

MADE_UP = "made-up-members"
BUNDLED = "bundled:dummy-members.csv"


def name_the_parser_of_earlier_runs(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    runs = apps.get_model("core", "MemberImport")._default_manager.using(alias)
    sources = apps.get_model("core", "MemberSource")._default_manager.using(alias)
    bundled = sources.filter(location__startswith="bundled:").values_list(
        "pk", flat=True
    )
    runs.filter(member_source_id__in=list(bundled)).update(parser=MADE_UP)


def refuse_a_reverse_that_loses_run_details(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    runs = apps.get_model("core", "MemberImport")._default_manager.using(alias)
    held = (
        runs.exclude(parser__in=["", MADE_UP]).count()
        + runs.exclude(file_name="").count()
        + runs.exclude(file_size=0).count()
    )
    if held:
        raise RuntimeError(
            "Cannot reverse while a member import holds a parser, a file name "
            "or a file size (retained parsers and file names)."
        )


def give_every_run_a_source_back(apps: Any, schema_editor: Any) -> None:
    """On the way back: a run needs a source, so each organization with runs
    gets a bare one (two when it has made-up runs too) and they point to it."""
    alias = schema_editor.connection.alias
    runs = apps.get_model("core", "MemberImport")._default_manager.using(alias)
    sources = apps.get_model("core", "MemberSource")._default_manager.using(alias)
    made: dict[tuple[int, str], int] = {}
    for run in runs.order_by("pk"):
        key = (run.organization_id, BUNDLED if run.parser == MADE_UP else "")
        if key not in made:
            made[key] = sources.create(
                organization_id=key[0], kind="file upload", location=key[1]
            ).pk
        runs.filter(pk=run.pk).update(member_source_id=made[key])


class Migration(migrations.Migration):
    dependencies = [("core", "0039_setup_screens_audit")]

    operations = [
        migrations.AddField(
            model_name="memberimport",
            name="parser",
            field=models.CharField(default="", max_length=60),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="memberimport",
            name="file_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="memberimport",
            name="file_size",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(
            name_the_parser_of_earlier_runs, migrations.RunPython.noop
        ),
        # The source is optional for a moment, so that going back can add the
        # column, give each run a source and only then require it.
        migrations.AlterField(
            model_name="memberimport",
            name="member_source",
            field=models.ForeignKey(
                null=True,
                on_delete=models.deletion.PROTECT,
                to="core.membersource",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, give_every_run_a_source_back),
        migrations.RemoveConstraint(
            model_name="memberimport", name="memberimport_member_source_same_org"
        ),
        migrations.RemoveField(model_name="memberimport", name="member_source"),
        migrations.DeleteModel(name="MemberSource"),
        # Last, so a reverse runs it first: refused before anything is changed.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_a_reverse_that_loses_run_details
        ),
    ]
