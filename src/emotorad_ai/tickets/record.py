"""The ticket record: one document in `tickets` per ticket (spec section 3).

Written in the turn through the seam (seam.py) and sent to Zoho Desk after
the reply by the worker. It holds no copy of the transcript: the worker reads
the conversation store itself, for this run's turns only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .clock import plus
from .kinds import KINDS, is_urgent

# States. "waiting" and "stuck" have work outstanding; "sent" has none; "gone"
# means the Zoho ticket was deleted or merged in Desk and takes no more work.
# "held" is never stored: it is how the listing and /health show an
# outstanding record of the other mode, which is never sent.
WAITING = "waiting"
SENT = "sent"
STUCK = "stuck"
GONE = "gone"
HELD = "held"
OUTSTANDING = (WAITING, STUCK)

MODES = ("test", "live")
IDENTITIES = ("verified", "unverified")

# Where support has got to with the ticket, as the rider sees it (the Amiigo
# contract, "Tickets and Zoho Desk"): two statuses in v1. A closure from Zoho
# Desk sets `support_status` and `closed_at` once; a record from before these
# fields reads as open, and so does any other Zoho state (on hold, escalated).
SUPPORT_OPEN = "open"
SUPPORT_CLOSED = "closed"

# The fallback for a turn that died before its end: the turn's end wakes the
# record at once, and otherwise it is first sent two minutes after creation.
FIRST_ATTEMPT_SECONDS = 120


def new_record(
    *,
    reference: str,
    chat_reference: str,
    source_key: str,
    mode: str,
    kind: str,
    conversation_id: str,
    started_at: Optional[str],
    cluster_id: Optional[str],
    channel: Optional[str],
    phone: Optional[str],
    identity: str,
    category: Optional[str],
    ai_severity: Optional[str],
    summary: str,
    claims: Dict[str, Any],
    bike: Optional[Dict[str, Any]],
    coverage: Optional[str],
    customer_name: Optional[str],
    created_at: str,
    evidence_check: Optional[str] = None,
) -> Dict[str, Any]:
    """A new record: waiting, nothing posted, first due two minutes on.

    `evidence_check` is what Gemini saw in media that passed the evidence
    check (evidence_check.py), on a support or handover ticket raised after
    one. The field exists only on such a record."""
    if kind not in KINDS:
        raise ValueError("unknown ticket kind: %r" % (kind,))
    if mode not in MODES:
        raise ValueError("unknown mode: %r" % (mode,))
    if identity not in IDENTITIES:
        raise ValueError("unknown identity: %r" % (identity,))
    if not source_key:
        raise ValueError("a ticket record needs a source_key")
    if not conversation_id:
        raise ValueError("a ticket record needs a conversation_id")
    record = {
        "_id": reference,
        "chat_reference": chat_reference,
        "source_key": source_key,
        "mode": mode,
        "kind": kind,
        "urgent": is_urgent(kind, category),
        "conversation_id": conversation_id,
        "cluster_id": cluster_id,
        "started_at": started_at,
        "ended_at": None,
        "channel": channel,
        "created_at": created_at,
        "phone": phone,
        "identity": identity,
        "category": category,
        "ai_severity": ai_severity,
        "summary": summary,
        "claims": dict(claims or {}),
        "bike": dict(bike) if bike else None,
        "coverage": coverage,
        "customer_name": customer_name,
        "notes": [],
        "zoho": {"contact_id": None, "ticket_id": None, "ticket_number": None, "web_url": None,
                 "comment_ids": [], "attachment_ids": []},
        "posted_turns": [],
        "posted_media": [],
        "posted_notes": [],
        "state": WAITING,
        "due_since": created_at,
        "wake": 0,
        "attempts": 0,
        "next_attempt_at": plus(created_at, FIRST_ATTEMPT_SECONDS),
        "lease_until": None,
        "lease_token": None,
        "intent": None,
        "last_error": None,
        "support_status": SUPPORT_OPEN,
        "closed_at": None,
    }
    if evidence_check:
        record["evidence_check"] = evidence_check
    return record


def support_status(record: Dict[str, Any]) -> Dict[str, Any]:
    """The ticket as the rider sees it: {"reference", "status", "closed_at"},
    `closed_at` only when closed."""
    closed = record.get("support_status") == SUPPORT_CLOSED
    return {"reference": record["_id"], "status": SUPPORT_CLOSED if closed else SUPPORT_OPEN,
            "closed_at": record.get("closed_at") if closed else None}
