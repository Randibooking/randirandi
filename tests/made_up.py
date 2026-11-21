# SPDX-License-Identifier: AGPL-3.0-only
"""A made-up organization for the tests: one row of every object.

In English and unlike any real organization. Two of them, made on different
days, let a test try to reach from one into the other.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from django.db import transaction

from randirandi.core import booking_conditions
from randirandi.core import member_import as member_import_module
from randirandi.core.models import (
    AccessCause,
    AccessEvent,
    Asset,
    AssetInventory,
    AuditEvent,
    Authenticator,
    AvailabilityCalendar,
    AvailabilityClosure,
    AvailableSpan,
    Block,
    Booking,
    BookingCategory,
    BookingConditions,
    BookingConfiguration,
    BookingLine,
    BookingState,
    BookingType,
    CategoryHoursSpan,
    ConfigurationCategory,
    ConsumptionMode,
    Event,
    EventDay,
    EventNotice,
    EventRepeat,
    EventSkippedDay,
    ImportOutcome,
    LedgerEntry,
    LedgerTopic,
    LoginAttempt,
    LoginCode,
    LoginRequest,
    Member,
    MemberImport,
    MemberMembership,
    Membership,
    MemberStatus,
    Order,
    Organization,
    Origin,
    Owned,
    Payment,
    PaymentLedger,
    PaymentProvider,
    PaymentProviderAccount,
    PaymentState,
    PaymentTrouble,
    Payout,
    PeriodType,
    PriceBook,
    PriceBookEntry,
    PriceTier,
    Refund,
    Role,
    RoleCategory,
    SecurityNotice,
    Space,
    Spot,
    SpotGroup,
    Status,
    TakenTime,
    TiersGoBy,
    Weekday,
)
from randirandi.member_file import MemberTable
from randirandi.member_parsers import Parser
from tests.sealed import sealed

A_DAY = date(2027, 3, 1)


def accepted(who: Any) -> str:
    """The identifier of the booking conditions in force for a member or an
    organization: what an order carries as `accepted_version` ."""
    from randirandi.core import booking_conditions
    from randirandi.core.models import Organization

    found = getattr(who, "organization", who)
    return booking_conditions.current(Organization.objects.get(pk=found.pk)).digest


def order_body(total: int, **more: Any) -> dict[str, Any]:
    """The body of an order call of the only organization there is: the total
    and the conditions in force."""
    from randirandi.core.models import Organization

    only = Organization.objects.order_by("pk").first()
    assert only is not None
    return {"total": total, "accepted_version": accepted(only), **more}


def at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def floor_plan_of(asset: Asset) -> AssetInventory:
    """The asset's first floor plan, the one made with it."""
    return (
        AssetInventory.objects.of(asset.organization_id)
        .filter(asset=asset.pk)
        .earliest("pk")
    )


def spots(floor_plan: AssetInventory, total: int) -> None:
    """Add spots to the floor plan, one by one, until it has `total`."""
    floor_plan.refresh_from_db()
    for _ in range(total - floor_plan.capacity):
        Spot.objects.create(
            organization_id=floor_plan.organization_id, floor_plan=floor_plan
        )
    floor_plan.refresh_from_db()


def spots_of(floor_plan: AssetInventory, *numbers: int) -> list[Spot]:
    """The floor plan's spots with these numbers, in that order; all of
    them, lowest first, without numbers."""
    rows = Spot.objects.of(floor_plan.organization_id).filter(floor_plan=floor_plan)
    if not numbers:
        return list(rows.order_by("number"))
    by_number = {spot.number: spot for spot in rows.filter(number__in=numbers)}
    return [by_number[number] for number in numbers]


def fewer_spots(floor_plan: AssetInventory, total: int) -> None:
    """Take the last spots off the floor plan until it has `total`, and its
    capacity down with them in the same transaction, as the administrator editor does
    remove spots."""
    organization = floor_plan.organization_id
    with transaction.atomic():
        Spot.objects.of(organization).filter(
            floor_plan=floor_plan, place__gt=total
        ).delete()
        AssetInventory.objects.of(organization).filter(pk=floor_plan.pk).update(
            capacity=total
        )
    floor_plan.refresh_from_db()


def plain_floor_plans(organization: Organization) -> None:
    """Remove the spots' names and the groups, which only floor plans hold,
    so that a test can take the database back before them."""
    Spot.objects.of(organization).update(name="", group=None)
    SpotGroup.objects.of(organization).delete()


def organization(subdomain: str = "harbourview") -> Organization:
    return Organization.objects.create(
        name=f"The {subdomain} housing co-op",
        subdomain=subdomain,
        time_zone="Europe/Oslo",
        currency="NOK",
        contact_email=f"board@{subdomain}.example",
        member_retention_months=12,
        booking_retention_months=36,
    )


@dataclass
class MadeUp:
    """One organization with one row of every object."""

    organization: Organization
    day: date
    private: BookingCategory
    standard: Role
    board: Role
    membership: Membership
    member: Member
    administrator: Member
    asset: Asset
    booking_type: BookingType
    price_book: PriceBook
    configuration: BookingConfiguration
    order: Order
    # An order with no payment.
    spare_order: Order
    booking: Booking
    # Taken time that no booking or block points to; it has expired.
    spare_taken_time: TakenTime
    payment: Payment
    rows: dict[type[Owned], Owned] = field(default_factory=dict)

    def book(
        self,
        start_hour: int,
        end_hour: int,
        state: BookingState = BookingState.CONFIRMED,
        configuration: BookingConfiguration | None = None,
        order: Order | None = None,
        units: Sequence[Spot] | None = None,
    ) -> Booking:
        """A booking on the organization's day, between two hours, under the
        container, by default the organization's own. It is the booking of
        the member who made the order. On a shared container it holds
        `units`, by default its floor plan's first spot.
        """
        order = order or self.spare_order
        configuration = configuration or self.configuration
        if configuration.consumption_mode == ConsumptionMode.SHARED and units is None:
            assert configuration.floor_plan is not None
            units = spots_of(configuration.floor_plan, 1)
        return Booking.objects.take(
            organization=self.organization,
            asset=configuration.asset,
            booking_configuration=configuration,
            starts_at=at(self.day, start_hour),
            ends_at=at(self.day, end_hour),
            state=state,
            units=units,
            order=order,
            member_id=order.member_id,
            booking_type=self.booking_type,
            booking_category=self.private,
            booking_type_name=self.booking_type.name,
            booking_category_name=self.private.name,
        )

    def block(
        self,
        start_hour: int,
        end_hour: int,
        configuration: BookingConfiguration | None = None,
        units: Sequence[Spot] | None = None,
    ) -> Block:
        """A block on the organization's day, between two hours, on the
        container, by default the organization's own."""
        configuration = configuration or self.configuration
        return Block.objects.take(
            organization=self.organization,
            asset=configuration.asset,
            booking_configuration=configuration,
            starts_at=at(self.day, start_hour),
            ends_at=at(self.day, end_hour),
            units=units,
            made_by=self.administrator,
        )

    def shared(
        self,
        name: str,
        spots: int,
        *,
        space: Space | None = None,
        whole_space: bool = False,
        unit: str = "place",
        status: Status = Status.ENABLED,
    ) -> BookingConfiguration:
        """Another asset, with a floor plan of `spots` and a shared container
        like the organization's own: Mondays, 10:00 to 22:00 local time, by
        the hour, under the same booking type, category and prices. A
        disabled one may cover the same time as a shared container on another
        floor plan of its asset, which the validator refuses, so that a test
        can write claims past it."""
        asset = Asset(
            organization=self.organization,
            name=name,
            space=space,
            whole_space=whole_space,
        )
        asset.save(
            floor_plan=AssetInventory(capacity=spots, unit=unit),
        )
        calendar = AvailabilityCalendar.objects.create(
            organization=self.organization, asset=asset, start_date=self.day
        )
        AvailableSpan.objects.create(
            organization=self.organization,
            availability_calendar=calendar,
            weekday=Weekday.MONDAY,
            start_time=8 * 60,
            end_time=23 * 60,
        )
        configuration = BookingConfiguration.objects.create(
            organization=self.organization,
            asset=asset,
            booking_type=self.booking_type,
            period_type=PeriodType.FLEXIBLE,
            period_start=10 * 60,
            period_end=22 * 60,
            period_start_weekdays=[Weekday.MONDAY],
            interval=60,
            minimum_length=60,
            maximum_length=12 * 60,
            status=status,
            price_book=self.price_book,
            consumption_mode=ConsumptionMode.SHARED,
            floor_plan=floor_plan_of(asset),
        )
        ConfigurationCategory.objects.create(
            organization=self.organization,
            booking_configuration=configuration,
            booking_category=self.private,
        )
        return configuration


def everything(subdomain: str = "harbourview", day: date = A_DAY) -> MadeUp:
    """Make the organization. Its booking is 10:00 to 12:00 on `day`, and its
    block 14:00 to 15:00.
    """
    org = organization(subdomain)

    private = BookingCategory.objects.create(organization=org, name="private")
    standard = Role.objects.create(organization=org, name="Standard")
    board = Role.objects.create(organization=org, name="Board", admin_access=True)
    role_category = RoleCategory.objects.create(
        organization=org, role=standard, booking_category=private
    )
    org.standard_role = standard
    org.save()

    # No address unit, section or address: a migration test can reverse
    # 0021 without them. A test that needs them sets them.
    membership = Membership.objects.create(organization=org, identifier="H0201")
    member = Member.objects.create(
        organization=org,
        role=standard,
        full_name="Alex Rivers",
        first_name="Alex",
        last_name="Rivers",
        email=f"alex@{subdomain}.example",
        phone="+4790000001",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    administrator = Member.objects.create(
        organization=org,
        role=board,
        full_name="Sam Field",
        first_name="Sam",
        last_name="Field",
        email=f"sam@{subdomain}.example",
        phone="+4790000002",
        status=MemberStatus.ACTIVE,
        origin=Origin.ADMINISTRATOR,
    )
    member_membership = MemberMembership.objects.create(
        organization=org, member=member, membership=membership
    )
    member_import = MemberImport.objects.create(
        organization=org,
        parser="made-up-members",
        outcome=ImportOutcome.APPLIED,
        created=1,
    )
    access_event = AccessEvent.objects.create(
        organization=org,
        member=member,
        change=MemberStatus.ACTIVE,
        cause=AccessCause.MEMBER_IMPORT,
    )
    login_code = LoginCode.objects.create(
        organization=org,
        member=member,
        scrambled_code="scrambled",
        expires_at=at(day, 9) + timedelta(minutes=10),
    )

    login_request = LoginRequest.objects.create(
        organization=org,
        identifier_digest="b" * 64,
        browser_digest="a" * 64,
        challenge="a" * 32,
        network="192.0.2.1",
        at=login_code.expires_at,
    )
    login_attempt = LoginAttempt.objects.create(
        organization=org,
        member=member,
        at=at(day, 9),
        network="192.0.2.1",
        kind="request",
        outcome="issued",
    )

    space = Space.objects.create(organization=org, name="Function room location")
    asset = Asset.objects.create(organization=org, name="Function room", space=space)
    floor_plan = floor_plan_of(asset)
    # Its floor plan's one spot, and a group with no spot in it yet.
    spot = Spot.objects.of(org).get(floor_plan=floor_plan)
    spot.name = "By the stage"
    spot.save()
    group = SpotGroup.objects.create(
        organization=org, floor_plan=floor_plan, name="Stage end", word="end"
    )
    calendar = AvailabilityCalendar.objects.create(
        organization=org, asset=asset, start_date=day
    )
    span = AvailableSpan.objects.create(
        organization=org,
        availability_calendar=calendar,
        weekday=Weekday.MONDAY,
        start_time=8 * 60,
        end_time=23 * 60,
    )
    booking_type = BookingType.objects.create(organization=org, name="Room hire")
    price_book = PriceBook.objects.create(organization=org, name="Prices 2027")
    configuration = BookingConfiguration.objects.create(
        organization=org,
        asset=asset,
        booking_type=booking_type,
        period_type=PeriodType.FLEXIBLE,
        period_start=10 * 60,
        period_end=22 * 60,
        period_start_weekdays=[Weekday.MONDAY],
        interval=60,
        minimum_length=60,
        maximum_length=12 * 60,
        status=Status.ENABLED,
        price_book=price_book,
    )
    configuration_category = ConfigurationCategory.objects.create(
        organization=org, booking_configuration=configuration, booking_category=private
    )
    entry = PriceBookEntry.objects.create(
        organization=org,
        price_book=price_book,
        booking_configuration=configuration,
        booking_category=private,
        tiers_go_by=TiersGoBy.LENGTH,
    )
    tier = PriceTier.objects.create(
        organization=org,
        price_book_entry=entry,
        up_to=4 * 60,
        paid_per=4 * 60,
        amount=25000,
    )

    order = Order.objects.create(organization=org, member=member, total=25000)
    spare_order = Order.objects.create(organization=org, member=member, total=0)
    booking = Booking.objects.take(
        organization=org,
        asset=asset,
        booking_configuration=configuration,
        starts_at=at(day, 10),
        ends_at=at(day, 12),
        state=BookingState.CONFIRMED,
        order=order,
        member=member,
        booking_type=booking_type,
        booking_category=private,
        booking_type_name=booking_type.name,
        booking_category_name=private.name,
    )
    spare_taken_time = TakenTime.objects.create(
        organization=org,
        asset=asset,
        starts_at=at(day, 20),
        ends_at=at(day, 21),
        state=BookingState.EXPIRED,
    )
    line = BookingLine.objects.create(
        organization=org,
        booking=booking,
        booking_configuration=configuration,
        price_book=price_book,
        up_to=4 * 60,
        quantity=1,
        amount=25000,
        configuration_number=configuration.number,
        price_book_number=price_book.number,
    )
    payment = Payment.objects.create(
        currency="NOK",
        organization=org,
        order=order,
        payment_provider=PaymentProvider.VIPPS,
        provider_reference=f"{subdomain}-1",
        amount=25000,
        state=PaymentState.CAPTURED,
        captured_amount=25000,
        refunded_amount=10000,
    )
    refund = Refund.objects.create(
        organization=org, payment=payment, amount=10000, made_by=administrator
    )
    block = Block.objects.take(
        organization=org,
        asset=asset,
        booking_configuration=configuration,
        starts_at=at(day, 14),
        ends_at=at(day, 15),
        note="Floor sanding",
        made_by=administrator,
    )
    account = PaymentProviderAccount.objects.create(
        organization=org,
        payment_provider=PaymentProvider.VIPPS,
        merchant_number="000000",
        credentials="THE_NAME_OF_A_SECRET",
        test_mode=True,
    )
    audit_event = AuditEvent.objects.create(
        organization=org,
        administrator=administrator,
        action="made block",
        object_kind="block",
        object_id=block.pk,
    )

    authenticator = Authenticator.objects.create(
        organization=org,
        member=member,
        secret=sealed("ab" * 20, org.pk, member.pk),
        created_at=at(day, 9),
    )
    security_notice = SecurityNotice.objects.create(
        organization=org,
        recipient=member,
        event=audit_event,
        created_at=at(day, 9),
        delivered_at=at(day, 9),
    )

    made = MadeUp(
        organization=org,
        day=day,
        private=private,
        standard=standard,
        board=board,
        membership=membership,
        member=member,
        administrator=administrator,
        asset=asset,
        booking_type=booking_type,
        price_book=price_book,
        configuration=configuration,
        order=order,
        spare_order=spare_order,
        booking=booking,
        spare_taken_time=spare_taken_time,
        payment=payment,
    )
    for row in (
        private,
        standard,
        role_category,
        membership,
        member,
        member_membership,
        member_import,
        access_event,
        login_code,
        login_attempt,
        login_request,
        asset,
        space,
        floor_plan,
        group,
        spot,
        calendar,
        span,
        booking_type,
        price_book,
        configuration,
        configuration_category,
        entry,
        tier,
        order,
        booking.taken_time,
        booking.taken_time.allocations.get(),
        booking,
        line,
        payment,
        refund,
        block,
        account,
        audit_event,
        authenticator,
        security_notice,
    ):
        made.rows[type(row)] = row
    return made


def with_category_hours(made: MadeUp) -> MadeUp:
    """The made-up organization with the container's own hours for its
    booking category. Apart from `everything`, so that a migration test can
    reverse 0022 without them."""
    link = made.rows[ConfigurationCategory]
    assert isinstance(link, ConfigurationCategory)
    made.rows[CategoryHoursSpan] = CategoryHoursSpan.objects.create(
        organization=made.organization,
        configuration_category=link,
        weekday=Weekday.MONDAY,
        start_time=10 * 60,
        end_time=22 * 60,
    )
    return made


def with_payout(made: MadeUp) -> MadeUp:
    """The made-up organization with a ledger, one entry of it and one payout
    , none of them linked to the payment. Apart from `everything`,
    so that a migration test can reverse 0051 without them."""
    account = made.rows[PaymentProviderAccount]
    assert isinstance(account, PaymentProviderAccount)
    ledger = PaymentLedger.objects.create(
        organization=made.organization,
        account=account,
        ledger_id="9001",
        currency="NOK",
    )
    at = datetime(2027, 2, 20, 11, 0, tzinfo=UTC)
    made.rows[PaymentLedger] = ledger
    made.rows[LedgerEntry] = LedgerEntry.objects.create(
        organization=made.organization,
        ledger=ledger,
        topic=LedgerTopic.FUNDS,
        entry_type="capture",
        psp_reference="9001-1",
        reference="rr-1042-0001",
        ledger_date=at.date(),
        at=at,
        amount=25000,
        balance_before=0,
        balance_after=25000,
    )
    made.rows[Payout] = Payout.objects.create(
        organization=made.organization,
        ledger=ledger,
        psp_reference="9001-2",
        ledger_date=at.date(),
        scheduled_at=at,
        currency="NOK",
        amount=25000,
        captured_total=25000,
        refunded_total=0,
        fee_total=0,
        other_total=0,
        captured_count=1,
        refunded_count=0,
    )
    return made


def with_closure(made: MadeUp) -> MadeUp:
    """The made-up organization with a closure by date on its asset, a long way
    from every day a test books . Apart from `everything`, so
    that the tests of offers keep to the open days they count."""
    far = made.day + timedelta(days=400)
    made.rows[AvailabilityClosure] = AvailabilityClosure.objects.create(
        organization=made.organization, asset=made.asset, first_date=far, last_date=far
    )
    return made


def with_event(made: MadeUp) -> MadeUp:
    """The made-up organization with a one-off event on its asset, a repeating
    event beside it with its rule, a skipped day and the notice about it . All of them
    lie a long way from every day a test books and
    hold no time. Apart from `everything`, so that a migration test can
    reverse 0052 without them."""
    far = made.day + timedelta(days=500)
    organization = made.organization
    one_off = Event.objects.create(
        organization=organization,
        category="social",
        title="Summer party",
        asset=made.asset,
        contact=made.member,
        made_by=made.administrator,
    )
    made.rows[Event] = one_off
    made.rows[EventDay] = EventDay.objects.create(
        organization=organization,
        event=one_off,
        day=far,
        starts_at=at(far, 16),
        ends_at=at(far, 22),
    )
    repeating = Event.objects.create(
        organization=organization,
        category="open_house",
        title="Open house",
        contact=made.member,
        made_by=made.administrator,
    )
    made.rows[EventRepeat] = EventRepeat.objects.create(
        organization=organization,
        event=repeating,
        weekdays=1 << 5,
        first_date=far,
        start_minute=10 * 60,
        end_minute=14 * 60,
        made_through=far,
    )
    skipped = EventSkippedDay.objects.create(
        organization=organization,
        event=repeating,
        day=far + timedelta(days=7),
        reason="closed",
    )
    made.rows[EventSkippedDay] = skipped
    made.rows[EventNotice] = EventNotice.objects.create(
        organization=organization, skipped_day=skipped, recipient=made.administrator
    )
    # The text of the booking conditions in force, frozen as an acceptance
    # would; no order of the made-up organization points to it.
    made.rows[BookingConditions] = booking_conditions.freeze(
        organization, booking_conditions.current(organization)
    )
    return made


def with_trouble(made: MadeUp) -> MadeUp:
    """The made-up organization with one ended spell of trouble on its payment.
    Apart from `everything`, so that the History and the tests of the payment
    check keep to the lines they count."""
    payment = made.payment
    made.rows[PaymentTrouble] = PaymentTrouble.objects.create(
        organization=made.organization,
        payment=payment,
        since=payment.order.created_at,
        ended_at=payment.order.created_at + timedelta(minutes=1),
        step="read",
        kind="unanswered",
        first_step="read",
        first_kind="unanswered",
        tries=2,
    )
    return made


@dataclass
class Source:
    """What a test imports with: an organization and the way a made-up list is
    read (a column map and a layout), as a parser holds them. The sort of
    parser the product holds is `randirandi.member_parsers.Parser`."""

    organization: Organization
    column_map: Any = field(default_factory=dict)
    layout: Any = field(default_factory=dict)

    @property
    def organization_id(self) -> int:
        return self.organization.pk

    @property
    def parser(self) -> Parser:
        return Parser(
            id="made-up-members",
            name="A made-up test parser",
            reads="",
            accept=".csv",
            read=lambda data: MemberTable((), ()),
            column_map=self.column_map,
            layout=self.layout,
        )


def import_members(source: Source, table: MemberTable, **given: Any) -> MemberImport:
    """`import_members` for a made-up source."""
    return member_import_module.import_members(
        source.organization, source.parser, table, **given
    )


def seen_categories(organization: Any, configuration: int) -> list[dict[str, Any]]:
    """What an up-to-date categories sheet saw: each category of the container
    with its own hours, as the screens read them."""
    from randirandi.core.models import CategoryHoursSpan, ConfigurationCategory

    found = []
    for link in ConfigurationCategory.objects.of(organization).filter(
        booking_configuration=configuration
    ):
        spans = CategoryHoursSpan.objects.of(organization).filter(
            configuration_category=link
        )
        found.append(
            {
                "category": link.booking_category_id,
                "hours": [
                    {"weekday": s.weekday, "start": s.start_time, "end": s.end_time}
                    for s in spans
                ],
            }
        )
    return found


def change_categories_as_seen(
    administrator: Any, configuration: int, fields: dict[str, Any], **more: Any
) -> None:
    """The service with the container as it is now as what the sheet saw."""
    from randirandi.core import container_admin

    if "seen" not in fields:
        fields = {
            **fields,
            "seen": seen_categories(administrator.organization_id, configuration),
        }
    container_admin.change_categories(administrator, configuration, fields, **more)
