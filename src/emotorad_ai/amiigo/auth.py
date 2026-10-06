"""Who the rider is: their Amiigo access token (docs/contracts/amiigo-support-chat.md,
"Authentication").

Amiigo's Go backend (server/common/token, aidanwoods.dev/go-paseto) signs a
PASETO v4.public token with its Ed25519 secret key. We hold only the public
half, in EMOTORAD_AMIIGO_PUBLIC_KEY (64 hex characters, set by a person in the
config store), so this server can check a token and can never make one.

v4.public, as the PASETO spec lays it out and the official test vectors pin
(tests/data/paseto_v4_public_vectors.json):

    v4.public.<base64url(message || 64-byte signature)>[.<base64url(footer)>]

with the signature over PAE("v4.public.", message, footer, implicit). Amiigo
sets no footer and no implicit assertion. It is built here on the
`cryptography` package's Ed25519 rather than on a PASETO library: the framing
is a few lines, and the vectors check it.

The message is JSON: `iat`, `nbf` and `exp` as RFC 3339 times, and one string
claim, `payload`, holding Amiigo's own JSON (`id`, `emuserId`, `level`,
`role`, `phone`, `token_type`, `issued_at`, `expired_at`). The checks run in
the order Amiigo's own checker runs them (signature, expiry, then the
payload), so an expired token is `token_expired` whatever its payload holds:

* `exp` after now minus 60 seconds and `nbf`, when present, before now plus 60
  seconds. The minute is for two servers' clocks disagreeing.
* `token_type` exactly "access". A refresh or OTP token proves something else:
  `token_type_not_allowed`.
* `phone` an Indian mobile, kept as the runtime keeps a verified number ("+91"
  and ten digits, tools/verification.py), so the rider's user key is the one a
  verified web chat with the same number gets.

Anything malformed is `token_invalid`, never an exception. Nothing here logs a
token, a phone or the key; logs tell riders apart by `rider_hash`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..tools.oms import OMSConfigError, normalise_mobile

_logger = logging.getLogger(__name__)

PUBLIC_KEY_ENV = "EMOTORAD_AMIIGO_PUBLIC_KEY"

# The contract's 401 codes ("Errors on the HTTP endpoints").
TOKEN_MISSING = "token_missing"
TOKEN_INVALID = "token_invalid"
TOKEN_EXPIRED = "token_expired"
TOKEN_TYPE_NOT_ALLOWED = "token_type_not_allowed"

# Amiigo's name for an access token (server/common/util/token.go).
ACCESS = "access"
HEADER = b"v4.public."
_SIGNATURE_BYTES = 64
# Two servers' clocks disagree a little. The token gets no longer than that.
LEEWAY = timedelta(seconds=60)

# The public key: 32 bytes written as 64 hex characters. An explicit class,
# not \w or \d, so no other script's digits pass for hex.
_KEY_HEX = re.compile(r"[0-9a-fA-F]{64}")
# base64url without padding, the only encoding PASETO uses.
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
# RFC 3339 as Go's time.RFC3339 writes it, which is how go-paseto sets iat, nbf
# and exp: "2026-10-06T10:00:00Z" or "2026-10-06T15:30:00+05:30". Fractional
# seconds are allowed, as Go's parser allows them.
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
# A verified number as the runtime keeps one: "+91" and ten ASCII digits, the
# first 6 to 9. normalise_mobile reads digits with \d, which a Devanagari
# digit matches too, so this has the last word.
_RIDER_PHONE = re.compile(r"\+91[6-9][0-9]{9}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def rider_hash_of(user_key: str) -> str:
    """How logs tell riders apart: the first 12 hex characters of the SHA-256
    of the user key. Never the phone."""
    return hashlib.sha256(user_key.encode("utf-8")).hexdigest()[:12]


class TokenInvalid(Exception):
    """The token cannot be read, or its signature does not check out. Carries
    a reason for a reader of the code, never any part of the token."""


@dataclass(frozen=True)
class CheckResult:
    """The verdict on one token. `phone` and `emuser_id` stay out of the repr,
    so a result printed in a traceback or a log names nobody."""

    ok: bool
    error: Optional[str] = None
    phone: Optional[str] = field(default=None, repr=False)
    emuser_id: Optional[str] = field(default=None, repr=False)
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class Rider:
    """A rider proved by an Amiigo access token. Only `rider_hash` and the
    expiry show in the repr; `rider_hash` is what logs carry."""

    phone: str = field(repr=False)
    user_key: str = field(repr=False)
    emuser_id: Optional[str] = field(repr=False)
    expires_at: datetime
    rider_hash: str


def _refused(error: str) -> CheckResult:
    return CheckResult(ok=False, error=error)


def _le64(n: int) -> bytes:
    # PAE's LE64 clears the top bit, for languages without unsigned integers.
    return struct.pack("<Q", n & 0x7FFFFFFFFFFFFFFF)


def pae(*pieces: bytes) -> bytes:
    """Pre-Authentication Encoding: the count, then each piece after its
    length, so no two different lists of pieces encode to the same bytes."""
    out = [_le64(len(pieces))]
    for piece in pieces:
        out += [_le64(len(piece)), piece]
    return b"".join(out)


def _b64url_decode(text: str) -> bytes:
    """Strict base64url without padding. A second spelling of the same bytes
    (stray bits set in the last character) is not the token that was signed."""
    if not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise TokenInvalid("not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text:
        raise TokenInvalid("not canonical base64url")
    return raw


def verify_v4_public(
    token: Any, public_key: Union[bytes, Ed25519PublicKey], implicit: bytes = b""
) -> Tuple[bytes, bytes]:
    """The signed message and the footer of a v4.public token, or TokenInvalid."""
    if not isinstance(token, str) or not token.startswith(HEADER.decode("ascii")):
        raise TokenInvalid("not v4.public")
    pieces = token[len(HEADER):].split(".")
    if len(pieces) > 2 or (len(pieces) == 2 and not pieces[1]):
        raise TokenInvalid("not v4.public")
    signed = _b64url_decode(pieces[0])
    footer = _b64url_decode(pieces[1]) if len(pieces) == 2 else b""
    if len(signed) < _SIGNATURE_BYTES:
        raise TokenInvalid("shorter than a signature")
    message, signature = signed[:-_SIGNATURE_BYTES], signed[-_SIGNATURE_BYTES:]
    key = public_key if isinstance(public_key, Ed25519PublicKey) else Ed25519PublicKey.from_public_bytes(public_key)
    try:
        key.verify(signature, pae(HEADER, message, footer, implicit))
    except InvalidSignature:
        raise TokenInvalid("signature does not check out") from None
    return message, footer


def _json_object(raw: Union[bytes, str]) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, RecursionError):
        # ValueError covers bytes that are not UTF-8 and text that is not JSON;
        # RecursionError, nesting deeper than the parser goes.
        raise TokenInvalid("not JSON") from None
    if not isinstance(value, dict):
        raise TokenInvalid("not a JSON object")
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise TokenInvalid("not an RFC 3339 time")
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        # A month 13, or a time past the calendar's end once in UTC.
        raise TokenInvalid("not a time") from None


def _rider_phone(value: Any) -> str:
    if not isinstance(value, str):
        raise TokenInvalid("no phone")
    try:
        phone = "+91" + normalise_mobile(value)
    except OMSConfigError:
        raise TokenInvalid("not an Indian mobile") from None
    if not _RIDER_PHONE.fullmatch(phone):
        raise TokenInvalid("not an Indian mobile")
    return phone


# Whether amiigo_tokens_not_configured has been logged by this process. Once
# is enough to see it in the deploy log, and the test suite, which reloads
# api.py many times, prints it once rather than on every reload.
_not_configured_logged = False


def _log_not_configured() -> None:
    global _not_configured_logged
    if not _not_configured_logged:
        _not_configured_logged = True
        _logger.warning(
            "amiigo_tokens_not_configured: %s is missing or not 64 hex characters; "
            "no Amiigo token will be accepted", PUBLIC_KEY_ENV)


def _load_key(public_key_hex: Optional[str]) -> Optional[Ed25519PublicKey]:
    text = (public_key_hex or "").strip()
    if not _KEY_HEX.fullmatch(text):
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(text))
    except ValueError:
        return None


class TokenCheck:
    """Checks Amiigo access tokens with Amiigo's public key.

    Without a usable key (missing, or not 64 hex characters) the check is
    off: `enabled` is false, every token is `token_invalid`, and
    `amiigo_tokens_not_configured` is logged, once in the process, when such
    a check is built (never per token). The routes answer 503 before reading a
    token while it is off (the plan's Ruling 1), so a missing key reads as our
    outage, not the rider's."""

    def __init__(self, public_key_hex: Optional[str], clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._key = _load_key(public_key_hex)
        if self._key is None:
            _log_not_configured()

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def check(self, token: Optional[str]) -> CheckResult:
        if self._key is None:
            return _refused(TOKEN_INVALID)
        if token is None or token == "":
            return _refused(TOKEN_MISSING)
        try:
            return self._check(token)
        except TokenInvalid:
            return _refused(TOKEN_INVALID)

    def _check(self, token: str) -> CheckResult:
        message, _footer = verify_v4_public(token, self._key)
        claims = _json_object(message)
        now = self._clock()
        expires_at = _time(claims.get("exp"))
        if expires_at <= now - LEEWAY:
            return _refused(TOKEN_EXPIRED)
        if "nbf" in claims and _time(claims["nbf"]) >= now + LEEWAY:
            raise TokenInvalid("not yet valid")
        payload = claims.get("payload")
        if not isinstance(payload, str):
            raise TokenInvalid("no payload claim")
        inner = _json_object(payload)
        if inner.get("token_type") != ACCESS:
            return _refused(TOKEN_TYPE_NOT_ALLOWED)
        phone = _rider_phone(inner.get("phone"))
        emuser_id = inner.get("emuserId")
        return CheckResult(
            ok=True,
            phone=phone,
            emuser_id=emuser_id if isinstance(emuser_id, str) and emuser_id else None,
            expires_at=expires_at,
        )


def token_check_from_env(environ: Mapping[str, str] = os.environ) -> TokenCheck:
    return TokenCheck(environ.get(PUBLIC_KEY_ENV))


def rider_from_header(authorization: Optional[str], check: TokenCheck) -> Union[Rider, str]:
    """The rider an `Authorization: Bearer <token>` header proves, or the
    contract's error code.

    The scheme is not case-sensitive (RFC 9110). No header, or "Bearer" with
    nothing after it, is token_missing: the app had no token to send. Any
    other scheme is token_invalid."""
    parts = (authorization or "").split(None, 1)
    if not parts:
        return TOKEN_MISSING
    if parts[0].lower() != "bearer":
        return TOKEN_INVALID
    token = parts[1].strip() if len(parts) == 2 else ""
    if not token:
        return TOKEN_MISSING
    result = check.check(token)
    if not result.ok:
        return result.error or TOKEN_INVALID
    user_key = "PHONE#" + result.phone
    return Rider(
        phone=result.phone,
        user_key=user_key,
        emuser_id=result.emuser_id,
        expires_at=result.expires_at,
        rider_hash=rider_hash_of(user_key),
    )
