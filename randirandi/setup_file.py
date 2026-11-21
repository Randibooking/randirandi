# SPDX-License-Identifier: AGPL-3.0-only
"""An organization's setup file.

An organization's configuration written out as one file: its assets and when
they are available, its booking types, booking categories and roles, its
booking configurations and its price books. No members, no secrets and no
personal data. The format is described in docs/setup-file.md.

This module reads a file and refuses whatever the format does not know: a
section or a field it has not heard of, a value of the wrong kind, a name
that points at nothing. It checks file-level floor plan relationships too,
and the most spots a floor plan holds, shared with the model (`limits.py`).
Model validation and database constraints run when the setup is loaded
(`randirandi/core/setup.py`); future admin screens use those model rules.

It also writes a setup out as a file, so that a running organization's setup
can be exported. What `write` gives, `parse` reads back as the same setup.

Times of day are held as minutes after midnight, 0 to 1440. Lengths are held
in minutes. Amounts are held in the smallest unit of the currency.
"""

import json
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from randirandi import category_hours_rule
from randirandi import labels as label_set
from randirandi.floor_plan_rule import NAME_USED, NEEDS_NAME, floor_plan_name_fault
from randirandi.limits import (
    CATEGORY_ICONS,
    CURRENCIES,
    DEFAULT_FREE_CANCELLATION_DAYS,
    MOST_FREE_CANCELLATION_DAYS,
    MOST_SPOTS,
    MOST_TAX_RATE,
    TOO_MANY_SPOTS,
    name_fault,
)
from randirandi.price_entries_rule import (
    GROUP_TWICE,
    GROUPS_PER_SPOT,
    NEEDS_NORMAL_PRICE,
    PER_CAPACITY_UNIT,
    what_the_entries_of_a_category_get_wrong,
)
from randirandi.space_rule import NEEDS_SPACE, SPACE_ALREADY_MARKED, space_mark_fault

FORMAT = 3
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PERIOD_TYPES = ("fixed", "flexible")
STATUSES = ("enabled", "disabled")
TIERS_GO_BY = ("length", "time of day")
CONSUMPTION_MODES = ("exclusive", "shared")
BILLING_BASES = ("per_booking", "per_capacity_unit")
BOOKING_PAGES = ("one_asset", "whole_space")
# The currencies the service knows (the payment client's), and how many
# decimals each has.
CURRENCY_DECIMALS = dict.fromkeys(CURRENCIES, 2)

_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_WEEK = re.compile(r"([0-9]{4})-W([0-9]{2})")
_LENGTH = re.compile(r"([1-9][0-9]*) (h|min)")
_SUBDOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_END_OF_DAY = 24 * 60
_A_TIME = "a time of day, written as 17:00"
_A_LENGTH = 'a length with its unit, written as "4 h" or "90 min"'
_A_WEEK = 'a calendar week, written as "2027-W01"'
_A_DATE = "a date, written as 2027-01-31"
# Why a date is closed (`ClosureReason`); a closure may give none.
CLOSURE_REASONS = ("holiday", "repair", "vacation")
# The days a closure may name, as the model holds them.
CLOSURE_FIRST_DAY = date(2000, 1, 1)
CLOSURE_LAST_DAY = date(2100, 12, 31)
_AN_AMOUNT = "an amount, written as 250 or 249.50"
_ZERO_OR_MORE = "must be zero or more."
_A_WEEKDAY = "must name its weekdays as: " + ", ".join(WEEKDAYS) + "."
# Every N weeks is at most a year's weeks, as a configuration allows.
_MOST_WEEKS = 52
# What the system works out for a fixed period, so the file leaves it out.
_FLEXIBLE_ONLY = ("interval", "minimum_length", "maximum_length")
# Format 2 had inventories, which floor plans under their asset replace.
FORMAT_TWO = (
    "format: format 2 is no longer read. Write the file in format 3, which "
    "gives each asset its floor plans in place of [[inventories]] "
    '(docs/setup-file.md, "Format 3").'
)
_SPOTS = (
    "a number of spots, written as 12, or a list of their names, written as "
    '["Bed 1", "Bed 2"], with "" for a spot without a name'
)


class SetupError(ValueError):
    """The file is not a setup the service can read. Every fault is listed."""

    def __init__(self, faults: list[str]) -> None:
        super().__init__("\n".join(faults))
        self.faults = faults


@dataclass(frozen=True)
class Organization:
    name: str
    subdomain: str
    time_zone: str
    currency: str
    contact_email: str
    standard_role: str
    member_retention_months: int
    booking_retention_months: int
    # The limits on a member's bookings; None, left out of the file: no limit.
    max_bookings_a_day: int | None = None
    book_ahead_months: int | None = None
    # The tax in the prices, in hundredths of a percent (2500 is 25 %); left
    # out of the file, none .
    tax_rate: int = 0
    # The days before a booked time's start until which cancelling it by email
    # is free ; left out of the file, 5.
    free_cancellation_days: int = DEFAULT_FREE_CANCELLATION_DAYS
    # The language of its pages and mails : a catalogue the code
    # ships; left out of the file, English.
    language: str = label_set.DEFAULT_LANGUAGE


@dataclass(frozen=True)
class Space:
    number: int
    name: str
    description: str = ""
    # What its booking page shows: one asset at a time, or the whole space.
    booking_page: str = "one_asset"


@dataclass(frozen=True)
class SpotGroup:
    name: str
    # The word for it: "table", "section", "row".
    word: str
    # Its spots, by their numbers on the floor plan.
    spots: tuple[int, ...]


@dataclass(frozen=True)
class FloorPlan:
    # Empty for none. An asset with more than one floor plan names each.
    name: str
    # The word for its spots: "seat", "bed", "machine".
    spot_word: str
    # Its spots in the order of their numbers, from 1: each its name, or
    # empty for none. Spots without names are written as their count.
    spots: tuple[str, ...]
    groups: tuple[SpotGroup, ...] = ()


@dataclass(frozen=True)
class Asset:
    number: int
    name: str
    description: str
    # The name of its space, empty for none.
    space: str = ""
    # "Covers its whole space": it needs a space.
    whole_space: bool = False
    # How many people the law allows. Information only.
    maximum_occupancy: int | None = None
    # Without one, the asset has a floor plan of one spot without a name.
    floor_plans: tuple[FloorPlan, ...] = ()
    # The zone its times are read in; empty for the organization's, also when
    # the file names the organization's own .
    time_zone: str = ""


@dataclass(frozen=True)
class AvailableSpan:
    weekday: str
    start: int
    end: int


@dataclass(frozen=True)
class AvailabilityCalendar:
    asset: str
    # Without one, the calendar applies from the day the setup is loaded.
    start_date: date | None
    spans: tuple[AvailableSpan, ...]


@dataclass(frozen=True)
class Closure:
    """A date, or a run of dates, closed beside an asset's weekly availability
    : whole days on the asset's clock, first and last included."""

    asset: str
    first_date: date
    last_date: date
    # One of `CLOSURE_REASONS`; "" is none.
    reason: str = ""


@dataclass(frozen=True)
class BookingType:
    name: str
    description: str


@dataclass(frozen=True)
class BookingCategory:
    name: str
    # One of `CATEGORY_ICONS`; "" is no icon.
    icon: str = ""


@dataclass(frozen=True)
class Role:
    name: str
    booking_categories: tuple[str, ...]
    admin_access: bool


@dataclass(frozen=True)
class CategoryHours:
    """One stretch of one weekday when a container offers one of its booking
    categories ; a category with none has the container's hours."""

    booking_category: str
    weekday: str
    start: int
    end: int


@dataclass(frozen=True)
class BookingConfiguration:
    number: int
    asset: str
    booking_type: str
    period_type: str
    period_start: int
    period_end: int
    end_day: int
    period_start_weekdays: tuple[str, ...]
    # Entered for a flexible period, which may leave the minimum out for the
    # system to set to the interval. For a fixed period the system sets them.
    interval: int | None
    minimum_length: int | None
    maximum_length: int | None
    # The calendar weeks, written as weeks and held as the Monday of the
    # first and the Sunday of the last, as they are stored.
    start_date: date | None
    stop_date: date | None
    status: str
    # Its price book; empty for a container without payment (W12, format 3).
    price_book: str
    booking_categories: tuple[str, ...]
    consumption_mode: str = "exclusive"
    every_weeks: int = 1
    # The name of its asset's floor plan a shared container books; empty
    # for an exclusive one, and for a floor plan without a name.
    floor_plan: str = ""
    # A booking category's own hours on the container, inside its hours ().
    category_hours: tuple[CategoryHours, ...] = ()


@dataclass(frozen=True)
class PriceTier:
    # A length where the entry's tiers go by length, a time of day where they
    # go by time of day.
    up_to: int
    paid_per: int
    amount: int


@dataclass(frozen=True)
class PriceBookEntry:
    booking_configuration: int
    booking_category: str
    tiers_go_by: str
    tiers: tuple[PriceTier, ...]
    billing_basis: str = "per_booking"
    # The name of a group of spots on the container's floor plan this is the
    # price of a spot in ; empty for the container's normal
    # price. A price for a group is per capacity unit.
    group: str = ""


@dataclass(frozen=True)
class PriceBook:
    number: int
    name: str
    entries: tuple[PriceBookEntry, ...]
    # What its amounts are in; empty for the organization's, also when the
    # file names the organization's own .
    currency: str = ""


@dataclass(frozen=True)
class Setup:
    organization: Organization
    assets: tuple[Asset, ...]
    availability_calendars: tuple[AvailabilityCalendar, ...]
    # Booking types and booking categories keep the order of the file: it is
    # their position in the lists people choose from.
    booking_types: tuple[BookingType, ...]
    booking_categories: tuple[BookingCategory, ...]
    roles: tuple[Role, ...]
    booking_configurations: tuple[BookingConfiguration, ...]
    price_books: tuple[PriceBook, ...]
    format: int = 1
    spaces: tuple[Space, ...] = ()
    # The organization's own texts for label keys (format 3), in its language,
    # in the order of the keys in `randirandi/locale/en.json`; only the keys it
    # set.
    labels: tuple[tuple[str, str], ...] = ()
    # Closures by date .
    closures: tuple[Closure, ...] = ()


def read(path: Path) -> Setup:
    """Read a setup file, or say everything that is wrong with it."""
    return parse(path.read_text(encoding="utf-8"))


def parse(text: str) -> Setup:
    """Read a setup from the text of a file, or say everything that is wrong."""
    try:
        values = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as error:
        raise SetupError([f"The file cannot be read as TOML: {error}."]) from None

    faults: list[str] = []
    top = _Fields(values, "", faults)
    before = top.faults_so_far()
    version = top.integer("format", minimum=1)
    fault = _format_fault(version) if top.faults_so_far() == before else None
    if version == 2:
        raise SetupError([FORMAT_TWO])
    if fault:
        top.fault("format", fault)
    organization = _organization(top.table("organization"))
    setup = Setup(
        organization=organization,
        format=version,
        spaces=(
            tuple(_space(fields) for fields in top.tables("spaces", required=False))
            if version == FORMAT
            else ()
        ),
        labels=_labels(top.table("labels", required=False), organization.language)
        if version == FORMAT
        else (),
        assets=tuple(
            _asset(fields, version, organization.time_zone)
            for fields in top.tables("assets")
        ),
        availability_calendars=tuple(
            _availability_calendar(fields)
            for fields in top.tables("availability_calendars")
        ),
        closures=(
            tuple(_closure(fields) for fields in top.tables("closures", required=False))
            if version == FORMAT
            else ()
        ),
        booking_types=tuple(
            _booking_type(fields) for fields in top.tables("booking_types")
        ),
        booking_categories=tuple(
            _booking_category(fields) for fields in top.tables("booking_categories")
        ),
        roles=tuple(_role(fields) for fields in top.tables("roles")),
        booking_configurations=tuple(
            _booking_configuration(fields, version)
            for fields in top.tables("booking_configurations")
        ),
        price_books=tuple(
            _price_book(fields, organization.currency, version)
            for fields in top.tables("price_books")
        ),
    )
    top.done()
    _check_names(setup, faults)
    _check_capacity(setup, faults)
    _check_category_hours(setup, faults)
    _check_space_zones(setup, faults)
    _check_closures(setup, faults)
    if faults:
        raise SetupError(faults)
    return _named_floor_plans(setup)


def _named_floor_plans(setup: Setup) -> Setup:
    """A shared container that leaves its floor plan out books its asset's
    only one: the setup names it, as an export does."""
    plans = {asset.name: asset.floor_plans for asset in setup.assets}
    return replace(
        setup,
        booking_configurations=tuple(
            replace(item, floor_plan=plans[item.asset][0].name)
            if item.consumption_mode == "shared"
            and not item.floor_plan
            and len(plans[item.asset]) == 1
            else item
            for item in setup.booking_configurations
        ),
    )


class _Fields:
    """One table of the file. A key that is never asked for is a fault.

    A value that cannot be read is reported, and an empty one stands in for
    it, so that everything wrong with a file is found in one reading. What a
    value must be is said by the rules below the class, which `validate`
    applies to a setup built in code as well.
    """

    def __init__(self, values: object, where: str, faults: list[str]) -> None:
        self._where = where
        self._faults = faults
        self._values: dict[str, Any] = {}
        if isinstance(values, dict):
            self._values = dict(values)
        else:
            faults.append(f"{where}: must be a table.")

    def fault(self, key: str, message: str) -> None:
        self._faults.append(f"{self._place(key)}: {message}")

    def has(self, key: str) -> bool:
        return key in self._values

    def faults_so_far(self) -> int:
        return len(self._faults)

    def done(self) -> None:
        for key in self._values:
            self.fault(key, "is not part of the format.")

    def rest(self) -> dict[str, Any]:
        """Every key not asked for yet, taken: for a table whose keys are
        not fixed by the format."""
        taken, self._values = self._values, {}
        return taken

    def refuse(self, key: str, rule: Callable[[object], str | None]) -> None:
        """A key the file must leave out, said by its rule."""
        if key in self._values:
            self._judged(key, self._values.pop(key), rule, None)

    def text(
        self, key: str, *, required: bool = True, preserve_whitespace: bool = False
    ) -> str:
        value = self._take(key, str, "a text", required=required)
        if value is None:
            return ""
        return self._judged(
            key,
            value,
            lambda text: _text_fault(text, preserve_whitespace=preserve_whitespace),
            "",
        )

    def integer(
        self,
        key: str,
        *,
        minimum: int,
        maximum: int | None = None,
        required: bool = True,
    ) -> int:
        value = self._take(key, int, "a whole number", required=required)
        if value is None:
            return minimum
        return self._judged(
            key,
            value,
            lambda number: _whole_number_fault(
                number, minimum=minimum, maximum=maximum
            ),
            # Kept, as written: a refused number is not also reported as
            # given twice.
            value,
        )

    def yes_no(self, key: str, *, required: bool = True) -> bool:
        value = self._take(key, bool, "true or false", required=required)
        if value is None:
            return False
        return self._judged(key, value, _yes_no_fault, False)

    def whole(self, key: str, *, default: int, minimum: int, maximum: int) -> int:
        """A whole number the file may leave out: `default` then."""
        if key not in self._values:
            return default
        return self.integer(key, minimum=minimum, maximum=maximum)

    def count(self, key: str) -> int | None:
        """A whole number from 1 the file may leave out."""
        if key not in self._values:
            return None
        return self.integer(key, minimum=1)

    def day(self, key: str, *, required: bool = False) -> date | None:
        value = self._take(key, date, _A_DATE, required=required)
        if value is None:
            return None
        return self._judged(key, value, _day_fault, None)

    def choice(
        self, key: str, choices: tuple[str, ...], *, required: bool = True
    ) -> str:
        """One of the choices. Left out where it may be, the first."""
        value = self._take(key, str, "a text", required=required)
        if value is None:
            return choices[0]
        return self._judged(
            key, value, lambda chosen: _choice_fault(chosen, choices), choices[0]
        )

    def optional_choice(self, key: str, choices: tuple[str, ...]) -> str:
        """One of the choices the file may leave out: "" then."""
        if key not in self._values:
            return ""
        return self.choice(key, choices)

    def time_of_day(self, key: str, *, end_of_day: bool = False) -> int:
        value = self._take(key, str, _A_TIME)
        if value is None:
            return 0
        if end_of_day and value == "24:00":
            minutes = _END_OF_DAY
        elif match := _TIME.fullmatch(value):
            minutes = int(match[1]) * 60 + int(match[2])
        else:
            self.fault(key, f"must be {_A_TIME}.")
            return 0
        return self._judged(
            key,
            minutes,
            lambda held: _time_of_day_fault(held, end_of_day=end_of_day),
            0,
        )

    def length(self, key: str) -> int:
        value = self._take(key, str, _A_LENGTH)
        if value is None:
            return 0
        match = _LENGTH.fullmatch(value)
        if match is None:
            self.fault(key, f"must be {_A_LENGTH}.")
            return 0
        minutes = int(match[1]) * (60 if match[2] == "h" else 1)
        return self._judged(key, minutes, _length_fault, 0)

    def week(self, key: str, *, last: bool) -> date | None:
        """A calendar week the file may leave out: its Monday, or its Sunday
        when it is the last.
        """
        value = self._take(key, str, _A_WEEK, required=False)
        if value is None:
            return None
        match = _WEEK.fullmatch(value)
        if match is None:
            self.fault(key, f"must be {_A_WEEK}.")
            return None
        try:
            day = date.fromisocalendar(int(match[1]), int(match[2]), 7 if last else 1)
        except ValueError:
            self.fault(key, f"is not a week: {match[1]} has no week {match[2]}.")
            return None
        return self._judged(key, day, lambda held: _week_fault(held, last=last), None)

    def optional_integer(self, key: str, *, minimum: int, maximum: int) -> int:
        """A whole number the file may leave out; left out, it is the least."""
        if key not in self._values:
            return minimum
        return self.integer(key, minimum=minimum, maximum=maximum)

    def optional_length(self, key: str) -> int | None:
        """A length the file may leave out, for the system to set."""
        if key not in self._values:
            return None
        return self.length(key)

    def amount(self, key: str, currency: str) -> int:
        """Money, written in the currency's main unit, held in its smallest."""
        if key not in self._values:
            self.fault(key, "is missing.")
            return 0
        value: object = self._values.pop(key)
        # Python counts true and false as numbers. The file does not.
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            self.fault(key, f"must be {_AN_AMOUNT}.")
            return 0
        decimals = CURRENCY_DECIMALS.get(currency, 2)
        smallest = Decimal(value).scaleb(decimals)
        if not smallest.is_finite() or smallest < 0:
            self.fault(key, _ZERO_OR_MORE)
            return 0
        if smallest != smallest.to_integral_value():
            self.fault(key, f"must not have more than {decimals} decimals.")
            return 0
        return self._judged(key, int(smallest), _amount_fault, 0)

    def names(self, key: str) -> tuple[str, ...]:
        values = self._take(key, list, "a list")
        if values is None:
            return ()
        return self._judged(key, tuple(values), _names_fault, ())

    def spots(self, key: str) -> tuple[str, ...]:
        """A floor plan's spots: their count, or their names in the order of
        their numbers. A name is given once; "" is a spot without one."""
        if key not in self._values:
            self.fault(key, "is missing.")
            return ()
        value: object = self._values.pop(key)
        if type(value) is int:
            fault = _spot_count_fault(value)
            if fault:
                self.fault(key, fault)
                return ()
            return ("",) * value
        if type(value) is not list or not value:
            self.fault(key, f"must be {_SPOTS}, each name given once.")
            return ()
        return self._judged(key, tuple(value), _spots_fault, ())

    def numbers(self, key: str) -> tuple[int, ...]:
        """Spot numbers, from 1, each given once."""
        values = self._take(key, list, "a list")
        if values is None:
            return ()
        return self._judged(key, tuple(values), _spot_numbers_fault, ())

    def weekdays(self, key: str) -> tuple[str, ...]:
        values = self._take(key, list, "a list")
        if values is None:
            return ()
        return self._judged(key, tuple(values), _weekdays_fault, ())

    def table(self, key: str, *, required: bool = True) -> _Fields | None:
        values = self._take(key, dict, "a table", required=required)
        if values is None:
            return None
        return _Fields(values, self._place(key), self._faults)

    def tables(self, key: str, *, required: bool = True) -> list[_Fields]:
        values = self._take(key, list, "a list of tables", required=required)
        return [
            _Fields(value, f"{self._place(key)}[{place}]", self._faults)
            for place, value in enumerate(values or [], start=1)
        ]

    def _place(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def _judged[T, U](
        self, key: str, value: T, rule: Callable[[T], str | None], instead: U
    ) -> T | U:
        """The value, or what stands in for it when its rule refuses it."""
        fault = rule(value)
        if fault:
            self.fault(key, fault)
            return instead
        return value

    def _take[T](
        self, key: str, kind: type[T], what: str, *, required: bool = True
    ) -> T | None:
        if key not in self._values:
            if required:
                self.fault(key, "is missing.")
            return None
        value: object = self._values.pop(key)
        # The kind itself and nothing built on it: Python counts true as a
        # number and a moment as a day. The file does not.
        if isinstance(value, kind) and type(value) is kind:
            return value
        self.fault(key, f"must be {what}.")
        return None


# The rules for each value, as the setup holds it. The reader applies each
# to what it has read, field by field; `validate` applies the same ones to a
# setup built in code (`_check_as_read`). Each gives its fault, or None.


def _text_fault(value: object, *, preserve_whitespace: bool = False) -> str | None:
    if type(value) is not str:
        return "must be a text."
    if not value or (not preserve_whitespace and value != value.strip()):
        return "must not be empty, or start or end with a space."
    return None


def _whole_number_fault(
    value: object, *, minimum: int, maximum: int | None = None
) -> str | None:
    if type(value) is not int:
        return "must be a whole number."
    if value < minimum:
        return f"must be {minimum} or more."
    if maximum is not None and value > maximum:
        return f"must be {maximum} or less."
    return None


def _yes_no_fault(value: object) -> str | None:
    return None if type(value) is bool else "must be true or false."


def _day_fault(value: object) -> str | None:
    return None if type(value) is date else f"must be {_A_DATE}."


def _choice_fault(value: object, choices: tuple[str, ...]) -> str | None:
    if type(value) is str and value in choices:
        return None
    return "must be one of: " + ", ".join(choices) + "."


def _time_of_day_fault(value: object, *, end_of_day: bool = False) -> str | None:
    last = _END_OF_DAY if end_of_day else _END_OF_DAY - 1
    if type(value) is int and 0 <= value <= last:
        return None
    return f"must be {_A_TIME}."


def _length_fault(value: object) -> str | None:
    return None if type(value) is int and value >= 1 else f"must be {_A_LENGTH}."


def _week_fault(value: object, *, last: bool) -> str | None:
    """A calendar week is held as its Monday, or its Sunday when it is the
    last."""
    if type(value) is date and value.isoweekday() == (7 if last else 1):
        return None
    return f"must be {_A_WEEK}."


def _amount_fault(value: object) -> str | None:
    if type(value) is not int:
        return f"must be {_AN_AMOUNT}."
    return None if value >= 0 else _ZERO_OR_MORE


def _names_fault(values: Iterable[object]) -> str | None:
    texts = list(values)
    if (
        not texts
        or not all(type(text) is str and text for text in texts)
        or len(set(texts)) != len(texts)
    ):
        return "must be a list of one or more texts, each named once."
    return None


def _weekdays_fault(values: Iterable[object]) -> str | None:
    days = list(values)
    if fault := _names_fault(days):
        return fault
    if any(day not in WEEKDAYS for day in days):
        return _A_WEEKDAY
    return None


def _spot_count_fault(count: int) -> str | None:
    if count < 1:
        return "must be 1 or more."
    if count > MOST_SPOTS:
        return TOO_MANY_SPOTS
    return None


def _spots_fault(names: Iterable[object]) -> str | None:
    """Spots held as their names, "" for one without: at least one, at most
    the most a floor plan holds, a name given once."""
    spots = list(names)
    if not all(type(name) is str for name in spots):
        return f"must be {_SPOTS}, each name given once."
    if fault := _spot_count_fault(len(spots)):
        return fault
    given: set[object] = set()
    for name in spots:
        if name and name in given:
            return f"the name {name!r} is given to more than one spot."
        given.add(name)
    return None


def _spot_numbers_fault(values: Iterable[object]) -> str | None:
    numbers = list(values)
    if not all(type(value) is int and value >= 1 for value in numbers) or len(
        set(numbers)
    ) != len(numbers):
        return "must be a list of spot numbers, each given once."
    return None


def _subdomain_fault(value: str) -> str | None:
    if _SUBDOMAIN.fullmatch(value):
        return None
    return "must be one part of a web address: a to z, 0 to 9 and hyphens."


def _time_zone_fault(value: str) -> str | None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return "must be a time zone, written as Europe/Oslo."
    return None


def _currency_fault(value: str) -> str | None:
    if value in CURRENCY_DECIMALS:
        return None
    return "must be one of: " + ", ".join(CURRENCY_DECIMALS) + "."


def _email_fault(value: str) -> str | None:
    return None if _EMAIL.fullmatch(value) else "must be an email address."


def _span_end_fault(start: int, end: int) -> str | None:
    return "must be later than the start." if end <= start else None


def _fixed_period_fault(value: object) -> str | None:
    """Interval, minimum and maximum of a fixed period: set by the system."""
    if value is None:
        return None
    return "is set by the system for a fixed period. Leave it out."


def _tiers_fault(tiers: Iterable[object]) -> str | None:
    return None if list(tiers) else "must hold one or more price tiers."


def _format_fault(version: object) -> str | None:
    if type(version) is not int or version < 1:
        return _whole_number_fault(version, minimum=1)
    if version == 2:
        return FORMAT_TWO.removeprefix("format: ")
    if version > FORMAT:
        return f"is newer than this code, which reads format {FORMAT}."
    return None


def _organization(fields: _Fields | None) -> Organization:
    if fields is None:
        return Organization("", "", "", "", "", "", 0, 0)
    organization = Organization(
        name=fields.text("name"),
        subdomain=fields.text("subdomain"),
        time_zone=fields.text("time_zone"),
        currency=fields.text("currency"),
        contact_email=fields.text("contact_email"),
        standard_role=fields.text("standard_role"),
        member_retention_months=fields.integer("member_retention_months", minimum=1),
        booking_retention_months=fields.integer("booking_retention_months", minimum=1),
        max_bookings_a_day=fields.count("max_bookings_a_day"),
        book_ahead_months=fields.count("book_ahead_months"),
        tax_rate=fields.integer(
            "tax_rate", minimum=0, maximum=MOST_TAX_RATE, required=False
        ),
        free_cancellation_days=fields.whole(
            "free_cancellation_days",
            default=DEFAULT_FREE_CANCELLATION_DAYS,
            minimum=0,
            maximum=MOST_FREE_CANCELLATION_DAYS,
        ),
        language=fields.text("language")
        if fields.has("language")
        else label_set.DEFAULT_LANGUAGE,
    )
    for key, rule in _ORGANIZATION_RULES:
        value = getattr(organization, key)
        if value and (fault := rule(value)):
            fields.fault(key, fault)
    fields.done()
    return organization


# What the organization's texts must be beyond a text, field by field.
_ORGANIZATION_RULES: tuple[tuple[str, Callable[[str], str | None]], ...] = (
    ("subdomain", _subdomain_fault),
    ("time_zone", _time_zone_fault),
    ("language", label_set.language_fault),
    ("currency", _currency_fault),
    ("contact_email", _email_fault),
)


def organization_text_fault(key: str, value: str) -> str | None:
    """The reader's rule for one of the organization's texts (subdomain,
    time_zone, currency, contact_email), for the admin screen ."""
    return dict(_ORGANIZATION_RULES)[key](value)


def whole_number_fault(
    value: object, *, minimum: int, maximum: int | None = None
) -> str | None:
    """The reader's rule for a whole number, for the admin screen."""
    return _whole_number_fault(value, minimum=minimum, maximum=maximum)


def _in_label_order(pairs: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """The pairs in the order of the keys in the label set, so that a setup
    reads back the same however its file ordered them."""
    rank = {key: place for place, key in enumerate(label_set.DEFAULTS)}
    return tuple(sorted(pairs, key=lambda pair: rank.get(pair[0], len(rank))))


def _labels(fields: _Fields | None, language: str) -> tuple[tuple[str, str], ...]:
    """The `[labels]` table: label key = the organization's text, in its
    language."""
    if fields is None:
        return ()
    found: list[tuple[str, str]] = []
    for key, value in fields.rest().items():
        if fault := label_set.fault(language, key, value):
            fields.fault(key, fault)
        else:
            found.append((key, value))
    return _in_label_order(found)


def _space(fields: _Fields) -> Space:
    space = Space(
        number=fields.integer("number", minimum=1),
        name=fields.text("name", preserve_whitespace=True),
        description=fields.text(
            "description", required=False, preserve_whitespace=True
        ),
        booking_page=fields.choice("booking_page", BOOKING_PAGES, required=False),
    )
    fields.done()
    return space


def _asset(fields: _Fields, version: int, organization_zone: str) -> Asset:
    if version != FORMAT:
        asset = Asset(
            number=fields.integer("number", minimum=1),
            name=fields.text("name"),
            description=fields.text("description", required=False),
        )
        fields.done()
        return asset
    asset = Asset(
        number=fields.integer("number", minimum=1),
        name=fields.text("name"),
        description=fields.text("description", required=False),
        space=fields.text("space", required=False, preserve_whitespace=True),
        whole_space=fields.yes_no("whole_space", required=False),
        maximum_occupancy=fields.count("maximum_occupancy"),
        floor_plans=tuple(
            _floor_plan(plan) for plan in fields.tables("floor_plans", required=False)
        ),
        time_zone=_own(fields, "time_zone", _time_zone_fault, organization_zone),
    )
    fields.done()
    return asset


def _own(
    fields: _Fields,
    key: str,
    rule: Callable[[str], str | None],
    organizations: str,
) -> str:
    """An optional field that is the organization's when left out: empty
    when left out or when it names the organization's own, else the value."""
    value = fields.text(key, required=False)
    if value and (fault := rule(value)):
        fields.fault(key, fault)
        return ""
    return "" if value == organizations else value


def _floor_plan(fields: _Fields) -> FloorPlan:
    floor_plan = FloorPlan(
        name=fields.text("name", required=False, preserve_whitespace=True),
        spot_word=fields.text("spot_word", preserve_whitespace=True),
        spots=fields.spots("spots"),
        groups=tuple(
            _spot_group(group) for group in fields.tables("groups", required=False)
        ),
    )
    fields.done()
    return floor_plan


def _spot_group(fields: _Fields) -> SpotGroup:
    group = SpotGroup(
        name=fields.text("name", preserve_whitespace=True),
        # Left out, "group", as the model gives it.
        word=fields.text("word", required=False, preserve_whitespace=True) or "group",
        spots=fields.numbers("spots"),
    )
    fields.done()
    return group


def _availability_calendar(fields: _Fields) -> AvailabilityCalendar:
    spans: list[AvailableSpan] = []
    for span in fields.tables("spans"):
        before = span.faults_so_far()
        weekdays = span.weekdays("weekdays")
        start = span.time_of_day("start")
        end = span.time_of_day("end", end_of_day=True)
        if span.faults_so_far() == before and (fault := _span_end_fault(start, end)):
            span.fault("end", fault)
        span.done()
        # One line may name several weekdays. It is one span for each.
        spans.extend(AvailableSpan(weekday, start, end) for weekday in weekdays)
    calendar = AvailabilityCalendar(
        asset=fields.text("asset"),
        start_date=fields.day("start_date"),
        spans=tuple(spans),
    )
    fields.done()
    return calendar


def _closure(fields: _Fields) -> Closure:
    asset = fields.text("asset")
    first = fields.day("first_date", required=True)
    # A single date is a run of one.
    last = fields.day("last_date") or first
    closure = Closure(
        asset=asset,
        first_date=first or CLOSURE_FIRST_DAY,
        last_date=last or CLOSURE_FIRST_DAY,
        reason=fields.optional_choice("reason", CLOSURE_REASONS),
    )
    fields.done()
    return closure


def _category_hours(fields: _Fields) -> tuple[CategoryHours, ...]:
    found: list[CategoryHours] = []
    for hours in fields.tables("category_hours", required=False):
        before = hours.faults_so_far()
        category = hours.text("booking_category")
        weekdays = hours.weekdays("weekdays")
        start = hours.time_of_day("start")
        end = hours.time_of_day("end", end_of_day=True)
        if hours.faults_so_far() == before and (fault := _span_end_fault(start, end)):
            hours.fault("end", fault)
        hours.done()
        # One line may name several weekdays. It is one stretch for each.
        found.extend(CategoryHours(category, day, start, end) for day in weekdays)
    return tuple(found)


def _booking_type(fields: _Fields) -> BookingType:
    booking_type = BookingType(
        name=fields.text("name"),
        description=fields.text("description", required=False),
    )
    fields.done()
    return booking_type


def _booking_category(fields: _Fields) -> BookingCategory:
    booking_category = BookingCategory(
        name=fields.text("name"), icon=fields.optional_choice("icon", CATEGORY_ICONS)
    )
    fields.done()
    return booking_category


def _role(fields: _Fields) -> Role:
    role = Role(
        name=fields.text("name"),
        booking_categories=fields.names("booking_categories"),
        admin_access=fields.yes_no("admin_access"),
    )
    fields.done()
    return role


def _booking_configuration(fields: _Fields, version: int) -> BookingConfiguration:
    period_type = fields.choice("period_type", PERIOD_TYPES)
    interval = minimum_length = maximum_length = None
    if period_type == "flexible":
        interval = fields.length("interval")
        # Left out, the system sets it to the interval (docs/booking-model.md).
        minimum_length = fields.optional_length("minimum_length")
        maximum_length = fields.length("maximum_length")
    else:
        for key in _FLEXIBLE_ONLY:
            fields.refuse(key, _fixed_period_fault)
    fields.refuse("start_date", lambda _: f"is now start_week, {_A_WEEK}.")
    fields.refuse("stop_date", lambda _: f"is now stop_week, {_A_WEEK}.")
    configuration = BookingConfiguration(
        number=fields.integer("number", minimum=1),
        asset=fields.text("asset"),
        booking_type=fields.text("booking_type"),
        period_type=period_type,
        period_start=fields.time_of_day("period_start"),
        period_end=fields.time_of_day("period_end"),
        end_day=fields.integer("end_day", minimum=0),
        period_start_weekdays=fields.weekdays("period_start_weekdays"),
        interval=interval,
        minimum_length=minimum_length,
        maximum_length=maximum_length,
        start_date=fields.week("start_week", last=False),
        stop_date=fields.week("stop_week", last=True),
        status=fields.choice("status", STATUSES),
        # Left out in format 3, the container is without payment (W12).
        price_book=fields.text("price_book", required=version != FORMAT),
        booking_categories=fields.names("booking_categories"),
        category_hours=_category_hours(fields),
        consumption_mode=(
            fields.choice("consumption_mode", CONSUMPTION_MODES)
            if version == FORMAT
            else "exclusive"
        ),
        every_weeks=fields.optional_integer(
            "every_weeks", minimum=1, maximum=_MOST_WEEKS
        ),
        floor_plan=(
            fields.text("floor_plan", required=False, preserve_whitespace=True)
            if version == FORMAT
            else ""
        ),
    )
    fields.done()
    return configuration


def _price_book(fields: _Fields, organization_currency: str, version: int) -> PriceBook:
    # Its amounts are read in its own currency, the organization's when left out.
    own = (
        _own(fields, "currency", _currency_fault, organization_currency)
        if version == FORMAT
        else ""
    )
    currency = own or organization_currency
    price_book = PriceBook(
        number=fields.integer("number", minimum=1),
        name=fields.text("name"),
        entries=tuple(
            _price_book_entry(entry, currency, version)
            for entry in fields.tables("entries", required=False)
        ),
        currency=own,
    )
    fields.done()
    return price_book


def _price_book_entry(fields: _Fields, currency: str, version: int) -> PriceBookEntry:
    tiers_go_by = fields.choice("tiers_go_by", TIERS_GO_BY)
    before = fields.faults_so_far()
    tiers: list[PriceTier] = []
    for tier in fields.tables("tiers"):
        tiers.append(
            PriceTier(
                up_to=(
                    tier.length("up_to")
                    if tiers_go_by == "length"
                    else tier.time_of_day("up_to", end_of_day=True)
                ),
                paid_per=tier.length("paid_per"),
                amount=tier.amount("amount", currency),
            )
        )
        tier.done()
    if fields.faults_so_far() == before and (fault := _tiers_fault(tiers)):
        fields.fault("tiers", fault)
    entry = PriceBookEntry(
        booking_configuration=fields.integer("booking_configuration", minimum=1),
        booking_category=fields.text("booking_category"),
        tiers_go_by=tiers_go_by,
        tiers=tuple(tiers),
        billing_basis=(
            fields.choice("billing_basis", BILLING_BASES)
            if version == FORMAT
            else "per_booking"
        ),
        group=(
            fields.text("group", required=False, preserve_whitespace=True)
            if version == FORMAT
            else ""
        ),
    )
    fields.done()
    return entry


def _check_names(setup: Setup, faults: list[str]) -> None:
    """Every name is given once, and every name that is used points at something."""

    def once(
        section: str, what: str, values: Iterable[object], *, fold: bool = False
    ) -> None:
        seen: set[object] = set()
        for value in values:
            key = value.lower() if fold and isinstance(value, str) else value
            # An empty value was already reported where it was read.
            if value and key in seen:
                faults.append(
                    f"{section}: the {what} {value!r} is used more than once."
                    + (" Names are the same whatever their case." if fold else "")
                )
            seen.add(key)

    def known(where: str, what: str, names: Iterable[str], among: list[str]) -> None:
        for name in names:
            if name and name not in among:
                faults.append(f"{where}: there is no {what} named {name!r}.")

    assets = [asset.name for asset in setup.assets]
    spaces = [space.name for space in setup.spaces]
    booking_types = [booking_type.name for booking_type in setup.booking_types]
    categories = [category.name for category in setup.booking_categories]
    roles = [role.name for role in setup.roles]
    price_books = [price_book.name for price_book in setup.price_books]
    configurations = {item.number: item for item in setup.booking_configurations}

    once("assets", "number", [asset.number for asset in setup.assets])
    once("assets", "name", assets)
    once("spaces", "number", [space.number for space in setup.spaces])
    once("spaces", "name", spaces)
    for place, asset in enumerate(setup.assets, start=1):
        known(f"assets[{place}].space", "space", [asset.space], spaces)
    once("booking_types", "name", booking_types, fold=True)
    once("booking_categories", "name", categories, fold=True)
    once("roles", "name", roles, fold=True)
    once(
        "booking_configurations",
        "number",
        [n.number for n in setup.booking_configurations],
    )
    once("price_books", "number", [book.number for book in setup.price_books])
    once("price_books", "name", price_books)
    once(
        "availability_calendars",
        "asset and start date",
        [
            f"{calendar.asset}, {calendar.start_date or 'no start date'}"
            for calendar in setup.availability_calendars
        ],
    )

    known(
        "organization.standard_role", "role", [setup.organization.standard_role], roles
    )
    for place, calendar in enumerate(setup.availability_calendars, start=1):
        known(
            f"availability_calendars[{place}].asset", "asset", [calendar.asset], assets
        )
    for place, role in enumerate(setup.roles, start=1):
        where = f"roles[{place}].booking_categories"
        known(where, "booking category", role.booking_categories, categories)
    for place, item in enumerate(setup.booking_configurations, start=1):
        where = f"booking_configurations[{place}]"
        known(f"{where}.asset", "asset", [item.asset], assets)
        known(
            f"{where}.booking_type", "booking type", [item.booking_type], booking_types
        )
        known(f"{where}.price_book", "price book", [item.price_book], price_books)
        known(
            f"{where}.booking_categories",
            "booking category",
            item.booking_categories,
            categories,
        )

    for book_place, price_book in enumerate(setup.price_books, start=1):
        for entry_place, entry in enumerate(price_book.entries, start=1):
            where = f"price_books[{book_place}].entries[{entry_place}]"
            priced_item = configurations.get(entry.booking_configuration)
            if priced_item is None:
                faults.append(
                    f"{where}.booking_configuration: there is no booking "
                    f"configuration numbered {entry.booking_configuration}."
                )
                continue
            if not priced_item.price_book:
                faults.append(
                    f"{where}.booking_configuration: configuration "
                    f"{priced_item.number} is without payment: it has no price "
                    "book, and no price applies to it."
                )
            elif priced_item.price_book != price_book.name:
                faults.append(
                    f"{where}.booking_configuration: configuration "
                    f"{priced_item.number} links to the price book "
                    f"{priced_item.price_book!r}, not to this one."
                )
            if entry.booking_category not in priced_item.booking_categories:
                faults.append(
                    f"{where}.booking_category: configuration {priced_item.number} "
                    f"does not apply to {entry.booking_category!r}."
                )
    _check_entries_of_a_category(setup, set(configurations), faults)


def _check_entries_of_a_category(
    setup: Setup, known: set[int], faults: list[str]
) -> None:
    """The entries of one container in one booking category, over every price
    book, by the rule the screens share (`price_entries_rule`). A fault is
    reported once for the category, at the entry that shows it: the second
    entry for a group named twice, the first for the rest."""
    found: dict[tuple[int, str], list[tuple[str, Any]]] = {}
    for book_place, book in enumerate(setup.price_books, start=1):
        for place, entry in enumerate(book.entries, start=1):
            if entry.booking_configuration in known:
                found.setdefault(
                    (entry.booking_configuration, entry.booking_category), []
                ).append((f"price_books[{book_place}].entries[{place}]", entry))
    for held in found.values():
        fault = what_the_entries_of_a_category_get_wrong(
            [(entry.group or None, entry.billing_basis) for _, entry in held]
        )
        if fault == GROUP_TWICE:
            seen: set[str] = set()
            for where, entry in held:
                if (entry.group or "") in seen:
                    faults.append(
                        f"{where}: this configuration and booking category "
                        + (
                            f"already have a price for the group {entry.group!r}."
                            if entry.group
                            else "already have a price."
                        )
                    )
                    break
                seen.add(entry.group or "")
        elif fault == NEEDS_NORMAL_PRICE:
            faults.append(
                f"{held[0][0]}.group: a price for a group needs the container's "
                "normal price for the booking category too, for the spots in no "
                "group."
            )
        elif fault == GROUPS_PER_SPOT:
            where = next(
                where
                for where, entry in held
                if entry.billing_basis != PER_CAPACITY_UNIT
            )
            faults.append(
                f"{where}.billing_basis: a category with prices for groups "
                "is priced per spot, per_capacity_unit, in every entry of it."
            )


def validate(setup: Setup) -> None:
    """Check references and lossless version semantics, also for API callers."""
    faults: list[str] = []
    if fault := _format_fault(setup.format):
        faults.append(f"format: {fault}")
    _check_names(setup, faults)
    _check_capacity(setup, faults)
    _check_as_read(setup, faults)
    _check_category_hours(setup, faults)
    _check_space_zones(setup, faults)
    _check_closures(setup, faults)
    if faults:
        raise SetupError(faults)


def _check_as_read(setup: Setup, faults: list[str]) -> None:
    """Every rule the reader applies field by field, for a setup built in
    code: the same rules, at the place each value has in a file."""

    def check(where: str, fault: str | None) -> None:
        if fault:
            faults.append(f"{where}: {fault}")

    def text(where: str, value: object, *, preserve_whitespace: bool = False) -> None:
        check(where, _text_fault(value, preserve_whitespace=preserve_whitespace))

    def optional_text(
        where: str, value: object, *, preserve_whitespace: bool = False
    ) -> None:
        # Empty, a text the file leaves out.
        if value != "":
            text(where, value, preserve_whitespace=preserve_whitespace)

    def number(where: str, value: object, minimum: int = 1) -> None:
        check(where, _whole_number_fault(value, minimum=minimum))

    organization = setup.organization
    for key in (
        "name",
        "subdomain",
        "time_zone",
        "currency",
        "contact_email",
        "standard_role",
    ):
        text(f"organization.{key}", getattr(organization, key))
    for key, rule in _ORGANIZATION_RULES:
        value = getattr(organization, key)
        if value and type(value) is str:
            check(f"organization.{key}", rule(value))
    text("organization.language", organization.language)
    number("organization.member_retention_months", organization.member_retention_months)
    number(
        "organization.booking_retention_months", organization.booking_retention_months
    )
    for key in ("max_bookings_a_day", "book_ahead_months"):
        if (limit := getattr(organization, key)) is not None:
            number(f"organization.{key}", limit)
    check(
        "organization.tax_rate",
        _whole_number_fault(organization.tax_rate, minimum=0, maximum=MOST_TAX_RATE),
    )
    check(
        "organization.free_cancellation_days",
        _whole_number_fault(
            organization.free_cancellation_days,
            minimum=0,
            maximum=MOST_FREE_CANCELLATION_DAYS,
        ),
    )
    given: set[str] = set()
    for key, label_text in setup.labels:
        check(f"labels.{key}", label_set.fault(organization.language, key, label_text))
        if key in given:
            faults.append(f"labels: the key {key!r} is used more than once.")
        given.add(key)
    for place, space in enumerate(setup.spaces, start=1):
        at = f"spaces[{place}]"
        number(f"{at}.number", space.number)
        text(f"{at}.name", space.name, preserve_whitespace=True)
        optional_text(f"{at}.description", space.description, preserve_whitespace=True)
        check(f"{at}.booking_page", _choice_fault(space.booking_page, BOOKING_PAGES))
    for place, asset in enumerate(setup.assets, start=1):
        at = f"assets[{place}]"
        number(f"{at}.number", asset.number)
        text(f"{at}.name", asset.name)
        optional_text(f"{at}.description", asset.description)
        optional_text(f"{at}.space", asset.space, preserve_whitespace=True)
        optional_text(f"{at}.time_zone", asset.time_zone)
        if asset.time_zone and type(asset.time_zone) is str:
            check(f"{at}.time_zone", _time_zone_fault(asset.time_zone))
        check(f"{at}.whole_space", _yes_no_fault(asset.whole_space))
        if asset.maximum_occupancy is not None:
            number(f"{at}.maximum_occupancy", asset.maximum_occupancy)
        for count, plan in enumerate(asset.floor_plans, start=1):
            within = f"{at}.floor_plans[{count}]"
            optional_text(f"{within}.name", plan.name, preserve_whitespace=True)
            text(f"{within}.spot_word", plan.spot_word, preserve_whitespace=True)
            check(f"{within}.spots", _spots_fault(plan.spots))
            for order, group in enumerate(plan.groups, start=1):
                part = f"{within}.groups[{order}]"
                text(f"{part}.name", group.name, preserve_whitespace=True)
                text(f"{part}.word", group.word, preserve_whitespace=True)
                check(f"{part}.spots", _spot_numbers_fault(group.spots))
    for place, calendar in enumerate(setup.availability_calendars, start=1):
        at = f"availability_calendars[{place}]"
        text(f"{at}.asset", calendar.asset)
        if calendar.start_date is not None:
            check(f"{at}.start_date", _day_fault(calendar.start_date))
        # As the writer writes them: one line for the weekdays that share a
        # start and an end, each weekday named once.
        lines: dict[tuple[int, int], list[str]] = {}
        for span in calendar.spans:
            lines.setdefault((span.start, span.end), []).append(span.weekday)
        for count, ((start, end), weekdays) in enumerate(lines.items(), start=1):
            within = f"{at}.spans[{count}]"
            before = len(faults)
            check(f"{within}.weekdays", _weekdays_fault(weekdays))
            check(f"{within}.start", _time_of_day_fault(start))
            check(f"{within}.end", _time_of_day_fault(end, end_of_day=True))
            if len(faults) == before:
                check(f"{within}.end", _span_end_fault(start, end))
    for place, booking_type in enumerate(setup.booking_types, start=1):
        check(f"booking_types[{place}].name", name_fault(booking_type.name))
        optional_text(f"booking_types[{place}].description", booking_type.description)
    for place, category in enumerate(setup.booking_categories, start=1):
        check(f"booking_categories[{place}].name", name_fault(category.name))
        if category.icon:
            check(
                f"booking_categories[{place}].icon",
                _choice_fault(category.icon, CATEGORY_ICONS),
            )
    for place, role in enumerate(setup.roles, start=1):
        at = f"roles[{place}]"
        check(f"{at}.name", name_fault(role.name))
        check(f"{at}.booking_categories", _names_fault(role.booking_categories))
        check(f"{at}.admin_access", _yes_no_fault(role.admin_access))
    for place, item in enumerate(setup.booking_configurations, start=1):
        at = f"booking_configurations[{place}]"
        number(f"{at}.number", item.number)
        text(f"{at}.asset", item.asset)
        text(f"{at}.booking_type", item.booking_type)
        check(f"{at}.period_type", _choice_fault(item.period_type, PERIOD_TYPES))
        check(f"{at}.period_start", _time_of_day_fault(item.period_start))
        check(f"{at}.period_end", _time_of_day_fault(item.period_end))
        number(f"{at}.end_day", item.end_day, minimum=0)
        check(
            f"{at}.period_start_weekdays",
            _weekdays_fault(item.period_start_weekdays),
        )
        lengths = (
            ("interval", item.interval, True),
            ("minimum_length", item.minimum_length, False),
            ("maximum_length", item.maximum_length, True),
        )
        for key, length, required in lengths:
            if item.period_type != "flexible":
                check(f"{at}.{key}", _fixed_period_fault(length))
            elif length is not None:
                check(f"{at}.{key}", _length_fault(length))
            elif required:
                check(f"{at}.{key}", "is missing.")
        if item.start_date is not None:
            check(f"{at}.start_week", _week_fault(item.start_date, last=False))
        if item.stop_date is not None:
            check(f"{at}.stop_week", _week_fault(item.stop_date, last=True))
        check(f"{at}.status", _choice_fault(item.status, STATUSES))
        # Left out without payment (W12); format 1 needs it (`_check_capacity`).
        optional_text(f"{at}.price_book", item.price_book)
        check(f"{at}.booking_categories", _names_fault(item.booking_categories))
        check(
            f"{at}.consumption_mode",
            _choice_fault(item.consumption_mode, CONSUMPTION_MODES),
        )
        check(
            f"{at}.every_weeks",
            _whole_number_fault(item.every_weeks, minimum=1, maximum=_MOST_WEEKS),
        )
        optional_text(f"{at}.floor_plan", item.floor_plan, preserve_whitespace=True)
        # As the writer writes them: one line for the weekdays of one
        # category that share a start and an end.
        written: dict[tuple[str, int, int], list[str]] = {}
        for hours in item.category_hours:
            written.setdefault(
                (hours.booking_category, hours.start, hours.end), []
            ).append(hours.weekday)
        for count, ((name, start, end), weekdays) in enumerate(
            written.items(), start=1
        ):
            within = f"{at}.category_hours[{count}]"
            before = len(faults)
            text(f"{within}.booking_category", name)
            check(f"{within}.weekdays", _weekdays_fault(weekdays))
            check(f"{within}.start", _time_of_day_fault(start))
            check(f"{within}.end", _time_of_day_fault(end, end_of_day=True))
            if len(faults) == before:
                check(f"{within}.end", _span_end_fault(start, end))
    for place, book in enumerate(setup.price_books, start=1):
        at = f"price_books[{place}]"
        number(f"{at}.number", book.number)
        text(f"{at}.name", book.name)
        optional_text(f"{at}.currency", book.currency)
        if book.currency and type(book.currency) is str:
            check(f"{at}.currency", _currency_fault(book.currency))
        for count, entry in enumerate(book.entries, start=1):
            within = f"{at}.entries[{count}]"
            number(f"{within}.booking_configuration", entry.booking_configuration)
            text(f"{within}.booking_category", entry.booking_category)
            check(
                f"{within}.tiers_go_by", _choice_fault(entry.tiers_go_by, TIERS_GO_BY)
            )
            check(
                f"{within}.billing_basis",
                _choice_fault(entry.billing_basis, BILLING_BASES),
            )
            optional_text(f"{within}.group", entry.group, preserve_whitespace=True)
            check(f"{within}.tiers", _tiers_fault(entry.tiers))
            for order, tier in enumerate(entry.tiers, start=1):
                part = f"{within}.tiers[{order}]"
                check(
                    f"{part}.up_to",
                    _length_fault(tier.up_to)
                    if entry.tiers_go_by == "length"
                    else _time_of_day_fault(tier.up_to, end_of_day=True),
                )
                check(f"{part}.paid_per", _length_fault(tier.paid_per))
                check(f"{part}.amount", _amount_fault(tier.amount))


def _check_capacity(setup: Setup, faults: list[str]) -> None:
    """Spaces, floor plans and how a container books its asset."""
    if setup.format != FORMAT:
        if (
            setup.spaces
            or any(
                a.space or a.whole_space or a.maximum_occupancy or a.floor_plans
                for a in setup.assets
            )
            or any(
                c.consumption_mode != "exclusive" or c.floor_plan
                for c in setup.booking_configurations
            )
            or any(
                e.billing_basis != "per_booking" or e.group
                for b in setup.price_books
                for e in b.entries
            )
        ):
            faults.append(
                f"format: format 1 would lose floor plan data; use format {FORMAT}."
            )
        if setup.labels:
            faults.append(f"format: format 1 would lose labels; use format {FORMAT}.")
        for place, item in enumerate(setup.booking_configurations, start=1):
            if not item.price_book:
                faults.append(
                    f"booking_configurations[{place}].price_book: is missing: "
                    "format 1 has no container without payment; use format "
                    f"{FORMAT}."
                )
        return
    marked: set[str] = set()
    plans: dict[str, dict[str, FloorPlan]] = {}
    for place, asset in enumerate(setup.assets, start=1):
        where = f"assets[{place}]"
        fault = space_mark_fault(
            whole_space=asset.whole_space, space=asset.space, marked_spaces=marked
        )
        if fault == NEEDS_SPACE:
            faults.append(
                f"{where}.whole_space: needs a space: an asset without one "
                "cannot cover its whole space."
            )
        elif fault == SPACE_ALREADY_MARKED:
            faults.append(
                f"{where}.whole_space: this space already has an asset that "
                "covers it; different whole-space offers are its containers."
            )
        elif asset.whole_space:
            marked.add(asset.space)
        plans[asset.name] = {plan.name: plan for plan in asset.floor_plans}
        named = [plan.name for plan in asset.floor_plans]
        for number, plan in enumerate(asset.floor_plans, start=1):
            at = f"{where}.floor_plans[{number}]"
            # The model's own rule (`AssetInventory.clean`), asked of the
            # asset's other floor plans.
            fault = floor_plan_name_fault(
                plan.name,
                [x for place, x in enumerate(named, start=1) if place != number],
            )
            if fault == NEEDS_NAME:
                faults.append(
                    f"{at}.name: is missing: an asset with more than one floor "
                    "plan names each."
                )
            elif fault == NAME_USED and named.index(plan.name) != number - 1:
                faults.append(
                    f"{where}.floor_plans: the name {plan.name!r} is used more "
                    "than once."
                )
            _check_groups(at, plan, faults)
    exclusive = set()
    for place, item in enumerate(setup.booking_configurations, start=1):
        where = f"booking_configurations[{place}].floor_plan"
        asset_plans = plans.get(item.asset)
        if item.consumption_mode == "exclusive":
            exclusive.add(item.number)
            if item.floor_plan:
                faults.append(
                    f"{where}: an exclusive container takes its whole asset and "
                    "names no floor plan."
                )
        elif asset_plans is None:
            continue  # An unknown asset was reported by _check_names.
        elif item.floor_plan and item.floor_plan not in asset_plans:
            faults.append(
                f"{where}: {item.asset} has no floor plan named {item.floor_plan!r}."
            )
        elif not item.floor_plan and len(asset_plans) > 1:
            faults.append(
                f"{where}: is missing: a shared container names the floor plan "
                f"of {item.asset} it books."
            )
    configurations = {item.number: item for item in setup.booking_configurations}
    for book_place, book in enumerate(setup.price_books, start=1):
        for place, entry in enumerate(book.entries, start=1):
            where = f"price_books[{book_place}].entries[{place}]"
            if (
                entry.billing_basis == "per_capacity_unit"
                and entry.booking_configuration in exclusive
            ):
                faults.append(
                    f"{where}.billing_basis: "
                    "an exclusive booking configuration is priced per booking; "
                    "per_capacity_unit needs a shared one."
                )
            container = configurations.get(entry.booking_configuration)
            if (
                not entry.group
                or container is None
                or container.consumption_mode != "shared"
            ):
                continue
            asset_plans = plans.get(container.asset, {})
            booked: FloorPlan | None
            if not container.floor_plan and len(asset_plans) == 1:
                booked = next(iter(asset_plans.values()))  # Its only one.
            elif len(asset_plans) > 1 and container.floor_plan not in asset_plans:
                continue  # The container's floor plan was reported above.
            else:
                booked = asset_plans.get(container.floor_plan)
            if booked is None or entry.group not in [g.name for g in booked.groups]:
                faults.append(
                    f"{where}.group: the floor plan of configuration "
                    f"{container.number} has no group named {entry.group!r}."
                )


def _check_category_hours(setup: Setup, faults: list[str]) -> None:
    """A container's own hours for a booking category : for a category
    the container names, no two stretches of a weekday share time, each inside
    the container's hours (`category_hours_rule`, the rule the validator
    asks). Lines are counted as the writer writes them."""
    for place, item in enumerate(setup.booking_configurations, start=1):
        lines: dict[tuple[str, int, int], list[str]] = {}
        for hours in item.category_hours:
            lines.setdefault(
                (hours.booking_category, hours.start, hours.end), []
            ).append(hours.weekday)
        at = f"booking_configurations[{place}].category_hours"
        valid: dict[str, list[tuple[int, int, int]]] = {}
        container_days = [
            WEEKDAYS.index(day) for day in item.period_start_weekdays if day in WEEKDAYS
        ]
        for count, ((name, start, end), weekdays) in enumerate(lines.items(), start=1):
            within = f"{at}[{count}]"
            if name not in item.booking_categories:
                if name:
                    faults.append(
                        f"{within}.booking_category: configuration {item.number} "
                        f"does not apply to {name!r}."
                    )
                continue
            if not (0 <= start < end <= _END_OF_DAY) or any(
                day not in WEEKDAYS for day in weekdays
            ):
                continue  # Reported where the line is read.
            stretches = [(WEEKDAYS.index(day), start, end) for day in weekdays]
            outside = category_hours_rule.hours_outside(
                item.period_start,
                item.period_end,
                item.end_day,
                container_days,
                stretches,
            )
            if outside:
                faults.append(
                    f"{within}: {', '.join(WEEKDAYS[day] for day, _, _ in outside)} "
                    f"{_time(start)} to {_time(end)} is not inside the hours of "
                    f"configuration {item.number}."
                )
            valid.setdefault(name, []).extend(stretches)
        for name, stretches in valid.items():
            if shared := category_hours_rule.overlapping(stretches):
                one, other = shared
                faults.append(
                    f"{at}: {name!r} has {WEEKDAYS[one[0]]} {_time(one[1])} to "
                    f"{_time(one[2])} and {_time(other[1])} to {_time(other[2])}, "
                    "which share time."
                )


def _check_space_zones(setup: Setup, faults: list[str]) -> None:
    """The assets of one space share one time zone: the space's calendar
    answers a date period for all of them ."""
    found: dict[str, tuple[str, str]] = {}
    for place, asset in enumerate(setup.assets, start=1):
        if not asset.space or not isinstance(asset.time_zone, str):
            continue
        zone = asset.time_zone or setup.organization.time_zone
        first = found.setdefault(asset.space, (asset.name, zone))
        if first[1] != zone:
            faults.append(
                f"assets[{place}].time_zone: {first[0]!r} in the same space is in "
                f"{first[1]}, and the assets of a space share one time zone."
            )


def _check_closures(setup: Setup, faults: list[str]) -> None:
    """A closure names an asset, its dates are days of the supported years, it
    ends on or after its first date, and it needs format 3."""
    names = {asset.name for asset in setup.assets}
    if setup.closures and setup.format != FORMAT:
        faults.append(f"format: format 1 would lose closures; use format {FORMAT}.")
    for place, closure in enumerate(setup.closures, start=1):
        at = f"closures[{place}]"
        if closure.asset and closure.asset not in names:
            faults.append(f"{at}.asset: there is no asset named {closure.asset!r}.")
        if _text_fault(closure.asset):
            faults.append(f"{at}.asset: {_text_fault(closure.asset)}")
        dates = [closure.first_date, closure.last_date]
        if any(_day_fault(value) for value in dates):
            faults.append(f"{at}: the dates must be {_A_DATE}.")
            continue
        if not all(CLOSURE_FIRST_DAY <= value <= CLOSURE_LAST_DAY for value in dates):
            faults.append(f"{at}: the dates must be from 2000 to 2100.")
        elif closure.last_date < closure.first_date:
            faults.append(f"{at}.last_date: must not be before first_date.")
        if closure.reason and _choice_fault(closure.reason, CLOSURE_REASONS):
            faults.append(
                f"{at}.reason: {_choice_fault(closure.reason, CLOSURE_REASONS)}"
            )


def _check_groups(where: str, plan: FloorPlan, faults: list[str]) -> None:
    """A group's spots are on its floor plan, and a spot is in one group at
    most."""
    names = [group.name for group in plan.groups]
    grouped: dict[int, str] = {}
    for number, group in enumerate(plan.groups, start=1):
        at = f"{where}.groups[{number}]"
        if group.name and names.index(group.name) != number - 1:
            faults.append(
                f"{where}.groups: the name {group.name!r} is used more than once."
            )
        for spot in group.spots:
            if not isinstance(spot, int) or isinstance(spot, bool):
                continue  # The spot-number rule names it.
            if spot > len(plan.spots):
                faults.append(
                    f"{at}.spots: there is no spot {spot}; the floor plan has "
                    f"{len(plan.spots)}."
                )
            elif spot in grouped:
                faults.append(
                    f"{at}.spots: spot {spot} is in the group "
                    f"{grouped[spot]!r} already; a spot is in one group at most."
                )
            else:
                grouped[spot] = group.name


def write(setup: Setup) -> str:
    """A setup as the text of a setup file."""
    validate(setup)
    organization = setup.organization
    lines = [
        f"# The setup of {organization.name}.",
        "# The format is described in docs/setup-file.md.",
        "",
        f"format = {setup.format}",
        # Empty collections still need to survive writing and reading.
        *[
            f"{key} = []"
            for key in (
                "assets",
                "availability_calendars",
                "booking_types",
                "booking_categories",
                "roles",
                "booking_configurations",
                "price_books",
            )
            if not getattr(setup, key)
        ],
        "",
        "[organization]",
        f"name = {_text(organization.name)}",
        f"subdomain = {_text(organization.subdomain)}",
        f"time_zone = {_text(organization.time_zone)}",
        f"currency = {_text(organization.currency)}",
        f"contact_email = {_text(organization.contact_email)}",
        f"standard_role = {_text(organization.standard_role)}",
        f"member_retention_months = {organization.member_retention_months}",
        f"booking_retention_months = {organization.booking_retention_months}",
    ]
    for key in ("max_bookings_a_day", "book_ahead_months"):
        if (limit := getattr(organization, key)) is not None:
            lines += [f"{key} = {limit}"]
    if organization.tax_rate:
        lines += [f"tax_rate = {organization.tax_rate}"]
    if organization.free_cancellation_days != DEFAULT_FREE_CANCELLATION_DAYS:
        lines += [f"free_cancellation_days = {organization.free_cancellation_days}"]
    if organization.language != label_set.DEFAULT_LANGUAGE:
        lines += [f"language = {_text(organization.language)}"]
    if setup.labels:
        lines += [
            "",
            "[labels]",
            *[f"{_text(key)} = {_text(text)}" for key, text in setup.labels],
        ]
    for space in setup.spaces:
        lines += [
            "",
            "[[spaces]]",
            f"number = {space.number}",
            f"name = {_text(space.name)}",
        ]
        lines += _optional("description", space.description)
        if space.booking_page != BOOKING_PAGES[0]:
            lines += [f"booking_page = {_text(space.booking_page)}"]
    for asset in setup.assets:
        lines += ["", "[[assets]]", f"number = {asset.number}"]
        lines += [f"name = {_text(asset.name)}"]
        lines += _optional("description", asset.description)
        lines += _optional("space", asset.space)
        if asset.time_zone and asset.time_zone != organization.time_zone:
            lines += [f"time_zone = {_text(asset.time_zone)}"]
        if asset.whole_space:
            lines += ["whole_space = true"]
        if asset.maximum_occupancy is not None:
            lines += [f"maximum_occupancy = {asset.maximum_occupancy}"]
        for plan in asset.floor_plans:
            lines += ["", "[[assets.floor_plans]]", *_optional("name", plan.name)]
            lines += [f"spot_word = {_text(plan.spot_word)}"]
            if any(plan.spots):
                lines += ["spots = [", *[f"  {_text(name)}," for name in plan.spots]]
                lines += ["]"]
            else:
                lines += [f"spots = {len(plan.spots)}"]
            for group in plan.groups:
                lines += [
                    "",
                    "[[assets.floor_plans.groups]]",
                    f"name = {_text(group.name)}",
                ]
                if group.word != "group":
                    lines += [f"word = {_text(group.word)}"]
                lines += [f"spots = [{', '.join(map(str, group.spots))}]"]
    for calendar in setup.availability_calendars:
        lines += ["", "[[availability_calendars]]", f"asset = {_text(calendar.asset)}"]
        if calendar.start_date is not None:
            lines += [f"start_date = {calendar.start_date.isoformat()}"]
        lines += ["spans = [", *_span_lines(calendar.spans), "]"]
    for closure in setup.closures:
        lines += ["", "[[closures]]", f"asset = {_text(closure.asset)}"]
        lines += [f"first_date = {closure.first_date.isoformat()}"]
        if closure.last_date != closure.first_date:
            lines += [f"last_date = {closure.last_date.isoformat()}"]
        lines += _optional("reason", closure.reason)
    for booking_type in setup.booking_types:
        lines += ["", "[[booking_types]]", f"name = {_text(booking_type.name)}"]
        lines += _optional("description", booking_type.description)
    for category in setup.booking_categories:
        lines += ["", "[[booking_categories]]", f"name = {_text(category.name)}"]
        lines += _optional("icon", category.icon)
    for role in setup.roles:
        lines += [
            "",
            "[[roles]]",
            f"name = {_text(role.name)}",
            f"booking_categories = {_texts(role.booking_categories)}",
            f"admin_access = {'true' if role.admin_access else 'false'}",
        ]
    for item in setup.booking_configurations:
        lines += [
            "",
            "[[booking_configurations]]",
            f"number = {item.number}",
            f"asset = {_text(item.asset)}",
            f"booking_type = {_text(item.booking_type)}",
            f"period_type = {_text(item.period_type)}",
            f"period_start = {_text(_time(item.period_start))}",
            f"period_end = {_text(_time(item.period_end))}",
            f"end_day = {item.end_day}",
            f"period_start_weekdays = {_texts(item.period_start_weekdays)}",
        ]
        for key, value in (
            ("interval", item.interval),
            ("minimum_length", item.minimum_length),
            ("maximum_length", item.maximum_length),
        ):
            if value is not None:
                lines += [f"{key} = {_text(_length(value))}"]
        for key, day in (
            ("start_week", item.start_date),
            ("stop_week", item.stop_date),
        ):
            if day is not None:
                year, number, _ = day.isocalendar()
                lines += [f'{key} = "{year}-W{number:02d}"']
        if item.every_weeks != 1:
            lines += [f"every_weeks = {item.every_weeks}"]
        lines += [
            f"status = {_text(item.status)}",
            *_optional("price_book", item.price_book),
            f"booking_categories = {_texts(item.booking_categories)}",
        ]
        if item.category_hours:
            lines += ["category_hours = [", *_category_hours_lines(item), "]"]
        if setup.format == FORMAT:
            lines += [f"consumption_mode = {_text(item.consumption_mode)}"]
            lines += _optional("floor_plan", item.floor_plan)
    for price_book in setup.price_books:
        lines += [
            "",
            "[[price_books]]",
            f"number = {price_book.number}",
            f"name = {_text(price_book.name)}",
        ]
        if price_book.currency and price_book.currency != organization.currency:
            lines += [f"currency = {_text(price_book.currency)}"]
        # Its amounts in its own currency's decimals.
        decimals = CURRENCY_DECIMALS.get(
            price_book.currency or organization.currency, 2
        )
        for entry in price_book.entries:
            by_time = entry.tiers_go_by == "time of day"
            lines += [
                "",
                "[[price_books.entries]]",
                f"booking_configuration = {entry.booking_configuration}",
                f"booking_category = {_text(entry.booking_category)}",
                f"tiers_go_by = {_text(entry.tiers_go_by)}",
                *(
                    [f"billing_basis = {_text(entry.billing_basis)}"]
                    if setup.format == FORMAT
                    else []
                ),
                *(_optional("group", entry.group) if setup.format == FORMAT else []),
                "tiers = [",
            ]
            for tier in entry.tiers:
                up_to = _time(tier.up_to) if by_time else _length(tier.up_to)
                lines += [
                    f"  {{ up_to = {_text(up_to)}, "
                    f"paid_per = {_text(_length(tier.paid_per))}, "
                    f"amount = {_amount(tier.amount, decimals)} }},"
                ]
            lines += ["]"]
    return "\n".join(lines) + "\n"


def _text(value: str) -> str:
    """A text in quotes, as TOML writes it."""
    # JSON escapes what TOML wants escaped, but for one character.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _texts(values: Iterable[str]) -> str:
    return "[" + ", ".join(_text(value) for value in values) + "]"


def _optional(key: str, value: str) -> list[str]:
    return [f"{key} = {_text(value)}"] if value else []


def _time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _length(minutes: int) -> str:
    return f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes} min"


def _amount(smallest: int, decimals: int) -> str:
    """Money in the currency's main unit: 25000 is 250, and 24950 is 249.50."""
    whole, part = divmod(smallest, 10**decimals)
    return str(whole) if part == 0 else f"{whole}.{part:0{decimals}d}"


def _category_hours_lines(item: BookingConfiguration) -> list[str]:
    """One line for the weekdays of a category that share a start and an end."""
    together: dict[tuple[str, int, int], list[str]] = {}
    for hours in item.category_hours:
        together.setdefault(
            (hours.booking_category, hours.start, hours.end), []
        ).append(hours.weekday)
    return [
        f"  {{ booking_category = {_text(name)}, "
        f"weekdays = {_texts(sorted(weekdays, key=WEEKDAYS.index))}, "
        f"start = {_text(_time(start))}, end = {_text(_time(end))} }},"
        for (name, start, end), weekdays in together.items()
    ]


def _span_lines(spans: Iterable[AvailableSpan]) -> list[str]:
    """One line for the weekdays that share a start and an end."""
    together: dict[tuple[int, int], list[str]] = {}
    for span in spans:
        together.setdefault((span.start, span.end), []).append(span.weekday)
    return [
        f"  {{ weekdays = {_texts(sorted(weekdays, key=WEEKDAYS.index))}, "
        f"start = {_text(_time(start))}, end = {_text(_time(end))} }},"
        for (start, end), weekdays in together.items()
    ]
