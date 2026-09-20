"""Photos a customer sends, carried inline and never stored.

The agent's evidence gate asks for a picture before it will conclude a fault —
a melted terminal looks nothing like an intact one, and the whole
`battery-melted-terminal` record turns on that comparison. Until now there was
no way to send one.

Nothing is persisted. The image rides in on the message, is handed to the model
as vision content for that turn, and is gone. That is a deliberate trade: it
keeps a storage decision, a retention policy and a deletion story off the
critical path for a surface that is still internal-only. When photos need to
outlive the turn — attached to a Zoho ticket, or reviewed by an engineer later —
that is a different piece of work, and this module is where it starts.

The limits below are the whole security story for this path, so they are strict
and they are in code rather than in a prompt.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Dict, List, Optional

# What the model is being asked to look at. Anything else is either a mistake or
# somebody finding out what the endpoint accepts.
ALLOWED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")

# Per image, after decoding. The page downscales to 1280px on the long edge and
# re-encodes as JPEG before sending, which puts a phone photo comfortably under
# this; anything larger either skipped that step or did not come from the page.
MAX_BYTES = 4 * 1024 * 1024

# Enough to show a terminal from two angles and the charger, which is the most
# any knowledge record asks for.
MAX_ATTACHMENTS = 3

_DATA_URI = re.compile(r"^data:(?P<media_type>[^;,]+);base64,(?P<payload>.*)$", re.S)


class AttachmentError(Exception):
    """A refused attachment. Carries a message meant for the caller."""


def validate(raw: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Check inbound attachments and return them normalised.

    Only inline data is accepted. A remote URL would make this endpoint fetch an
    arbitrary address on the caller's behalf, which is a request forgery with
    extra steps, and there is no reason for the page to send one.
    """
    if not raw:
        return []
    if len(raw) > MAX_ATTACHMENTS:
        raise AttachmentError(
            "Too many attachments: %d sent, %d allowed." % (len(raw), MAX_ATTACHMENTS)
        )

    out: List[Dict[str, str]] = []
    for item in raw:
        url = (item or {}).get("url") or ""
        match = _DATA_URI.match(url)
        if not match:
            raise AttachmentError(
                "Attachments must be inline base64 data, not a link."
            )
        media_type = match.group("media_type").strip().lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise AttachmentError(
                "Unsupported attachment type %r. Expected one of: %s."
                % (media_type, ", ".join(ALLOWED_MEDIA_TYPES))
            )
        payload = match.group("payload")
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise AttachmentError("Attachment is not valid base64.")
        if len(decoded) > MAX_BYTES:
            raise AttachmentError(
                "Attachment is too large: %d bytes, limit %d." % (len(decoded), MAX_BYTES)
            )
        if not decoded:
            raise AttachmentError("Attachment is empty.")
        out.append({"kind": "image", "media_type": media_type, "data": payload})
    return out


def to_image_blocks(attachments: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Turn validated attachments into the content blocks the API takes."""
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": item["media_type"],
                "data": item["data"],
            },
        }
        for item in attachments
    ]
