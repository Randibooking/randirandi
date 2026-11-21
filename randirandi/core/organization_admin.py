# SPDX-License-Identifier: AGPL-3.0-only
"""The organization's own details, as an administrator sets them .

One save changes any of: name, contact email, days of free cancellation,
standard role, language ,
bookings a day, book ahead, tax rate, member and booking retention, and the
default time zone and currency. The subdomain is the address and is not
changed here; the organization's own words (labels) are not on this screen.

A save runs in `organization_transaction` (the organization's row locked), so
two administrators take turns. The sheet carries the `version` it was opened
on, a digest of the values; a save whose version is not the stored values' is
refused "stale" and writes nothing, so one never overwrites the other's edit.
The rules are the model's own (`full_clean`, as the setup file's loader
applies it) and the setup file reader's (`setup_file`), so an export made after
any save reads and loads again. The audit event is written first, in the same
transaction , and never says which detail changed.

Lowering a retention erases data at the erasing job's next run
(`retention.purge_organization`). Such a save is refused "confirm-erasure"
with the numbers the next run would erase under the new values; it is made
only when the body repeats exactly those numbers as `confirm` and carries a
fresh authenticator `code`, judged as member export, add member and email
change judge it (`second_factor.with_fresh_code`: "code-refused",
"code-limited"). A save that lowers nothing asks for no code.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from randirandi import labels as label_set
from randirandi import setup_file
from randirandi.core.audit import record_event
from randirandi.core.audit_types import AuditAction
from randirandi.core.models import Member, Organization, Role
from randirandi.core.models.organization import Currency
from randirandi.core.retention import erasure_preview
from randirandi.core.second_factor import with_fresh_code
from randirandi.core.setup_admin import SetupRefusedError, organization_transaction
from randirandi.limits import (
    LONGEST_NAME,
    MOST_FREE_CANCELLATION_DAYS,
    MOST_TAX_RATE,
    PLAIN_ADDRESS,
    name_fault,
)

# The organization's fields this screen changes, with what each may be.
TEXTS = ("name", "contact_email", "language", "time_zone", "currency")
LIMITS = ("max_bookings_a_day", "book_ahead_months")
WHOLE = (
    "tax_rate",
    "member_retention_months",
    "booking_retention_months",
    "free_cancellation_days",
)
ROLE = "standard_role"
FIELDS = (*TEXTS, ROLE, *LIMITS, *WHOLE)
# The least each number may be; the setup file's rule for it (`setup_file`).
LEAST = {
    "max_bookings_a_day": 1,
    "book_ahead_months": 1,
    "tax_rate": 0,
    "member_retention_months": 1,
    "booking_retention_months": 1,
    "free_cancellation_days": 0,
}
MOST = {
    "tax_rate": MOST_TAX_RATE,
    "free_cancellation_days": MOST_FREE_CANCELLATION_DAYS,
}


def _values(org: Organization) -> list[object]:
    return [getattr(org, "standard_role_id" if key == ROLE else key) for key in FIELDS]


def version_of(org: Organization) -> str:
    """A digest of the values the sheet edits: what a sheet was opened on."""
    return hashlib.sha256(json.dumps(_values(org)).encode()).hexdigest()[:24]


def place(org: Organization) -> dict[str, object]:
    """The details, the organization's own roles a standard role may be chosen
    from, the languages, time zones and currencies the service knows, and the
    `version` to send back with a save."""
    return {
        "name": org.name,
        "subdomain": org.subdomain,
        "contact_email": org.contact_email,
        "standard_role": org.standard_role_id,
        "max_bookings_a_day": org.max_bookings_a_day,
        "book_ahead_months": org.book_ahead_months,
        "tax_rate": org.tax_rate,
        "member_retention_months": org.member_retention_months,
        "booking_retention_months": org.booking_retention_months,
        "free_cancellation_days": org.free_cancellation_days,
        "language": org.language,
        "time_zone": org.time_zone,
        "currency": org.currency,
        "version": version_of(org),
        "roles": [
            {"id": role.pk, "name": role.name, "admin_access": role.admin_access}
            for role in Role.objects.of(org).order_by("pk")
        ],
        "languages": [
            {"code": code, "name": label_set.language_name(code)}
            for code in label_set.languages()
        ],
        "time_zones": sorted(available_timezones()),
        "currencies": [choice.value for choice in Currency],
    }


# --- The body -------------------------------------------------------------------


def _shape(
    fields: Mapping[str, object],
) -> tuple[dict[str, Any], str, dict[str, int], str]:
    """The values a body gives, its version and its confirmation, by type only
    (a wrong type is a bad request; a wrong value is refused with its reason)."""
    if set(fields) - {*FIELDS, "version", "confirm", "code"}:
        raise ValueError("The body is wrong.")
    version = fields.get("version")
    if not isinstance(version, str):
        raise ValueError("A save names the version it was opened on.")
    found: dict[str, Any] = {}
    for key in FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key in TEXTS:
            if not isinstance(value, str):
                raise ValueError("A text is text.")
            # The address is trimmed of spaces only: a line break at its end is
            # refused, not trimmed away.
            found[key] = (
                value.strip()
                if key == "name"
                else value.strip(" ")
                if key == "contact_email"
                else value
            )
        elif key in LIMITS and value is None:
            found[key] = None
        elif type(value) is int or key == "free_cancellation_days":
            # The days are refused in words, whatever was sent (1.5, text).
            found[key] = value
        else:
            raise ValueError("A number is a whole number.")
    if not found:
        raise ValueError("The body is wrong.")
    confirm = fields.get("confirm", {})
    if (
        not isinstance(confirm, dict)
        or set(confirm) - {"members", "bookings"}
        or any(type(v) is not int for v in confirm.values())
    ):
        raise ValueError("A confirmation is the numbers shown.")
    code = fields.get("code", "")
    if not isinstance(code, str):
        raise ValueError("A code is text.")
    return found, version, confirm, "".join(code.split())


def _refuse(field: str, reason: str) -> SetupRefusedError:
    return SetupRefusedError(f"invalid-{field.replace('_', '-')}", reason=reason)


def contact_fault(value: str) -> str | None:
    """Why a contact address is refused, or None: one plain address, no
    display name, no list, no line break. Members' replies go to it."""
    if not value:
        return "must not be empty."
    if any(ord(c) < 32 or ord(c) == 127 or c in "\u0085\u2028\u2029" for c in value):
        return "must be on one line."
    if any(c in value for c in '<>"'):
        return "must be the address alone, without a name."
    if any(c in value for c in ",; \t"):
        return "must be one address, not a list."
    if len(value) > 254 or not re.fullmatch(PLAIN_ADDRESS, value):
        return "must be an email address."
    return setup_file.organization_text_fault("contact_email", value)


def _checked_by_the_file(values: Mapping[str, Any]) -> None:
    """The setup file reader's rule for each value; a fault is its sentence."""
    for key, value in values.items():
        fault: str | None = None
        if key == "name":
            fault = name_fault(value)
            if fault is None and len(value) > LONGEST_NAME:
                fault = f"must be at most {LONGEST_NAME} characters."
        elif key == "contact_email":
            fault = contact_fault(value)
        elif key == "language":
            fault = label_set.language_fault(value)
        elif key in ("time_zone", "currency"):
            fault = setup_file.organization_text_fault(key, value)
            if fault is None and not value:
                fault = "must not be empty."
        elif key in LEAST:
            if value is not None or key == "free_cancellation_days":
                fault = setup_file.whole_number_fault(
                    value, minimum=LEAST[key], maximum=MOST.get(key)
                )
        if fault:
            raise _refuse(key, fault)


def _checked_by_the_model(org: Organization) -> None:
    """The model's own validation, as the setup file's loader applies it."""
    try:
        org.full_clean()
    except ValidationError as error:
        errors = getattr(error, "error_dict", {})
        for key in FIELDS:
            if key in errors:
                raise _refuse(key, errors[key][0].messages[0]) from None
        raise SetupRefusedError("invalid") from None


# --- The change -------------------------------------------------------------------


def change(
    administrator: Member, fields: Mapping[str, object], *, network: str = "127.0.0.1"
) -> None:
    """Save the details the body gives (any of the fields; the rest stay).

    Refusals: "invalid-<field>" with the rule's sentence, "unknown-role",
    "standard-admin", "stale" (the values changed since the sheet was opened),
    "confirm-erasure" (a retention is lowered: the numbers to confirm are in
    `erase`), and, for the confirmed lowering, `CodeRefusedError` (the
    authenticator code was not accepted). A save that changes nothing writes
    nothing.
    """
    values, version, confirm, code = _shape(fields)
    _checked_by_the_file(values)
    asked_to_erase = "confirm" in fields
    # Whether the save lowers a retention is read again under the lock; this
    # read only chooses whether the code is judged.
    now = Organization.objects.get(pk=administrator.organization_id)
    lowers = _lowers(now, values)
    judged = lowers and asked_to_erase

    def apply(org: Organization) -> None:
        if version != version_of(org):
            raise SetupRefusedError("stale")
        role = None
        if ROLE in values:
            role = Role.objects.of(org).filter(pk=values[ROLE]).first()
            if role is None:
                raise SetupRefusedError("unknown-role")
            if role.admin_access:
                raise SetupRefusedError("standard-admin")
        changes = [
            key
            for key, value in values.items()
            if value != getattr(org, "standard_role_id" if key == ROLE else key)
        ]
        if not changes:
            return
        member_months = org.member_retention_months
        booking_months = org.booking_retention_months
        for key in changes:
            if key == ROLE:
                org.standard_role = role
            else:
                setattr(org, key, values[key])
        _checked_by_the_model(org)
        if (
            org.member_retention_months < member_months
            or org.booking_retention_months < booking_months
        ):
            if not judged and asked_to_erase:
                # It was not a lowering when the call began.
                raise SetupRefusedError("stale")
            erase = erasure_preview(
                org, org.member_retention_months, org.booking_retention_months
            )
            if confirm != erase or not judged:
                raise SetupRefusedError("confirm-erasure", details={"erase": erase})
        record_event(
            actor=administrator, action=AuditAction.CHANGED_ORGANIZATION, target=org
        )
        try:
            org.save(update_fields=changes)
        except IntegrityError:
            raise SetupRefusedError("invalid") from None

    if judged:
        with_fresh_code(administrator, code, apply, network=network)
    else:
        organization_transaction(administrator.organization_id, apply)


def _lowers(org: Organization, values: Mapping[str, Any]) -> bool:
    return any(
        key in values and values[key] < getattr(org, key)
        for key in ("member_retention_months", "booking_retention_months")
    )
