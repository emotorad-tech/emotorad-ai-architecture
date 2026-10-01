"""Self-service "delete my data" (spec 2026-10-01).

The chat (runtime erasure_gate) and the Amiigo app (POST /erasure-requests)
only record a request. The nightly job (erasure_job.py) is the only thing that
deletes. This module holds what they share: the phrases, the fixed replies,
the reference and the shape of the erasure_log audit record.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from typing import Any, Callable, Dict, Optional

# No 0/O, 1/I/L or U: a reference is read aloud and typed back.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

# ConversationState.erasure_step
WANTED = "wanted"
CANCEL_WANTED = "cancel_wanted"
CONFIRMING = "confirming"

# No \b around the Hindi: Devanagari vowel signs are not word characters.
_DELETE = re.compile(
    r"\b(?:delete|erase|remove)\s+my\s+(?:data|account|details|conversation\s+data|chat\s+data)\b"
    r"|\bforget\s+me\b"
    r"|मेरा\s+डेटा\s+(?:हटाओ|हटा\s+दो|डिलीट)"
    r"|\b(?:borrar|eliminar)\s+mis\s+datos\b|\beliminar\s+mi\s+cuenta\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"\bcancel\s+(?:my|the)\s+deletion\b|\b(?:don'?t|do\s+not)\s+delete\s+my\s+data\b",
    re.IGNORECASE,
)

ERASURE_DIALOG = (
    "This deletes everything this chat holds about you: your past chats with me, "
    "the photos and videos you sent, and the record of where you chatted from. "
    "It does not delete your warranty registration, orders, invoices or service "
    "tickets, which EMotorad keeps for your warranty and by law. It's done within "
    "30 days."
)
ERASURE_CONFIRM = ERASURE_DIALOG + " Reply DELETE to confirm, or anything else to keep your data."
ERASURE_REQUESTED = (
    "Your deletion request is {reference}. Everything this chat holds about you "
    "will be deleted in tonight's run and removed for good within 30 days. If you "
    "change your mind before then, say 'cancel my deletion'."
)
ERASURE_KEPT = "OK, nothing has been deleted."
ERASURE_EXISTING = (
    "You've already asked for this. Your request is {reference}, and it will be "
    "done in tonight's run."
)
ERASURE_CANCELLED = "Your deletion request {reference} is cancelled. Nothing has been deleted."
ERASURE_NOTHING_TO_CANCEL = "There's no deletion request to cancel."
ERASURE_FAILED = "I couldn't record your request just now. Please try again in a few minutes."
ERASURE_SIGN_IN = "Sign in to the app to delete your data."


def wants_cancel(text: Optional[str]) -> bool:
    return bool(_CANCEL.search(text or ""))


def wants_deletion(text: Optional[str]) -> bool:
    # "don't delete my data" holds "delete my data": a cancel is never a request.
    return bool(_DELETE.search(text or "")) and not wants_cancel(text)


def is_confirmation(text: Optional[str]) -> bool:
    return (text or "").strip().casefold() == "delete"


def new_reference(choice: Callable[[str], str] = secrets.choice) -> str:
    return "DEL-" + "".join(choice(REFERENCE_ALPHABET) for _ in range(6))


def key_sha256(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def audit_record(
    subject: str,
    reason: str,
    run_by: str,
    at: datetime,
    deleted: Optional[Dict[str, int]] = None,
    s3_objects: Optional[int] = None,
    s3_versions: Optional[int] = None,
    incomplete: bool = False,
) -> Dict[str, Any]:
    """An erasure_log record: who was erased, as a hash, and why. The shape
    scripts/delete_person.py and the nightly job both write."""
    record: Dict[str, Any] = {
        "key_sha256": key_sha256(subject),
        "kind": subject.split("#", 1)[0],
        "reason": reason,
        "run_by": run_by,
        "at": at,
    }
    if deleted is not None:
        record["deleted"] = deleted
    if incomplete:
        record["incomplete"] = True
    if s3_objects is not None:
        record["s3_objects"] = s3_objects
    if s3_versions is not None:
        record["s3_versions"] = s3_versions
    return record
