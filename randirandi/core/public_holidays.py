# SPDX-License-Identifier: AGPL-3.0-only
"""Norway's public holidays, for the closures sheet .

The first customer's list: the helligdager (New Year's Day, Maundy Thursday,
Good Friday, Easter Sunday and Monday, Ascension Day, Whit Sunday and Monday,
Christmas Day and Boxing Day) and 1 and 17 May. It is offered to pick from, and
a picked day becomes an ordinary closure by date with the reason "holiday": the
list is not stored and nothing reads it at run time.
"""

from datetime import date, timedelta

FIRST_YEAR = 1900
LAST_YEAR = 2099


def easter(year: int) -> date:
    """Easter Sunday of a year (the Gregorian computus)."""
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise ValueError("The year is outside 1900 to 2099.")
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holidays_of(year: int) -> list[tuple[date, str]]:
    """The year's public holidays by date, each with its name. Two on one day
    (17 May and Whit Monday in 2027) are one entry with both names."""
    sunday = easter(year)
    found = [
        (date(year, 1, 1), "New Year's Day"),
        (sunday - timedelta(days=3), "Maundy Thursday"),
        (sunday - timedelta(days=2), "Good Friday"),
        (sunday, "Easter Sunday"),
        (sunday + timedelta(days=1), "Easter Monday"),
        (date(year, 5, 1), "Labour Day"),
        (sunday + timedelta(days=39), "Ascension Day"),
        (date(year, 5, 17), "Constitution Day"),
        (sunday + timedelta(days=49), "Whit Sunday"),
        (sunday + timedelta(days=50), "Whit Monday"),
        (date(year, 12, 25), "Christmas Day"),
        (date(year, 12, 26), "Boxing Day"),
    ]
    merged: list[tuple[date, str]] = []
    for day, name in sorted(found):
        if merged and merged[-1][0] == day:
            merged[-1] = (day, f"{merged[-1][1]}, {name}")
        else:
            merged.append((day, name))
    return merged
