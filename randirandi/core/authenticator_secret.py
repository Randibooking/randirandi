# SPDX-License-Identifier: AGPL-3.0-only
"""The authenticator secret at rest: AES-256-GCM under RANDIRANDI_AUTHENTICATOR_KEY.

Stored form: ``v1:`` and 64 URL-safe base64 characters holding a 12-byte nonce,
the 20-byte secret's ciphertext and the 16-byte tag. The prefix names the key
version, so a later rotation can tell old rows from new. The tag covers the
organization and the member the secret belongs to: a row copied to another
member, a changed byte and a wrong key are all refused.

The plain secret exists only inside ``unseal``'s caller: where the code is
checked and where the enrolment QR is drawn. Nothing here logs a value.
"""

import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
STORED_PATTERN = r"^v1:[A-Za-z0-9_-]{64}$"
KEY_PATTERN = r"^[0-9a-f]{64}$"
NONCE_BYTES = 12
SECRET_BYTES = 20


class SecretUnreadableError(Exception):
    """The stored secret could not be opened. The reason is deliberately not given."""


class KeyMissingError(Exception):
    """RANDIRANDI_AUTHENTICATOR_KEY is not set or not 64 hex characters."""


def _cipher(key: str) -> AESGCM:
    if not re.fullmatch(KEY_PATTERN, key or ""):
        raise KeyMissingError(
            "RANDIRANDI_AUTHENTICATOR_KEY is not set or is not 64 lower-case hex "
            "characters (32 random bytes; make one with: openssl rand -hex 32)."
        )
    return AESGCM(bytes.fromhex(key))


def _bound_to(organization_id: int, member_id: int) -> bytes:
    return f"randirandi authenticator {VERSION} {organization_id} {member_id}".encode()


def seal(plain_hex: str, *, key: str, organization_id: int, member_id: int) -> str:
    plain = bytes.fromhex(plain_hex)
    if len(plain) != SECRET_BYTES:
        raise ValueError("An authenticator secret is 20 bytes.")
    nonce = os.urandom(NONCE_BYTES)
    sealed = _cipher(key).encrypt(nonce, plain, _bound_to(organization_id, member_id))
    return f"{VERSION}:" + base64.urlsafe_b64encode(nonce + sealed).decode()


def unseal(stored: str, *, key: str, organization_id: int, member_id: int) -> str:
    cipher = _cipher(key)
    if not re.fullmatch(STORED_PATTERN, stored):
        raise SecretUnreadableError(
            "The stored authenticator secret is not in the known form."
        )
    try:
        raw = base64.urlsafe_b64decode(stored[len(VERSION) + 1 :])
        plain = cipher.decrypt(
            raw[:NONCE_BYTES], raw[NONCE_BYTES:], _bound_to(organization_id, member_id)
        )
    except InvalidTag, binascii.Error, ValueError:
        raise SecretUnreadableError(
            "The stored authenticator secret did not open: wrong key or changed row."
        ) from None
    return plain.hex()
