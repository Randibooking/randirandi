# SPDX-License-Identifier: AGPL-3.0-only
"""Typed boundary to the reviewed TOTP primitive; no upstream device models."""

import base64
import re
from io import BytesIO
from typing import Protocol, cast
from urllib.parse import quote, urlencode

import segno
from django_otp.oath import TOTP  # type: ignore[import-untyped]


class ClockCode(Protocol):
    time: float

    def verify(self, token: int, tolerance: int, min_t: int) -> bool: ...
    def t(self) -> int: ...


def accepted_counter(secret: str, code: str, *, now: float, last: int) -> int | None:
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    clock = cast(ClockCode, TOTP(bytes.fromhex(secret), step=30, digits=6))
    clock.time = now
    return clock.t() if clock.verify(int(code), tolerance=1, min_t=last + 1) else None


def enrollment(secret: str, *, organization: str, member_id: int) -> tuple[str, bytes]:
    key = base64.b32encode(bytes.fromhex(secret)).decode().rstrip("=")
    # No email or person's name in the authenticator account label.
    issuer = "RandiRandi · " + organization
    uri = "otpauth://totp/" + quote(f"{issuer}:administrator {member_id}", safe="")
    uri += "?" + urlencode({"secret": key, "issuer": issuer, "digits": 6, "period": 30})
    output = BytesIO()
    segno.make_qr(uri).save(
        output, kind="svg", scale=4, xmldecl=False, light="#ffffff", dark="#000000"
    )
    return key, output.getvalue()
