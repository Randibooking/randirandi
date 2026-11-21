# SPDX-License-Identifier: AGPL-3.0-only
"""The price list and the booking conditions .

Every organization has conditions from its first day; no order is placed
without accepting the text in force; the accepted text is frozen with the
order; two public pages and every footer link them.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import django.db.utils
import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from randirandi import setup_file
from randirandi.core import (
    booking_admin,
    booking_cart,
    booking_checkout,
    booking_conditions,
    booking_outcome,
)
from randirandi.core import setup as setups
from randirandi.core.booking_cart import CartRow
from randirandi.core.booking_emails import send_confirmations
from randirandi.core.booking_quote import Choice
from randirandi.core.member_data import export_member
from randirandi.core.models import (
    BookingConditions,
    Member,
    Order,
    Organization,
    PriceTier,
)
from randirandi.core.ordering import OrderConditionsError, create_order
from randirandi.core.retention import purge_organization
from tests import made_up
from tests.capacity_app import application_login
from tests.fresh_code import fresh
from tests.operator_erasure import erase
from tests.test_booking_checkout import fill_page, send
from tests.test_booking_emails import text_of
from tests.test_calendar import DAY, Example, at
from tests.test_calendar import made as made
from tests.test_login_sessions import HOST, config_of, get, sign_in
from tests.test_login_sessions import page_settings as page_settings
from tests.test_member_bookings import day, other
from tests.test_member_calendar import ids
from tests.test_ordering import NOW
from tests.test_setup_file import SMALLEST

pytestmark = pytest.mark.django_db(transaction=True)

OTHER_HOST = "elsewhere.sandbox.example.org"
CANCELLING = (
    "To cancel a booked time, email {contact}. If you send it at least {days} "
    "days before the time starts, you get back what you paid for it. After "
    "that there is no refund."
)


def current(made: Example) -> booking_conditions.Text:
    return booking_conditions.current(Organization.objects.get(pk=made.org.pk))


def free(made: Example) -> None:
    PriceTier.objects.of(made.org).filter(price_book_entry=made.price).update(amount=0)


def standard_role(made: Example) -> None:
    Organization.objects.filter(pk=made.org.pk).update(
        standard_role_id=made.member.role_id
    )


def before_conditions(made: Example, days: int = 1) -> Order:
    """A legacy order without recorded conditions: it points to no text."""
    order = accepted_order(made, days)
    Order.objects.of(made.org).filter(pk=order.pk).update(
        conditions=None, conditions_accepted_at=None
    )
    order.refresh_from_db()
    return order


def accepted_order(made: Example, days: int = 1, version: str | None = None) -> Order:
    return create_order(
        member=made.member,
        requests=[day(made, days)],
        now=NOW,
        accepted_version=made_up.accepted(made.org) if version is None else version,
    )


def admin_lines(made: Example, order: Order) -> list[dict[str, Any]]:
    page = booking_admin.booking(made.org, order)
    return cast(list[dict[str, Any]], page["lines"])


def browser(made: Example, member: Member | None = None) -> Client:
    client = Client(enforce_csrf_checks=True)
    assert sign_in(client, member or made.member).status_code == 302
    return client


def answer(response: Any) -> Any:
    return json.loads(response.content)


# --- The text ---


def test_every_organization_has_conditions_from_its_first_day(made: Example) -> None:
    text = current(made)
    assert text.version == booking_conditions.VERSION == 1
    assert text.days == 5
    assert (text.organization_name, text.contact_email) == (
        made.org.name,
        made.org.contact_email,
    )
    assert text.title == "Booking conditions"
    assert [heading for heading, _ in text.sections] == [
        "Payment",
        "Cancelling",
        "If we cancel",
        "Using the space",
        "Damage",
        "Changes",
        "Questions",
    ]
    assert text.intro == (
        f"These conditions apply to every booking you make with {made.org.name}. "
        "By booking you accept them, and the prices on the price list."
    )
    assert dict(text.sections)["Cancelling"] == CANCELLING.format(
        contact=made.org.contact_email, days=5
    )
    assert dict(text.sections)["Questions"] == f"Email {made.org.contact_email}."
    # No text holds a placeholder that was not filled in.
    assert "{" not in text.intro + "".join(h + t for h, t in text.sections)


def test_the_days_and_the_words_are_the_organizations_and_change_the_text(
    made: Example,
) -> None:
    before = current(made)
    Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=7)
    seven = current(made)
    assert "at least 7 days" in dict(seven.sections)["Cancelling"]
    assert seven.digest != before.digest
    assert current(made).digest == seven.digest
    Organization.objects.filter(pk=made.org.pk).update(
        labels={"conditions.damage": "You pay for what you break."}
    )
    own = current(made)
    assert dict(own.sections)["Damage"] == "You pay for what you break."
    assert own.digest not in (before.digest, seven.digest)


def test_an_organization_cannot_leave_its_days_outside_none_to_a_year(
    made: Example,
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=366)
    # None at all is a choice (r7): every cancelling then is without refund.
    Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=0)
    assert Organization.objects.get(pk=made.org.pk).free_cancellation_days == 0


def test_the_cancelling_rule_counts_days_of_24_hours_before_the_start() -> None:
    start = at(DAY, 10)
    assert booking_conditions.free_until(start, 5) == start - timedelta(hours=120)
    assert booking_conditions.free_until(start, 1) == start - timedelta(hours=24)


# --- The pages, signed out ---


def test_both_pages_read_without_signing_in(made: Example) -> None:
    standard_role(made)
    client = Client()
    conditions = get(client, "/conditions/")
    assert conditions.status_code == 200
    page = conditions.content.decode()
    # Without JavaScript the text is in the page itself.
    assert "<h1>Booking conditions</h1>" in page
    assert made.org.name in page and made.org.contact_email in page
    assert "at least 5 days before the time starts" in page
    assert config_of(conditions)["conditions"] == current(made).page()
    assert config_of(conditions)["screen"] == "conditions"
    prices = get(client, "/prices/")
    assert prices.status_code == 200 and "<h1>Prices</h1>" in prices.content.decode()
    assert config_of(prices)["screen"] == "prices-public"
    assert config_of(prices)["price_categories"] == [
        {"id": made.category.pk, "name": "Private", "icon": ""}
    ]


def test_the_pages_are_the_organization_of_the_address(made: Example) -> None:
    other_org = made_up.organization("elsewhere")
    Organization.objects.filter(pk=other_org.pk).update(
        free_cancellation_days=9, name="Elsewhere housing"
    )
    here = get(Client(), "/conditions/")
    there = get(Client(), "/conditions/", OTHER_HOST)
    assert made.org.name in here.content.decode()
    assert "Elsewhere housing" in there.content.decode()
    assert "at least 9 days" in there.content.decode()
    assert "at least 9 days" not in here.content.decode()
    assert config_of(there)["organization"] == "Elsewhere housing"
    # An address no organization has is not found; the pages never take an
    # organization from the query or a header.
    assert (
        get(Client(), "/conditions/", "nobody.sandbox.example.org").status_code == 404
    )
    forged = Client().get(
        "/conditions/?organization=elsewhere",
        HTTP_HOST=HOST,
        HTTP_X_FORWARDED_HOST=OTHER_HOST,
        secure=True,
    )
    assert made.org.name in forged.content.decode()


def test_the_public_price_list_holds_nothing_of_a_member(made: Example) -> None:
    standard_role(made)
    neighbour = other(made)
    for member in (made.member, neighbour):
        create_order(
            member=member,
            accepted_version=made_up.accepted(member),
            requests=[
                day(
                    made,
                    1,
                    starts_at=at(
                        DAY + timedelta(days=1), 10 + 2 * (member is neighbour)
                    ),
                    ends_at=at(DAY + timedelta(days=1), 12 + 2 * (member is neighbour)),
                )
            ],
            now=NOW,
        )
    query = (
        f"?asset={made.asset.pk}&category={made.category.pk}&start={DAY}"
        f"&days=14&prices=1"
    )
    signed_out = Client()
    choices = get(signed_out, "/prices/choices/")
    body = answer(choices)
    assert choices.status_code == 200
    assert [row["id"] for row in body["categories"]] == [made.category.pk]
    assert body["category"] == made.category.pk
    assert [row["name"] for row in body["assets"]] == ["Pool"]
    calendar = get(signed_out, "/prices/calendar/" + query)
    assert calendar.status_code == 200
    found = answer(calendar)
    assert found["containers"]
    periods = [p for row in found["containers"] for p in row["periods"]]
    assert periods and all(p["taken"] == [] for p in periods)
    assert all(row["prices"] for row in found["containers"])
    # Nothing of any member, in any of the three answers.
    everything_read = " ".join(
        get(signed_out, path).content.decode()
        for path in ("/prices/", "/prices/choices/", "/prices/calendar/" + query)
    )
    for member in (made.member, neighbour):
        for value in (member.email, member.first_name, member.last_name):
            assert value and value not in everything_read
    # The members' own answer for the same days does name the other member.
    signed_in = browser(made)
    own = answer(get(signed_in, "/booking/calendar/" + query))
    taken = [t for row in own["containers"] for p in row["periods"] for t in p["taken"]]
    assert any(t.get("mine") for t in taken)
    assert any(t.get("name") == "Zebedee Quill" for t in taken)
    assert (
        get(
            signed_out, "/prices/calendar/" + query.replace("days=14", "days=15")
        ).status_code
        == 422
    )


def test_the_public_price_list_is_empty_without_a_standard_role(made: Example) -> None:
    body = answer(get(Client(), "/prices/choices/"))
    assert body == {"categories": [], "category": None, "assets": [], "spaces": []}
    assert (
        get(Client(), "/prices/calendar/?asset=1&start=2027-03-01&days=1").status_code
        == 404
    )
    assert config_of(get(Client(), "/prices/"))["price_categories"] == []


def test_every_footer_links_the_two_pages_beside_documentation(
    made: Example,
) -> None:
    for path in ("/login/", "/prices/", "/conditions/"):
        page = get(Client(), path).content.decode()
        links = [
            page.index('href="/prices/"'),
            page.index('href="/conditions/"'),
            page.index("https://github.com/Randibooking/public"),
        ]
        assert links == sorted(links), path
        assert config_of(get(Client(), path))["footer_links"] is True
    signed_in = browser(made)
    for path in ("/book/", "/book/cart/", "/book/prices/", "/me/"):
        shown = get(signed_in, path)
        assert 'href="/prices/"' in shown.content.decode(), path
        assert config_of(shown)["footer_links"] is True
    # Admin's own pages are unchanged: Documentation only; and an error page,
    # which may have no organization, has no links.
    error = get(Client(), "/conditions/", "nobody.sandbox.example.org")
    assert 'href="/prices/"' not in error.content.decode()
    assert config_of(error)["footer_links"] is False


def test_the_admin_page_without_javascript_keeps_documentation_only(
    everything: made_up.MadeUp,
) -> None:
    client = Client()
    assert sign_in(client, everything.administrator).status_code in (200, 302)
    page = get(client, "/admin/").content.decode()
    assert 'href="/prices/"' not in page


# --- The gate ---


def test_the_cart_carries_the_conditions_in_force(made: Example) -> None:
    client = browser(made)
    fill_page(client, made)
    cart = answer(get(client, "/booking/cart/"))
    assert cart["conditions"] == {"version": current(made).digest}


def test_an_xx_organization_reads_and_freezes_its_conditions_in_xx(
    made: Example, second_language: None
) -> None:
    standard_role(made)
    english = current(made)
    Organization.objects.filter(pk=made.org.pk).update(language="xx")
    xx = current(made)
    assert xx.title == "Conditions xx"
    assert xx.intro == f"Terms xx for {made.org.name}."
    assert dict(xx.sections)["Damage"] == "Damage xx"
    assert xx.digest != english.digest
    # The public page reads it.
    page = get(Client(), "/conditions/")
    assert "<h1>Conditions xx</h1>" in page.content.decode()
    assert config_of(page)["conditions"] == xx.page()
    # An order freezes the xx text and its digest.
    order = accepted_order(made, 1)
    assert order.conditions_id is not None
    frozen = BookingConditions.objects.of(made.org).get(pk=order.conditions_id)
    assert (frozen.title, frozen.digest) == ("Conditions xx", xx.digest)
    assert frozen.intro == xx.intro


def test_an_xx_organization_is_refused_in_xx(
    made: Example, second_language: None
) -> None:
    Organization.objects.filter(pk=made.org.pk).update(language="xx")
    client = browser(made)
    fill_page(client, made)
    response = send(client, "/booking/cart/order/", {"total": 200})
    assert response.status_code == 409
    assert answer(response) == {
        "reason": "conditions-not-accepted",
        "text": "accept xx",
    }


def test_an_order_without_acceptance_is_refused_in_words(made: Example) -> None:
    client = browser(made)
    fill_page(client, made)
    for body, reason, text in (
        (
            {"total": 200},
            "conditions-not-accepted",
            "accept the booking conditions and the prices to book",
        ),
        (
            {"total": 200, "accepted_version": ""},
            "conditions-not-accepted",
            "accept the booking conditions and the prices to book",
        ),
        (
            {"total": 200, "accepted_version": 5},
            "conditions-not-accepted",
            "accept the booking conditions and the prices to book",
        ),
        (
            {"total": 200, "accepted_version": "0" * 64},
            "conditions-changed",
            "the booking conditions have changed: read them and accept again",
        ),
    ):
        response = send(client, "/booking/cart/order/", body)
        assert response.status_code == 409, body
        assert answer(response) == {"reason": reason, "text": text}
    # Nothing was ordered and the cart is as it was.
    assert Order.objects.of(made.org).count() == 0
    assert answer(get(client, "/booking/cart/"))["count"] == 1


def test_an_order_with_the_current_text_keeps_it_and_the_moment(
    made: Example,
) -> None:
    free(made)
    client = browser(made)
    fill_page(client, made, total=0)
    response = send(client, "/booking/cart/order/", made_up.order_body(0))
    assert response.status_code == 200 and answer(response)["confirmed"] is True
    order = Order.objects.of(made.org).select_related("conditions").get()
    assert order.conditions is not None and order.conditions_accepted_at is not None
    assert order.conditions.digest == current(made).digest
    assert booking_conditions.kept(order.conditions) == current(made)


def test_a_free_order_is_refused_without_acceptance_too(made: Example) -> None:
    free(made)
    client = browser(made)
    fill_page(client, made, total=0)
    response = send(client, "/booking/cart/order/", {"total": 0})
    assert response.status_code == 409
    assert answer(response)["reason"] == "conditions-not-accepted"
    assert Order.objects.of(made.org).count() == 0


def test_the_page_without_javascript_places_no_order(made: Example) -> None:
    # A plain form post, the way a page without JavaScript would send it: the
    # call takes JSON only, and nothing is ordered.
    client = browser(made)
    fill_page(client, made)
    response = client.post(
        "/booking/cart/order/",
        {"total": "200", "accepted_version": made_up.accepted(made.org)},
        HTTP_HOST=HOST,
        HTTP_ORIGIN="https://" + HOST,
        secure=True,
    )
    assert response.status_code in (400, 403, 415)
    assert Order.objects.of(made.org).count() == 0


def test_the_service_gate_refuses_before_any_booking_is_made(made: Example) -> None:
    session = SessionStore()
    start = at(DAY, 10)
    found = ids(made.org, made.asset.name, made.kind.name, "Private")
    wanted = Choice(
        found["configuration_id"],
        found["booking_category_id"],
        1,
        start,
        start + timedelta(hours=2),
        (),
    )
    assert isinstance(booking_cart.add(session, made.member, wanted, 200, NOW), CartRow)
    assert booking_checkout.order(
        session, made.member, 200, NOW, accepted_version=None
    ) == booking_cart.Refusal("conditions-not-accepted")
    assert booking_checkout.order(
        session, made.member, 200, NOW, accepted_version="f" * 64
    ) == booking_cart.Refusal("conditions-changed")
    placed = booking_checkout.order(
        session, made.member, 200, NOW, accepted_version=made_up.accepted(made.org)
    )
    assert isinstance(placed, booking_checkout.Placed)
    assert Order.objects.of(made.org).count() == 1


def test_the_order_checks_the_text_in_its_own_transaction(made: Example) -> None:
    # The text changes between the cart's look and the order: the old
    # identifier is refused by the order itself, and nothing is kept.
    shown = made_up.accepted(made.org)
    Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=3)
    with pytest.raises(OrderConditionsError) as refused:
        accepted_order(made, version=shown)
    assert refused.value.missing is False
    with pytest.raises(OrderConditionsError) as none:
        accepted_order(made, version="")
    assert none.value.missing is True
    assert Order.objects.of(made.org).count() == 0
    assert BookingConditions.objects.of(made.org).count() == 0


def test_every_order_carries_its_acceptance_and_none_is_made_without_it(
    made: Example,
) -> None:
    order = accepted_order(made)
    assert order.conditions_id is not None and order.conditions_accepted_at == NOW
    with pytest.raises(TypeError, match="accepted_version"):
        create_order(member=made.member, requests=[day(made, 2)], now=NOW)  # type: ignore[call-arg]
    assert Order.objects.of(made.org).count() == 1


# --- Frozen ---


def test_the_accepted_text_is_frozen_and_one_row_serves_every_order_of_it(
    made: Example,
) -> None:
    first = accepted_order(made, 1)
    second = accepted_order(made, 2)
    assert first.conditions_id == second.conditions_id
    assert BookingConditions.objects.of(made.org).count() == 1
    frozen = BookingConditions.objects.of(made.org).get()
    assert frozen.standard_version == 1 and frozen.free_cancellation_days == 5
    # Editing the words and the days later changes no earlier order's text.
    Organization.objects.filter(pk=made.org.pk).update(
        free_cancellation_days=7,
        labels={"conditions.damage": "You pay for what you break."},
    )
    client = browser(made)
    old = get(client, f"/book/order/{first.public_id}/conditions/")
    assert old.status_code == 200
    words = " ".join(s["text"] for s in config_of(old)["conditions"]["sections"])
    assert "at least 5 days" in words and "at least 7 days" not in words
    assert "You pay for what you break." not in words
    # The next order is of the new text, a row of its own.
    third = accepted_order(made, 3)
    assert third.conditions_id != first.conditions_id
    assert BookingConditions.objects.of(made.org).count() == 2
    new = " ".join(
        s["text"]
        for s in config_of(get(client, f"/book/order/{third.public_id}/conditions/"))[
            "conditions"
        ]["sections"]
    )
    assert "at least 7 days" in new and "You pay for what you break." in new
    # The first order still reads its own.
    again = get(client, f"/book/order/{first.public_id}/conditions/")
    assert "at least 5 days" in again.content.decode()


def test_a_frozen_text_holds_no_personal_data(made: Example) -> None:
    order = accepted_order(made)
    assert order.conditions is not None
    frozen = json.dumps(
        {
            field.name: str(getattr(order.conditions, field.attname))
            for field in BookingConditions._meta.get_fields()
            if hasattr(field, "attname")
        }
    )
    for value in (made.member.email, made.member.first_name, made.member.last_name):
        assert value and value not in frozen


def test_the_application_cannot_change_a_frozen_text(made: Example) -> None:
    accepted_order(made)
    with application_login():
        assert BookingConditions.objects.of(made.org).count() == 1
        with pytest.raises(django.db.utils.ProgrammingError, match="permission denied"):
            BookingConditions.objects.of(made.org).update(title="Changed")


def test_a_text_of_one_organization_cannot_be_an_orders_of_another(
    made: Example,
) -> None:
    elsewhere = made_up.everything("elsewhere")
    order = accepted_order(made)
    foreign = booking_conditions.freeze(
        elsewhere.organization,
        booking_conditions.current(elsewhere.organization),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Order.objects.of(made.org).filter(pk=order.pk).update(conditions_id=foreign.pk)


def test_an_order_with_one_half_of_its_acceptance_is_refused_by_the_database(
    made: Example,
) -> None:
    order = accepted_order(made)
    with pytest.raises(IntegrityError), transaction.atomic():
        Order.objects.of(made.org).filter(pk=order.pk).update(
            conditions_accepted_at=None
        )


# --- An order's own text ---


def test_an_orders_text_is_shown_to_its_member_only(made: Example) -> None:
    order = accepted_order(made)
    path = f"/book/order/{order.public_id}/conditions/"
    assert get(browser(made), path).status_code == 200
    neighbour = other(made)
    assert get(browser(made, neighbour), path).status_code == 404
    gone = f"/book/order/{'0' * 8}-0000-4000-8000-{'0' * 12}/conditions/"
    assert get(browser(made), gone).status_code == 404
    signed_out = get(Client(), path)
    assert signed_out.status_code == 302
    assert signed_out["Location"].startswith("/login/")


def test_an_order_from_before_the_conditions_reads_not_recorded(
    made: Example,
) -> None:
    free(made)
    old = before_conditions(made)
    client = browser(made)
    # Its own text: none.
    assert get(client, f"/book/order/{old.public_id}/conditions/").status_code == 404
    # It can still be read, in the order's read and in the member's list.
    read = booking_outcome.read(made.org, made.member, old.public_id)
    assert read is not None and read["conditions"] is False
    listed = answer(get(client, "/booking/bookings/"))
    assert [item["order"]["conditions"] for item in listed["coming"]] == [False]
    # And the administrator's cancel sees the line, with no moment to show.
    lines = admin_lines(made, old)
    assert all(line["coming"] for line in lines)
    assert [line["free_until"] for line in lines] == [None]


def test_the_outcome_and_my_page_link_the_text_the_order_accepted(
    made: Example,
) -> None:
    free(made)
    order = accepted_order(made)
    client = browser(made)
    read = answer(get(client, f"/booking/order/{order.public_id}/"))
    assert read["conditions"] is True
    listed = answer(get(client, "/booking/bookings/"))
    (item,) = listed["coming"]
    assert item["order"] == {
        "id": str(order.public_id),
        "reference": order.reference,
        "conditions": True,
    }


# --- The confirmation mail ---


def link_of(order: Order) -> str:
    return f"https://{HOST}/book/order/{order.public_id}/conditions/"


def test_the_confirmation_links_the_conditions_its_order_accepted(
    made: Example,
) -> None:
    free(made)
    order = accepted_order(made)
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    plain = str(message.body)
    assert f"Booking conditions: {link_of(order)}" in plain
    html = text_of(message, "text/html")
    assert f'href="{link_of(order)}"' in html and "Booking conditions</a>" in html
    assert "not recorded" not in plain


def test_the_confirmation_of_an_order_from_before_says_not_recorded(
    made: Example,
) -> None:
    free(made)
    before_conditions(made)
    mail.outbox.clear()
    send_confirmations(now=NOW + timedelta(seconds=5))
    (message,) = mail.outbox
    html = text_of(message, "text/html")
    for text in (str(message.body), html):
        assert "Booking conditions: not recorded" in text
        assert "/conditions/" not in text


# --- The administrator's cancel ---


def test_the_cancel_dialog_gets_the_moment_free_cancelling_ended_per_line(
    made: Example,
) -> None:
    order = create_order(
        member=made.member,
        requests=[day(made, 6), day(made, 9)],
        now=NOW,
        accepted_version=made_up.accepted(made.org),
    )
    # Later changes of the days do not move what the order accepted.
    Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=2)
    first, second = admin_lines(made, order)
    # Five days of 24 hours before 10:00 on the asset's clock (Oslo).
    assert first["free_until"] == {
        "date": (DAY + timedelta(days=1)).isoformat(),
        "time": "10:00",
    }
    assert second["free_until"] == {
        "date": (DAY + timedelta(days=4)).isoformat(),
        "time": "10:00",
    }


# --- Export, retention, erasure ---


@pytest.fixture
def everything() -> made_up.MadeUp:
    return made_up.everything()


@pytest.fixture
def accepted_everything(everything: made_up.MadeUp) -> made_up.MadeUp:
    row = booking_conditions.freeze(
        everything.organization,
        booking_conditions.current(everything.organization),
        NOW,
    )
    Order.objects.of(everything.organization).filter(pk=everything.order.pk).update(
        conditions=row, conditions_accepted_at=NOW
    )
    return everything


def test_the_members_export_includes_their_acceptances(
    accepted_everything: made_up.MadeUp,
) -> None:
    made = accepted_everything
    with application_login():
        result = export_member(
            actor=made.administrator,
            member_id=made.member.pk,
            **fresh(made.administrator),
        )
    row = BookingConditions.objects.of(made.organization).get()
    assert result["orders"][0]["conditions_id"] == row.pk
    assert result["orders"][0]["conditions_accepted_at"].startswith("2027-02-27T23:00")
    (kept,) = result["accepted_conditions"]
    assert kept["digest"] == row.digest and kept["free_cancellation_days"] == 5
    assert kept["sections"][1][0] == "Cancelling"


def test_an_acceptance_goes_with_its_order_at_retention_and_the_text_stays(
    accepted_everything: made_up.MadeUp,
) -> None:
    made = accepted_everything
    with application_login():
        purge_organization(made.organization, now=at(DAY + timedelta(days=5000)))
    assert not Order.objects.of(made.organization).filter(pk=made.order.pk).exists()
    assert BookingConditions.objects.of(made.organization).count() == 1


def test_erasing_a_member_leaves_the_acceptance_and_no_personal_data(
    accepted_everything: made_up.MadeUp,
) -> None:
    made = accepted_everything
    email = made.member.email
    erase(made, made.member.pk)
    order = Order.objects.of(made.organization).get(pk=made.order.pk)
    assert order.conditions_id is not None and order.conditions_accepted_at is not None
    row = BookingConditions.objects.of(made.organization).get()
    assert email not in json.dumps(booking_conditions.kept(row).page())


# --- Setup file and model ---


def test_the_setup_file_reads_and_writes_the_days() -> None:
    plain = setup_file.parse(SMALLEST)
    assert plain.organization.free_cancellation_days == 5
    assert "free_cancellation_days" not in setup_file.write(plain)
    seven = setup_file.parse(
        SMALLEST.replace(
            "booking_retention_months = 36\n",
            "booking_retention_months = 36\nfree_cancellation_days = 7\n",
            1,
        )
    )
    assert seven.organization.free_cancellation_days == 7
    assert "free_cancellation_days = 7" in setup_file.write(seven)
    assert setup_file.parse(setup_file.write(seven)) == seven


@pytest.mark.parametrize("days", ["-1", "366", '"5"', "1.5"])
def test_a_setup_file_refuses_days_outside_none_to_a_year(days: str) -> None:
    text = SMALLEST.replace(
        "booking_retention_months = 36\n",
        f"booking_retention_months = 36\nfree_cancellation_days = {days}\n",
        1,
    )
    with pytest.raises(setup_file.SetupError, match="free_cancellation_days"):
        setup_file.parse(text)


def test_the_days_survive_load_and_export() -> None:
    read = setup_file.parse(
        SMALLEST.replace(
            "booking_retention_months = 36\n",
            "booking_retention_months = 36\nfree_cancellation_days = 7\n",
            1,
        )
    )
    organization = setups.load(read, today=DAY)
    assert organization.free_cancellation_days == 7
    assert booking_conditions.current(organization).days == 7
    assert setups.export(organization).organization.free_cancellation_days == 7


def test_a_setup_with_no_days_loads_and_its_text_reads_none() -> None:
    read = setup_file.parse(
        SMALLEST.replace(
            "booking_retention_months = 36\n",
            "booking_retention_months = 36\nfree_cancellation_days = 0\n",
            1,
        )
    )
    assert read.organization.free_cancellation_days == 0
    organization = setups.load(read, today=DAY)
    assert organization.free_cancellation_days == 0
    text = booking_conditions.current(organization)
    assert text.days == 0
    assert "at least 0 days before the time starts" in dict(text.sections)["Cancelling"]
    assert setups.export(organization).organization.free_cancellation_days == 0
    assert "free_cancellation_days = 0" in setup_file.write(read)


def test_a_setup_without_the_days_has_the_default_five() -> None:
    from pathlib import Path

    text = Path("setups/fjordside.toml").read_text(encoding="utf-8")
    assert "free_cancellation_days" not in text
    assert setup_file.parse(text).organization.free_cancellation_days == 5


# --- The migration ---

BEFORE = [("core", "0052_events")]


@pytest.fixture
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def reverse() -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(BEFORE)


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.reverses_conditions
@pytest.mark.usefixtures("restore_current_schema")
def test_reversing_is_refused_while_a_text_is_frozen_and_clean_when_none_is(
    made: Example,
) -> None:
    assert "free_cancellation_days" in columns("core_organization")
    order = accepted_order(made)
    with pytest.raises(RuntimeError, match="retained acceptances"):
        reverse()
    assert "conditions_id" in columns("core_order")
    Order.objects.of(made.org).filter(pk=order.pk).update(
        conditions=None, conditions_accepted_at=None
    )
    with pytest.raises(RuntimeError, match="frozen text"):
        reverse()
    BookingConditions.objects.of(made.org).all().delete()
    Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=7)
    with pytest.raises(RuntimeError, match="own days"):
        reverse()
    Organization.objects.filter(pk=made.org.pk).update(free_cancellation_days=5)
    reverse()
    assert "free_cancellation_days" not in columns("core_organization")
    assert "conditions_id" not in columns("core_order")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert "free_cancellation_days" in columns("core_organization")
    # An organization that existed before has the default; its orders point to
    # nothing.
    assert Organization.objects.get(pk=made.org.pk).free_cancellation_days == 5
    assert Order.objects.of(made.org).filter(conditions__isnull=False).count() == 0
