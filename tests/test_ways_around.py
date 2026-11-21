# SPDX-License-Identifier: AGPL-3.0-only
"""The ways around the organization boundary are few, and each one is meant
(control C7).

A query through `objects` has to name its organization. Some code has no
organization to go by, or asks the database directly. Every file that does
is listed here with its reason. A new one fails this test until it is added,
which is the moment to ask whether it should cross at all.
"""

from pathlib import Path

CODE = Path(__file__).resolve().parent.parent / "randirandi"

# What reads rows without naming an organization: the manager that crosses,
# the managers Django itself reads through, and SQL written by hand.
WAYS_AROUND = (
    "across_organizations",
    "default_manager",
    "base_manager",
    ".raw(",
    "RawSQL",
    "cursor(",
    ".extra(",
)

MEANT = {
    "core/payments.py": (
        "Takes a per-order session advisory lock across provider calls. "
        "SQL reads no tenant rows; all model queries name their organization."
    ),
    "core/authentication.py": (
        "Django reloads the globally unique member id from a trusted server session. "
        "Immediately following SessionPolicy checks hostname organization and current "
        "authority before any view receives authenticated access."
    ),
    "core/member_data.py": (
        "Sets serializable mode for member data and retention operations. "
        "All member records are read through organization-scoped queries."
    ),
    "core/payouts.py": (
        "Takes a per-account session advisory lock so one worker reads one "
        "account's report at a time. SQL reads no rows; all model queries name "
        "their organization."
    ),
    "core/booking_quote.py": (
        "Sets isolation/read-only mode for one coherent price snapshot. "
        "SQL reads no rows; all model queries name their organization."
    ),
    "core/booking_checkout.py": (
        "Takes a per-member session advisory lock so one press makes one order. "
        "SQL reads no rows; all model queries name their organization."
    ),
    "core/booking_choices.py": (
        "Sets isolation/read-only mode for one coherent snapshot of the member's "
        "choices. SQL reads no rows; all model queries name their organization."
    ),
    "core/member_calendar.py": (
        "Sets isolation/read-only mode for one coherent calendar snapshot. "
        "SQL reads no rows; all model queries name their organization."
    ),
    "mail_configuration.py": (
        "The sandbox mail router decides on an address alone, with no organization "
        "to go by: it asks only whether an active member added by an administrator "
        "has that email, in any organization, and reads no other field."
    ),
    "core/models/base.py": "Makes the manager that crosses, and names it.",
    "core/calendar.py": (
        "Sets isolation/read-only mode for one coherent calendar snapshot. "
        "SQL reads no rows; all model queries name their organization."
    ),
    "core/ordering.py": (
        "Sets serializable transaction mode for atomic checkouts. SQL reads no "
        "rows; request data and allocations use organization-scoped queries."
    ),
    "core/old_booking_import.py": (
        "Sets serializable transaction mode for each brought-in entry, as an "
        "order does. SQL reads no rows; all model queries name their organization."
    ),
    "core/block_admin.py": (
        "Sets serializable transaction mode for atomic blocks, as an order's. "
        "SQL reads no rows; all model queries name their organization."
    ),
    "core/pricing.py": (
        "Sets isolation/read-only mode for a coherent price snapshot. "
        "SQL reads no rows; all model queries name their organization."
    ),
    "core/setup.py": (
        "Locks configuration tables for a coherent export. The SQL reads no "
        "tenant rows; exported rows still use organization-scoped ORM queries."
    ),
    "ops/management/commands/grants.py": (
        "Gives the app's database user its rights, as the owner of the tables, "
        "before the migrations. It reads no row."
    ),
    "ops/management/commands/apply_privileges.py": (
        "Brings the app's database user to its declared rights, as the owner "
        "of the tables, after the migrations. It reads no row."
    ),
    "ops/management/commands/check_privileges.py": (
        "Compares the app's database user with its declared rights, read-only. "
        "It reads no row."
    ),
    "ops/views.py": (
        "The health page asks the database whether it answers. It reads no table."
    ),
}


def files_that_go_around() -> set[str]:
    found = set()
    for path in CODE.rglob("*.py"):
        # A migration changes tables, as the table owner, for every
        # organization at once. It is read as such when it is written.
        if "migrations" in path.parts:
            continue
        text = path.read_text()
        if any(way in text for way in WAYS_AROUND):
            found.add(path.relative_to(CODE).as_posix())
    return found


def test_only_the_listed_files_go_around_the_organization_boundary() -> None:
    assert files_that_go_around() == set(MEANT)
