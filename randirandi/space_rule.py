# SPDX-License-Identifier: AGPL-3.0-only
"""Who may carry the whole-space mark (W17), asked once.

Kept free of Django so the setup file's checks (`randirandi.setup_file`) can
ask it; `randirandi.core.models.bookable` imports it beside `kept_apart` and
`kept_whole`, and its `kept_apart` docstring lists every place that holds the
space rule for bookings.
"""

from collections.abc import Container

NEEDS_SPACE = "needs-space"
SPACE_ALREADY_MARKED = "space-already-marked"


def space_mark_fault(
    *, whole_space: bool, space: object | None, marked_spaces: Container[object]
) -> str | None:
    """Why an asset may not cover its whole space, or None: it has no space
    (`NEEDS_SPACE`), or another asset already covers that space
    (`SPACE_ALREADY_MARKED`, one mark per space). The checks keep their own
    words: `Asset._may_cover_its_space` and the setup file's check of
    `whole_space`.
    """
    if not whole_space:
        return None
    if space is None or space == "":
        return NEEDS_SPACE
    return SPACE_ALREADY_MARKED if space in marked_spaces else None
