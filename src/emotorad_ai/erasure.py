"""Self-service "delete my data" (spec 2026-10-01).

The chat (runtime erasure_gate) and the Amiigo app (POST /erasure-requests,
and POST /amiigo/v1/erasure-requests with the rider's token) only record a
request. A person deletes, with erasure_admin.py, after reading the request
(manual erasure spec). This module holds what they share: the phrases, the
fixed replies, the reference, the shape of the erasure_log audit record, and
the bodies of the request endpoints (asking, the status, cancelling).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

# The Amiigo app's channel, where a signed-in rider asks (app_requester).
APP_CHANNEL = "amiigo_app"

# No 0/O, 1/I/L or U: a reference is read aloud and typed back.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

# ConversationState.erasure_step
WANTED = "wanted"
CANCEL_WANTED = "cancel_wanted"
CONFIRMING = "confirming"

# No \b around the Hindi: Devanagari vowel signs are not word characters.
# "delete my chat" and its kind were missing (staging, 2026-10-01): the
# request reached the model, which said nothing could be deleted.
_DELETE = re.compile(
    r"\b(?:delete|erase|remove|clear|wipe)\s+(?:all\s+(?:of\s+)?)?(?:my|this|our|the)\s+"
    r"(?:data|account|details|chats?|conversations?|chat\s+history|history|messages"
    r"|conversation\s+data|chat\s+data)\b"
    r"|\bforget\s+me\b"
    r"|मेरा\s+डेटा\s+(?:हटाओ|हटा\s+दो|डिलीट)|मेरी\s+चैट\s+(?:हटाओ|हटा\s+दो|डिलीट)"
    r"|\b(?:borrar|eliminar)\s+(?:mis\s+datos|mi\s+cuenta|mi\s+chat|mis\s+chats|mis\s+conversaciones"
    r"|mi\s+historial)\b",
    re.IGNORECASE,
)
# The whole message, nothing else: "delete", "delete it", "erase it all".
# After a hand-over, "delete" reached the model, which said it had deleted
# the chat (staging, 2026-10-01). Only an offer follows: DELETE still has to
# come back as the next message. "clear" and "remove" are left out: "Clear."
# is an answer, and "remove it" is usually about a part.
_BARE_DELETE = re.compile(
    r"^\W*(?:please\s+)?(?:delete|erase|wipe)(?:\s+(?:it|this|that|everything|all|it\s+all|all\s+of\s+it))?"
    r"(?:\s+please)?\W*$"
    r"|^\W*(?:डिलीट|हटाओ)(?:\s+(?:करो|कर\s+दो))?\W*$"
    r"|^\W*(?:borrar|b[oó]rralo|eliminar|elim[ií]nalo)\W*$",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"\bcancel\s+(?:my\s+|the\s+)?deletion\b|\b(?:don'?t|do\s+not)\s+delete\s+my\s+data\b",
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
    "Your deletion request is {reference}. Our team will check it and delete "
    "everything this chat holds about you within 30 days. If you change your "
    "mind before then, say 'cancel my deletion'."
)
ERASURE_KEPT = "OK, nothing has been deleted."
ERASURE_EXISTING = (
    "You've already asked for this. Your request is {reference}, and our team "
    "will complete it within 30 days."
)
ERASURE_CANCELLED = "Your deletion request {reference} is cancelled. Nothing has been deleted."
ERASURE_NOTHING_TO_CANCEL = "There's no deletion request to cancel."
ERASURE_FAILED = "I couldn't record your request just now. Please try again in a few minutes."
# A model reply that said something was deleted is replaced by this (the
# erasure gate only records a request; a person deletes it with erasure_admin).
ERASURE_NOT_BY_MODEL = (
    "I can't delete anything myself. To delete what this chat holds about you, "
    "say 'delete my data' and I'll ask you to confirm."
)
ERASURE_SIGN_IN = "Sign in to the app to delete your data."
ERASURE_VERIFY_FIRST = "Verify your number in the chat first, then try again."


def wants_cancel(text: Optional[str]) -> bool:
    return bool(_CANCEL.search(text or ""))


def wants_deletion(text: Optional[str]) -> bool:
    # "don't delete my data" holds "delete my data": a cancel is never a request.
    text = text or ""
    return bool(_DELETE.search(text) or _BARE_DELETE.match(text.strip())) and not wants_cancel(text)


def is_confirmation(text: Optional[str]) -> bool:
    return (text or "").strip().casefold() == "delete"


def proof_of(otp_verified_at: Optional[str]) -> Dict[str, str]:
    """How the person asking was proven, for the review (manual erasure spec):
    a code by SMS in this chat, and when, or the app's sign-in."""
    if otp_verified_at:
        return {"method": "otp", "verified_at": otp_verified_at}
    return {"method": "app_sign_in"}


def app_requester(phone: str) -> Tuple[str, str, Dict[str, str]]:
    """The Amiigo app's signed-in rider as the person asking: their user key,
    the app's channel and the app's sign-in as the proof. The app's session
    path (api.py) and its token path (amiigo/routes.py) both ask as this."""
    return "PHONE#" + phone, APP_CHANNEL, proof_of(None)


# -- the requests the app and the website chat's button record ----------------
#
# One body each for asking, the status and cancelling, called by the website's
# POST /erasure-requests* (api.py) and the app's POST
# /amiigo/v1/erasure-requests* (amiigo/routes.py). Each path finds the person
# and words its own errors; `logged` adds fields to the log lines (the app's
# rider_hash), which never carry a phone or a token.


class RequestsUnavailable(Exception):
    """The requests could not be read or written. Already logged, as
    erasure_request_failed, by the error's class."""


def _unavailable(log: Any, exc: Exception, conversation_id: Optional[str],
                 logged: Dict[str, Any]) -> RequestsUnavailable:
    log.emit("erasure_request_failed", conversation_id or "erasure", error=type(exc).__name__, **logged)
    return RequestsUnavailable()


def record_request(conversations: Any, log: Any, user_key: str, channel: str, proof: Dict[str, str],
                   conversation_id: Optional[str], now: str, **logged: Any) -> Tuple[bool, Dict[str, Any]]:
    """The person's pending request, recorded now unless one already is:
    (whether it was recorded now, the answer)."""
    try:
        pending = conversations.pending_erasure_of(user_key)
        reference = pending["_id"] if pending else conversations.request_erasure(
            user_key, channel, conversation_id, now, proof=proof)
    except Exception as exc:
        raise _unavailable(log, exc, conversation_id, logged) from None
    if pending:
        return False, {"reference": reference, "status": "pending",
                       "text": ERASURE_EXISTING.format(reference=reference)}
    log.emit("erasure_requested", conversation_id or "erasure", reference=reference, **logged)
    return True, {"reference": reference, "status": "pending", "text": ERASURE_REQUESTED.format(reference=reference)}


def request_status(conversations: Any, log: Any, user_key: str, conversation_id: Optional[str],
                   **logged: Any) -> Dict[str, Any]:
    try:
        pending = conversations.pending_erasure_of(user_key)
    except Exception as exc:
        raise _unavailable(log, exc, conversation_id, logged) from None
    if pending is None:
        return {"reference": None, "status": "none"}
    return {"reference": pending["_id"], "status": "pending", "requested_at": pending["requested_at"]}


def cancel_request(conversations: Any, log: Any, user_key: str, conversation_id: Optional[str], now: str,
                   **logged: Any) -> Optional[Dict[str, Any]]:
    """The answer once the pending request is cancelled, or None when nothing is pending."""
    try:
        reference = conversations.cancel_erasure(user_key, now)
    except Exception as exc:
        raise _unavailable(log, exc, conversation_id, logged) from None
    if reference is None:
        return None
    log.emit("erasure_cancelled", conversation_id or "erasure", reference=reference, **logged)
    return {"reference": reference, "status": "cancelled", "text": ERASURE_CANCELLED.format(reference=reference)}


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
    scripts/delete_person.py and erasure_admin both write."""
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
