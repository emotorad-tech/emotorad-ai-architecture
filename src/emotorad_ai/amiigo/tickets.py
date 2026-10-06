"""A ticket closed in Zoho Desk, as the rider sees it (docs/contracts/amiigo-support-chat.md:
`ticket_update`, "Tickets and Zoho Desk"; the plan's Task 6).

`close_ticket` is what the Zoho Desk webhook (webhooks.py) does with a
closure, in this order:

1. The ticket record sent to Zoho Desk as that ticket is closed, once, in one
   conditional update (`close_support` on the ticket store): `support_status`
   "closed" and `closed_at`, the first closure's. A Zoho ticket no record was
   sent as is `not_ours`, and nothing else happens.
2. Nothing more for a chat with nothing recorded (`unknown`): erased at the
   person's request (erasure does not reach `tickets` yet), so nothing is
   written back into it.
3. A `system` notice in the chat (`add_notice`), with the person whose chat
   it is: written once, so a repeat of the closure, or the agent editing a
   closed ticket, finds it there and is `already_closed`, with nothing sent.
   A closure whose notice was never written (the store failed after step 1,
   and the webhook answered 503) is finished by Zoho's next call.
4. `ticket_update` to every open socket of the rider, only when the chat is
   one of the rider's app chats (Ruling 18: v1 history holds app chats only,
   so a push would name a chat the app cannot open). A rider with no socket
   open sees it in history.

A store that cannot answer raises StoreUnavailable to the caller, which
answers 503 so Zoho tries again: every step is safe to repeat.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from ..conversation import SHARED_OWNER
from ..tickets.record import SUPPORT_CLOSED
from . import history
from .auth import rider_hash_of

NOTICE_KIND = "ticket_closed"
# A draft until the support lead approves it (the contract's open questions).
NOTICE_TEXT = "Your support request {reference} was closed by our support team."

# What close_ticket answers.
CLOSED = "closed"
ALREADY_CLOSED = "already_closed"
NOT_OURS = "not_ours"
UNKNOWN = "unknown"


def notice_text(reference: str) -> str:
    return NOTICE_TEXT.format(reference=reference)


def _no_link(_stored_url: Optional[str]) -> Any:
    return None, None


def ticket_update_frame(conversation_id: str, reference: str, closed_at: str, notice: Dict[str, Any]) -> Dict[str, Any]:
    """The contract's `ticket_update`: the ticket as history shows it, and the
    notice as a history message."""
    return {
        "type": "ticket_update",
        "conversation_id": conversation_id,
        "ticket": {"reference": reference, "status": SUPPORT_CLOSED,
                   "closed_at": history.utc_text(datetime.fromisoformat(closed_at))},
        "message": history.message_view(notice, _no_link),
    }


def close_ticket(stores: Any, registry: Any, *, zoho_ticket_id: str, closed_at: Optional[datetime], now: datetime,
                 log: Any = None) -> str:
    """Support closed the Zoho Desk ticket `zoho_ticket_id` at `closed_at`
    (Zoho's time, or None: then `now`, when it reached us). `closed`,
    `already_closed`, `not_ours` or `unknown` (ours, but its chat holds
    nothing). `now` is when the notice is written. Raises StoreUnavailable."""
    found = stores.tickets.close_support(zoho_ticket_id, (closed_at or now).isoformat())
    if found is None:
        return NOT_OURS
    record, _ = found
    reference, conversation_id = record["_id"], record["conversation_id"]
    conversations = stores.conversations
    if history.is_new_conversation(conversations, conversation_id):
        return UNKNOWN
    owner = conversations.owner_of(conversation_id)
    user_key = owner if owner and owner != SHARED_OWNER else None
    app_chat = user_key is not None and history.is_app_chat_of(stores, user_key, conversation_id)
    notice, written = conversations.add_notice(conversation_id, user_key, NOTICE_KIND, notice_text(reference),
                                               now.isoformat())
    if not written:
        return ALREADY_CLOSED
    pushed = 0
    if app_chat and registry is not None:
        pushed = registry.push(user_key, ticket_update_frame(conversation_id, reference, record["closed_at"], notice))
    if log is not None:
        log.emit("amiigo_ticket_closed", conversation_id, reference=reference, pushed=pushed, app_chat=app_chat,
                 rider_hash=rider_hash_of(user_key) if user_key else None)
    return CLOSED
