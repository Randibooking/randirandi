# SPDX-License-Identifier: AGPL-3.0-only
"""Who holds a booking that comes from an organization's earlier calendar.

An earlier calendar names its bookings in free text: "Kari N", "Per A.E.",
"Kari N/Ola N (overnight)". Moved in, each booking is held by a member
record, found by the name in its title . A title that fits one member by name gives that
member;
anything less is for an administrator to choose. See docs/setup-file.md,
"Bookings from an earlier calendar".
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from randirandi.core.models import Member, MemberStatus, Organization

# How well a person in a title fits a member, best first. "name": the given
# names and every initial fit; "surname initial": the given names and the
# last initial fit, a middle initial does not; "first name": the title has
# no initials and its given names are not the whole name.
FITS = ("name", "surname initial", "first name")

_ASIDE = re.compile(r"\([^)]*\)")
_BETWEEN_PEOPLE = re.compile(r"[/&+,]")


@dataclass(frozen=True)
class Person:
    """One person a title names: given names from the front, then initials."""

    words: tuple[str, ...]
    initials: str


@dataclass(frozen=True)
class Holder:
    """How a title fits the members: one of FITS, "several people",
    "email" or "none", and the members that fit that well. Anything but one
    member by name or by email is unresolved."""

    fit: str
    member_ids: tuple[int, ...]

    @property
    def found(self) -> bool:
        """Resolved: one person, one member, by name or by email. The
        booking can be theirs without a person choosing."""
        return self.fit in ("name", "email") and len(self.member_ids) == 1


def name_words(text: str) -> tuple[str, ...]:
    """A name as lower-case words; a hyphen parts words like a space, so
    "Jan-Erik" and "Jan Erik" are the same name. "Å" typed as A and a ring
    is the same letter as "Å"."""
    text = unicodedata.normalize("NFC", text).casefold()
    return tuple(word for word in re.split(r"[\s\-]+", text) if word)


def people(title: str) -> list[Person]:
    """The people a title names. A slash, an ampersand, a plus or a comma
    parts two people; an aside in parentheses and a question mark are not
    names. A single letter, or up to three capitals, is initials: "P.",
    "A.T.", "KM". After the first initial a word ends the name: "Kari N
    overnight"."""
    found = []
    text = unicodedata.normalize("NFC", _ASIDE.sub(" ", title)).replace("?", " ")
    for part in _BETWEEN_PEOPLE.split(text):
        words: tuple[str, ...] = ()
        initials = ""
        for token in part.split():
            bare = token.replace(".", "")
            if not bare:
                continue
            if len(bare) == 1 or (bare.isupper() and len(bare) <= 3):
                initials += bare.upper()
            elif initials:
                break
            else:
                words += name_words(bare)
        if words or initials:
            found.append(Person(words, initials))
    return found


def fit(person: Person, name: tuple[str, ...]) -> str | None:
    """How the person fits a member's name, or None."""
    if not person.words or name[: len(person.words)] != person.words:
        return None
    rest = [word[0] for word in name[len(person.words) :]]
    if not person.initials:
        # A name of one word is a first name even when the member has no
        # other: "Kari" is not taken for a member whose surname is missing.
        return "name" if not rest and len(person.words) > 1 else "first name"
    wanted = person.initials.casefold()
    if not rest or wanted[-1] != rest[-1]:
        return None
    middle = iter(rest[:-1])
    # The earlier initials in the order the middle names come.
    return (
        "name" if all(letter in middle for letter in wanted[:-1]) else "surname initial"
    )


def holder(title: str, names: dict[int, tuple[str, ...]]) -> Holder:
    """The members a title fits best. A title that names more than one
    person is never resolved: "several people", with the members each fits
    best in the title's order, for a person to choose ."""
    named = people(title)
    best = [_best_fit(person, names) for person in named]
    if len(named) > 1:
        return Holder(
            "several people", tuple(dict.fromkeys(pk for _, ids in best for pk in ids))
        )
    if not best:
        return Holder("none", ())
    return Holder(*best[0])


def _best_fit(
    person: Person, names: dict[int, tuple[str, ...]]
) -> tuple[str, tuple[int, ...]]:
    for kind in FITS:
        ids = tuple(pk for pk, name in names.items() if fit(person, name) == kind)
        if ids:
            return kind, ids
    return "none", ()


def member_name(member: Member) -> tuple[str, ...]:
    return name_words(f"{member.first_name} {member.last_name}") or name_words(
        member.full_name
    )


def holders(
    organization: Organization, asks: Iterable[tuple[str, str]]
) -> list[Holder]:
    """For each name and email, the member of the organization it names. An
    email names its member exactly; it is how a booking is given to an
    account that no title names, such as the administrator account that
    holds what is unresolved . Only active members:
    a booking to come needs a holder who can log in."""
    members = (
        Member.objects.of(organization)
        .filter(status=MemberStatus.ACTIVE)
        .only("pk", "first_name", "last_name", "full_name", "email")
    )
    names = {member.pk: member_name(member) for member in members}
    emails = {member.email: member.pk for member in members}
    found = []
    for name, email in asks:
        if email:
            pk = emails.get(email.strip().lower())
            found.append(Holder("email", (pk,)) if pk else Holder("none", ()))
        else:
            found.append(holder(name, names))
    return found
