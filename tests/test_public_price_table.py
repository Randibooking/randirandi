# SPDX-License-Identifier: AGPL-3.0-only
"""The public price list and booking conditions inside the member shell, the
beta bar, and the price table without JavaScript .

A member of the organization of the address sees both pages in the member
shell, with Book, Events and My page; a visitor who is not signed in keeps the
header with Sign in. The price list's server-drawn page holds the standard
role's rows, the rows the app draws from the same public answers.
"""

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from randirandi import labels, setup_file
from randirandi.core import price_table
from randirandi.core import setup as setups
from randirandi.core.models import (
    Member,
    MemberStatus,
    Organization,
    Origin,
    Role,
)
from randirandi.core.public_pages import _stand_in
from tests import made_up
from tests.test_booking_conditions import (
    accepted_order,
    answer,
    browser,
    standard_role,
)
from tests.test_booking_conditions import everything as everything
from tests.test_calendar import Example
from tests.test_calendar import made as made
from tests.test_login_sessions import NOW, config_of, get, sign_in
from tests.test_login_sessions import page_settings as page_settings

pytestmark = pytest.mark.django_db(transaction=True)

BOOK = Path(__file__).parent.parent / "frontend/src/book"
# The data both ports must reach, as the JavaScript priceBook worked it out
# (frontend/src/book/prices-golden.test.js is the other side): a made-up
# organization, and the Fjordside setup as the server answers it.
GOLDEN = json.loads((BOOK / "price-book.golden.json").read_text(encoding="utf-8"))
FJORDSIDE = json.loads(
    (BOOK / "price-book-fjordside.golden.json").read_text(encoding="utf-8")
)
FJORDSIDE_HOST = "fjordside.sandbox.example.org"
PLACES = ('href="/book/"', 'href="/events/"', 'href="/me/"')


@pytest.mark.parametrize("golden", [GOLDEN, FJORDSIDE], ids=["made-up", "fjordside"])
def test_the_price_book_reaches_the_data_the_app_reaches(
    golden: dict[str, Any],
) -> None:
    # The golden's `expected` is the JavaScript priceBook's output, not this
    # port's: the port is held to the app's data, never to itself.
    found = price_table.price_book(
        golden["answers"],
        golden["categories"],
        golden["order"],
        price_table.Words(Organization(labels={})),
    )
    assert found == golden["expected"]


def test_the_price_book_takes_the_organizations_own_words() -> None:
    held = {"prices.every-day": "Daily", "length.hours.other": "{count} hrs"}
    found = price_table.price_book(
        GOLDEN["answers"],
        GOLDEN["categories"],
        GOLDEN["order"],
        price_table.Words(Organization(labels=held)),
    )
    sauna = found[0]["blocks"][0]
    assert sauna["days"] == "Daily"
    assert "4 hrs" in sauna["cells"][0]["lines"][0]
    assert set(held) <= set(labels.DEFAULTS)


def public_answers(client: Client, made: Example) -> tuple[Any, list[Any]]:
    """What the app reads, over HTTP, to draw the page: the choices and one
    calendar answer for each category of the standard role."""
    choices = answer(get(client, "/prices/choices/"))
    page = price_table.first_page(choices)
    assert page is not None
    start = timezone.now().astimezone().date()
    found = []
    for category in choices["categories"]:
        query = "&".join(
            f"{name}={value}"
            for name, value in {
                **page[0],
                "category": category["id"],
                "start": start,
                "days": 14,
                "prices": 1,
            }.items()
        )
        response = get(client, f"/prices/calendar/?{query}")
        assert response.status_code == 200
        found.append(answer(response))
    return choices, found


def rows_of(page: str) -> tuple[list[str], list[list[str]]]:
    """The table of a server-drawn page: its column heads and, per row, the
    texts of its cells (the row head first)."""
    table = re.search(r"<table.*?</table>", page, re.S)
    assert table

    def cells(row: str) -> list[list[str]]:
        return [
            html.unescape(re.sub(r"<[^>]+>", " ", text)).split()
            for text in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        ]

    head = re.search(r"<thead>(.*?)</thead>", table[0], re.S)
    assert head
    heads = [" ".join(c) for c in cells(head[1])]
    body = [
        [" ".join(c) for c in cells(row)]
        for row in re.findall(r"<tr>(.*?)</tr>", table[0].split("</thead>")[1], re.S)
    ]
    return heads, body


def squashed(rows: list[list[str]]) -> list[list[str]]:
    """The texts as `rows_of` reads a page: runs of white space, the no-break
    spaces among them, are one space."""
    return [[" ".join(text.split()) for text in row] for row in rows]


def wanted_rows(expected: list[dict[str, Any]]) -> list[list[str]]:
    """The rows the page holds for price-book data: a section head, then per row
    its head and the texts of its cells, as the page words them."""
    want: list[list[str]] = []
    for kind in expected:
        want.append([kind["name"]])
        for block in kind["blocks"]:
            want.append(
                [
                    f"{block['days']} {price_table.keep(block['hours'])}",
                    *[
                        " ".join(
                            [
                                *map(price_table.keep, cell["lines"]),
                                *map(price_table.keep, cell["hours"]),
                            ]
                        )
                        if cell["offered"]
                        else "Not offered"
                        for cell in block["cells"]
                    ],
                ]
            )
            if block["examples"]:
                want.append(["For example"])
            for example in block["examples"]:
                want.append(
                    [
                        f"{example['hours']} · {example['length']}",
                        *[
                            price_table.keep(total) if total else "—"
                            for total in example["totals"]
                        ],
                    ]
                )
    return want


def test_the_price_list_without_javascript_holds_the_rows_the_app_draws(
    made: Example,
) -> None:
    standard_role(made)
    client = Client()
    choices, answers = public_answers(client, made)
    org = Organization.objects.get(pk=made.org.pk)
    columns = [
        {"id": one["id"], "name": one["name"], "icon": one["icon"]}
        for one in choices["categories"]
    ]
    expected = price_table.price_book(
        answers,
        columns,
        price_table.type_order(choices),
        price_table.Words(org),
    )
    # A seeded organization with prices: the page has something to compare.
    assert expected and expected[0]["blocks"]
    page = get(client, "/prices/").content.decode()
    heads, body = rows_of(page)
    assert heads == ["Price", *[one["name"] for one in columns]]
    want = wanted_rows(expected)
    assert body == squashed(want)
    # What the app keeps together stays together on this page too.
    kept = price_table.keep(expected[0]["blocks"][0]["cells"][0]["lines"][0])
    assert "\u00a0" in kept and html.escape(kept, quote=False) in page
    # The page is the organization's, with its rows once: not a second page.
    assert page.count("<table") == 1 and org.name in page
    # The same rows from the module that the page calls.
    found = price_table.table(org, _stand_in(org), choices, columns)
    assert found is not None and found["types"] == expected


def test_no_table_without_a_standard_role(made: Example) -> None:
    page = get(Client(), "/prices/").content.decode()
    assert "<h1>Prices</h1>" in page and "<table" not in page


def test_nothing_personal_in_the_price_list_page(made: Example) -> None:
    standard_role(made)
    page = get(Client(), "/prices/").content.decode()
    assert "<table" in page
    config = config_of(get(Client(), "/prices/"))
    assert "member" not in config and "member_name" not in config
    assert not [key for key in config if key.startswith("member_")]
    for value in (made.member.email, made.member.first_name, made.member.last_name):
        assert value and value not in page


@pytest.mark.parametrize("path", ["/prices/", "/conditions/"])
def test_a_visitor_has_sign_in_and_the_beta_bar_without_javascript(
    made: Example, path: str
) -> None:
    standard_role(made)
    page = get(Client(), path).content.decode()
    assert '<a href="/login/">Sign in</a>' in page
    assert not any(place in page for place in PLACES)
    assert page.count('class="fallback-beta"') == 1
    assert f'mailto:{made.org.contact_email}">{made.org.contact_email}</a>' in page
    config = config_of(get(Client(), path))
    assert config["contact_email"] == made.org.contact_email
    assert "member" not in config


@pytest.mark.parametrize("path", ["/prices/", "/conditions/"])
def test_a_member_has_the_member_shell_without_javascript(
    made: Example, path: str
) -> None:
    standard_role(made)
    page = get(browser(made), path).content.decode()
    for place in PLACES:
        assert place in page, place
    assert "Sign in" not in page and 'href="/login/"' not in page
    assert page.count('class="fallback-beta"') == 1
    assert made.org.contact_email in page


def test_a_member_has_the_member_shell_with_javascript(made: Example) -> None:
    standard_role(made)
    client = browser(made)
    prices = config_of(get(client, "/prices/"))
    # The member shell draws it (main.jsx): the member's own screen, the member's
    # choices and the standard role's columns, and the address for the beta bar.
    assert prices["screen"] == "prices"
    assert prices["choices"]["categories"]
    assert prices["price_categories"]
    assert prices["contact_email"] == made.org.contact_email
    assert prices["member_name"]
    conditions = config_of(get(client, "/conditions/"))
    assert conditions["screen"] == "conditions" and conditions["member"] is True
    assert conditions["conditions"]["title"] == "Booking conditions"
    assert conditions["contact_email"] == made.org.contact_email
    assert conditions["csrf"]
    # Another member's address, a visitor's: no member in the page.
    visitor = config_of(get(Client(), "/conditions/"))
    assert "member" not in visitor and "member_name" not in visitor


def test_a_members_order_conditions_are_in_the_member_shell(made: Example) -> None:
    order = accepted_order(made)
    shown = get(browser(made), f"/book/order/{order.public_id}/conditions/")
    page = shown.content.decode()
    assert all(place in page for place in PLACES)
    assert config_of(shown)["member"] is True


def fjordside() -> tuple[Organization, Member]:
    """The Fjordside setup (setups/fjordside.toml) in the database, with one member of
    its standard role."""
    org = setups.load(
        setup_file.read(Path(__file__).parent.parent / "setups/fjordside.toml"),
        today=timezone.now().date(),
    )
    member = Member.objects.create(
        organization=org,
        role=Role.objects.of(org).get(name="Standard"),
        email="resident@fjordside.example",
        first_name="Ruth",
        last_name="Resident",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    return org, member


def test_the_fjordside_setup_renders_its_price_table() -> None:
    # A smoke test on the live setups/fjordside.toml: the page draws a table with the
    # setup's columns and a row. The rows themselves are held to the JavaScript
    # priceBook by price-book-fjordside.golden.json (test above). The date is pinned,
    # for the setup's load and for the page's 14 days.
    day = datetime(2027, 3, 1, 9, tzinfo=UTC)
    with mock.patch("django.utils.timezone.now", return_value=day):
        _, member = fjordside()
        heads, body = rows_of(
            get(Client(), "/prices/", FJORDSIDE_HOST).content.decode()
        )
        assert heads == ["Price", "Private", "Work"]
        assert len(body) > 1
        client = Client(enforce_csrf_checks=True)
        assert sign_in(client, member, FJORDSIDE_HOST).status_code == 302
        signed = rows_of(get(client, "/prices/", FJORDSIDE_HOST).content.decode())
        assert signed[0] == heads and len(signed[1]) > 1


def test_without_javascript_the_table_follows_the_role_the_app_shows(
    made: Example,
) -> None:
    standard_role(made)
    visitor = rows_of(get(Client(), "/prices/").content.decode())
    assert visitor[1]
    # A member of the standard role: the app draws the same rows.
    member = get(browser(made), "/prices/")
    assert rows_of(member.content.decode()) == visitor
    # A member whose role books nothing: the app has no page to draw for them
    # (their choices hold no category), and neither has this page.
    idle = Member.objects.create(
        organization=made.org,
        role=Role.objects.create(organization=made.org, name="Idle"),
        email="idle@example.com",
        first_name="Ida",
        last_name="Idle",
        status=MemberStatus.ACTIVE,
        origin=Origin.MEMBER_IMPORT,
    )
    own = get(browser(made, idle), "/prices/")
    assert config_of(own)["choices"]["categories"] == []
    assert "<h1>Prices</h1>" in own.content.decode()
    assert "<table" not in own.content.decode()
    # The visitor, in the same test, still has the standard role's table.
    assert "<table" in get(Client(), "/prices/").content.decode()


@pytest.mark.parametrize("path", ["/prices/", "/conditions/"])
def test_the_beta_bar_and_the_address_are_on_both_pages_signed_in_and_out(
    made: Example, path: str
) -> None:
    standard_role(made)
    words = labels.DEFAULTS["shell.beta"].replace("{address}", made.org.contact_email)
    for client in (Client(), browser(made)):
        shown = get(client, path)
        bar = re.search(
            r'<div class="fallback-beta">(.*?)</div>', shown.content.decode(), re.S
        )
        assert bar
        assert html.unescape(re.sub(r"<[^>]+>", "", bar[1])) == words
        assert f'href="mailto:{made.org.contact_email}"' in bar[1]
        # The app draws its bar from this address (BetaBanner.jsx).
        assert config_of(shown)["contact_email"] == made.org.contact_email


def confirmed_session(client: Client, member: Member) -> None:
    """The member signed in as an administrator who passed the authenticator
    step: the session keys the login and the second step set."""
    client.force_login(member, backend=settings.AUTHENTICATION_BACKENDS[0])
    session = client.session
    session["organization_id"] = member.organization_id
    session["admin_confirmed"] = True
    session["active_at"] = session["login_at"] = NOW.timestamp()
    session.save()


@pytest.mark.parametrize("path", ["/prices/", "/conditions/"])
def test_an_administrator_has_the_member_shell_once_signed_in(
    everything: made_up.MadeUp, path: str
) -> None:
    # An administrator is a Member whose role gives admin access
    # (core/models/members.py; AUTH_USER_MODEL is Member): no account signed in
    # is anything else. After the email step alone no one is signed in yet, so
    # the page is the visitor's, with Sign in; after the authenticator step the
    # administrator has the member shell like any member, and the shell knows
    # them as the administrator (`admin`, the row to Admin on My page).
    Organization.objects.filter(pk=everything.organization.pk).update(
        standard_role_id=everything.standard.pk
    )
    halfway = Client(enforce_csrf_checks=True)
    assert sign_in(halfway, everything.administrator).status_code == 302
    page = get(halfway, path).content.decode()
    assert '<a href="/login/">Sign in</a>' in page
    assert not any(place in page for place in PLACES)
    signed = Client(enforce_csrf_checks=True)
    confirmed_session(signed, everything.administrator)
    shown = get(signed, path)
    page = shown.content.decode()
    assert shown.status_code == 200
    assert all(place in page for place in PLACES)
    assert "Sign in" not in page
    assert config_of(shown)["admin"] is True
    assert config_of(shown)["member_name"] == "Sam Field"
