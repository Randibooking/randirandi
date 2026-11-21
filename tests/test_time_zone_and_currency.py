# SPDX-License-Identifier: AGPL-3.0-only
"""a currency on the price book, a time zone on the asset.

A made-up harbour deck in America/New_York beside an organization in
Europe/Oslo, across the day the United States changes its clocks (Sunday 14
March 2027; Oslo changes on 28 March), and a price book in EUR beside one in
NOK.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import Client

from randirandi import limits
from randirandi.core import booking_cart, booking_outcome, payment_receipt, payments
from randirandi.core.booking_cart import CartRow, Refusal
from randirandi.core.booking_emails import (
    CURRENCY_WORD,
    money_words,
    send_confirmations,
)
from randirandi.core.booking_quote import Choice, cart_quotes, quote
from randirandi.core.models import (
    Asset,
    AvailabilityCalendar,
    BookingConfiguration,
    ConfigurationCategory,
    Currency,
    Order,
    Organization,
    Payment,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Space,
    TakenTime,
)
from randirandi.core.ordering import (
    BookingRequest,
    OrderUnavailableError,
    create_order,
)
from randirandi.core.pricing import price_booking
from tests import made_up
from tests.capacity_app import application_login
from tests.payment_wire import Wire
from tests.payment_wire import wire as wire
from tests.test_booking_checkout import send
from tests.test_calendar import DAY, Example, at, times
from tests.test_calendar import made as made
from tests.test_login_sessions import get, sign_in
from tests.test_member_bookings import settle
from tests.test_payment_callbacks import page_settings as page_settings
from tests.test_payments import REDIRECT, Provider
from tests.test_payments import provider as provider
from tests.test_pricing import tiers

pytestmark = pytest.mark.django_db(transaction=True)

NEW_YORK = ZoneInfo("America/New_York")
SATURDAY = date(2027, 3, 13)
SUNDAY = date(2027, 3, 14)
BEFORE = at(SATURDAY - timedelta(days=3))


def deck_time(day: date, hour: int) -> datetime:
    """A moment on the harbour deck's wall clock."""
    return datetime.combine(day, time(hour), NEW_YORK).astimezone(UTC)


def euro_deck(made: Example, *, per_hour: int = 1250) -> Example:
    """A harbour deck in New York with a container (10:00 to 14:00, one to
    four hours) priced per hour in a book of its own in EUR."""
    asset = Asset.objects.create(
        organization=made.org, name="Harbor Deck", time_zone="America/New_York"
    )
    book = PriceBook.objects.create(
        organization=made.org, name="Euro prices", currency="EUR"
    )
    config = BookingConfiguration.objects.create(
        organization=made.org,
        asset=asset,
        booking_type=made.kind,
        period_type="flexible",
        period_start=600,
        period_end=840,
        end_day=0,
        period_start_weekdays=list(range(7)),
        interval=60,
        minimum_length=60,
        maximum_length=240,
        status="enabled",
        price_book=book,
    )
    ConfigurationCategory.objects.create(
        organization=made.org,
        booking_configuration=config,
        booking_category=made.category,
    )
    entry = PriceBookEntry.objects.create(
        organization=made.org,
        price_book=book,
        booking_configuration=config,
        booking_category=made.category,
        tiers_go_by="length",
    )
    PriceTier.objects.create(
        organization=made.org,
        price_book_entry=entry,
        up_to=240,
        paid_per=60,
        amount=per_hour,
    )
    availability = AvailabilityCalendar.objects.create(
        organization=made.org, asset=asset, start_date=date(2020, 1, 1)
    )
    deck = Example(
        made.org,
        made.member,
        asset,
        made.kind,
        config,
        availability,
        made.category,
        entry,
    )
    deck.available([(0, 1440)])
    return deck


def ask(deck: Example, start: datetime, end: datetime) -> BookingRequest:
    return BookingRequest(deck.config.pk, deck.category.pk, start, end)


def choice(deck: Example, start: datetime, end: datetime) -> Choice:
    return Choice(deck.config.pk, deck.category.pk, 1, start, end)


def sql(query: str, *values: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(query, values)


# --- The asset's time zone -----------------------------------------------------


def test_an_asset_made_without_a_zone_takes_the_organizations_and_keeps_its_own(
    made: Example,
) -> None:
    assert made.asset.time_zone == "Europe/Oslo"
    Organization.objects.filter(pk=made.org.pk).update(time_zone="Europe/London")
    made.asset.refresh_from_db()
    assert made.asset.time_zone == "Europe/Oslo"
    fresh = Asset.objects.create(organization=made.org, name="Fresh")
    assert fresh.time_zone == "Europe/London"
    assert made.asset.zone == ZoneInfo("Europe/Oslo")


def test_a_zone_the_service_does_not_know_is_refused_as_the_organizations_is(
    made: Example,
) -> None:
    asset = Asset(organization=made.org, name="Nowhere Hut", time_zone="Europe/Nowhere")
    with pytest.raises(ValidationError) as caught:
        asset.full_clean()
    assert caught.value.message_dict == {
        "time_zone": ["Not a time zone the service knows."]
    }
    made.org.time_zone = "Europe/Nowhere"
    with pytest.raises(ValidationError) as organization:
        made.org.full_clean()
    made.org.time_zone = "Europe/Oslo"
    assert (
        organization.value.message_dict["time_zone"]
        == (caught.value.message_dict["time_zone"])
    )
    # The database holds the other half: an empty zone is no zone.
    with pytest.raises(IntegrityError, match="asset_has_a_time_zone"):
        Asset.objects.of(made.org).filter(pk=made.asset.pk).update(time_zone="")


@pytest.mark.parametrize("state", ["confirmed", "held", "blocked"])
def test_the_zone_stays_while_a_booking_or_a_block_is_still_to_come(
    made: Example, state: str
) -> None:
    made.claim(at(hour=10), at(hour=12), state=state)
    made.asset.time_zone = "America/New_York"
    with pytest.raises(ValidationError) as caught:
        made.asset.save(now=at(DAY - timedelta(days=1)))
    assert caught.value.message_dict == {
        "time_zone": [
            "Pool has a booking or a block, also a past one, so its time zone stays "
            "as it is; make a new asset in that zone instead."
        ]
    }
    assert Asset.objects.of(made.org).get(pk=made.asset.pk).time_zone == "Europe/Oslo"


def test_the_database_holds_the_zone_still_once_the_asset_has_any_claim(
    made: Example,
) -> None:
    claim = made.claim(at(hour=10), at(hour=12))
    assert claim.time_zone == "Europe/Oslo"
    # The model says why, also for a claim that is over: its times were read
    # in this zone.
    made.asset.time_zone = "America/New_York"
    with pytest.raises(ValidationError) as caught:
        made.asset.save(now=at(DAY + timedelta(days=30)))
    assert "also a past one" in caught.value.message_dict["time_zone"][0]
    # A write that goes round the model meets the key.
    with pytest.raises(IntegrityError, match="takentime_asset_time_zone"):
        sql("UPDATE core_asset SET time_zone = 'UTC' WHERE id = %s", made.asset.pk)
    # So does a claim written with another copy.
    with pytest.raises(IntegrityError, match="takentime_asset_time_zone"):
        sql("UPDATE core_takentime SET time_zone = 'UTC' WHERE id = %s", claim.pk)
    TakenTime.objects.of(made.org).filter(pk=claim.pk).delete()
    sql("UPDATE core_asset SET time_zone = 'UTC' WHERE id = %s", made.asset.pk)


def test_an_asset_without_claims_changes_its_zone_and_its_new_claims_copy_it(
    made: Example,
) -> None:
    made.asset.time_zone = "America/New_York"
    made.asset.save()
    assert made.claim(at(hour=10), at(hour=12)).time_zone == "America/New_York"


def test_the_assets_of_a_space_share_one_zone(made: Example) -> None:
    room = Space.objects.create(organization=made.org, name="Boathouse")
    made.asset.space = room
    made.asset.save()
    deck = Asset(organization=made.org, name="Harbor Deck", space=room, time_zone="UTC")
    with pytest.raises(ValidationError) as caught:
        deck.save()
    assert caught.value.message_dict == {
        "time_zone": [
            "Pool in the same space is in Europe/Oslo, and the assets of a space "
            "share one time zone; to change it, take the assets out of the "
            "space, change each, and put them back."
        ]
    }
    # Made in the space's zone, or moved to it, it is fine; in another zone it
    # may not move in, and the first may not leave the others.
    deck = Asset.objects.create(
        organization=made.org, name="Harbor Deck", time_zone="UTC"
    )
    deck.space = room
    with pytest.raises(ValidationError, match="share one time zone"):
        deck.save()
    deck.time_zone = "Europe/Oslo"
    deck.save()
    deck.time_zone = "UTC"
    with pytest.raises(ValidationError, match="share one time zone"):
        deck.save()


def test_the_application_login_may_change_an_assets_zone_and_never_a_claims() -> None:
    org = made_up.organization()
    deck = Asset.objects.create(organization=org, name="Harbor Deck")
    with application_login():
        deck.time_zone = "America/New_York"
        deck.save()
        with pytest.raises(Exception, match="permission denied"):
            sql("UPDATE core_takentime SET time_zone = 'UTC'")
    assert Asset.objects.of(org).get(pk=deck.pk).time_zone == "America/New_York"


# --- The price book's currency -------------------------------------------------


def test_a_book_made_without_a_currency_takes_the_organizations(made: Example) -> None:
    assert (
        PriceBook.objects.of(made.org).get(pk=made.config.price_book_id or 0).currency
        == "NOK"
    )
    Organization.objects.filter(pk=made.org.pk).update(currency="EUR")
    assert PriceBook.objects.create(organization=made.org, name="Fresh").currency == (
        "EUR"
    )
    kept = PriceBook.objects.create(
        organization=made.org, name="Krone prices", currency="NOK"
    )
    assert kept.currency == "NOK"
    assert (
        PriceBook.objects.of(made.org).get(pk=made.config.price_book_id or 0).currency
        == "NOK"
    )


def test_the_currencies_are_the_ones_the_payment_client_takes(made: Example) -> None:
    assert tuple(Currency.values) == limits.CURRENCIES
    book = PriceBook(organization=made.org, name="Pound prices", currency="GBP")
    with pytest.raises(ValidationError) as caught:
        book.full_clean()
    assert "currency" in caught.value.message_dict
    with pytest.raises(IntegrityError, match="pricebook_currency_is_known"):
        PriceBook.objects.create(
            organization=made.org, name="Pound prices", currency="GBP"
        )
    for currency in Currency.values:
        assert (
            PriceBook.objects.create(
                organization=made.org, name=f"{currency} prices", currency=currency
            ).currency
            == currency
        )


def test_the_currency_stays_once_the_book_has_an_entry(made: Example) -> None:
    book = PriceBook.objects.of(made.org).get(pk=made.config.price_book_id or 0)
    book.currency = "EUR"
    with pytest.raises(ValidationError) as caught:
        book.save()
    assert caught.value.message_dict == {
        "currency": [
            "It has a price, so its currency stays as it is; make another price "
            "book for the other currency."
        ]
    }
    assert made.price.currency == "NOK"
    # Raw writes meet the key, on either side.
    with pytest.raises(IntegrityError, match="pricebookentry_book_currency"):
        sql("UPDATE core_pricebook SET currency = 'EUR' WHERE id = %s", book.pk)
    with pytest.raises(IntegrityError, match="pricebookentry_book_currency"):
        sql(
            "UPDATE core_pricebookentry SET currency = 'EUR' WHERE id = %s",
            made.price.pk,
        )
    # A book without an entry changes its currency.
    empty = PriceBook.objects.create(organization=made.org, name="Empty prices")
    empty.currency = "DKK"
    empty.save()
    assert PriceBook.objects.of(made.org).get(pk=empty.pk).currency == "DKK"


def test_an_entry_copies_its_books_currency(made: Example) -> None:
    deck = euro_deck(made)
    assert deck.price.currency == "EUR"
    assert PriceBookEntry.objects.of(made.org).get(pk=made.price.pk).currency == "NOK"


# --- Every reader follows ------------------------------------------------------


def test_the_calendar_reads_the_asset_in_its_zone_across_a_clock_change(
    made: Example,
) -> None:
    deck = euro_deck(made)
    for day in (SATURDAY, SUNDAY):
        assert times(deck.offers(day, now=BEFORE)) == {
            (deck_time(day, a), deck_time(day, a + n))
            for a in range(10, 14)
            for n in range(1, 15 - a)
        }
    # The clocks changed in between: 10:00 is 15:00 UTC, then 14:00.
    assert deck_time(SATURDAY, 10).hour == 15 and deck_time(SUNDAY, 10).hour == 14
    # Oslo's own asset is still read on Oslo's clock, on the same day.
    assert times(made.offers(SUNDAY, now=BEFORE)) == {
        (at(SUNDAY, a), at(SUNDAY, a + n))
        for a in range(10, 14)
        for n in range(1, 15 - a)
    }


def test_a_price_by_time_of_day_is_read_on_the_assets_clock(made: Example) -> None:
    deck = euro_deck(made)
    # Up to 12:00 on the deck's clock 5.00 an hour, then 15.00 an hour.
    tiers(deck, [(720, 60, 500), (840, 60, 1500)], by="time of day")
    result = price_booking(
        configuration=deck.config,
        booking_category=deck.category,
        starts_at=deck_time(SUNDAY, 11),
        ends_at=deck_time(SUNDAY, 13),
    )
    assert [(line.up_to, line.amount) for line in result.lines] == [
        (720, 500),
        (840, 1500),
    ]
    assert result.total == 2000


def test_the_validator_names_a_booking_in_the_assets_clock(made: Example) -> None:
    deck = euro_deck(made)
    create_order(
        member=made.member,
        requests=[ask(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))],
        now=BEFORE,
        accepted_version=made_up.accepted(made.member),
    )
    deck.config.period_start = 660
    with pytest.raises(ValidationError) as caught:
        deck.config.save(now=BEFORE)
    # 10:00 on the deck's clock, not 15:00 on Oslo's.
    assert "from Sunday 14 March 2027 at 10:00" in str(caught.value.message_dict)


def test_the_confirmation_shows_the_booking_in_its_zone_and_currency(
    made: Example,
) -> None:
    deck = euro_deck(made)
    order = create_order(
        member=made.member,
        requests=[ask(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))],
        now=BEFORE,
        accepted_version=made_up.accepted(made.member),
    )
    assert order.total == 2500
    settle(order)
    Order.objects.of(made.org).filter(pk=order.pk).update(confirmation_due_at=BEFORE)
    mail.outbox.clear()
    send_confirmations(now=BEFORE + timedelta(seconds=5))
    (message,) = mail.outbox
    assert f"Reference {order.reference}\nPaid 25 \N{EURO SIGN}" in message.body
    assert "10:00" in message.body and "15:00" not in message.body


def test_the_receipt_and_the_outcome_follow_the_asset_and_the_book(
    made: Example,
) -> None:
    deck = euro_deck(made)
    order = create_order(
        member=made.member,
        requests=[ask(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))],
        now=BEFORE,
        accepted_version=made_up.accepted(made.member),
    )
    description, receipt = payment_receipt.build(made.org, order)
    assert "10:00" in description
    assert json.loads(receipt)["bottomLine"]["currency"] == "EUR"
    found = booking_outcome.read(made.org, made.member, order.public_id, now=BEFORE)
    assert found is not None
    (booking,) = found["bookings"]
    assert booking["currency"] == "EUR"
    assert booking["start"] == "2027-03-14T10:00:00-04:00"


def test_an_order_of_free_lines_in_a_euro_book_is_in_euros(made: Example) -> None:
    deck = euro_deck(made, per_hour=0)
    order = create_order(
        member=made.member,
        requests=[ask(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))],
        now=BEFORE,
        accepted_version=made_up.accepted(made.member),
    )
    assert order.total == 0
    found = booking_outcome.read(made.org, made.member, order.public_id, now=BEFORE)
    assert found is not None
    assert found["currency"] == "EUR"


def test_the_cart_shows_the_hold_end_on_the_first_bookings_clock(
    made: Example, wire: Wire
) -> None:
    deck = euro_deck(made)
    client = Client(enforce_csrf_checks=True)
    sign_in(client, made.member)
    start = deck_time(SUNDAY, 10)
    booking = {
        "container": deck.config.pk,
        "category": deck.category.pk,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
        "total": 2500,
    }
    assert send(client, "/booking/cart/add/", booking).status_code == 200
    assert (
        send(client, "/booking/cart/order/", made_up.order_body(2500)).status_code
        == 200
    )
    cart = json.loads(get(client, "/booking/cart/").content)
    assert cart["currency"] == "EUR"
    held = cart["order"]
    hold = Order.objects.of(made.org).get().hold_expires_at
    assert hold is not None
    shown = datetime.fromisoformat(held["hold_expires_at"])
    assert shown == hold
    assert shown.utcoffset() == hold.astimezone(NEW_YORK).utcoffset()
    assert shown.utcoffset() != hold.astimezone(ZoneInfo("Europe/Oslo")).utcoffset()


# --- The cart and the order ----------------------------------------------------


def test_a_cart_takes_paid_lines_in_one_currency(made: Example) -> None:
    deck = euro_deck(made)
    kroner = choice(made, at(SUNDAY, 10), at(SUNDAY, 12))
    euros = choice(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))
    session = SessionStore()
    assert isinstance(
        booking_cart.add(session, made.member, kroner, 200, BEFORE), CartRow
    )
    assert booking_cart.add(session, made.member, euros, 2500, BEFORE) == Refusal(
        "other-currency"
    )
    assert len(booking_cart.rows(session, made.member)) == 1
    assert booking_cart.look(session, made.member, BEFORE).currency == "NOK"
    beside = quote(
        member=made.member,
        configuration_id=euros.configuration_id,
        booking_category_id=euros.booking_category_id,
        quantity=1,
        starts_at=euros.starts_at,
        ends_at=euros.ends_at,
        now=BEFORE,
        cart=[kroner],
    )
    assert beside.reason == "other-currency"


def test_the_whole_cart_check_and_the_order_refuse_the_same(made: Example) -> None:
    deck = euro_deck(made)
    kroner = choice(made, at(SUNDAY, 10), at(SUNDAY, 12))
    euros = choice(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))
    first, second = cart_quotes(member=made.member, cart=[kroner, euros], now=BEFORE)
    assert (first.reason, first.currency) == (None, "NOK")
    assert second.reason == "other-currency"
    # The first paid booking decides.
    first, second = cart_quotes(member=made.member, cart=[euros, kroner], now=BEFORE)
    assert (first.reason, first.currency) == (None, "EUR")
    assert second.reason == "other-currency"
    with pytest.raises(OrderUnavailableError, match="one currency"):
        create_order(
            member=made.member,
            requests=[
                ask(made, kroner.starts_at, kroner.ends_at),
                ask(deck, euros.starts_at, euros.ends_at),
            ],
            now=BEFORE,
            accepted_version=made_up.accepted(made.member),
        )
    assert not Order.objects.of(made.org).exists()


def test_a_free_booking_has_no_currency_and_goes_with_any(made: Example) -> None:
    deck = euro_deck(made)
    # The Oslo container is without payment: no book, no prices.
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).delete()
    PriceBookEntry.objects.of(made.org).filter(pk=made.price.pk).delete()
    BookingConfiguration.objects.of(made.org).filter(pk=made.config.pk).update(
        price_book=None
    )
    order = create_order(
        member=made.member,
        requests=[
            ask(made, at(SUNDAY, 10), at(SUNDAY, 12)),
            ask(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12)),
        ],
        now=BEFORE,
        accepted_version=made_up.accepted(made.member),
    )
    assert order.total == 2500


def test_the_payment_takes_the_currency_of_the_orders_book(
    made: Example, provider: Provider
) -> None:
    deck = euro_deck(made)
    order = create_order(
        member=made.member,
        requests=[ask(deck, deck_time(SUNDAY, 10), deck_time(SUNDAY, 12))],
        now=BEFORE,
        accepted_version=made_up.accepted(made.member),
    )
    # The organization's own currency is only a default.
    assert made.org.currency == "NOK"
    with application_login():
        first = payments.checkout(
            member=made.member, order_public_id=order.public_id, now=BEFORE
        )
    assert first is not None and first.redirect_url == REDIRECT
    payment = Payment.objects.of(made.org).get(order=order)
    assert (payment.currency, payment.amount) == ("EUR", 2500)
    assert provider.calls[0]["currency"] == "EUR"
    # A payment cannot lose its currency.
    with pytest.raises(IntegrityError, match="payment_currency_is_known"):
        sql("UPDATE core_payment SET currency = '' WHERE id = %s", payment.pk)


# --- Words ---------------------------------------------------------------------

# What the page's `moneyWords` (Intl, nb-NO) gives, read with node 25: the
# same amounts in each currency (frontend/src/book/add.js).
PAGE_WORDS = {
    "NOK": {
        0: "0 kr",
        5: "0,05 kr",
        99: "0,99 kr",
        100: "1 kr",
        1005: "10,05 kr",
        12550: "125,50 kr",
        125000: "1 250 kr",
        123456789: "1 234 567,89 kr",
    },
    "DKK": {
        0: "0 DKK",
        5: "0,05 DKK",
        99: "0,99 DKK",
        100: "1 DKK",
        1005: "10,05 DKK",
        12550: "125,50 DKK",
        125000: "1 250 DKK",
        123456789: "1 234 567,89 DKK",
    },
    "EUR": {
        0: "0 \N{EURO SIGN}",
        5: "0,05 \N{EURO SIGN}",
        99: "0,99 \N{EURO SIGN}",
        100: "1 \N{EURO SIGN}",
        1005: "10,05 \N{EURO SIGN}",
        12550: "125,50 \N{EURO SIGN}",
        125000: "1 250 \N{EURO SIGN}",
        123456789: "1 234 567,89 \N{EURO SIGN}",
    },
}


def test_the_emails_write_money_as_the_page_does() -> None:
    assert set(CURRENCY_WORD) == set(PAGE_WORDS) == set(limits.CURRENCIES)
    for currency, words in PAGE_WORDS.items():
        for amount, written in words.items():
            assert money_words(amount, currency) == written
    with pytest.raises(ValueError, match="No words for the currency GBP"):
        money_words(100, "GBP")


def test_the_defaults_stay_on_the_organization(made: Example) -> None:
    assert (made.org.time_zone, made.org.currency) == ("Europe/Oslo", "NOK")
    _ = (TakenTime, Space)
