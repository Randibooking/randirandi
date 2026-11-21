# SPDX-License-Identifier: AGPL-3.0-only
"""The names of one asset's floor plans , asked once.

Kept free of Django so the setup file's checks (`randirandi.setup_file`) can
ask it; `AssetInventory.clean` and the admin screens ask it too. A floor plan
is pointed at by its name (a shared container in a setup file names the one it
books), so when an asset has more than one, each has a name and no two share
one. An asset's only floor plan may go without.
"""

from collections.abc import Sequence

NEEDS_NAME = "needs-name"
NAME_USED = "name-used"
SIBLING_UNNAMED = "sibling-unnamed"


def floor_plan_name_fault(name: str, siblings: Sequence[str]) -> str | None:
    """Why a floor plan with this name may not stand beside the asset's other
    floor plans (their names, "" for one without), or None: it has none and
    there are others (`NEEDS_NAME`), another has the same (`NAME_USED`), or it
    has one and another has none (`SIBLING_UNNAMED`, which the other would be
    told as `NEEDS_NAME`).
    """
    if not siblings:
        return None
    if not name:
        return NEEDS_NAME
    if name in siblings:
        return NAME_USED
    if "" in siblings:
        return SIBLING_UNNAMED
    return None
