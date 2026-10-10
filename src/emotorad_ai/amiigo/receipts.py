"""Receipts for the rider's messages on the chat socket
(docs/contracts/amiigo-support-chat.md, "Resending").

"The server remembers ids for 24 hours: a message it has already accepted is
not handled again; it sends the same ack, and the same reply once that
exists." One receipt per message, `_id` `<user_key>#<client_message_id>`
(`receipt_id`), so two riders' ids never meet:

* `processing` from the moment the message is accepted (`claim`) until its
  turn is over;
* `done` with what the rider was answered (`finish`), from then until the
  receipt expires, 24 hours after the message was accepted;
* removed (`release`) when the message could not be handled at all (an
  upload that is not there), so the app can send it again. Each claim
  carries the claimer's own id (`claim`, made by the socket), and a release
  names it: a socket whose claim was lost in a store failure never lets go
  of a claim another socket made for the same message (the final review's
  Minor 7).

A turn that was recorded leaves none of the rider's words in its receipt:
`ack` names the stored turn by message id, and `reply` names the bot's,
with what only a live reply carries: the reply's text as sent (the plan's
Ruling 16; history keeps it masked), actions, escalated, the ticket, which
part answered, and each attachment's type, caption and poster. The frames
are built again each time they are sent, with fresh links
(amiigo/socket.py). Only a turn the store could not record keeps the
rider's message here too, masked as a transcript masks it. Erasing a
person or a conversation takes their receipts (the plan's Ruling 14:
`conversations_of`, `delete_conversation`, called by the conversation
store's own erasure).

One message at a time per conversation ("conversation_busy"): a claim is
refused while another message of the same conversation is processing. A
processing receipt past its lease (`LEASE`, longer than any turn) belongs to
a turn that died with its server: it no longer holds the conversation, and
its own message may be claimed again.

The MongoDB version is `stores.mongo.MongoAmiigoReceipts`, held to the same
behaviour by tests/test_amiigo_socket.py.
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Set, Tuple

RECEIPT_TTL = timedelta(hours=24)
# Longer than any turn: a video's description and the reply are allowed 150
# seconds ("Frames the server sends").
LEASE = timedelta(minutes=10)

PROCESSING = "processing"
DONE = "done"
# A processing receipt whose lease ran out, set aside so the conversation is
# free again (MongoDB's one-processing-per-conversation index needs it).
ABANDONED = "abandoned"

# What `claim` answers.
CLAIMED = "claimed"
DUPLICATE = "duplicate"
BUSY = "busy"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def receipt_id(user_key: str, client_message_id: str) -> str:
    return "%s#%s" % (user_key, client_message_id)


def new_claim_id() -> str:
    """An id for one claim, which only its claimer knows: what `release` names."""
    return uuid.uuid4().hex


def new_receipt(rid: str, user_key: str, conversation_id: str, now: datetime,
                ttl: timedelta = RECEIPT_TTL, claim_id: Optional[str] = None) -> Dict[str, Any]:
    return {"_id": rid, "user_key": user_key, "conversation_id": conversation_id, "state": PROCESSING,
            "ack": None, "reply": None, "at": now, "expires_at": now + ttl, "claim": claim_id or new_claim_id()}


def holds(doc: Dict[str, Any], now: datetime, lease: timedelta = LEASE) -> bool:
    """A processing receipt within its lease: its turn is running."""
    return doc.get("state") == PROCESSING and doc["at"] > now - lease


def answers(doc: Optional[Dict[str, Any]], now: datetime, lease: timedelta = LEASE) -> bool:
    """A receipt that answers for its message: done, or still running."""
    return doc is not None and (doc.get("state") == DONE or holds(doc, now, lease))


class InMemoryAmiigoReceipts:
    """The receipts of one process, behind one lock: what the in-memory
    store and a deployment without the collection's TTL index use."""

    def __init__(self, now: Callable[[], datetime] = _utc_now, lease: timedelta = LEASE,
                 ttl: timedelta = RECEIPT_TTL) -> None:
        self._now = now
        self._lease = lease
        self._ttl = ttl
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _sweep(self, now: datetime) -> None:
        """Drop expired receipts. The caller holds the lock."""
        for rid in [rid for rid, doc in self._docs.items() if doc["expires_at"] <= now]:
            del self._docs[rid]

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._sweep(self._now())
            doc = self._docs.get(rid)
            return copy.deepcopy(doc) if doc is not None else None

    def answering(self, rid: str) -> Optional[Dict[str, Any]]:
        """The receipt when it answers for its message (done, or running
        within its lease), else None."""
        with self._lock:
            now = self._now()
            self._sweep(now)
            doc = self._docs.get(rid)
            return copy.deepcopy(doc) if answers(doc, now, self._lease) else None

    def in_flight(self, conversation_id: str) -> Optional[str]:
        """The user key whose message of this conversation is processing, or None."""
        with self._lock:
            now = self._now()
            self._sweep(now)
            for doc in self._docs.values():
                if doc["conversation_id"] == conversation_id and holds(doc, now, self._lease):
                    return doc["user_key"]
        return None

    def claim(self, rid: str, user_key: str, conversation_id: str,
              claim_id: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
        """CLAIMED (the message is this caller's to handle), DUPLICATE with
        the receipt that answers for it, or BUSY (another message of the
        conversation is processing). `claim_id` is the caller's id for this
        claim, which a release must name (one is made when none is given,
        for a caller that never lets go)."""
        with self._lock:
            now = self._now()
            self._sweep(now)
            doc = self._docs.get(rid)
            if answers(doc, now, self._lease):
                return DUPLICATE, copy.deepcopy(doc)
            if any(other != rid and d["conversation_id"] == conversation_id and holds(d, now, self._lease)
                   for other, d in self._docs.items()):
                return BUSY, None
            self._docs[rid] = new_receipt(rid, user_key, conversation_id, now, self._ttl, claim_id)
            return CLAIMED, None

    def finish(self, rid: str, ack: Dict[str, Any], reply: Dict[str, Any]) -> bool:
        """The message was answered: False when its receipt is gone."""
        with self._lock:
            doc = self._docs.get(rid)
            if doc is None:
                return False
            doc.update(state=DONE, ack=copy.deepcopy(ack), reply=copy.deepcopy(reply))
            return True

    def release(self, rid: str, claim_id: str) -> None:
        """The message was not handled: it may be sent again. Only the claim
        `claim_id` names, while it is processing: a done receipt stays, and
        so does another's claim."""
        with self._lock:
            doc = self._docs.get(rid)
            if doc is not None and doc["state"] == PROCESSING and doc.get("claim") == claim_id:
                del self._docs[rid]

    # -- erasure (the conversation store calls these) ---------------------------

    def conversations_of(self, user_key: str) -> Set[str]:
        with self._lock:
            return {doc["conversation_id"] for doc in self._docs.values() if doc["user_key"] == user_key}

    def delete_conversation(self, conversation_id: str, dry_run: bool = False) -> int:
        """Every receipt of one conversation, whoever sent it: how many."""
        with self._lock:
            mine = [rid for rid, doc in self._docs.items() if doc["conversation_id"] == conversation_id]
            if not dry_run:
                for rid in mine:
                    del self._docs[rid]
            return len(mine)

    def __len__(self) -> int:
        with self._lock:
            return len(self._docs)
