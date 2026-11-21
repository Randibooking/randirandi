# SPDX-License-Identifier: AGPL-3.0-only
"""Who holds a booking from an organization's earlier calendar: the member
record its title names .
"""

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from randirandi.core.models import Member, MemberStatus, Origin
from randirandi.core.old_bookings import (
    Holder,
    Person,
    holder,
    holders,
    name_words,
    people,
)
from tests import made_up

NAMES = {
    1: name_words("Kari Nordmann"),
    2: name_words("Ola Nordmann"),
    3: name_words("Ola Testesen"),
    4: name_words("Liv Marie Strand"),
    5: name_words("Per Arne Eksempel"),
    6: name_words("Jan-Erik Prøve"),
    7: name_words("Siri Berg-Dahl"),
    8: name_words("Åse Øvre"),
}


def test_a_title_names_people_by_given_names_then_initials() -> None:
    assert people("Viktor T/Silje F (overnight)") == [
        Person(("viktor",), "T"),
        Person(("silje",), "F"),
    ]
    assert people("Stig A.T.") == [Person(("stig",), "AT")]
    assert people("Mona KM") == [Person(("mona",), "KM")]
    assert people("Janne MS(confirmation)") == [Person(("janne",), "MS")]
    assert people("Janne Mari S") == [Person(("janne", "mari"), "S")]
    assert people("Jan-Roger N") == [Person(("jan", "roger"), "N")]
    assert people("Bente T?") == [Person(("bente",), "T")]
    assert people("Pernille/Anders") == [
        Person(("pernille",), ""),
        Person(("anders",), ""),
    ]
    assert people("Kari N & Ola T, Per E + Liv S") == [
        Person(("kari",), "N"),
        Person(("ola",), "T"),
        Person(("per",), "E"),
        Person(("liv",), "S"),
    ]
    # A word after the initials ends the name.
    assert people("Kari N overnight") == [Person(("kari",), "N")]


@pytest.mark.parametrize(
    ("title", "fit", "ids"),
    [
        # A surname initial, with and without its dot.
        ("Kari N", "name", (1,)),
        ("Kari N.", "name", (1,)),
        # A double given name written out, or as an initial.
        ("Liv Marie S", "name", (4,)),
        ("Liv MS (overnight)", "name", (4,)),
        ("Per A.E.", "name", (5,)),
        ("Per E", "name", (5,)),
        # A middle initial the member's name does not carry.
        ("Kari B.N.", "surname initial", (1,)),
        # Hyphens either way; a double surname as two capitals or its last.
        ("Jan-Erik P", "name", (6,)),
        ("Jan Erik P", "name", (6,)),
        ("Siri BD", "name", (7,)),
        ("Siri D", "name", (7,)),
        ("Åse Ø.", "name", (8,)),
        # The first name alone: one member, or two.
        ("Kari", "first name", (1,)),
        ("Ola", "first name", (2, 3)),
        ("Ola T", "name", (3,)),
        # The whole name.
        ("Ola Testesen", "name", (3,)),
        # Two people are never resolved; each one's best fits are given, in
        # the title's order.
        ("Ola/Kari N (confirmation)", "several people", (2, 3, 1)),
        ("Ola N/Kari N", "several people", (2, 1)),
        ("Kari N & Nobody X", "several people", (1,)),
        ("Nobody X/Somebody Y", "several people", ()),
        # "Å" typed as A and a ring.
        ("A\u030ase Ø.", "name", (8,)),
        # A wrong initial, a group, a question mark.
        ("Kari T", "none", ()),
        ("Lilleby School", "none", ()),
        ("Per E?", "name", (5,)),
    ],
)
def test_a_title_fits_the_members_it_names(
    title: str, fit: str, ids: tuple[int, ...]
) -> None:
    assert holder(title, NAMES) == Holder(fit, ids)


def test_only_one_member_by_name_is_found_without_a_person_choosing() -> None:
    assert holder("Kari N", NAMES).found
    assert not holder("Kari", NAMES).found
    assert not holder("Kari B.N.", NAMES).found
    assert not holder("Ola", NAMES).found
    assert not holder("Ola N/Kari N", NAMES).found
    assert not holder("Kari N/Nobody X", NAMES).found
    assert not holder("Nobody", NAMES).found


def test_one_word_is_a_first_name_even_for_a_member_with_no_surname() -> None:
    names = {1: name_words("Kari"), 2: name_words("Kari Nordmann")}

    assert holder("Kari", names) == Holder("first name", (1, 2))


def member(
    made: made_up.MadeUp,
    first: str,
    last: str,
    status: MemberStatus = MemberStatus.ACTIVE,
) -> Member:
    return Member.objects.create(
        organization=made.organization,
        role=made.standard,
        first_name=first,
        last_name=last,
        email=f"{first}.{last}@{made.organization.subdomain}.example".casefold(),
        status=status,
        origin=Origin.MEMBER_IMPORT,
    )


@pytest.mark.django_db
def test_the_holder_is_the_member_record_of_the_organization() -> None:
    ours = made_up.everything("harbourview")
    theirs = made_up.everything("lakeside", made_up.A_DAY + timedelta(days=7))
    robin = member(ours, "Robin", "Shore")
    member(theirs, "Robin", "Stone")
    member(ours, "Robin", "Sand", MemberStatus.DISABLED)

    assert holders(ours.organization, [("Robin S", ""), ("Alex R", "")]) == [
        Holder("name", (robin.pk,)),
        Holder("name", (ours.member.pk,)),
    ]


@pytest.mark.django_db
def test_an_email_names_its_member_exactly_whatever_the_title() -> None:
    ours = made_up.everything("harbourview")
    theirs = made_up.everything("lakeside", made_up.A_DAY + timedelta(days=7))
    gone = member(ours, "Robin", "Sand", MemberStatus.DISABLED)

    assert holders(
        ours.organization,
        [
            ("Parents' meeting", ours.administrator.email.upper()),
            ("Robin S", gone.email),
            ("Alex R", theirs.administrator.email),
        ],
    ) == [
        Holder("email", (ours.administrator.pk,)),
        Holder("none", ()),
        Holder("none", ()),
    ]
    assert Holder("email", (ours.administrator.pk,)).found


@pytest.mark.django_db
def test_the_command_prints_member_ids_and_no_names(tmp_path: Path) -> None:
    made = made_up.everything()
    robin = member(made, "Robin", "Shore")
    entries = tmp_path / "entries.json"
    entries.write_text(
        json.dumps(
            [
                {"title": "Robin S (overnight)"},
                {"title": "Board meeting", "holder": "Alex Rivers"},
                {"title": "Lilleby School"},
                {"title": "Robin S/Alex R"},
                {"title": "Parents' meeting", "holder_email": made.administrator.email},
            ]
        ),
        encoding="utf-8",
    )
    out = StringIO()

    call_command(
        "match_old_bookings", made.organization.subdomain, str(entries), stdout=out
    )

    assert json.loads(out.getvalue()) == [
        {"fit": "name", "member_ids": [robin.pk], "resolved": True},
        {"fit": "name", "member_ids": [made.member.pk], "resolved": True},
        {"fit": "none", "member_ids": [], "resolved": False},
        {
            "fit": "several people",
            "member_ids": [robin.pk, made.member.pk],
            "resolved": False,
        },
        {"fit": "email", "member_ids": [made.administrator.pk], "resolved": True},
    ]
    assert "Robin" not in out.getvalue()
    assert "Alex" not in out.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"title": "Robin S"}),
        json.dumps([{"name": "Robin S"}]),
        json.dumps([{"title": "Robin S", "holder": 7}]),
        json.dumps([{"title": "Robin S", "holder_email": None}]),
    ],
)
def test_the_command_refuses_a_file_it_cannot_read(tmp_path: Path, text: str) -> None:
    made = made_up.everything()
    entries = tmp_path / "entries.json"
    entries.write_text(text, encoding="utf-8")

    with pytest.raises(CommandError) as refused:
        call_command("match_old_bookings", made.organization.subdomain, str(entries))
    # The refusal does not repeat what the file holds.
    assert "Robin" not in str(refused.value)
    assert "not json" not in str(refused.value)


@pytest.mark.django_db
def test_the_command_reads_standard_input_and_an_empty_holder_is_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = made_up.everything()
    robin = member(made, "Robin", "Shore")
    monkeypatch.setattr(
        "sys.stdin", StringIO(json.dumps([{"title": "Robin S", "holder": ""}]))
    )
    out = StringIO()

    call_command("match_old_bookings", made.organization.subdomain, "-", stdout=out)

    assert json.loads(out.getvalue()) == [
        {"fit": "name", "member_ids": [robin.pk], "resolved": True}
    ]


@pytest.mark.django_db
def test_the_command_needs_an_organization(tmp_path: Path) -> None:
    entries = tmp_path / "entries.json"
    entries.write_text("[]", encoding="utf-8")

    with pytest.raises(CommandError, match="no organization"):
        call_command("match_old_bookings", "does-not-exist", str(entries))
