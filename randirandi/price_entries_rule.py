# SPDX-License-Identifier: AGPL-3.0-only
"""What makes the entries of one container in one booking category unable to
price it, asked once.

Kept free of Django so the setup file's checks (`randirandi.setup_file`) can
ask it; the administrator's price screens (`price_admin`) ask it too, and
`pricing._priced_by_book` holds the same rule at pricing time.
"""

from collections.abc import Collection

GROUP_TWICE = "group-twice"
NEEDS_NORMAL_PRICE = "needs-normal-price"
GROUPS_PER_SPOT = "groups-per-spot"
PER_CAPACITY_UNIT = "per_capacity_unit"


def what_the_entries_of_a_category_get_wrong(
    entries: Collection[tuple[int | str | None, str]],
) -> str | None:
    """Says what is wrong when the entries one container has for one booking
    category, each as (group of spots or none, billing basis), cannot price
    it: a group named twice, a price for a group without the normal price for
    the spots in no group, or a price for a group beside a normal price that
    is not per spot. Returns a fixed word, or none."""
    groups = [group for group, _ in entries]
    if len(set(groups)) != len(groups):
        return GROUP_TWICE
    if None not in groups:
        return NEEDS_NORMAL_PRICE
    if len(groups) > 1 and any(basis != PER_CAPACITY_UNIT for _, basis in entries):
        return GROUPS_PER_SPOT
    return None
