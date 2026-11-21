# SPDX-License-Identifier: AGPL-3.0-only
"""No double booking (docs/booking-model.md, rule 1; docs/data-model.md, check 1).

On one asset, no two of these overlap in time: bookings that are held or
confirmed, and blocks. Each keeps its time in one table, taken time, and one
rule on that table refuses an overlap, whatever the code does. The rule
stands on Postgres itself, with no extension and no trigger under it.
"""

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.db.models import ProtectedError

from randirandi.core.models import (
    Asset,
    Block,
    Booking,
    BookingConfiguration,
    BookingState,
    TakenTime,
)
from tests import made_up

# The made-up organization has a booking from 10:00 to 12:00 and a block
# from 14:00 to 15:00, on one asset and one day.

THE_RULE = "capacity_no_double_booking"


def constraint_of(error: pytest.ExceptionInfo[IntegrityError]) -> str:
    cause = error.value.__cause__
    assert cause is not None
    name: str = cause.diag.constraint_name  # type: ignore[attr-defined]
    return name


def time_of(made: made_up.MadeUp, row: Booking | Block) -> models.QuerySet[TakenTime]:
    """The row's taken time, to change it the way code that checks nothing
    would.
    """
    return TakenTime.objects.of(made.organization).filter(pk=row.taken_time_id)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start", "end"),
    [(10, 12), (9, 11), (11, 13), (9, 13), (11, 12)],
    ids=["the same", "into it", "out of it", "around it", "inside it"],
)
@pytest.mark.parametrize("state", [BookingState.HELD, BookingState.CONFIRMED])
def test_a_booking_over_a_booking_is_refused(
    start: int, end: int, state: BookingState
) -> None:
    made = made_up.everything()

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        made.book(start, end, state)

    assert constraint_of(refused) == THE_RULE


@pytest.mark.django_db
def test_a_held_booking_holds_its_time_too() -> None:
    made = made_up.everything()
    made.book(16, 18, BookingState.HELD)

    with pytest.raises(IntegrityError), transaction.atomic():
        made.book(17, 19)


@pytest.mark.django_db
def test_bookings_that_meet_do_not_overlap() -> None:
    made = made_up.everything()

    before = made.book(8, 10)
    after = made.book(12, 14)

    assert before.taken_time.ends_at == made.booking.taken_time.starts_at
    assert after.taken_time.starts_at == made.booking.taken_time.ends_at


@pytest.mark.django_db
@pytest.mark.parametrize("state", [BookingState.CANCELLED, BookingState.EXPIRED])
def test_a_booking_that_no_longer_holds_its_time_frees_it(state: BookingState) -> None:
    made = made_up.everything()
    time_of(made, made.booking).update(state=state)

    again = made.book(10, 12)

    assert again.pk != made.booking.pk


@pytest.mark.django_db
def test_a_cancelled_booking_cannot_come_back_onto_taken_time() -> None:
    made = made_up.everything()
    cancelled = made.book(10, 12, BookingState.CANCELLED)

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        time_of(made, cancelled).update(state=BookingState.CONFIRMED)

    assert constraint_of(refused) == THE_RULE


@pytest.mark.django_db
def test_another_asset_is_another_matter() -> None:
    made = made_up.everything()
    guest_room = Asset.objects.create(organization=made.organization, name="Guest room")
    stay = BookingConfiguration.objects.of(made.organization).get(
        pk=made.configuration.pk
    )
    # The same container on the guest room, with a number of its own.
    stay.pk, stay.number, stay.asset = None, 99, guest_room
    stay._state.adding = True
    stay.save()

    booking = made.book(10, 12, configuration=stay)

    assert booking.taken_time.asset == guest_room


@pytest.mark.django_db
def test_a_booking_over_a_block_is_refused() -> None:
    made = made_up.everything()

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        made.book(13, 15)

    assert constraint_of(refused) == THE_RULE


@pytest.mark.django_db
def test_a_booking_moved_onto_a_block_is_refused() -> None:
    made = made_up.everything()
    booking = made.book(16, 17)

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        booking.taken_time.revise(
            starts_at=made_up.at(made.day, 14),
            ends_at=made_up.at(made.day, 16),
            mode="exclusive",
            quantity=1,
        )

    assert constraint_of(refused) == THE_RULE


@pytest.mark.django_db
def test_a_block_over_a_booking_is_refused() -> None:
    made = made_up.everything()

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        made.block(11, 13)

    assert constraint_of(refused) == THE_RULE


@pytest.mark.django_db
def test_a_block_over_a_block_is_refused() -> None:
    made = made_up.everything()

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        made.block(14, 16)

    assert constraint_of(refused) == THE_RULE


@pytest.mark.django_db
def test_a_block_may_cover_a_cancelled_booking_and_meet_a_live_one() -> None:
    made = made_up.everything()
    made.book(16, 18, BookingState.CANCELLED)

    over_the_cancelled = made.block(16, 18)
    meeting_the_live_one = made.block(12, 14)

    assert Block.objects.of(made.organization).count() == 3
    assert over_the_cancelled.pk != meeting_the_live_one.pk


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Booking, Block])
def test_time_runs_forwards(model: type[Booking] | type[Block]) -> None:
    made = made_up.everything()
    if model is Block:
        # A block takes whole cells of its container's grid, so the
        # application refuses it first; the rule below is the same row's.
        with pytest.raises(ValueError, match="whole cells"):
            made.block(20, 20)
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            TakenTime.objects.take(
                organization=made.organization,
                asset=made.asset,
                starts_at=made_up.at(made.day, 20),
                ends_at=made_up.at(made.day, 20),
                state="blocked",
            )
    else:
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            made.book(20, 20)

    assert constraint_of(refused) == "takentime_ends_after_it_starts"


# --- One table of taken time ---------------------------------------------------


@pytest.mark.django_db
def test_a_state_the_rule_does_not_know_is_refused() -> None:
    # Such a row would hold no time, and a second booking would fit over it.
    made = made_up.everything()

    with pytest.raises(IntegrityError) as refused, transaction.atomic():
        time_of(made, made.booking).update(state="Confirmed")

    assert constraint_of(refused) == "takentime_state_is_known"


@pytest.mark.django_db
def test_a_booking_that_cannot_be_made_takes_no_time() -> None:
    made = made_up.everything()
    taken_before = TakenTime.objects.of(made.organization).count()

    # It names no order.
    with pytest.raises(IntegrityError):
        Booking.objects.take(
            organization=made.organization,
            asset=made.asset,
            booking_configuration=made.configuration,
            starts_at=made_up.at(made.day, 16),
            ends_at=made_up.at(made.day, 18),
            state=BookingState.CONFIRMED,
            member=made.member,
            booking_type=made.booking_type,
            booking_category=made.private,
            booking_type_name=made.booking_type.name,
            booking_category_name=made.private.name,
        )

    assert TakenTime.objects.of(made.organization).count() == taken_before
    assert made.book(16, 18).taken_time.state == BookingState.CONFIRMED


@pytest.mark.django_db
def test_a_booking_cannot_lose_its_taken_time() -> None:
    made = made_up.everything()

    with pytest.raises(ProtectedError):
        made.booking.taken_time.delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        Booking.objects.of(made.organization).filter(pk=made.booking.pk).update(
            taken_time=None
        )


@pytest.mark.django_db
def test_a_block_that_is_removed_frees_its_time() -> None:
    made = made_up.everything()
    block = made.block(16, 18)
    taken_before = TakenTime.objects.of(made.organization).count()

    block.delete()

    assert not Block.objects.of(made.organization).filter(pk=block.pk).exists()
    assert TakenTime.objects.of(made.organization).count() == taken_before - 1
    assert made.book(16, 18).taken_time.asset == made.asset


@pytest.mark.django_db
def test_the_rule_stands_when_every_extension_is_removed() -> None:
    # Found at Scaleway on 2026-10-10: there any database user may remove an
    # extension together with everything that stands on it. So nothing of
    # ours stands on one, and no trigger of ours does any of the refusing.
    made = made_up.everything()

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")
        assert cursor.fetchone() == (0,)
        # Postgres installs this one by itself. A migration that adds another
        # fails here, and is then looked at with this in mind.
        cursor.execute("SELECT extname FROM pg_extension")
        assert cursor.fetchall() == [("plpgsql",)]
        cursor.execute("DROP EXTENSION plpgsql CASCADE")
        cursor.execute("SELECT count(*) FROM pg_extension")
        assert cursor.fetchone() == (0,)

    for take in (lambda: made.book(11, 13), lambda: made.block(11, 13)):
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            take()
        assert constraint_of(refused) == THE_RULE
    assert made.book(12, 14).taken_time.state == BookingState.CONFIRMED
