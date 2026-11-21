# SPDX-License-Identifier: AGPL-3.0-only
"""The floor plans migration : what it maps, what it refuses.

Every case starts from a database at the migration before it, made with the
models of that migration. The first customer's setup is loaded into it, and
each case adds what it is about.
"""

from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import StateApps

from randirandi import setup_file

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("core", "0015_calendar_week_rules")]
FLOOR_PLANS = [("core", "0016_floor_plans")]
FJORDSIDE = Path(__file__).resolve().parent.parent / "setups" / "fjordside.toml"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# A day the setup is loaded on, and the week of the cases: Monday 4 January
# 2027 to Sunday 10 January.
TODAY = date(2026, 12, 1)
MONDAY = date(2027, 1, 4)


@pytest.fixture(autouse=True)
def restore_current_schema() -> Iterator[None]:
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        if FLOOR_PLANS[0] not in executor.loader.applied_migrations:
            # A refused case leaves rows that do not map; the test's own
            # flush after this empties the rest.
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE core_organization CASCADE")
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.fixture
def before() -> Before:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = executor.loader.project_state(BEFORE).apps
    return Before(apps, load_fjordside(apps))


def migrate_to(target: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)


def load_fjordside(apps: StateApps) -> Any:
    """The rows the setup loader writes for `fjordside.toml`, a format 1
    file, as it wrote them at the migration before floor plans. That loader
    writes through the models of its day, which cannot run on the schema of
    that migration in this tree; so the same rows are written here with the
    models of that migration, the way the loader's checks left them.
    """
    setup = setup_file.read(FJORDSIDE)
    setup_file.validate(setup)

    def make(model_name: str, **values: Any) -> Any:
        return apps.get_model("core", model_name)._default_manager.create(**values)

    about = setup.organization
    org = make(
        "Organization",
        name=about.name,
        subdomain=about.subdomain,
        time_zone=about.time_zone,
        currency=about.currency,
        contact_email=about.contact_email,
        member_retention_months=about.member_retention_months,
        booking_retention_months=about.booking_retention_months,
        last_asset_number=max(item.number for item in setup.assets),
        last_price_book_number=max(item.number for item in setup.price_books),
        last_booking_configuration_number=max(
            item.number for item in setup.booking_configurations
        ),
    )
    categories = {
        item.name: make("BookingCategory", organization=org, name=item.name, position=n)
        for n, item in enumerate(setup.booking_categories, 1)
    }
    roles = {}
    for role in setup.roles:
        roles[role.name] = make(
            "Role", organization=org, name=role.name, admin_access=role.admin_access
        )
        for name in role.booking_categories:
            make(
                "RoleCategory",
                organization=org,
                role=roles[role.name],
                booking_category=categories[name],
            )
    org.standard_role = roles[about.standard_role]
    org.save()
    assets = {}
    for written_asset in setup.assets:
        # Format 1 gives every asset an inventory of its own: one booking.
        inventory = make("AssetInventory", organization=org)
        inventory.refresh_from_db()
        assets[written_asset.name] = make(
            "Asset",
            organization=org,
            number=written_asset.number,
            name=written_asset.name,
            description=written_asset.description,
            inventory=inventory,
            inventory_capacity=inventory.capacity,
            scope_key=inventory.scope_key,
        )
    for written_calendar in setup.availability_calendars:
        calendar = make(
            "AvailabilityCalendar",
            organization=org,
            asset=assets[written_calendar.asset],
            start_date=written_calendar.start_date or TODAY,
        )
        for span in written_calendar.spans:
            make(
                "AvailableSpan",
                organization=org,
                availability_calendar=calendar,
                weekday=WEEKDAYS.index(span.weekday),
                start_time=span.start,
                end_time=span.end,
            )
    booking_types = {
        item.name: make(
            "BookingType",
            organization=org,
            name=item.name,
            description=item.description,
            position=n,
        )
        for n, item in enumerate(setup.booking_types, 1)
    }
    price_books = {
        item.name: make(
            "PriceBook", organization=org, number=item.number, name=item.name
        )
        for item in setup.price_books
    }
    configurations = {}
    for configuration in setup.booking_configurations:
        length = (
            configuration.end_day * 24 * 60
            + configuration.period_end
            - configuration.period_start
        )
        configurations[configuration.number] = make(
            "BookingConfiguration",
            organization=org,
            number=configuration.number,
            asset=assets[configuration.asset],
            booking_type=booking_types[configuration.booking_type],
            period_type=configuration.period_type,
            period_start=configuration.period_start,
            period_end=configuration.period_end,
            end_day=configuration.end_day,
            period_start_weekdays=sorted(
                WEEKDAYS.index(day) for day in configuration.period_start_weekdays
            ),
            # What the model sets for a fixed period, and the minimum a
            # flexible one leaves out.
            interval=configuration.interval or length,
            minimum_length=configuration.minimum_length
            or configuration.interval
            or length,
            maximum_length=configuration.maximum_length or length,
            start_date=configuration.start_date,
            stop_date=configuration.stop_date,
            every_weeks=configuration.every_weeks,
            status=configuration.status,
            price_book=price_books[configuration.price_book],
            consumption_mode=configuration.consumption_mode,
        )
        for name in configuration.booking_categories:
            make(
                "ConfigurationCategory",
                organization=org,
                booking_configuration=configurations[configuration.number],
                booking_category=categories[name],
            )
    for book in setup.price_books:
        for entry in book.entries:
            row = make(
                "PriceBookEntry",
                organization=org,
                price_book=price_books[book.name],
                booking_configuration=configurations[entry.booking_configuration],
                booking_category=categories[entry.booking_category],
                tiers_go_by=entry.tiers_go_by,
                billing_basis=entry.billing_basis,
            )
            for tier in entry.tiers:
                make(
                    "PriceTier",
                    organization=org,
                    price_book_entry=row,
                    up_to=tier.up_to,
                    paid_per=tier.paid_per,
                    amount=tier.amount,
                )
    return org


class Before:
    """The database at the migration before floor plans, with the first
    customer's setup, and a way to add bookings and blocks to it."""

    def __init__(self, apps: StateApps, org: Any) -> None:
        self.apps = apps
        self.org = org
        self.zone = ZoneInfo(org.time_zone)
        self.member = self.model("Member").create(
            organization=org,
            role=org.standard_role,
            first_name="Robin",
            last_name="Example",
            email="robin@floor-plans.example",
            access="granted",
            origin="member import",
        )

    def model(self, name: str) -> Any:
        return self.apps.get_model("core", name)._default_manager

    def container(self, number: int) -> Any:
        return self.model("BookingConfiguration").get(
            organization=self.org, number=number
        )

    def copy(self, number: int, asset: Any, mode: str = "exclusive") -> Any:
        """Container `number` again, on `asset`, with the next number."""
        made = self.container(number)
        self.org.last_booking_configuration_number += 1
        self.org.save()
        made.pk, made.number, made.asset, made.consumption_mode = (
            None,
            self.org.last_booking_configuration_number,
            asset,
            mode,
        )
        made.save()
        return made

    def at(self, day: date, hour: int) -> datetime:
        return datetime.combine(day, time(), self.zone) + timedelta(hours=hour)

    def take(self, asset: Any, start: datetime, end: datetime, state: str) -> Any:
        # The time and its allocation together: the rule that they match is
        # checked when the transaction ends.
        with transaction.atomic():
            return self._take(asset, start, end, state)

    def _take(self, asset: Any, start: datetime, end: datetime, state: str) -> Any:
        taken = self.model("TakenTime").create(
            organization=self.org,
            asset=asset,
            inventory_id=asset.inventory_id,
            inventory_capacity=asset.inventory_capacity,
            scope_key=asset.scope_key,
            named_unit=asset.named_unit,
            whole_space=asset.whole_space,
            represented_space=asset.represented_space,
            starts_at=start,
            ends_at=end,
            state=state,
        )
        self.model("CapacityAllocation").create(
            organization=self.org,
            taken_time=taken,
            ordinal=1,
            inventory_id=asset.inventory_id,
            inventory_capacity=asset.inventory_capacity,
            scope_key=asset.scope_key,
            named_unit=asset.named_unit,
            whole_space=asset.whole_space,
            mode="exclusive",
            quantity=1,
            state=state,
            booking_start=start,
            booking_end=end,
            starts_at=start,
            ends_at=end,
            unit=0,
        )
        return taken

    def book(self, number: int, day: date, start: int, end: int) -> Any:
        """A confirmed booking under container `number`, with its line, from
        hour `start` of the day to hour `end` (past 24 is the next day)."""
        container = self.container(number)
        self.org.last_order_number += 1
        self.org.save()
        order = self.model("Order").create(
            organization=self.org,
            number=self.org.last_order_number,
            member=self.member,
            total=25000,
        )
        category = self.model("BookingCategory").get(
            organization=self.org, name="Private"
        )
        booking = self.model("Booking").create(
            organization=self.org,
            taken_time=self.take(
                container.asset,
                self.at(day, start),
                self.at(day, end),
                "confirmed",
            ),
            order=order,
            member=self.member,
            booking_type=container.booking_type,
            booking_category=category,
            booking_type_name=container.booking_type.name,
            booking_category_name=category.name,
        )
        self.line(booking, container)
        return booking

    def line(self, booking: Any, container: Any) -> Any:
        return self.model("BookingLine").create(
            organization=self.org,
            booking=booking,
            booking_configuration=container,
            price_book=container.price_book,
            up_to=720,
            quantity=1,
            amount=25000,
            configuration_number=container.number,
            price_book_number=container.price_book.number,
        )

    def block(self, asset: Any, day: date, start: int, end: int) -> Any:
        return self.model("Block").create(
            organization=self.org,
            taken_time=self.take(
                asset, self.at(day, start), self.at(day, end), "blocked"
            ),
            made_by=self.member,
            note="Floor sanding",
        )


def counts() -> dict[str, int]:
    """What the migration made, counted. Aggregates only."""
    queries = {
        "floor plans": "SELECT count(*) FROM core_assetinventory",
        "floor plans with their asset": (
            "SELECT count(*) FROM core_assetinventory WHERE asset_id IS NOT NULL"
        ),
        "spots": "SELECT count(*) FROM core_spot",
        "assets": "SELECT count(*) FROM core_asset",
        "assets with a space": (
            "SELECT count(*) FROM core_asset WHERE space_id IS NOT NULL"
        ),
        "assets with whole_space": "SELECT count(*) FROM core_asset WHERE whole_space",
        "shared containers with a floor plan": (
            "SELECT count(*) FROM core_bookingconfiguration "
            "WHERE floor_plan_id IS NOT NULL"
        ),
        "bookings": "SELECT count(*) FROM core_booking",
        "bookings with booking_configuration": (
            "SELECT count(*) FROM core_booking "
            "WHERE booking_configuration_id IS NOT NULL"
        ),
        "blocks": "SELECT count(*) FROM core_block",
        "blocks with a container": (
            "SELECT count(*) FROM core_block WHERE booking_configuration_id IS NOT NULL"
        ),
    }
    found = {}
    with connection.cursor() as cursor:
        for name, query in queries.items():
            cursor.execute(query)
            found[name] = cursor.fetchone()[0]
    return found


def one(query: str, *values: Any) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(query, values)
        return cursor.fetchone()


def test_the_setup_migrates_with_a_booking_and_a_block(
    before: Before,
) -> None:
    hall = before.model("Asset").get(organization=before.org, name="Hall")
    # An overnight stay, Monday night; and a block on Tuesday in the hall hire's time.
    booking = before.book(1, MONDAY, 22, 34)
    block = before.block(hall, MONDAY + timedelta(days=1), 12, 14)

    migrate_to(FLOOR_PLANS)

    found = counts()
    # Printed once, for the record of what the migration made of the file.
    print("floor plans migration, fjordside.toml:", found)  # noqa: T201
    assert found == {
        "floor plans": 1,
        "floor plans with their asset": 1,
        "spots": 1,
        "assets": 1,
        "assets with a space": 0,
        "assets with whole_space": 0,
        "shared containers with a floor plan": 0,
        "bookings": 1,
        "bookings with booking_configuration": 1,
        "blocks": 1,
        "blocks with a container": 1,
    }
    assert one(
        "SELECT c.number FROM core_booking b JOIN core_bookingconfiguration c "
        "ON c.id = b.booking_configuration_id WHERE b.id = %s",
        booking.pk,
    ) == (1,)
    assert one(
        "SELECT c.number FROM core_block b JOIN core_bookingconfiguration c "
        "ON c.id = b.booking_configuration_id WHERE b.id = %s",
        block.pk,
    ) == (2,)
    assert one(
        "SELECT s.number, s.name, i.last_spot_number FROM core_spot s JOIN "
        "core_assetinventory i ON i.id = s.floor_plan_id WHERE i.asset_id = %s",
        hall.pk,
    ) == (1, "", 1)
    # It runs on to the latest migration too.
    migrate_to(MigrationExecutor(connection).loader.graph.leaf_nodes())


def test_a_space_beds_and_a_whole_space_asset_map_to_floor_plans(
    before: Before,
) -> None:
    org = before.org
    space = before.model("Space").create(organization=org, number=1, name="Bunk room")
    org.last_space_number = 1
    org.last_asset_number = 3
    org.save()

    def asset(number: int, name: str, capacity: int, unit: str, **also: Any) -> Any:
        inventory = before.model("AssetInventory").create(
            organization=org, space=space, capacity=capacity, unit=unit
        )
        inventory.refresh_from_db()
        made = before.model("Asset").create(
            organization=org,
            number=number,
            name=name,
            inventory=inventory,
            inventory_capacity=capacity,
            scope_key=inventory.scope_key,
            **also,
        )
        made.refresh_from_db()
        return made

    room = asset(2, "Bunk room", 1, "booking", whole_space=True)
    beds = asset(3, "Bunk beds", 4, "bed")
    assert before.copy(1, room).number == 5
    assert before.copy(1, beds, "shared").number == 6
    before.book(5, MONDAY, 22, 34)
    before.block(room, MONDAY + timedelta(days=2), 22, 30)

    migrate_to(FLOOR_PLANS)

    found = counts()
    assert {name: found[name] for name in found if "booking" not in name} == {
        "floor plans": 3,
        "floor plans with their asset": 3,
        "spots": 6,
        "assets": 3,
        "assets with a space": 2,
        "assets with whole_space": 1,
        "shared containers with a floor plan": 1,
        "blocks": 1,
        "blocks with a container": 1,
    }
    assert one(
        "SELECT array_agg(s.number ORDER BY s.number), i.unit, i.last_spot_number "
        "FROM core_spot s JOIN core_assetinventory i ON i.id = s.floor_plan_id "
        "WHERE i.asset_id = %s GROUP BY i.id",
        beds.pk,
    ) == ([1, 2, 3, 4], "bed", 4)
    assert one(
        "SELECT a.space_id, a.whole_space FROM core_asset a WHERE a.id = %s", room.pk
    ) == (space.pk, True)
    assert one(
        "SELECT c.floor_plan_id = a.inventory_id FROM core_bookingconfiguration c "
        "JOIN core_asset a ON a.id = c.asset_id WHERE c.number = 6 "
        "AND c.organization_id = %s",
        org.pk,
    ) == (True,)
    assert one(
        "SELECT c.number FROM core_block b JOIN core_bookingconfiguration c "
        "ON c.id = b.booking_configuration_id"
    ) == (5,)


def named_units(before: Before) -> None:
    """Two named-unit assets, beds one and two of one inventory."""
    org = before.org
    inventory = before.model("AssetInventory").create(
        organization=org, capacity=2, unit="bed"
    )
    inventory.refresh_from_db()
    for unit in (1, 2):
        before.model("Asset").create(
            organization=org,
            number=1 + unit,
            name=f"Bed {unit}",
            inventory=inventory,
            inventory_capacity=2,
            scope_key=inventory.scope_key,
            named_unit=unit,
        )
    org.last_asset_number = 3
    org.save()


def without_a_line(before: Before) -> None:
    booking = before.book(1, MONDAY, 22, 34)
    booking.lines.all().delete()


def with_two_containers(before: Before) -> None:
    booking = before.book(1, MONDAY, 22, 34)
    before.line(booking, before.container(4))


def on_another_asset(before: Before) -> None:
    org = before.org
    inventory = before.model("AssetInventory").create(organization=org)
    inventory.refresh_from_db()
    before.model("Asset").create(
        organization=org,
        number=2,
        name="Laundry",
        inventory=inventory,
        inventory_capacity=1,
        scope_key=inventory.scope_key,
    )
    org.last_asset_number = 2
    org.save()
    laundry = before.model("Asset").get(organization=org, name="Laundry")
    booking = before.book(1, MONDAY, 22, 34)
    booking.lines.update(booking_configuration=before.copy(1, laundry))


def weekend_from_two(before: Before) -> None:
    """The weekend container as it was until 2026-10-25, from 14:00 for at
    most 8 hours: the weekend's 10:00 to 14:00 was then covered by no
    container. Now every hour of the Hall is."""
    before.model("BookingConfiguration").filter(pk=before.container(3).pk).update(
        period_start=14 * 60, maximum_length=8 * 60
    )


def block_outside(before: Before) -> None:
    # Saturday from 11 to 13: the shared time, which no container covers.
    weekend_from_two(before)
    hall = before.model("Asset").get(organization=before.org, name="Hall")
    before.block(hall, MONDAY + timedelta(days=5), 11, 13)


def block_over_two(before: Before) -> None:
    # Monday from 9 to 11: the end of Sunday's overnight stay and the start of
    # Monday's hall hire.
    hall = before.model("Asset").get(organization=before.org, name="Hall")
    before.block(hall, MONDAY, 9, 11)


@pytest.mark.parametrize(
    ("case", "listed"),
    [
        (
            named_units,
            [
                "named-unit assets (an asset that is one unit of an inventory): 2 (",
                "inventories with several assets: 1 (",
            ],
        ),
        (without_a_line, ["bookings without a booking line: 1 (booking "]),
        (
            with_two_containers,
            ["bookings whose lines name two or more containers: 1 (booking "],
        ),
        (
            on_another_asset,
            ["bookings whose line names a container on another asset: 1 (booking "],
        ),
        (block_outside, ["blocks no container covers: 1 (block "]),
        (block_over_two, ["blocks over several containers: 1 (block "]),
    ],
    ids=lambda value: getattr(value, "__name__", ""),
)
def test_what_does_not_map_stops_the_migration_with_counts(
    before: Before, case: Callable[[Before], None], listed: list[str]
) -> None:
    case(before)

    with pytest.raises(RuntimeError) as refused:
        migrate_to(FLOOR_PLANS)

    message = str(refused.value)
    assert message.startswith(
        "The floor plans migration (M6 part 2.5f1) changed nothing"
    )
    assert all(line in message for line in listed)
    assert message.count("\n") == len(listed)
    # Nothing changed: the database is still at the migration before.
    assert one("SELECT to_regclass('core_spot')") == (None,)
    applied = MigrationExecutor(connection).loader.applied_migrations
    assert FLOOR_PLANS[0] not in applied


def test_many_rows_are_counted_whole_and_named_ten_at_most(before: Before) -> None:
    weekend_from_two(before)
    hall = before.model("Asset").get(organization=before.org, name="Hall")
    for week in range(12):
        before.block(hall, MONDAY + timedelta(weeks=week, days=5), 11, 13)

    with pytest.raises(RuntimeError) as refused:
        migrate_to(FLOOR_PLANS)

    (line,) = str(refused.value).splitlines()[1:]
    assert line.startswith("blocks no container covers: 12 (block ")
    assert line.count("block ") == 10
    assert line.endswith("; …)")


def test_the_reverse_is_refused_while_a_floor_plan_holds_more(before: Before) -> None:
    migrate_to(FLOOR_PLANS)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core_spotgroup (organization_id, floor_plan_id, number, "
            "name, word) SELECT organization_id, id, 1, 'Window side', 'side' "
            "FROM core_assetinventory"
        )
        cursor.execute("UPDATE core_spot SET name = 'By the window'")

    with pytest.raises(RuntimeError) as refused:
        migrate_to(BEFORE)

    assert str(refused.value).splitlines()[1:] == [
        "groups of spots: 1",
        "spots with a name: 1",
    ]
    assert one("SELECT count(*) FROM core_spot") == (1,)


def test_a_plain_floor_plan_goes_back_and_comes_again_the_same(
    before: Before,
) -> None:
    hall = before.model("Asset").get(organization=before.org, name="Hall")
    before.book(1, MONDAY, 22, 34)
    before.block(hall, MONDAY + timedelta(days=1), 12, 14)
    migrate_to(FLOOR_PLANS)
    first = counts()

    migrate_to(BEFORE)
    assert one("SELECT to_regclass('core_spot')") == (None,)
    migrate_to(FLOOR_PLANS)

    assert counts() == first
