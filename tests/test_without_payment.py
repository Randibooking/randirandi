# SPDX-License-Identifier: AGPL-3.0-only
"""a container without payment (W12).

It has no price book. It is offered for its categories at 0, quoted with no
lines, and its bookings are confirmed without Vipps; beside a paid booking
in one cart it is held with the rest and confirmed when that is paid. Made-up
setups only: the pool of `tests.test_calendar`, with its paid swims from
10:00 to 14:00 and, here, free swims from 14:00 to 18:00.
"""

import uuid
from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from randirandi import setup_file as f
from randirandi.core import booking_cart, booking_checkout, payments, pricing
from randirandi.core import payment_callbacks as callbacks
from randirandi.core import payment_reconciliation as reconciliation
from randirandi.core import setup as setups
from randirandi.core.booking_cart import CartRow
from randirandi.core.booking_checkout import Placed
from randirandi.core.booking_choices import booking_choices
from randirandi.core.booking_quote import Choice, quote
from randirandi.core.calendar import offered_categories
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    Booking,
    BookingCategory,
    BookingConfiguration,
    BookingLine,
    BookingState,
    ConfigurationCategory,
    Member,
    Payment,
    PriceBookEntry,
    PriceTier,
    Refund,
    TakenTime,
)
from randirandi.core.ordering import BookingRequest, OrderUnavailableError
from randirandi.core.payment_adjustments import request_refund
from randirandi.core.payments import PaymentUnavailableError
from randirandi.core.pricing import PriceUnavailableError, price_booking
from randirandi.core.retention import purge_organization
from randirandi.vipps import PaymentStatus
from tests import made_up
from tests.fresh_code import fresh
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_ordering import NOW, checkout, request
from tests.test_payment_adjustments import administrator as administrator
from tests.test_payment_capture import Money
from tests.test_payment_capture import money as money
from tests.test_payments import Provider
from tests.test_payments import provider as provider
from tests.test_setup_file import SETUPS

pytestmark = pytest.mark.django_db(transaction=True)


def without_payment(made: Example) -> BookingConfiguration:
    """A second container on the pool, 14:00 to 18:00, with no price book."""
    config = BookingConfiguration.objects.create(
        organization=made.org,
        asset=made.asset,
        booking_type=made.kind,
        period_type="flexible",
        period_start=840,
        period_end=1080,
        end_day=0,
        period_start_weekdays=list(range(7)),
        interval=60,
        minimum_length=60,
        maximum_length=240,
        status="enabled",
    )
    ConfigurationCategory.objects.create(
        organization=made.org,
        booking_configuration=config,
        booking_category=made.category,
    )
    return config


def free_swim(
    made: Example, config: BookingConfiguration, day: date = DAY
) -> BookingRequest:
    return request(
        made,
        configuration_id=config.pk,
        starts_at=at(day, hour=15),
        ends_at=at(day, hour=16),
    )


def test_a_container_without_payment_is_offered_for_its_categories(
    made: Example,
) -> None:
    free = without_payment(made)
    assert not free.with_payment and made.config.with_payment
    assert offered_categories(made.org.pk, made.member.role_id, [free]) == {
        free.pk: (made.category.pk,)
    }
    offers = [row for row in made.offers() if row.configuration_id == free.pk]
    assert {(row.starts_at, row.ends_at) for row in offers} >= {
        (at(hour=14), at(hour=18)),
        (at(hour=17), at(hour=18)),
    }
    assert all(row.category_ids == (made.category.pk,) for row in offers)
    # The categories still decide who books: one the role cannot book is not
    # offered, and is refused when ordered, with payment or without.
    club = BookingCategory.objects.create(organization=made.org, name="Club")
    ConfigurationCategory.objects.create(
        organization=made.org, booking_configuration=free, booking_category=club
    )
    assert offered_categories(made.org.pk, made.member.role_id, [free]) == {
        free.pk: (made.category.pk,)
    }
    with pytest.raises(OrderUnavailableError):
        checkout(made, replace(free_swim(made, free), booking_category_id=club.pk))
    ConfigurationCategory.objects.of(made.org).filter(
        booking_configuration=free
    ).delete()
    assert offered_categories(made.org.pk, made.member.role_id, [free]) == {}


def test_it_is_quoted_at_nothing_with_no_lines(made: Example) -> None:
    free = without_payment(made)
    price = price_booking(
        configuration=free,
        booking_category=made.category,
        starts_at=at(hour=15),
        ends_at=at(hour=17),
    )
    assert (price.lines, price.total) == ((), 0)
    found = quote(
        member=made.member,
        configuration_id=free.pk,
        booking_category_id=made.category.pk,
        quantity=1,
        starts_at=at(hour=15),
        ends_at=at(hour=17),
        now=NOW,
    )
    assert found.reason is None and found.configuration_id == free.pk
    assert (found.lines, found.total, found.tiers_go_by) == ((), 0, "")
    # Its time rules still hold: no price for half an hour.
    with pytest.raises(PriceUnavailableError, match="time rules"):
        price_booking(
            configuration=free,
            booking_category=made.category,
            starts_at=at(hour=15),
            ends_at=at(hour=15, minute=30),
        )


def test_a_container_with_payment_still_refuses_a_price_with_no_priced_part(
    made: Example,
) -> None:
    """Zero is never substituted: priced without a single part, a container
    with payment is refused, unlike one without payment."""
    data = pricing._read(made.config, made.category)
    with pytest.raises(PriceUnavailableError, match="no priced part"):
        pricing._calculate(replace(data, tiers=()), at(hour=10), at(hour=12), 1)


def test_the_choices_say_which_containers_have_payment(made: Example) -> None:
    free = without_payment(made)
    found = booking_choices(made.member, now=NOW)
    (asset,) = found.assets
    (kind,) = asset.types
    assert [
        (row.id, row.number, row.with_payment, row.category_ids)
        for row in kind.containers
    ] == [
        (made.config.pk, made.config.number, True, (made.category.pk,)),
        (free.pk, free.number, False, (made.category.pk,)),
    ]


def test_an_order_without_payment_is_confirmed_without_vipps(made: Example) -> None:
    free = without_payment(made)
    order = checkout(made, free_swim(made, free))
    assert (order.total, order.hold_expires_at) == (0, None)
    booking = Booking.objects.of(made.org).select_related("taken_time").get()
    assert booking.booking_configuration_id == free.pk
    assert booking.taken_time.state == BookingState.CONFIRMED
    assert not BookingLine.objects.of(made.org).exists()
    assert (
        payments.checkout(member=made.member, order_public_id=order.public_id) is None
    )
    assert not Payment.objects.of(made.org).exists()


def test_a_cart_of_free_bookings_is_confirmed_at_once(made: Example) -> None:
    free = without_payment(made)
    session = SessionStore()
    wanted = Choice(free.pk, made.category.pk, 1, at(hour=15), at(hour=16))
    assert isinstance(booking_cart.add(session, made.member, wanted, 0, NOW), CartRow)
    placed = booking_checkout.order(
        session, made.member, 0, NOW, accepted_version=made_up.accepted(made.member)
    )
    assert isinstance(placed, Placed) and placed.total == 0
    assert Booking.objects.of(made.org).get().booking_configuration_id == free.pk
    assert list(TakenTime.objects.of(made.org).values_list("state", flat=True)) == [
        BookingState.CONFIRMED
    ]
    cart = booking_cart.look(session, made.member, NOW)
    assert cart.items == () and cart.order is not None
    assert cart.order.state == "confirmed"


def paid_and_free(
    made: Example,
    money: Money,
    free: BookingConfiguration,
    *,
    paid_day: date = DAY,
    free_day: date = DAY,
) -> Payment:
    """One cart: a paid swim 10:00 to 12:00 (200) and a free one 15:00 to
    16:00, each on its day; paid and captured."""
    paid = request(made, starts_at=at(paid_day, hour=10), ends_at=at(paid_day, hour=12))
    order = checkout(made, paid, free_swim(made, free, free_day))
    assert order.total == 200 and order.hold_expires_at is not None
    assert set(TakenTime.objects.of(made.org).values_list("state", flat=True)) == {
        BookingState.HELD
    }
    payments.checkout(member=made.member, order_public_id=order.public_id, now=NOW)
    payment = Payment.objects.of(made.org).get(order=order)
    money.snapshots[payment.provider_reference] = PaymentStatus(
        payment.provider_reference, "AUTHORIZED", order.total, order.total, 0, 0, 0
    )
    callbacks.request_reconciliation(made.org, order.public_id, now=NOW)
    reconciliation.reconcile_requested(now=NOW)
    payment.refresh_from_db()
    return payment


def test_a_mixed_cart_is_held_and_paid_for_its_paid_lines(
    made: Example, provider: Provider, money: Money
) -> None:
    free = without_payment(made)
    payment = paid_and_free(made, money, free)
    lines = BookingLine.objects.of(made.org)
    assert {line.booking.booking_configuration_id for line in lines} == {made.config.pk}
    assert payment.amount == sum(line.total for line in lines) == 200
    assert payment.state == "captured" and payment.captured_amount == 200
    assert set(TakenTime.objects.of(made.org).values_list("state", flat=True)) == {
        BookingState.CONFIRMED
    }


def test_refunds_count_only_the_paid_lines(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    free = without_payment(made)
    payment = paid_and_free(made, money, free)
    bookings = {
        row.booking_configuration_id: row for row in Booking.objects.of(made.org)
    }
    with pytest.raises(PaymentUnavailableError, match="No refundable amount"):
        request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=uuid.uuid4(),
            booking_public_id=bookings[free.pk].public_id,
            now=NOW,
        )
    request_refund(
        administrator=administrator,
        order_public_id=payment.order.public_id,
        request_key=uuid.uuid4(),
        booking_public_id=bookings[made.config.pk].public_id,
        now=NOW,
    )
    assert list(Refund.objects.of(made.org).values_list("amount", flat=True)) == [200]


def test_retention_retires_nothing_for_a_free_booking(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    """The free swim is two years before the paid one: retention removes it
    first, while the paid booking keeps the order and its payment."""
    free = without_payment(made)
    payment = paid_and_free(made, money, free, paid_day=DAY + timedelta(days=730))
    purged = purge_organization(made.org, now=at(DAY + timedelta(days=365 * 3 + 30)))
    assert purged["bookings"] == 1 and purged["orders"] == 0
    payment.refresh_from_db()
    assert payment.retired_booking_amount == 0
    # The paid booking's refund still reconciles with the payment.
    request_refund(
        administrator=administrator,
        order_public_id=payment.order.public_id,
        request_key=uuid.uuid4(),
        now=NOW,
    )
    assert list(Refund.objects.of(made.org).values_list("amount", flat=True)) == [200]


def test_retention_retires_the_paid_lines_when_the_free_booking_outlives_them(
    made: Example, provider: Provider, money: Money, administrator: Member
) -> None:
    """The other way round: the paid swim is past and removed first, while
    the free one two years later keeps the order and its payment."""
    free = without_payment(made)
    payment = paid_and_free(made, money, free, free_day=DAY + timedelta(days=730))
    purged = purge_organization(made.org, now=at(DAY + timedelta(days=365 * 3 + 30)))
    assert purged["bookings"] == 1 and purged["orders"] == 0
    (left,) = Booking.objects.of(made.org)
    assert left.booking_configuration_id == free.pk
    assert not BookingLine.objects.of(made.org).exists()
    payment.refresh_from_db()
    assert (payment.retired_booking_amount, payment.retired_refund_amount) == (200, 0)
    # The free booking has nothing to refund; the order still reconciles,
    # its retired paid lines with the payment.
    with pytest.raises(PaymentUnavailableError, match="No refundable amount"):
        request_refund(
            administrator=administrator,
            order_public_id=payment.order.public_id,
            request_key=uuid.uuid4(),
            booking_public_id=left.public_id,
            now=NOW,
        )
    request_refund(
        administrator=administrator,
        order_public_id=payment.order.public_id,
        request_key=uuid.uuid4(),
        now=NOW,
    )
    assert list(Refund.objects.of(made.org).values_list("amount", flat=True)) == [200]


def test_the_member_export_shows_a_free_booking_without_lines(
    made: Example, administrator: Member
) -> None:
    free = without_payment(made)
    checkout(made, free_swim(made, free))
    exported = export_member(
        actor=administrator, member_id=made.member.pk, **fresh(administrator)
    )
    (booking,) = exported["bookings"]
    assert booking["booking_configuration__number"] == free.number
    assert booking["taken_time__state"] == BookingState.CONFIRMED
    assert exported["booking_lines"] == [] and exported["payments"] == []
    (order,) = exported["orders"]
    assert order["total"] == 0


def test_no_price_entry_for_a_container_without_payment(made: Example) -> None:
    free = without_payment(made)
    entry = PriceBookEntry(
        organization=made.org,
        price_book=made.price.price_book,
        booking_configuration=free,
        booking_category=made.category,
        tiers_go_by="length",
    )
    with pytest.raises(ValidationError, match="without payment"):
        entry.full_clean()


def test_a_price_written_past_the_application_charges_nothing(made: Example) -> None:
    """An entry for a container without payment written straight to the
    database, past `clean`, is never read: no price applies (W12)."""
    free = without_payment(made)
    stray = PriceBookEntry.objects.create(
        organization=made.org,
        price_book=made.price.price_book,
        booking_configuration=free,
        booking_category=made.category,
        tiers_go_by="length",
    )
    PriceTier.objects.create(
        organization=made.org,
        price_book_entry=stray,
        up_to=240,
        paid_per=60,
        amount=100,
    )
    assert offered_categories(made.org.pk, made.member.role_id, [free]) == {
        free.pk: (made.category.pk,)
    }
    found = quote(
        member=made.member,
        configuration_id=free.pk,
        booking_category_id=made.category.pk,
        quantity=1,
        starts_at=at(hour=15),
        ends_at=at(hour=16),
        now=NOW,
    )
    assert found.configuration_id == free.pk
    assert (found.lines, found.total) == ((), 0)
    order = checkout(made, free_swim(made, free))
    assert (order.total, order.hold_expires_at) == (0, None)
    assert not BookingLine.objects.of(made.org).exists()
    assert not Payment.objects.of(made.org).exists()


DROPPED = "It cannot be without payment while it has prices: remove its prices first."


def test_dropping_the_price_book_is_refused_while_the_container_has_prices(
    made: Example,
) -> None:
    config = made.config
    book = config.price_book
    assert book is not None
    config.price_book = None
    with pytest.raises(ValidationError) as refused:
        config.save()
    assert refused.value.message_dict == {"price_book": [DROPPED]}
    config.refresh_from_db()
    assert config.price_book_id == book.pk
    # Without its prices it may drop the book, and is free from then on.
    PriceBookEntry.objects.of(made.org).filter(booking_configuration=config).delete()
    config.price_book = None
    config.save()
    price = price_booking(
        configuration=config,
        booking_category=made.category,
        starts_at=at(hour=10),
        ends_at=at(hour=12),
    )
    assert (price.lines, price.total) == ((), 0)


def test_a_free_container_may_be_given_a_price_book(made: Example) -> None:
    free = without_payment(made)
    free.price_book = made.config.price_book
    free.save()
    free.refresh_from_db()
    assert free.with_payment
    # With payment and no price yet, it is not offered (rule 12).
    assert offered_categories(made.org.pk, made.member.role_id, [free]) == {}


LAUNDRY = (SETUPS / "scenarios" / "laundry.toml").read_text()
WASHER_A_PRICE = """[[price_books.entries]]
booking_configuration = 1
booking_category = "private"
tiers_go_by = "length"
billing_basis = "per_booking"
tiers = [
  { up_to = "8 h", paid_per = "1 h", amount = 20 },
]

"""


def free_washer(text: str = LAUNDRY, *, keep_price: bool = False) -> str:
    """The laundry with Washer A's container without payment."""
    head, rest = text.split('number = 1\nasset = "Washer A"', 1)
    rest = rest.replace('price_book = "Prices"\n', "", 1)
    text = head + 'number = 1\nasset = "Washer A"' + rest
    if not keep_price:
        assert WASHER_A_PRICE in text
        text = text.replace(WASHER_A_PRICE, "", 1)
    return text


def test_a_format_three_file_may_leave_the_price_book_out() -> None:
    setup = f.parse(free_washer())
    (washer,) = [c for c in setup.booking_configurations if c.number == 1]
    assert washer.price_book == ""
    org = setups.load(setup, today=date(2027, 3, 1))
    stored = BookingConfiguration.objects.of(org).get(number=1)
    assert stored.price_book is None and not stored.with_payment
    assert BookingConfiguration.objects.of(org).get(number=2).with_payment
    written = f.write(setups.export(org))
    assert f.parse(written).booking_configurations == setup.booking_configurations
    washer_block = written.split('asset = "Washer A"', 1)[1].split("[[", 1)[0]
    assert "price_book" not in washer_block


def test_a_price_for_a_container_without_payment_is_refused_in_the_file() -> None:
    with pytest.raises(f.SetupError, match="configuration 1 is without payment"):
        f.parse(free_washer(keep_price=True))


def test_format_one_still_needs_a_price_book() -> None:
    text = (SETUPS / "example.toml").read_text()
    assert 'price_book = "Standard"\n' in text
    with pytest.raises(f.SetupError, match=r"price_book: is missing"):
        f.parse(text.replace('price_book = "Standard"\n', "", 1))


def test_undoing_the_migration_is_refused_while_a_container_is_without_payment(
    made: Example,
) -> None:
    free = without_payment(made)
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="without payment"):
            MigrationExecutor(connection).migrate([("core", "0017_rule_by_spot")])
        free.refresh_from_db()
        assert free.price_book_id is None
    finally:
        MigrationExecutor(connection).migrate(latest)
