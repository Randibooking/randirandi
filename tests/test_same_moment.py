# SPDX-License-Identifier: AGPL-3.0-only
"""Two connections at the same moment (docs/data-model.md, check 1).

One takes a time on an asset and has not finished. The other asks for a time
that overlaps it. The second waits for the first, and is then refused, by
the one rule on taken time, whether each is a booking or a block.
"""

import threading
from collections.abc import Callable
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from tests import made_up

Take = Callable[[made_up.MadeUp], Any]


def at_the_same_moment(made: made_up.MadeUp, first: Take, second: Take) -> list[str]:
    """Run `first` and leave it unfinished, run `second` beside it, then let
    `first` finish. What happened, in order.
    """
    happened: list[str] = []
    first_has_taken = threading.Event()
    first_may_finish = threading.Event()

    def run_first() -> None:
        try:
            with transaction.atomic():
                first(made)
                happened.append("first took its time")
                first_has_taken.set()
                first_may_finish.wait(timeout=10)
            happened.append("first finished")
        finally:
            first_has_taken.set()
            connection.close()

    def run_second() -> None:
        try:
            with transaction.atomic():
                second(made)
            happened.append("second took its time")
        except IntegrityError as refused:
            cause = refused.__cause__
            assert cause is not None
            name = cause.diag.constraint_name  # type: ignore[attr-defined]
            happened.append(f"second refused: {name}")
        finally:
            connection.close()

    one = threading.Thread(target=run_first)
    two = threading.Thread(target=run_second)
    one.start()
    assert first_has_taken.wait(timeout=10)
    two.start()
    # The second is now waiting. Had it not waited, it would be done here.
    two.join(timeout=0.5)
    happened.append("second is waiting" if two.is_alive() else "second did not wait")
    first_may_finish.set()
    one.join(timeout=10)
    two.join(timeout=10)
    return happened


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (lambda made: made.book(16, 18), lambda made: made.book(17, 19)),
        (lambda made: made.book(16, 18), lambda made: made.block(17, 19)),
        (lambda made: made.block(16, 18), lambda made: made.book(17, 19)),
        (lambda made: made.block(16, 18), lambda made: made.block(17, 19)),
    ],
    ids=[
        "two bookings",
        "a block onto a booking",
        "a booking onto a block",
        "two blocks",
    ],
)
def test_of_two_at_the_same_moment_one_wins(first: Take, second: Take) -> None:
    made = made_up.everything()

    happened = at_the_same_moment(made, first, second)

    assert happened == [
        "first took its time",
        "second is waiting",
        "first finished",
        "second refused: capacity_no_double_booking",
    ]


@pytest.mark.django_db(transaction=True)
def test_times_that_do_not_overlap_are_both_taken() -> None:
    made = made_up.everything()

    happened = at_the_same_moment(
        made, lambda made: made.book(16, 18), lambda made: made.block(18, 19)
    )

    # The second does not have to wait for the first: their times only meet.
    assert "second took its time" in happened
    assert "second refused" not in " ".join(happened)
