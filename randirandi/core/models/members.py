# SPDX-License-Identifier: AGPL-3.0-only
"""Who can book: roles, memberships, members and how they come and go.

Names and contact details are on the member and nowhere else. Every other
object points to a member by link.
"""

from collections.abc import Collection
from typing import ClassVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.crypto import salted_hmac

from randirandi.core.models.base import Owned, organization_rules
from randirandi.core.models.bookable import BookingCategory


class Role(Owned):
    """Which booking categories a member can book."""

    name = models.CharField(max_length=200)
    booking_categories = models.ManyToManyField(
        BookingCategory, through="core.RoleCategory", related_name="roles"
    )
    # Whether the role gives access to the admin backend.
    admin_access = models.BooleanField(default=False)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "name"], name="%(class)s_name_once"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class RoleCategory(Owned):
    """A booking category that a member with a role can book."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    booking_category = models.ForeignKey(BookingCategory, on_delete=models.PROTECT)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("role", "booking_category"),
            models.UniqueConstraint(
                fields=["role", "booking_category"], name="%(class)s_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.role} · {self.booking_category}"


class Membership(Owned):
    """The entitlement that one or more members share, with the identifier
    the organization uses for it. At a housing co-op it is the unit.
    """

    identifier = models.CharField(max_length=100)
    # The street address, where the organization has more than one.
    address = models.CharField(max_length=200, blank=True)
    # Where in the address: a flat, a suite, an H-number. Not part of the
    # membership's identity.
    address_unit = models.CharField(max_length=50, blank=True)
    # An entrance, a club's section, a floor. Not part of the identity.
    section = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=100, blank=True)
    # ISO 3166-1 alpha-2, upper case; two letters, no country list.
    country = models.CharField(max_length=2, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "identifier", "address"],
                name="%(class)s_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.identifier} {self.address}".strip()


class MemberStatus(models.TextChoices):
    # Only an active member logs in and books.
    ACTIVE = "active"
    # Set by a member import: the list no longer has the member.
    DISABLED = "disabled"
    # Set by an administrator. A member import never changes it.
    BLOCKED = "blocked"


class Origin(models.TextChoices):
    MEMBER_IMPORT = "member import", "Member import"
    ADMINISTRATOR = "added by administrator", "Added by administrator"


class Member(AbstractBaseUser, Owned):
    """A person who can log in and book. An administrator is a member whose
    role gives admin access.

    This is Django's user object. There is no password: a member logs in
    with a one-time code. No time of login is kept either, so the login
     takes Django's own "last login" update out.

    What identifies a member is the organization and the email together.
    The same person can be a member of two organizations, as two members.
    Erasing a member blanks the name and the contact details in place; the
    row stays, so that bookings keep their link.
    """

    password = None  # type: ignore[assignment]
    last_login = None
    authenticator_generation = models.PositiveIntegerField(default=0)

    role = models.ForeignKey(Role, on_delete=models.PROTECT)
    memberships = models.ManyToManyField(
        Membership, through="core.MemberMembership", related_name="members"
    )
    # The name as the source gives it. First and last name are made from it
    # when the source has no separate fields.
    full_name = models.CharField(max_length=300, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    # Lower case. Login codes are sent here.
    email = models.EmailField(blank=True)
    # With the country code, as +4791234567. Can be typed in place of the
    # email to log in; the code still goes to the email.
    phone = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=8, choices=MemberStatus)
    # Whether the member list last said the member is on it. Every applied
    # import sets it for the members it manages, a blocked member too, so that
    # Unblock goes back to what the list says. A member who is not from an
    # import counts as listed.
    listed = models.BooleanField(default=True)
    # When the list last stopped listing the member; empty while listed, and
    # when unknown. Retention counts its months from here.
    unlisted_at = models.DateTimeField(null=True, blank=True)
    origin = models.CharField(max_length=22, choices=Origin)
    # When the member last signed in: the accepted login code, or for an
    # administrator the accepted authenticator code. Kept beyond the thirty
    # days of the login attempts; empty until the first sign-in after it was
    # added. Erasure clears it.
    last_login_at = models.DateTimeField(null=True, blank=True)
    # The booking category the member last chose in the booking page's Filter;
    # empty until the first choice. Deleting the category empties it: Django
    # does that with one update of this column, and the same-organization
    # foreign key beside it ignores a row whose category is empty.
    last_booking_category = models.ForeignKey(
        BookingCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("role", "last_booking_category"),
            # One member, one email, one phone number .
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=~Q(email=""),
                name="%(class)s_email_once",
            ),
            models.UniqueConstraint(
                fields=["organization", "phone"],
                condition=~Q(phone=""),
                name="%(class)s_phone_once",
            ),
            # So that one address or number cannot be written in two ways.
            models.CheckConstraint(
                condition=Q(email=Lower("email")), name="%(class)s_email_lower_case"
            ),
            models.CheckConstraint(
                condition=Q(phone="") | Q(phone__regex=r"^\+[1-9][0-9]{5,14}$"),
                name="%(class)s_phone_with_country_code",
            ),
            models.CheckConstraint(
                condition=~Q(status=MemberStatus.ACTIVE) | ~Q(email=""),
                name="%(class)s_active_needs_an_email",
            ),
        ]

    def __str__(self) -> str:
        # Ids only: this ends up in logs.
        return f"member {self.pk}"

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.status == MemberStatus.ACTIVE

    @property
    def is_staff(self) -> bool:
        return self.is_active and self.role.admin_access

    def _get_session_auth_hash(self, secret: str | None = None) -> str:
        # Access-event identity changes even if the member is disabled and made
        # active again between requests. The caller uses a freshly loaded member/role.
        event = (
            AccessEvent.objects.of(self.organization_id)
            .filter(member=self)
            .order_by("-pk")
            .values_list("pk", flat=True)
            .first()
        )
        state = (
            f"{self.organization_id}:{self.pk}:{self.email}:{self.status}:"
            f"{self.role_id}:{self.is_staff}:{event}:{self.authenticator_generation}"
        )
        return salted_hmac(
            "randirandi.member-session", state, secret=secret, algorithm="sha256"
        ).hexdigest()

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        # Written in the one way first, and checked as written.
        self.email = self.email.strip().lower()
        self.phone = self.phone.replace(" ", "")
        super().clean_fields(exclude)


class MemberMembership(Owned):
    """One of a member's memberships."""

    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("member", "membership"),
            models.UniqueConstraint(
                fields=["member", "membership"], name="%(class)s_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.member} · {self.membership}"


class ImportOutcome(models.TextChoices):
    APPLIED = "applied"
    # The safety stop refused it.
    STOPPED = "stopped"
    FAILED = "failed"


class MemberImport(Owned):
    """One run: a parser reads one uploaded file. It creates members and makes
    them active or disabled. It never deletes.
    """

    # The parser's id in code (randirandi/member_parsers.py); the product holds
    # parsers, never one list's columns .
    parser = models.CharField(max_length=60)
    # The file's name and size as uploaded. The file itself is never stored.
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    # The administrator who asked for it. Empty for a run no administrator
    # started (the sandbox's made-up members).
    started_by = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    outcome = models.CharField(max_length=7, choices=ImportOutcome)
    created = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    granted = models.PositiveIntegerField(default=0)
    revoked = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    # Row numbers, IDs and fixed reason codes only; never imported values.
    report = models.JSONField(default=dict, blank=True)

    class Meta(Owned.Meta):
        constraints = organization_rules("started_by")

    def __str__(self) -> str:
        return f"member import {self.pk}"


class AccessCause(models.TextChoices):
    MEMBER_IMPORT = "member import", "Member import"
    ADMINISTRATOR = "administrator", "Administrator"


class AccessEvent(Owned):
    """One change of a member's status. Together they answer who was active
    when.
    """

    member = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name="access_events"
    )
    # The status the member changed to.
    change = models.CharField(max_length=8, choices=MemberStatus)
    at = models.DateTimeField(default=timezone.now)
    cause = models.CharField(max_length=13, choices=AccessCause)
    # Set when an import caused it.
    member_import = models.ForeignKey(
        MemberImport, null=True, blank=True, on_delete=models.PROTECT
    )
    # Set when an administrator caused it.
    administrator = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("member", "member_import", "administrator"),
            models.CheckConstraint(
                condition=Q(member_import__isnull=True) | Q(administrator__isnull=True),
                name="%(class)s_one_cause",
            ),
            # Only an administrator blocks.
            models.CheckConstraint(
                condition=~Q(change=MemberStatus.BLOCKED)
                | Q(cause=AccessCause.ADMINISTRATOR, administrator__isnull=False),
                name="%(class)s_blocked_by_administrator",
            ),
        ]

    def __str__(self) -> str:
        return f"access event {self.pk}"


class LoginCode(Owned):
    """A one-time code sent to a member to log in."""

    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    # Scrambled. The code itself is never stored.
    scrambled_code = models.CharField(max_length=128)
    # Blank legacy rows cannot be verified by the issuance service.
    challenge = models.CharField(max_length=32, default="", blank=True)
    browser_digest = models.CharField(max_length=64, default="", blank=True)
    # Where the request came from: a new request voids only codes from the same
    # network. Empty on rows before part 8.4.
    network = models.GenericIPAddressField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("member"),
            models.UniqueConstraint(
                fields=["organization", "challenge"],
                condition=~Q(challenge=""),
                name="logincode_challenge_unique",
            ),
            models.CheckConstraint(
                condition=Q(attempts__lte=5), name="logincode_attempts_limit"
            ),
        ]

    def __str__(self) -> str:
        return f"login code {self.pk}"


class LoginAttempt(Owned):
    """Thirty-day abuse and sign-in history, without submitted identities."""

    member = models.ForeignKey(Member, null=True, blank=True, on_delete=models.CASCADE)
    at = models.DateTimeField()
    network = models.GenericIPAddressField()
    kind = models.CharField(
        max_length=7,
        choices=[
            ("request", "Request"),
            ("verify", "Verify"),
            ("mfa", "Authenticator"),
            ("return", "Payment return"),
        ],
    )
    outcome = models.CharField(
        max_length=8,
        choices=[
            ("issued", "Issued"),
            ("limited", "Limited"),
            ("unknown", "Unknown"),
            ("accepted", "Accepted"),
            ("refused", "Refused"),
        ],
    )

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules("member"),
            models.CheckConstraint(
                condition=(
                    Q(kind="request", outcome__in=["issued", "limited", "unknown"])
                    | Q(kind="verify", outcome__in=["accepted", "refused"])
                    | Q(kind="mfa", outcome__in=["accepted", "refused", "limited"])
                    | Q(kind="return", outcome="unknown")
                ),
                name="loginattempt_outcome_kind",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "kind", "network", "at"]),
            models.Index(fields=["organization", "kind", "member", "at"]),
        ]

    def __str__(self) -> str:
        return f"login attempt {self.pk}"


class LoginRequest(Owned):
    """Short-lived delivery queue; only an identity digest is held until claim."""

    identifier_digest = models.CharField(max_length=64, blank=True)
    browser_digest = models.CharField(max_length=64)
    challenge = models.CharField(max_length=32)
    network = models.GenericIPAddressField()
    at = models.DateTimeField()
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta(Owned.Meta):
        constraints = [
            *organization_rules(),
            models.UniqueConstraint(
                fields=["organization", "challenge"],
                name="loginrequest_challenge_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "network", "at"]),
            models.Index(fields=["organization", "claimed_at", "at"]),
        ]

    def __str__(self) -> str:
        return f"login request {self.pk}"
