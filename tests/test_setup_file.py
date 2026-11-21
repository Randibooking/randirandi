# SPDX-License-Identifier: AGPL-3.0-only
"""The setup file: what is read and what is refused (docs/setup-file.md)."""

import os
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from randirandi.setup_file import Setup, SetupError, parse, read, validate, write

SETUPS = Path(__file__).resolve().parent.parent / "setups"
FILES = sorted(SETUPS.glob("*.toml"))

# The smallest setup the format accepts, for a made-up organization.
SMALLEST = """\
format = 1

[organization]
name = "Lakeside Allotment Society"
subdomain = "lakeside"
time_zone = "Europe/Oslo"
currency = "NOK"
contact_email = "board@lakeside.example"
standard_role = "Member"
member_retention_months = 12
booking_retention_months = 36

[[assets]]
number = 1
name = "Clubhouse"

[[availability_calendars]]
asset = "Clubhouse"
spans = [{ weekdays = ["sat", "sun"], start = "09:00", end = "24:00" }]

[[booking_types]]
name = "Clubhouse hire"

[[booking_categories]]
name = "private"

[[roles]]
name = "Member"
booking_categories = ["private"]
admin_access = false

[[booking_configurations]]
number = 1
asset = "Clubhouse"
booking_type = "Clubhouse hire"
period_type = "flexible"
period_start = "10:00"
period_end = "18:00"
end_day = 0
period_start_weekdays = ["sat", "sun"]
interval = "30 min"
minimum_length = "1 h"
maximum_length = "8 h"
status = "enabled"
price_book = "Prices"
booking_categories = ["private"]

[[price_books]]
number = 1
name = "Prices"

[[price_books.entries]]
booking_configuration = 1
booking_category = "private"
tiers_go_by = "length"
tiers = [{ up_to = "8 h", paid_per = "2 h", amount = 149.50 }]
"""

# One change to the smallest setup, and the fault it must be refused with.
CHANGED = [
    (
        "a field the format does not have",
        'name = "Clubhouse"\n',
        'name = "Clubhouse"\nkind = "house"\n',
        "assets[1].kind: is not part of the format.",
    ),
    (
        "a newer format",
        "format = 1",
        "format = 4",
        "format: is newer than this code",
    ),
    (
        "a field left out",
        'time_zone = "Europe/Oslo"\n',
        "",
        "organization.time_zone: is missing.",
    ),
    (
        "an asset that does not exist",
        'asset = "Clubhouse"\nbooking_type',
        'asset = "Boathouse"\nbooking_type',
        "booking_configurations[1].asset: there is no asset named 'Boathouse'.",
    ),
    (
        "a booking type that does not exist",
        'booking_type = "Clubhouse hire"',
        'booking_type = "Sauna"',
        "booking_configurations[1].booking_type: there is no booking type named",
    ),
    (
        "a price book that does not exist",
        'price_book = "Prices"',
        'price_book = "Prices 2028"',
        "booking_configurations[1].price_book: there is no price book named",
    ),
    (
        "a role with a booking category that does not exist",
        'booking_categories = ["private"]\nadmin_access',
        'booking_categories = ["work"]\nadmin_access',
        "roles[1].booking_categories: there is no booking category named 'work'.",
    ),
    (
        "a standard role that does not exist",
        'standard_role = "Member"',
        'standard_role = "Board"',
        "organization.standard_role: there is no role named 'Board'.",
    ),
    (
        "a length without its unit",
        'interval = "30 min"',
        "interval = 30",
        "booking_configurations[1].interval: must be a length with its unit",
    ),
    (
        "a length in a unit the format does not have",
        'maximum_length = "8 h"',
        'maximum_length = "8 hours"',
        "booking_configurations[1].maximum_length: must be a length with its unit",
    ),
    (
        "a fixed period with an interval",
        'period_type = "flexible"',
        'period_type = "fixed"',
        "booking_configurations[1].interval: is set by the system for a fixed period.",
    ),
    (
        "a minimum length written without its unit",
        'minimum_length = "1 h"',
        "minimum_length = 60",
        "booking_configurations[1].minimum_length: must be a length with its unit",
    ),
    (
        "a flexible period without an interval",
        'interval = "30 min"\n',
        "",
        "booking_configurations[1].interval: is missing.",
    ),
    (
        "a period type the model does not have",
        'period_type = "flexible"',
        'period_type = "open"',
        "booking_configurations[1].period_type: must be one of: fixed, flexible.",
    ),
    (
        "a time of day written another way",
        'period_start = "10:00"',
        'period_start = "10.00"',
        "booking_configurations[1].period_start: must be a time of day",
    ),
    (
        "a period that ends at 24:00",
        'period_end = "18:00"',
        'period_end = "24:00"',
        "booking_configurations[1].period_end: must be a time of day",
    ),
    (
        "a weekday written another way",
        'period_start_weekdays = ["sat", "sun"]',
        'period_start_weekdays = ["saturday"]',
        "booking_configurations[1].period_start_weekdays: must name its weekdays",
    ),
    (
        "a weekday named twice",
        'period_start_weekdays = ["sat", "sun"]',
        'period_start_weekdays = ["sat", "sat"]',
        "booking_configurations[1].period_start_weekdays: must be a list of one or",
    ),
    (
        "a moment where a date is wanted",
        'asset = "Clubhouse"\nspans',
        'asset = "Clubhouse"\nstart_date = 2027-01-01T10:00:00\nspans',
        "availability_calendars[1].start_date: must be a date",
    ),
    (
        "a configuration's date where a calendar week is wanted",
        'status = "enabled"',
        'status = "enabled"\nstart_date = 2027-01-04',
        "booking_configurations[1].start_date: is now start_week, a calendar week",
    ),
    (
        "a calendar week written as a date",
        'status = "enabled"',
        'status = "enabled"\nstart_week = "2027-01-04"',
        "booking_configurations[1].start_week: must be a calendar week, written as",
    ),
    (
        "a calendar week the year does not have",
        'status = "enabled"',
        'status = "enabled"\nstop_week = "2027-W53"',
        "booking_configurations[1].stop_week: is not a week: 2027 has no week 53.",
    ),
    (
        "every no week",
        'status = "enabled"',
        'status = "enabled"\nstart_week = "2027-W01"\nevery_weeks = 0',
        "booking_configurations[1].every_weeks: must be 1 or more.",
    ),
    (
        "an available span that ends before it starts",
        'start = "09:00", end = "24:00"',
        'start = "09:00", end = "08:00"',
        "availability_calendars[1].spans[1].end: must be later than the start.",
    ),
    (
        "tiers by time of day with a length",
        'tiers_go_by = "length"',
        'tiers_go_by = "time of day"',
        "price_books[1].entries[1].tiers[1].up_to: must be a time of day",
    ),
    (
        "tiers by length with a time of day",
        'up_to = "8 h"',
        'up_to = "18:00"',
        "price_books[1].entries[1].tiers[1].up_to: must be a length with its unit",
    ),
    (
        "an entry without a price tier",
        'tiers = [{ up_to = "8 h", paid_per = "2 h", amount = 149.50 }]',
        "tiers = []",
        "price_books[1].entries[1].tiers: must hold one or more price tiers.",
    ),
    (
        "an amount with too many decimals",
        "amount = 149.50",
        "amount = 149.505",
        "price_books[1].entries[1].tiers[1].amount: must not have more than 2",
    ),
    (
        "an amount below zero",
        "amount = 149.50",
        "amount = -1",
        "price_books[1].entries[1].tiers[1].amount: must be zero or more.",
    ),
    (
        "an amount that is not a number",
        "amount = 149.50",
        "amount = nan",
        "price_books[1].entries[1].tiers[1].amount: must be zero or more.",
    ),
    (
        "an amount written as text",
        "amount = 149.50",
        'amount = "149.50"',
        "price_books[1].entries[1].tiers[1].amount: must be an amount",
    ),
    (
        "yes written as text",
        "admin_access = false",
        'admin_access = "no"',
        "roles[1].admin_access: must be true or false.",
    ),
    (
        "a subdomain that is not one part of an address",
        'subdomain = "lakeside"',
        'subdomain = "Lake Side"',
        "organization.subdomain: must be one part of a web address",
    ),
    (
        "a currency the service does not know",
        'currency = "NOK"',
        'currency = "GBP"',
        "organization.currency: must be one of: NOK, DKK, EUR.",
    ),
    (
        "a time zone that does not exist",
        'time_zone = "Europe/Oslo"',
        'time_zone = "Oslo"',
        "organization.time_zone: must be a time zone",
    ),
    (
        "a contact address that is not an address",
        'contact_email = "board@lakeside.example"',
        'contact_email = "the board"',
        "organization.contact_email: must be an email address.",
    ),
    (
        "a number below one",
        "number = 1\nasset",
        "number = 0\nasset",
        "booking_configurations[1].number: must be 1 or more.",
    ),
]

# Something added to the end of the smallest setup, and the fault.
ADDED = [
    (
        "a section the format does not have",
        '[[members]]\nemail = "someone@lakeside.example"\n',
        "members: is not part of the format.",
    ),
    (
        "a number used twice",
        '[[assets]]\nnumber = 1\nname = "Shed"\n',
        "assets: the number 1 is used more than once.",
    ),
    (
        "a name used twice",
        '[[booking_types]]\nname = "Clubhouse hire"\n',
        "booking_types: the name 'Clubhouse hire' is used more than once.",
    ),
    (
        "two availability calendars for one asset from the same day",
        '[[availability_calendars]]\nasset = "Clubhouse"\nspans = []\n',
        "availability_calendars: the asset and start date",
    ),
    (
        "a price for a booking category the configuration does not apply to",
        '[[booking_categories]]\nname = "work"\n'
        "[[price_books.entries]]\nbooking_configuration = 1\n"
        'booking_category = "work"\ntiers_go_by = "length"\n'
        'tiers = [{ up_to = "8 h", paid_per = "8 h", amount = 0 }]\n',
        "price_books[1].entries[2].booking_category: configuration 1 does not",
    ),
    (
        "a second price for the same configuration and booking category",
        "[[price_books.entries]]\nbooking_configuration = 1\n"
        'booking_category = "private"\ntiers_go_by = "length"\n'
        'tiers = [{ up_to = "8 h", paid_per = "8 h", amount = 0 }]\n',
        "price_books[1].entries[2]: this configuration and booking category",
    ),
    (
        "a price in another price book than the configuration's own",
        '[[price_books]]\nnumber = 2\nname = "Other"\n'
        "[[price_books.entries]]\nbooking_configuration = 1\n"
        'booking_category = "private"\ntiers_go_by = "length"\n'
        'tiers = [{ up_to = "8 h", paid_per = "8 h", amount = 0 }]\n',
        "links to the price book 'Prices', not to this one.",
    ),
    (
        "a price for a configuration that does not exist",
        "[[price_books.entries]]\nbooking_configuration = 7\n"
        'booking_category = "private"\ntiers_go_by = "length"\n'
        'tiers = [{ up_to = "8 h", paid_per = "8 h", amount = 0 }]\n',
        "there is no booking configuration numbered 7.",
    ),
]


def changed(old: str, new: str) -> str:
    assert SMALLEST.count(old) == 1, old
    return SMALLEST.replace(old, new)


def faults_of(text: str) -> list[str]:
    with pytest.raises(SetupError) as raised:
        parse(text)
    return raised.value.faults


def unpriced(setup: Setup) -> list[tuple[int, str]]:
    """What a configuration with payment applies to and its own price book
    has no price for; one without payment has none to give (W12)."""
    priced = {
        (price_book.name, entry.booking_configuration, entry.booking_category)
        for price_book in setup.price_books
        for entry in price_book.entries
    }
    return [
        (configuration.number, category)
        for configuration in setup.booking_configurations
        for category in configuration.booking_categories
        if configuration.price_book
        and (configuration.price_book, configuration.number, category) not in priced
    ]


def test_the_setup_file_is_read_without_django() -> None:
    # A fresh interpreter, isolated and without settings: the reader needs
    # neither Django nor its settings.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "DJANGO_SETTINGS_MODULE"
    }
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); import randirandi.setup_file; "
        "assert not [name for name in sys.modules if name.split('.')[0] == 'django']"
    )
    root = str(SETUPS.parent)
    done = subprocess.run(  # noqa: S603 — this interpreter, a fixed script
        [sys.executable, "-I", "-c", script, root],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_the_setups_in_the_repository() -> None:
    assert [path.name for path in FILES] == [
        "example.toml",
        "fjordside.toml",
    ]


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.name)
def test_a_setup_in_the_repository_is_read(path: Path) -> None:
    setup = read(path)

    assert setup.organization.name
    # A booking category without a price is not offered. These files leave
    # none out.
    assert unpriced(setup) == []


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.name)
def test_a_setup_holds_no_address_but_the_organizations_own(path: Path) -> None:
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if "@" in line
    ]

    assert len(lines) == 1
    assert lines[0].startswith("contact_email = ")


def test_the_smallest_setup_is_read_into_minutes_and_the_smallest_unit() -> None:
    setup = parse(SMALLEST)

    configuration = setup.booking_configurations[0]
    assert (configuration.period_start, configuration.period_end) == (600, 1080)
    assert configuration.interval == 30
    assert configuration.minimum_length == 60
    assert configuration.maximum_length == 480
    assert configuration.start_date is None
    tier = setup.price_books[0].entries[0].tiers[0]
    assert (tier.up_to, tier.paid_per, tier.amount) == (480, 120, 14950)
    calendar = setup.availability_calendars[0]
    assert calendar.start_date is None
    # One line that names two weekdays is two spans, and 24:00 is the end of
    # the day.
    assert [(span.weekday, span.start, span.end) for span in calendar.spans] == [
        ("sat", 540, 1440),
        ("sun", 540, 1440),
    ]


def test_a_fixed_period_leaves_interval_and_lengths_to_the_system() -> None:
    text = changed('period_type = "flexible"', 'period_type = "fixed"')
    for line in (
        'interval = "30 min"\n',
        'minimum_length = "1 h"\n',
        'maximum_length = "8 h"\n',
    ):
        text = text.replace(line, "")

    configuration = parse(text).booking_configurations[0]

    assert configuration.period_type == "fixed"
    assert configuration.interval is None
    assert configuration.minimum_length is None
    assert configuration.maximum_length is None


def test_a_flexible_period_may_leave_its_minimum_length_out() -> None:
    text = changed('minimum_length = "1 h"\n', "")

    configuration = parse(text).booking_configurations[0]

    # The loader leaves it to the system, which sets it to the interval.
    assert configuration.minimum_length is None
    assert (configuration.interval, configuration.maximum_length) == (30, 480)


def test_the_fjordside_scenario_has_the_shape_the_tests_lean_on() -> None:
    setup = read(SETUPS / "fjordside.toml")

    assert len(setup.assets) == 1
    assert len(setup.availability_calendars) == 1
    assert len(setup.booking_types) == 2
    assert len(setup.booking_categories) == 3
    # Private comes first, so it is what a member's Filter starts on.
    assert [row.name for row in setup.booking_categories] == [
        "Private",
        "Work",
        "Internal",
    ]
    assert len(setup.roles) == 3
    assert len(setup.booking_configurations) == 4
    assert len(setup.price_books) == 1
    entries = setup.price_books[0].entries
    assert len(entries) == 9
    assert sum(len(entry.tiers) for entry in entries) == 12

    # Available around the clock every day.
    spans = setup.availability_calendars[0].spans
    assert [(span.start, span.end) for span in spans if span.weekday == "sat"] == [
        (0, 1440),
    ]

    # The prices, in øre.
    prices = {
        (entry.booking_configuration, entry.booking_category): [
            (tier.up_to, tier.paid_per, tier.amount) for tier in entry.tiers
        ]
        for entry in entries
    }
    assert prices[(1, "Private")] == [(720, 720, 30000)]
    assert prices[(2, "Work")] == [(17 * 60, 240, 60000), (22 * 60, 300, 120000)]
    assert prices[(2, "Private")] == [(240, 240, 20000), (720, 240, 15000)]
    assert prices[(3, "Private")] == [(180, 180, 40000), (720, 120, 10000)]
    assert prices[(4, "Private")] == [(720, 720, 45000)]
    internal = [
        tiers for (_, category), tiers in prices.items() if category == "Internal"
    ]
    assert len(internal) == 4
    assert all(tier[2] == 0 for tiers in internal for tier in tiers)


def test_the_made_up_organization_uses_every_part_of_the_format() -> None:
    setup = read(SETUPS / "example.toml")

    configurations = setup.booking_configurations
    assert len(setup.assets) == 3
    assert {configuration.period_type for configuration in configurations} == {
        "fixed",
        "flexible",
    }
    assert {configuration.status for configuration in configurations} == {
        "enabled",
        "disabled",
    }
    # Calendar weeks, held as the Sunday of the last and the Monday of the
    # first: week 21 of 2027 ends on 30 May, and week 22 starts on 31 May.
    assert configurations[0].stop_date == date(2027, 5, 30)
    assert configurations[1].start_date == date(2027, 5, 31)
    # Interval, minimum and maximum alike: slots of one length that repeat.
    laundry = configurations[4]
    assert laundry.interval == laundry.minimum_length == laundry.maximum_length == 180

    assert len(setup.price_books) == 2
    entries = [entry for book in setup.price_books for entry in book.entries]
    assert {entry.tiers_go_by for entry in entries} == {"length", "time of day"}
    assert any(tier.amount == 0 for entry in entries for tier in entry.tiers)

    calendars = {calendar.asset: calendar for calendar in setup.availability_calendars}
    assert calendars["Laundry room"].start_date == date(2027, 1, 1)
    assert calendars["Guest room"].start_date is None
    # A weekday that is not named is not available.
    assert "sun" not in {span.weekday for span in calendars["Laundry room"].spans}


@pytest.mark.parametrize(
    ("old", "new", "fault"),
    [case[1:] for case in CHANGED],
    ids=[case[0] for case in CHANGED],
)
def test_a_changed_setup_is_refused(old: str, new: str, fault: str) -> None:
    faults = faults_of(changed(old, new))

    assert any(fault in line for line in faults), faults


@pytest.mark.parametrize(
    ("addition", "fault"),
    [case[1:] for case in ADDED],
    ids=[case[0] for case in ADDED],
)
def test_a_setup_with_something_added_is_refused(addition: str, fault: str) -> None:
    faults = faults_of(SMALLEST + "\n" + addition)

    assert any(fault in line for line in faults), faults


def test_one_fault_is_one_line() -> None:
    assert faults_of(changed("format = 1", "format = 4")) == [
        "format: is newer than this code, which reads format 3."
    ]


def test_format_two_is_refused_with_a_message_naming_format_three() -> None:
    # Whatever else the file holds: format 3 replaces it.
    assert faults_of(changed("format = 1", "format = 2\nmembers = 3")) == [
        "format: format 2 is no longer read. Write the file in format 3, which "
        "gives each asset its floor plans in place of [[inventories]] "
        '(docs/setup-file.md, "Format 3").'
    ]


def test_every_fault_is_found_in_one_reading() -> None:
    text = changed('currency = "NOK"', 'currency = "GBP"').replace(
        'asset = "Clubhouse"\nbooking_type', 'asset = "Boathouse"\nbooking_type'
    )

    faults = faults_of(text)

    assert len(faults) == 2


def test_a_file_that_is_not_toml_is_refused() -> None:
    assert faults_of("format = ")[0].startswith("The file cannot be read as TOML")


# --- The label set  ------------------------------------------------

LABELS = (
    "\n[labels]\n"
    '"place.book" = "Reserve"\n'
    '"stack.needs" = "At {time} {length} is needed"\n'
)


# The smallest setup in format 3: it names how its container books its asset
# and how its price is paid.
FORMAT_THREE = (
    SMALLEST.replace("format = 1", "format = 3")
    .replace(
        'price_book = "Prices"\nbooking_categories = ["private"]\n',
        'price_book = "Prices"\nbooking_categories = ["private"]\n'
        'consumption_mode = "exclusive"\n',
    )
    .replace(
        'tiers_go_by = "length"\n',
        'tiers_go_by = "length"\nbilling_basis = "per_booking"\n',
    )
)


def in_format_three(text: str = "") -> str:
    return FORMAT_THREE + text


def test_a_labels_table_is_read_in_the_order_of_the_label_set() -> None:
    setup = parse(in_format_three(LABELS))

    assert setup.labels == (
        ("place.book", "Reserve"),
        ("stack.needs", "At {time} {length} is needed"),
    )
    # However the file orders them.
    flipped = in_format_three(
        '\n[labels]\n"stack.needs" = "At {time} {length} is needed"\n'
        '"place.book" = "Reserve"\n'
    )
    assert parse(flipped) == setup


def test_a_setup_without_labels_has_none() -> None:
    assert parse(in_format_three()).labels == ()
    assert parse(in_format_three("\n[labels]\n")).labels == ()


def test_a_label_text_may_be_as_long_as_two_hundred_characters() -> None:
    text = "x" * 200

    assert parse(in_format_three(f'\n[labels]\n"place.me" = "{text}"\n')).labels == (
        ("place.me", text),
    )


@pytest.mark.parametrize(
    ("table", "fault"),
    [
        (
            '"no.such" = "Reserve"',
            "labels.no.such: is not a label key. The keys are in "
            "randirandi/locale/en.json.",
        ),
        (
            '"place.book" = ""',
            "labels.place.book: must not be empty, or start or end with a space.",
        ),
        (
            '"place.book" = " Reserve"',
            "labels.place.book: must not be empty, or start or end with a space.",
        ),
        (
            '"place.book" = 3',
            "labels.place.book: must be a text.",
        ),
        (
            f'"place.me" = "{"x" * 201}"',
            "labels.place.me: must not be longer than 200 characters.",
        ),
        (
            '"stack.needs" = "Needs {length} free"',
            "labels.stack.needs: must use exactly the placeholders of its "
            "default: {length}, {time}.",
        ),
        (
            '"stack.needs" = "Needs {length} free from {time} on {day}"',
            "labels.stack.needs: must use exactly the placeholders of its "
            "default: {length}, {time}.",
        ),
        (
            '"stack.needs" = "Needs {length} free from {clock}"',
            "labels.stack.needs: must use exactly the placeholders of its "
            "default: {length}, {time}.",
        ),
        (
            '"place.book" = "Reserve {now}"',
            "labels.place.book: must not use placeholders: its default has none.",
        ),
        (
            '"stack.needs" = "Needs free time"',
            "labels.stack.needs: must use exactly the placeholders of its "
            "default: {length}, {time}.",
        ),
    ],
)
def test_a_label_the_set_does_not_allow_is_refused(table: str, fault: str) -> None:
    assert faults_of(in_format_three(f"\n[labels]\n{table}\n")) == [fault]


def test_a_placeholder_may_be_used_twice_and_in_any_order() -> None:
    text = "{time}: {length}, again {time}"

    assert parse(in_format_three(f'\n[labels]\n"stack.needs" = "{text}"\n')).labels == (
        ("stack.needs", text),
    )


def test_every_label_fault_is_found_in_one_reading() -> None:
    faults = faults_of(
        in_format_three('\n[labels]\n"a.b" = "x"\n"place.book" = ""\n"place.me" = 1\n')
    )

    assert len(faults) == 3


def test_labels_are_not_a_table_when_the_file_says_otherwise() -> None:
    assert faults_of(FORMAT_THREE.replace("format = 3", "format = 3\nlabels = 3")) == [
        "labels: must be a table."
    ]


def test_format_one_refuses_a_labels_table() -> None:
    assert faults_of(SMALLEST + LABELS) == ["labels: is not part of the format."]


def test_labels_are_written_and_read_back() -> None:
    setup = parse(in_format_three(LABELS))

    written = write(setup)

    assert '[labels]\n"place.book" = "Reserve"\n' in written
    assert parse(written) == setup
    # Only the keys set: a setup without labels writes no table.
    assert "[labels]" not in write(parse(in_format_three()))


def test_a_setup_built_in_code_is_held_to_the_same_label_rules() -> None:
    setup = parse(in_format_three())

    for labels, fault in [
        ((("no.such", "x"),), "labels.no.such: is not a label key."),
        ((("place.book", ""),), "labels.place.book: must not be empty"),
        (
            (("place.book", "Reserve"), ("place.book", "Reserve")),
            "labels: the key 'place.book' is used more than once.",
        ),
    ]:
        with pytest.raises(SetupError) as raised:
            validate(replace(setup, labels=labels))
        assert any(fault in line for line in raised.value.faults), raised.value.faults
    with pytest.raises(SetupError) as raised:
        validate(replace(parse(SMALLEST), labels=(("place.book", "Reserve"),)))
    assert raised.value.faults == ["format: format 1 would lose labels; use format 3."]


def test_a_time_zone_on_an_asset_and_a_currency_on_a_price_book_are_read() -> None:
    setup = parse(
        in_format_three().replace(
            'name = "Clubhouse"\n',
            'name = "Clubhouse"\ntime_zone = "America/New_York"\n',
            1,
        )
    )
    assert setup.assets[0].time_zone == "America/New_York"
    # Left out, or the organization's own, it is the same setup.
    plain = parse(in_format_three())
    own = parse(
        in_format_three().replace(
            'name = "Clubhouse"\n', 'name = "Clubhouse"\ntime_zone = "Europe/Oslo"\n', 1
        )
    )
    assert own == plain and plain.assets[0].time_zone == ""


@pytest.mark.parametrize(
    ("line", "fault"),
    [
        ('time_zone = "Oslo"', "assets[1].time_zone: must be a time zone"),
        ("time_zone = 3", "assets[1].time_zone: must be a text."),
    ],
)
def test_an_asset_time_zone_the_service_does_not_know_is_refused(
    line: str, fault: str
) -> None:
    text = in_format_three().replace(
        'name = "Clubhouse"\n', f'name = "Clubhouse"\n{line}\n', 1
    )
    assert any(found.startswith(fault) for found in faults_of(text))


def test_a_price_book_currency_is_read_and_a_strange_one_is_refused() -> None:
    text = in_format_three()
    assert 'name = "Prices"\n' in text
    setup = parse(
        text.replace('name = "Prices"\n', 'name = "Prices"\ncurrency = "EUR"\n', 1)
    )
    assert setup.price_books[0].currency == "EUR"
    assert parse(text).price_books[0].currency == ""
    own = text.replace('name = "Prices"\n', 'name = "Prices"\ncurrency = "NOK"\n', 1)
    assert parse(own) == parse(text)
    bad = text.replace('name = "Prices"\n', 'name = "Prices"\ncurrency = "GBP"\n', 1)
    assert faults_of(bad) == ["price_books[1].currency: must be one of: NOK, DKK, EUR."]
