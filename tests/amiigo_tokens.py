"""Amiigo access tokens minted inside a test, with a keypair made at run time.

Never Amiigo's real key: each test makes its own Ed25519 pair, so Amiigo's
secret key is never needed, copied or used. The signing here is written
separately from `emotorad_ai.amiigo.auth` (its own PAE), so a mistake in the
checker's framing is not repeated on this side and hidden.

The layout is Amiigo's (server/common/token in the backend repo, go-paseto
v4.public): the standard `iat`, `nbf` and `exp` claims as RFC 3339 times, and
one string claim, `payload`, holding the JSON of {"id", "emuserId", "level",
"role", "phone", "token_type", "issued_at", "expired_at"}. `id` and `emuserId`
are UUIDs there. `level` and `role` are placeholders: the checker never reads
them. No footer, no implicit assertion.
"""

from __future__ import annotations

import base64
import json
import struct
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HEADER = b"v4.public."
# The Amiigo test rider (fixtures.WARRANTY_RECORDS), as Amigo stores a phone.
RIDER_PHONE = "+919700000010"
EMUSER_ID = "6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
# A claim or payload field set to this is left out of the token.
DROP = object()


def _le64(n: int) -> bytes:
    return struct.pack("<Q", n & 0x7FFFFFFFFFFFFFFF)


def _pae(*pieces: bytes) -> bytes:
    out = _le64(len(pieces))
    for piece in pieces:
        out += _le64(len(piece)) + piece
    return out


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def rfc3339(moment: datetime) -> str:
    """Go's time.RFC3339: seconds, and Z for UTC or the offset otherwise."""
    if moment.utcoffset() == timedelta(0):
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.isoformat(timespec="seconds")


def _without_drops(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not DROP}


class Keypair:
    """An Ed25519 pair made for one test."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public_bytes = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def public_hex(self) -> str:
        return self.public_bytes.hex()

    def sign(self, message: bytes, footer: bytes = b"", implicit: bytes = b"") -> str:
        signature = self.private.sign(_pae(HEADER, message, footer, implicit))
        token = HEADER.decode("ascii") + b64(message + signature)
        if footer:
            token += "." + b64(footer)
        return token

    def token(
        self,
        phone: Any = RIDER_PHONE,
        token_type: Any = "access",
        now: Optional[datetime] = None,
        lifetime: timedelta = timedelta(hours=1),
        not_before: Optional[datetime] = None,
        payload: Optional[Dict[str, Any]] = None,
        claims: Optional[Dict[str, Any]] = None,
        emuser_id: Any = EMUSER_ID,
        footer: bytes = b"",
    ) -> str:
        """An Amiigo token as the Go backend makes it, with any claim or
        payload field replaced (or left out, with DROP)."""
        now = now or datetime.now(timezone.utc)
        inner = {
            "id": "0b6f3c1e-8a2d-4f5b-9c7e-1d2a3b4c5d6e",
            "emuserId": emuser_id,
            "level": 1,
            "role": "user",
            "phone": phone,
            "token_type": token_type,
            "issued_at": rfc3339(now),
            "expired_at": rfc3339(now + lifetime),
        }
        inner.update(payload or {})
        outer: Dict[str, Any] = {
            "iat": rfc3339(now),
            "nbf": rfc3339(not_before or now),
            "exp": rfc3339(now + lifetime),
            "payload": json.dumps(_without_drops(inner)),
        }
        outer.update(claims or {})
        return self.sign(json.dumps(_without_drops(outer)).encode("utf-8"), footer=footer)
