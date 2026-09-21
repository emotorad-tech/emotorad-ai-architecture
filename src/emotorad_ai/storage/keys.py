"""S3 key derivation. Every key the platform writes is produced here, from
closed vocabularies, and validated. The client never supplies a key.

Two trees:

    assets/<programme>/<category>/<kind>/<slug>.<ext>      authored by us
    customers/<cluster_id>/<conversation_id>/<kind>/<upload_id>.<ext>   evidence

Customer keys use the identity-graph cluster id, never the phone number: a phone
in a key is PII in every access log line. `programme` and both `kind` lists are
closed on purpose — adding one is a code change with a test, the same discipline
as the tool allowlists.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Dict

PROGRAMMES = ("afs", "presales", "dealer")
ASSET_KINDS = ("photos", "videos", "tips", "docs")
CUSTOMER_KINDS = ("images", "videos", "docs")

MIME_TYPES: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "application/pdf": "pdf",
}

SIZE_CAPS: Dict[str, int] = {
    "images": 10 * 1024 * 1024,
    "videos": 100 * 1024 * 1024,
    "docs": 10 * 1024 * 1024,
}

_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGITS = re.compile(r"^\+?\d{10,15}$")

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


class KeyValidationError(ValueError):
    """A segment is outside its vocabulary or shape. Raised before any AWS call."""


def extension_for(mime: str) -> str:
    try:
        return MIME_TYPES[mime]
    except KeyError:
        raise KeyValidationError("unsupported content type %r; allowed: %s" % (mime, ", ".join(MIME_TYPES))) from None


def customer_kind_for(mime: str) -> str:
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("video/"):
        return "videos"
    return "docs"


def _segment(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value):
        raise KeyValidationError("%s must match [a-z0-9][a-z0-9-]* (got %r)" % (name, value))
    return value


def _identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise KeyValidationError("%s must be an identifier (got %r)" % (name, value))
    if _DIGITS.match(value):
        # Looks like a phone number. The cluster id is the right identifier;
        # this check makes sure nothing upstream slipped the phone through.
        raise KeyValidationError("%s must not be a phone number" % name)
    return value


def asset_key(programme: str, category: str, kind: str, slug: str, mime: str) -> str:
    if programme not in PROGRAMMES:
        raise KeyValidationError("programme must be one of %s (got %r)" % (", ".join(PROGRAMMES), programme))
    if kind not in ASSET_KINDS:
        raise KeyValidationError("asset kind must be one of %s (got %r)" % (", ".join(ASSET_KINDS), kind))
    return "assets/%s/%s/%s/%s.%s" % (
        programme, _segment("category", category), kind, _segment("slug", slug), extension_for(mime)
    )


def derivative_keys(key: str) -> Dict[str, str]:
    """Derivatives are made once at upload time (no CDN resizes on request).
    Images get a 900px WebP; videos get a poster frame; documents get nothing."""
    stem, _, ext = key.rpartition(".")
    if ext in ("jpg", "png", "webp"):
        return {"w900": "%s.w900.webp" % stem}
    if ext == "mp4":
        return {"poster": "%s.poster.jpg" % stem}
    return {}


def customer_key(cluster_id: str, conversation_id: str, kind: str, upload_id: str, mime: str) -> str:
    if kind not in CUSTOMER_KINDS:
        raise KeyValidationError("customer kind must be one of %s (got %r)" % (", ".join(CUSTOMER_KINDS), kind))
    if customer_kind_for(mime) != kind:
        raise KeyValidationError("kind %r does not match content type %r" % (kind, mime))
    return "customers/%s/%s/%s/%s.%s" % (
        _identifier("cluster_id", cluster_id),
        _identifier("conversation_id", conversation_id),
        kind,
        _identifier("upload_id", upload_id),
        extension_for(mime),
    )


def playground_key(chat_id: str, kind: str, blob_id: str, mime: str) -> str:
    """Playground attachments live under the customers/ tree (so the 180-day
    rule applies) with a fixed `playground` cluster: they are test traffic, not
    a person's evidence."""
    if kind not in CUSTOMER_KINDS:
        raise KeyValidationError("customer kind must be one of %s (got %r)" % (", ".join(CUSTOMER_KINDS), kind))
    return "customers/playground/%s/%s/%s.%s" % (
        _identifier("chat_id", chat_id), kind, _identifier("blob_id", blob_id), extension_for(mime)
    )


def new_upload_id() -> str:
    """Time-sortable: ten base-36 digits of milliseconds, then eight random."""
    ms = int(time.time() * 1000)
    digits = ""
    for _ in range(10):
        ms, rem = divmod(ms, 36)
        digits = _ALPHABET[rem] + digits
    random_part = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return "upl_%s%s" % (digits, random_part)


def is_customer_key(key: str) -> bool:
    return key.startswith("customers/")


def is_asset_key(key: str) -> bool:
    return key.startswith("assets/")


def cluster_of(key: str) -> str:
    if not is_customer_key(key):
        raise KeyValidationError("not a customer key: %r" % key)
    return key.split("/", 2)[1]
