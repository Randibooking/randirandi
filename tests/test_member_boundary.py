# SPDX-License-Identifier: AGPL-3.0-only
"""A member reads their own orders and bookings, and nobody else's (control
C7). Tried between two members of one organization, and for one person who
is a member of two.
"""

from dataclasses import dataclass
from datetime import timedelta

import pytest
from django.apps import apps
from django.db import models

from randirandi.core.models import (
    AccessEvent,
    Asset,
    Authenticator,
    Booking,
    BookingLine,
    LoginAttempt,
    LoginCode,
    Member,
    MemberMembership,
    MemberStatus,
    Order,
    Origin,
    Owned,
    Payment,
    PaymentProvider,
    PaymentState,
)
from tests import made_up

# About a member, and read by the login, the member import and the
# administrators. No page lists them to the member.
NOT_FOR_A_MEMBERS_PAGES: set[type[Owned]] = {
    AccessEvent,
    LoginAttempt,
    LoginCode,
    Authenticator,
    MemberMembership,
}


@dataclass
class Neighbour:
    """A second member of the same organization, with an order of their own."""

    member: Member
    order: Order
    booking: Booking
    line: BookingLine
    payment: Payment


def neighbour(ours: made_up.MadeUp) -> Neighbour:
    org = ours.organization
    member = Member.objects.create(
        organization=org,
        role=ours.standard,
        first_name="Robin",
        last_name="Hill",
        email=f"robin@{org.subdomain}.example",
        phone="+4790000003",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    order = Order.objects.create(organization=org, member=member, total=25000)
    booking = ours.book(16, 18, order=order)
    line = BookingLine.objects.create(
        organization=org,
        booking=booking,
        booking_configuration=ours.configuration,
        price_book=ours.price_book,
        up_to=4 * 60,
        quantity=1,
        amount=25000,
        configuration_number=ours.configuration.number,
        price_book_number=ours.price_book.number,
    )
    payment = Payment.objects.create(
        currency="NOK",
        organization=org,
        order=order,
        payment_provider=PaymentProvider.VIPPS,
        provider_reference=f"{org.subdomain}-2",
        amount=25000,
        state=PaymentState.CAPTURED,
        captured_amount=25000,
    )
    return Neighbour(member, order, booking, line, payment)


def test_every_object_with_a_member_is_read_by_member_or_is_not_for_their_pages() -> (
    None
):
    with_a_member = {
        model
        for model in apps.get_app_config("core").get_models()
        if issubclass(model, Owned)
        and any(
            isinstance(field, models.ForeignKey) and field.name == "member"
            for field in model._meta.get_fields()
        )
    }
    read_by_member = {
        model for model in with_a_member if hasattr(model.objects, "of_member")
    }

    assert read_by_member == {Order, Booking}
    assert with_a_member - read_by_member == NOT_FOR_A_MEMBERS_PAGES
    # An object that is nobody's own has no such way in.
    assert not hasattr(Asset.objects, "of_member")


@pytest.mark.django_db
def test_a_member_reads_their_own_orders_and_bookings_only() -> None:
    ours = made_up.everything()
    theirs = neighbour(ours)

    assert set(Order.objects.of_member(ours.member)) == {ours.order, ours.spare_order}
    assert set(Booking.objects.of_member(ours.member)) == {ours.booking}
    assert set(Order.objects.of_member(theirs.member)) == {theirs.order}
    assert set(Booking.objects.of_member(theirs.member)) == {theirs.booking}
    # The administrator has made none, and so reads none as a member.
    assert not Order.objects.of_member(ours.administrator).exists()
    assert not Booking.objects.of_member(ours.administrator).exists()


@pytest.mark.django_db
def test_a_member_cannot_look_up_anothers_order_or_booking() -> None:
    ours = made_up.everything()
    theirs = neighbour(ours)
    own_orders = Order.objects.of_member(ours.member)
    own_bookings = Booking.objects.of_member(ours.member)

    # Not by the id in an address, not by the number on a confirmation, and
    # not by the internal id.
    with pytest.raises(Order.DoesNotExist):
        own_orders.get(public_id=theirs.order.public_id)
    with pytest.raises(Order.DoesNotExist):
        own_orders.get(number=theirs.order.number)
    with pytest.raises(Order.DoesNotExist):
        own_orders.get(pk=theirs.order.pk)
    with pytest.raises(Booking.DoesNotExist):
        own_bookings.get(public_id=theirs.booking.public_id)
    with pytest.raises(Booking.DoesNotExist):
        own_bookings.get(pk=theirs.booking.pk)

    assert own_orders.get(public_id=ours.order.public_id) == ours.order
    assert own_bookings.get(public_id=ours.booking.public_id) == ours.booking


@pytest.mark.django_db
def test_what_hangs_on_a_members_orders_is_their_own_too() -> None:
    ours = made_up.everything()
    theirs = neighbour(ours)

    bookings: set[Booking] = set()
    lines: set[BookingLine] = set()
    payments: set[Payment] = set()
    for order in Order.objects.of_member(ours.member):
        bookings.update(order.bookings.all())
        payments.update(Payment.objects.of(ours.organization).filter(order=order))
    for booking in bookings:
        lines.update(booking.lines.all())

    assert bookings == {ours.booking}
    assert theirs.line not in lines
    assert len(lines) == 1
    assert payments == {ours.payment}
    assert theirs.payment not in payments


@pytest.mark.django_db
def test_one_person_in_two_organizations_reads_each_as_its_own_member() -> None:
    ours = made_up.everything("harbourview")
    theirs = made_up.everything("lakeside", made_up.A_DAY + timedelta(days=7))
    # The same person: the same email in both.
    Member.objects.of(theirs.organization).filter(pk=theirs.member.pk).update(
        email=ours.member.email
    )
    theirs.member.refresh_from_db()
    assert theirs.member.email == ours.member.email

    here = set(Order.objects.of_member(ours.member))
    there = set(Order.objects.of_member(theirs.member))

    assert here == {ours.order, ours.spare_order}
    assert there == {theirs.order, theirs.spare_order}
    assert {order.organization_id for order in here} == {ours.organization.pk}
    assert not set(Booking.objects.of_member(ours.member)) & set(
        Booking.objects.of_member(theirs.member)
    )
