# SPDX-License-Identifier: AGPL-3.0-only
# a member's access (granted, revoked) becomes a status (active,
# disabled, blocked), and the member keeps what the member list last said
# (listed). A revoked member whose latest access event was caused by an
# administrator is blocked; any other revoked member is disabled. The events
# change the same way. Reversing maps active back to granted and the rest to
# revoked: the difference between disabled and blocked, and listed, are lost.

from typing import Any

from django.db import migrations, models
from django.db.models import F, OuterRef, Q, Subquery

STATUSES = [("active", "Active"), ("disabled", "Disabled"), ("blocked", "Blocked")]
OLD_ACTIONS = [
    ("operator named first administrator", "Bootstrap"),
    ("exported member", "Export Member"),
    ("corrected member", "Correct Member"),
    ("erased member", "Erase Member"),
    ("requested member import", "Import Members"),
    ("inspected", "Inspect"),
    ("made block", "Made Block"),
    ("administrator signed in", "Admin Login"),
    ("enrolled authenticator", "Enroll Authenticator"),
    ("reset authenticator", "Reset Authenticator"),
    ("operator reset authenticator", "Operator Reset Authenticator"),
    ("requested refund", "Request Refund"),
    ("cancelled unpaid order", "Cancel Payment"),
]
NEW_ACTIONS = [
    *OLD_ACTIONS[:4],
    ("blocked member", "Block Member"),
    ("unblocked member", "Unblock Member"),
    *OLD_ACTIONS[4:],
]
NEW_ONLY = ["blocked member", "unblocked member"]


def to_status(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    members = apps.get_model("core", "Member")._default_manager.using(alias)
    events = apps.get_model("core", "AccessEvent")._default_manager.using(alias)
    latest = events.filter(member=OuterRef("pk")).order_by("-at", "-pk")
    latest_import = latest.filter(cause="member import")
    by_administrator = Q(cause="administrator", administrator__isnull=False)

    members.filter(access="granted").update(status="active", listed=True)
    members.filter(access="revoked").update(status="disabled")
    members.filter(access="revoked").annotate(
        last_cause=Subquery(latest.values("cause")[:1]),
        last_administrator=Subquery(latest.values("administrator_id")[:1]),
    ).filter(last_cause="administrator", last_administrator__isnull=False).update(
        status="blocked"
    )
    # Listed is what the latest import said; a member without an import event,
    # or not from an import, counts as listed.
    # Unlisted at: the time of that import event; empty when unknown.
    members.filter(origin="member import").annotate(
        last_import=Subquery(latest_import.values("change")[:1]),
        last_import_at=Subquery(latest_import.values("at")[:1]),
    ).filter(last_import="revoked").update(
        listed=False, unlisted_at=F("last_import_at")
    )
    # An erased member keeps no name, no contact and no history.
    members.filter(
        access="revoked",
        full_name="",
        first_name="",
        last_name="",
        email="",
        phone="",
    ).update(status="disabled", listed=False)

    events.filter(by_administrator, change="revoked").update(change="blocked")
    events.filter(change="revoked").update(change="disabled")
    events.filter(change="granted").update(change="active")


def to_access(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    if (
        apps.get_model("core", "AuditEvent")
        ._default_manager.using(alias)
        .filter(action__in=NEW_ONLY)
        .exists()
    ):
        raise RuntimeError("Cannot reverse retained block and unblock history.")
    members = apps.get_model("core", "Member")._default_manager.using(alias)
    events = apps.get_model("core", "AccessEvent")._default_manager.using(alias)
    members.filter(status="active").update(access="granted")
    members.exclude(status="active").update(access="revoked")
    events.filter(change="active").update(change="granted")
    events.exclude(change="granted").update(change="revoked")


class Migration(migrations.Migration):
    dependencies = [("core", "0024_vipps_description_and_receipt")]

    operations = [
        migrations.RemoveConstraint(model_name="auditevent", name="audit_fixed_action"),
        migrations.RemoveConstraint(
            model_name="member", name="member_access_needs_an_email"
        ),
        migrations.AlterField(
            model_name="accessevent",
            name="change",
            field=models.CharField(choices=STATUSES, max_length=8),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(choices=NEW_ACTIONS, max_length=100),
        ),
        migrations.AddField(
            model_name="member",
            name="status",
            field=models.CharField(choices=STATUSES, default="active", max_length=8),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="member",
            name="listed",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="member",
            name="unlisted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # So that reversing can add the column back to a table with rows.
        migrations.AlterField(
            model_name="member",
            name="access",
            field=models.CharField(
                choices=[("granted", "Granted"), ("revoked", "Revoked")],
                default="revoked",
                max_length=7,
            ),
        ),
        # The updated rows have deferred checks queued; the table changes
        # after the update need them settled. Reversing runs this one second.
        migrations.RunSQL(migrations.RunSQL.noop, "SET CONSTRAINTS ALL IMMEDIATE"),
        migrations.RunPython(to_status, to_access),
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="accessevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("change", "blocked"), _negated=True),
                    models.Q(
                        ("administrator__isnull", False), ("cause", "administrator")
                    ),
                    _connector="OR",
                ),
                name="accessevent_blocked_by_administrator",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("action__in", [a for a, _ in NEW_ACTIONS])),
                name="audit_fixed_action",
            ),
        ),
        migrations.AddConstraint(
            model_name="member",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "active"), _negated=True),
                    models.Q(("email", ""), _negated=True),
                    _connector="OR",
                ),
                name="member_active_needs_an_email",
            ),
        ),
        migrations.RemoveField(model_name="member", name="access"),
    ]
