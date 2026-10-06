"""Conversations and idempotency receipts in MongoDB (database `emotorad_ai`).

Spec: docs/superpowers/specs/2026-09-29-mongodb-conversation-store-design.md.

Thirteen collections. `transcript_turns`, `conversation_summaries`, `media`
and `conversation_notices` (a notice in a chat, such as a ticket closed in
Zoho Desk) are the conversation record and are kept permanently: no TTL
index, and deletion on request through `delete_person` or
`delete_conversation`.
`conversations` (working state), `idempotency_keys` and
`verification_sessions` (the number each web chat proved, twelve hours)
and `amiigo_receipts` (a rider's sent messages, 24 hours: amiigo/receipts.py)
expire through TTL indexes. `conversation_origins` (where each run came
from), `erasure_requests` and `erasure_log` are described beside their names
below.

`tickets` (the Zoho Desk ticket record, tickets/record.py) and `counters`
(the number behind each reference) are permanent too. Erasure does not reach
`tickets` yet: spec 2026-10-05 section 11 is deferred, so a person erasing
someone removes their ticket records by hand.

Every driver failure becomes a typed error the runtime handles:
ConversationConflict when another server saved first, StoreUnavailable for
anything else. Nothing here ever continues on an empty state after a failed
read, which would silently lose the customer's conversation.

The connection string holds a password. It is read from EMOTORAD_MONGO_URI
here and nowhere else, and never logged or put into an error message.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .. import erasure as erasure_rules
from ..amiigo.receipts import (
    ABANDONED,
    BUSY,
    CLAIMED,
    DONE,
    DUPLICATE,
    LEASE,
    PROCESSING,
    RECEIPT_TTL,
    answers,
    new_receipt,
)
from ..contract import InboundMessage, Reply
from ..conversation import (
    SHARED_OWNER,
    ConversationConflict,
    ConversationState,
    ConversationSummaryItem,
    StoreUnavailable,
    TranscriptTurn,
    _is_customer_turn,
    notice_doc,
    notice_seq,
    summary_key,
    transcript_turns,
    utc_now_iso,
)
from ..tickets.clock import plus
from ..tickets.kinds import FIRST_DESK_NUMBER, STUCK_SECONDS, URGENT_LATE_SECONDS, desk_reference
from ..tickets.record import GONE, OUTSTANDING, SENT, STUCK, SUPPORT_CLOSED, WAITING, support_status
from ..tickets.store import age_seconds, listing_row
from ..tools.registry import CLAIM_LEASE_SECONDS, write_in_progress

MONGO_URI_ENV = "EMOTORAD_MONGO_URI"

CONVERSATIONS = "conversations"
TRANSCRIPT_TURNS = "transcript_turns"
CONVERSATION_SUMMARIES = "conversation_summaries"
IDEMPOTENCY_KEYS = "idempotency_keys"
MEDIA = "media"
# Where each run of a conversation came from (origin.py). Permanent, like the
# transcript, and erased with the person or the conversation.
CONVERSATION_ORIGINS = "conversation_origins"
# Notices in a chat, read back as `system` messages by the Amiigo history
# (amiigo/history.py): `_id` `<conversation_id>#N<seq:05d>`, `conversation_id`,
# `user_key`, `kind` ("ticket_closed"), `text` and `at` (ISO 8601 UTC, as a
# transcript turn's). Permanent, and erased with the person or the conversation.
CONVERSATION_NOTICES = "conversation_notices"
# How often a notice is tried when its number or its text was taken by
# another server between the look and the write.
NOTICE_ATTEMPTS = 5
# Self-service erasure requests (erasure.py), and the audit log every erasure
# writes. Neither is erased with the person: a closed request holds only a hash.
ERASURE_REQUESTS = "erasure_requests"
ERASURE_LOG = "erasure_log"
# The Zoho Desk ticket record (tickets/record.py), permanent like the
# transcript, and the counter that numbers its references.
TICKETS = "tickets"
COUNTERS = "counters"
# The counters document behind EM-1000001, EM-1000002, ...
TICKET_COUNTER = "ticket_reference"
# Which number each web chat has proved (tools/verification.py), so a restart
# of the one process does not make a verified chat anonymous. Not enough for
# several servers: codes stay in each process's memory, which answers first.
# Expires with the proof after twelve hours, and is erased with the person or
# the conversation. Used only once its TTL index exists (wiring.build_stores).
VERIFICATION_SESSIONS = "verification_sessions"
# The chat socket's receipts (amiigo/receipts.py): one per message a rider
# sent, `_id` `<user_key>#<client_message_id>`, kept 24 hours. Used only once
# its TTL index exists (wiring.build_stores).
AMIIGO_RECEIPTS = "amiigo_receipts"

# Collection -> [(keys, options)]. The permanent record has no TTL index.
INDEXES: Dict[str, List[Tuple[List[Tuple[str, int]], Dict[str, Any]]]] = {
    CONVERSATIONS: [
        ([("expires_at", 1)], {"name": "expires_at_ttl", "expireAfterSeconds": 0}),
        ([("user_key", 1)], {"name": "user_key"}),
    ],
    TRANSCRIPT_TURNS: [
        ([("conversation_id", 1), ("n", 1)], {"name": "conversation_turn", "unique": True}),
        ([("user_key", 1)], {"name": "user_key"}),
    ],
    CONVERSATION_SUMMARIES: [
        ([("user_key", 1), ("started_at", -1)], {"name": "user_recent"}),
        # The Amiigo history: a rider's runs, and who owns a conversation.
        ([("user_key", 1), ("last_at", -1)], {"name": "user_last"}),
        ([("conversation_id", 1)], {"name": "conversation"}),
    ],
    CONVERSATION_NOTICES: [
        ([("conversation_id", 1), ("at", 1)], {"name": "conversation_at"}),
        # A chat says one thing once: two servers writing the same ticket's
        # closure make one notice (add_notice).
        ([("conversation_id", 1), ("kind", 1), ("text", 1)], {"name": "one_notice_per_text", "unique": True}),
    ],
    IDEMPOTENCY_KEYS: [
        ([("expires_at", 1)], {"name": "expires_at_ttl", "expireAfterSeconds": 0}),
    ],
    # Every photo and video kept for a conversation. Permanent, like the
    # transcript: no TTL index, and removed only through delete_conversation.
    MEDIA: [
        ([("conversation_id", 1)], {"name": "conversation"}),
    ],
    CONVERSATION_ORIGINS: [
        ([("conversation_id", 1)], {"name": "conversation"}),
        ([("user_key", 1)], {"name": "user_key"}),
        ([("started_at", 1)], {"name": "started_at"}),
        ([("country", 1), ("region", 1)], {"name": "place"}),
    ],
    ERASURE_REQUESTS: [
        ([("user_key", 1), ("status", 1)], {"name": "user_status"}),
        # One pending request per person, enforced by the database: a double
        # tap cannot make two (the final review, 2026-10-01).
        ([("user_key", 1)], {"name": "one_pending_per_person", "unique": True,
                             "partialFilterExpression": {"status": "pending"}}),
        ([("status", 1), ("requested_at", 1)], {"name": "status_requested"}),
    ],
    TICKETS: [
        # One record per source key: a retried create finds the first, and two
        # servers racing make one. Zoho stays off without it (spec section 1).
        ([("source_key", 1)], {"name": "source_key", "unique": True}),
        # The worker's due query.
        ([("state", 1), ("next_attempt_at", 1)], {"name": "due"}),
        ([("phone", 1)], {"name": "phone"}),
        ([("conversation_id", 1)], {"name": "conversation"}),
        # The cap on unverified tickets (tickets/caps.py) counts a window of
        # them on every one it records: unverified_since.
        ([("identity", 1), ("urgent", 1), ("created_at", 1)], {"name": "unverified_recent"}),
        # A closure from Zoho Desk names the ticket by Zoho's id (close_support).
        # Not unique: every record not yet sent has none.
        ([("zoho.ticket_id", 1)], {"name": "zoho_ticket"}),
    ],
    # One small document per sequence, found by its _id: no index of its own.
    COUNTERS: [],
    VERIFICATION_SESSIONS: [
        ([("expires_at", 1)], {"name": "expires_at_ttl", "expireAfterSeconds": 0}),
        ([("user_key", 1)], {"name": "user_key"}),
    ],
    AMIIGO_RECEIPTS: [
        ([("expires_at", 1)], {"name": "expires_at_ttl", "expireAfterSeconds": 0}),
        # One message of a conversation processing at a time, held by the
        # database across servers ("conversation_busy"); also how the socket
        # finds the message in flight.
        ([("conversation_id", 1)], {"name": "one_processing_per_conversation", "unique": True,
                                    "partialFilterExpression": {"state": PROCESSING}}),
        # Erasure: a person's receipts, and a conversation's (Ruling 14).
        ([("user_key", 1)], {"name": "user_key"}),
        ([("conversation_id", 1), ("at", 1)], {"name": "conversation_at"}),
    ],
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def connect(
    uri: Optional[str] = None,
    db_name: str = "emotorad_ai",
    client: Any = None,
    timeout_ms: int = 3000,
    operation_timeout_ms: int = 5000,
) -> Any:
    """The `emotorad_ai` database. Short timeouts on finding a server and on
    every operation, so a cluster that is unreachable or stops answering fails
    the turn quickly into a handover, never a customer left waiting."""
    if client is None:
        uri = uri if uri is not None else os.environ.get(MONGO_URI_ENV, "")
        if not uri:
            raise StoreUnavailable("%s is not set" % MONGO_URI_ENV)
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms,
                             timeoutMS=operation_timeout_ms, appname="emotorad-ai")
    return client[db_name]


def ensure_indexes(db: Any) -> Dict[str, List[str]]:
    """Create the collections and indexes the stores rely on. Safe to rerun:
    MongoDB leaves an identical index alone. Returns collection -> index names."""
    existing = set(db.list_collection_names())
    report: Dict[str, List[str]] = {}
    for collection, indexes in INDEXES.items():
        if collection not in existing:
            db.create_collection(collection)
        for keys, options in indexes:
            db[collection].create_index(keys, **options)
        report[collection] = sorted(db[collection].index_information())
    return report


def _receipts_of(conversation_id: str) -> Dict[str, Any]:
    """Idempotency receipts are keyed `<conversation id>:<run start>:<tool>:<key>`,
    or `<conversation id>:<tool>:<key>` for a call with no run, and can hold
    what a tool returned about the person (a booking's customer id). Both
    begin with the conversation id and a colon, which is what this matches."""
    return {"_id": {"$regex": "^%s:" % re.escape(conversation_id)}}


def _summary_item(doc: Dict[str, Any]) -> ConversationSummaryItem:
    """A stored summary as the dataclass, ignoring `_id` and any field a
    newer version wrote."""
    fields = set(ConversationSummaryItem.__dataclass_fields__)
    return ConversationSummaryItem(**{k: v for k, v in doc.items() if k in fields})


def _turn_starts(history: Sequence[Dict[str, Any]]) -> List[int]:
    """Indexes where a customer turn begins, by the same rule the history
    window uses (conversation._is_customer_turn): a customer's message may be a
    list of blocks when it carries a photo, and only tool results must never be
    cut from the tool call before them."""
    return [i for i, m in enumerate(history) if _is_customer_turn(m)]


class MongoConversationStore:
    def __init__(
        self,
        db: Any,
        clock: Callable[[], str] = utc_now_iso,
        now: Callable[[], datetime] = _utc_now,
        state_ttl_hours: int = 48,
        log: Any = None,
        max_state_bytes: int = 4_000_000,
    ) -> None:
        self._db = db
        self._clock = clock
        self._now = now
        self._state_ttl = timedelta(hours=state_ttl_hours)
        self._log = log
        self._max_state_bytes = max_state_bytes

    def _collection(self, name: str) -> Any:
        return self._db[name]

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    # -- working state -----------------------------------------------------

    def get(self, conversation_id: str) -> ConversationState:
        doc = self._guard("find_one", lambda: self._collection(CONVERSATIONS).find_one({"_id": conversation_id}))
        if not doc:
            # New, or its working state expired. The permanent record may hold
            # an earlier run's turns: number on from the last of them.
            last = self._guard("find_one", lambda: self._collection(TRANSCRIPT_TURNS).find_one(
                {"conversation_id": conversation_id}, sort=[("n", -1)], projection={"n": 1}))
            return ConversationState(conversation_id=conversation_id, started_at=self._clock(),
                                     turn_offset=int(last["n"]) if last else 0)
        state = ConversationState.from_json(doc["state"])
        state.version = int(doc["version"])
        return state

    def peek(self, conversation_id: str) -> Optional[ConversationState]:
        """Like `get`, but never creates a state: checking who owns a
        conversation must not itself count as that conversation starting."""
        doc = self._guard("find_one", lambda: self._collection(CONVERSATIONS).find_one({"_id": conversation_id}))
        if not doc:
            return None
        state = ConversationState.from_json(doc["state"])
        state.version = int(doc["version"])
        return state

    def save(self, state: ConversationState) -> None:
        self._trim(state)
        expected = state.version
        fields = {
            "state": state.to_json(),
            "version": expected + 1,
            "user_key": state.user_key,
            "updated_at": self._now(),
            "expires_at": self._now() + self._state_ttl,
        }
        collection = self._collection(CONVERSATIONS)
        if expected == 0:
            try:
                self._guard("insert_one", lambda: collection.insert_one(dict(fields, _id=state.conversation_id)))
            except DuplicateKeyError:
                raise ConversationConflict("conversation was first saved by another server") from None
        else:
            result = self._guard(
                "update_one",
                lambda: collection.update_one({"_id": state.conversation_id, "version": expected}, {"$set": fields}),
            )
            if result.matched_count == 0:
                raise ConversationConflict("conversation was saved by another server")
        state.version = expected + 1

    def _trim(self, state: ConversationState) -> None:
        """Keep the document well under MongoDB's 16 MB limit by dropping the
        oldest whole turns. The transcript is separate and keeps everything."""
        if len(state.to_json().encode("utf-8")) > self._max_state_bytes:
            # A photo's bytes are the bulk. Replace the data of every photo
            # before the latest customer turn with a note, before dropping any
            # turn: one large photo must not push out the symptoms typed and
            # the warranty looked up. The model saw each photo on its own turn.
            starts = _turn_starts(state.history)
            latest = starts[-1] if starts else len(state.history)
            compacted = 0
            for entry in state.history[:latest]:
                content = entry.get("content")
                if entry.get("role") != "user" or not isinstance(content, list):
                    continue
                for i, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") in ("image", "document"):
                        content[i] = {"type": "text", "text": "[The customer sent a %s here.]" % (
                            "photo" if block.get("type") == "image" else "document")}
                        compacted += 1
            if compacted and self._log is not None:
                self._log.emit("history_media_compacted", state.conversation_id, blocks=compacted)
        dropped = 0
        while len(state.to_json().encode("utf-8")) > self._max_state_bytes:
            starts = _turn_starts(state.history)
            if len(starts) < 2:
                break  # one turn left; the write will fail loudly rather than lose it
            del state.history[: starts[1]]
            dropped += 1
        if dropped and self._log is not None:
            self._log.emit("history_trimmed", state.conversation_id, turns_dropped=dropped)

    # -- the record: transcript and summaries (permanent) ------------------

    def record_turn(
        self,
        state: ConversationState,
        inbound: InboundMessage,
        reply: Reply,
        summary: Optional[ConversationSummaryItem] = None,
    ) -> None:
        turns = self._collection(TRANSCRIPT_TURNS)
        for turn in transcript_turns(state, inbound, reply, self._clock()):
            doc = dict(asdict(turn), conversation_id=state.conversation_id, user_key=state.user_key,
                       attachments=[dict(a) for a in turn.attachments])
            key = "%s#%05d" % (state.conversation_id, turn.n)
            self._guard("replace_one", lambda: turns.replace_one({"_id": key}, dict(doc, _id=key), upsert=True))
        if summary is not None and state.user_key:
            summaries = self._collection(CONVERSATION_SUMMARIES)
            key = summary_key(summary.conversation_id, summary.started_at)
            doc = dict(asdict(summary), _id=key)
            self._guard("replace_one", lambda: summaries.replace_one({"_id": key}, doc, upsert=True))

    def transcript(self, conversation_id: str) -> List[TranscriptTurn]:
        docs = self._guard(
            "find", lambda: list(self._collection(TRANSCRIPT_TURNS).find({"conversation_id": conversation_id}).sort("n", 1)),
        )
        return [
            TranscriptTurn(
                n=d["n"], role=d["role"], text=d["text"], at=d["at"],
                attachments=tuple(d.get("attachments") or ()), handled_by=d.get("handled_by", ""), path=d.get("path", ""),
                conversation_id=d.get("conversation_id") or conversation_id,
            )
            for d in docs
        ]

    # -- the Amiigo history (amiigo/history.py) ------------------------------

    def runs_of(self, user_key: str, channel: Optional[str] = None) -> List[ConversationSummaryItem]:
        """Every run of every conversation of one person: one summary each.
        Grouped into conversations by amiigo/history.py."""
        query: Dict[str, Any] = {"user_key": user_key}
        if channel is not None:
            query["channel"] = channel
        docs = self._guard(
            "find", lambda: list(self._collection(CONVERSATION_SUMMARIES).find(query).sort("last_at", -1)))
        return [_summary_item(d) for d in docs]

    def owner_of(self, conversation_id: str) -> Optional[str]:
        """The user key of the conversation's summaries: None when it has
        none, SHARED_OWNER when they name more than one person."""
        summaries = self._collection(CONVERSATION_SUMMARIES)
        keys = [k for k in self._guard(
            "distinct", lambda: summaries.distinct("user_key", {"conversation_id": conversation_id})) if k]
        if not keys:
            return None
        return keys[0] if len(keys) == 1 else SHARED_OWNER

    def turns_of(self, conversation_id: str) -> List[TranscriptTurn]:
        return self.transcript(conversation_id)

    def count_turns(self, conversation_id: str) -> int:
        turns = self._collection(TRANSCRIPT_TURNS)
        return self._guard("count_documents", lambda: turns.count_documents({"conversation_id": conversation_id}))

    def notices_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        notices = self._collection(CONVERSATION_NOTICES)
        return self._guard(
            "find", lambda: list(notices.find({"conversation_id": conversation_id}).sort([("at", 1), ("_id", 1)])))

    def add_notice(self, conversation_id: str, user_key: Optional[str], kind: str, text: str,
                   at: str) -> Tuple[Dict[str, Any], bool]:
        """A notice in the chat, numbered after its others, and whether this
        call wrote it. A notice of the same kind and text already in the chat
        is returned as it is. Two servers writing at once collide on the
        `_id` or on `one_notice_per_text` (mongo_setup.py): the loser looks
        again, and finds the other's notice or takes the next number."""
        notices = self._collection(CONVERSATION_NOTICES)
        same_query = {"conversation_id": conversation_id, "kind": kind, "text": text}
        for _ in range(NOTICE_ATTEMPTS):
            same = self._guard("find_one", lambda: notices.find_one(same_query))
            if same is not None:
                return same, False
            taken = self._guard("find", lambda: list(notices.find({"conversation_id": conversation_id}, {"_id": 1})))
            doc = notice_doc(conversation_id, max((notice_seq(d["_id"]) for d in taken), default=0) + 1,
                             user_key, kind, text, at)
            try:
                self._guard("insert_one", lambda: notices.insert_one(dict(doc)))
            except DuplicateKeyError:
                continue
            return doc, True
        raise StoreUnavailable("MongoDB insert_one failed (notice collided %d times)" % NOTICE_ATTEMPTS)

    def record_media(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (the S3 key): recording the same object twice
        (a retried claim) replaces its record rather than duplicating it."""
        media = self._collection(MEDIA)
        self._guard("replace_one", lambda: media.replace_one({"_id": record["_id"]}, record, upsert=True))

    def record_origin(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (one per run)."""
        origins = self._collection(CONVERSATION_ORIGINS)
        self._guard("replace_one", lambda: origins.replace_one({"_id": record["_id"]}, record, upsert=True))

    def origins_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self._guard(
            "find",
            lambda: list(self._collection(CONVERSATION_ORIGINS).find({"conversation_id": conversation_id})
                         .sort("started_at", 1)),
        )

    def pending_erasure_of(self, user_key: str) -> Optional[Dict[str, Any]]:
        requests = self._collection(ERASURE_REQUESTS)
        return self._guard("find_one", lambda: requests.find_one({"user_key": user_key, "status": "pending"}))

    def request_erasure(self, user_key: str, channel: str, conversation_id: Optional[str], now: str,
                        proof: Optional[Dict[str, str]] = None) -> str:
        """One pending request per person: asking again returns it."""
        pending = self.pending_erasure_of(user_key)
        if pending is not None:
            return pending["_id"]
        reference = erasure_rules.new_reference()
        record = {"_id": reference, "user_key": user_key, "status": "pending", "requested_at": now,
                  "channel": channel, "conversation_id": conversation_id, "attempts": 0, "last_error": None,
                  "proof": proof}
        requests = self._collection(ERASURE_REQUESTS)
        try:
            self._guard("insert_one", lambda: requests.insert_one(record))
        except DuplicateKeyError:
            # Another request for this person landed between the look and the
            # insert (a double tap): that one is the pending request.
            existing = self._guard(
                "find_one", lambda: requests.find_one({"user_key": user_key, "status": "pending"}))
            if existing is None:
                raise
            return existing["_id"]
        return reference

    def cancel_erasure(self, user_key: str, now: str) -> Optional[str]:
        pending = self.pending_erasure_of(user_key)
        if pending is None:
            return None
        self.close_erasure(pending["_id"], "cancelled", None, None, now)
        return pending["_id"]

    def pending_erasures(self) -> List[Dict[str, Any]]:
        requests = self._collection(ERASURE_REQUESTS)
        return self._guard("find", lambda: list(requests.find({"status": "pending"}).sort("requested_at", 1)))

    def record_erasure_failure(self, reference: str, error: str) -> int:
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$inc": {"attempts": 1}, "$set": {"last_error": error}}))
        return self._guard("find_one", lambda: requests.find_one({"_id": reference}))["attempts"]

    def close_erasure(self, reference: str, status: str, counts: Optional[Dict[str, int]],
                      error: Optional[str], now: str, by: Optional[str] = None) -> None:
        """Closed: the person's key is replaced by its hash. Reviews, a hold
        and who closed it stay: names and times only."""
        requests = self._collection(ERASURE_REQUESTS)
        record = self._guard("find_one", lambda: requests.find_one({"_id": reference})) or {}
        fields: Dict[str, Any] = {"status": status, "processed_at": now, "counts": counts}
        if record.get("user_key"):
            fields["key_sha256"] = erasure_rules.key_sha256(record["user_key"])
        if error is not None:
            fields["last_error"] = error
        if by is not None:
            fields["by"] = by
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$set": fields, "$unset": {"user_key": ""}}))

    def record_erasure_review(self, reference: str, by: str, at: str, totals: Dict[str, int]) -> None:
        """Who read a request before deciding (erasure_admin show), and what it then held."""
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$push": {"reviews": {"by": by, "at": at, "totals": dict(totals)}}}))

    def hold_erasure(self, reference: str, by: str, at: str, note: str) -> None:
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$set": {"held": {"by": by, "at": at, "note": note}}}))

    def erasure_history(self, user_key: str) -> List[Dict[str, Any]]:
        """Every request of one person, open or closed (a closed one by its hash)."""
        requests = self._collection(ERASURE_REQUESTS)
        query = {"$or": [{"user_key": user_key}, {"key_sha256": erasure_rules.key_sha256(user_key)}]}
        return self._guard("find", lambda: list(requests.find(query).sort("requested_at", 1)))

    def erasure_record(self, reference: str) -> Optional[Dict[str, Any]]:
        requests = self._collection(ERASURE_REQUESTS)
        return self._guard("find_one", lambda: requests.find_one({"_id": reference}))

    def log_erasure(self, entry: Dict[str, Any]) -> None:
        log = self._collection(ERASURE_LOG)
        self._guard("insert_one", lambda: log.insert_one(dict(entry)))

    def media_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self._guard(
            "find",
            lambda: list(self._collection(MEDIA).find({"conversation_id": conversation_id}).sort("stored_at", 1)),
        )

    def recent_summaries(
        self, user_key: str, limit: int = 3, exclude: Optional[str] = None
    ) -> List[ConversationSummaryItem]:
        query: Dict[str, Any] = {"user_key": user_key}
        if exclude:
            query["_id"] = {"$ne": exclude}
        docs = self._guard(
            "find",
            lambda: list(self._collection(CONVERSATION_SUMMARIES).find(query).sort("started_at", -1).limit(limit)),
        )
        return [_summary_item(d) for d in docs]

    def conversations_of(self, user_key: str) -> List[str]:
        """Every conversation id tied to one person, in any collection."""
        found = set()
        for name, field in ((CONVERSATIONS, "_id"), (TRANSCRIPT_TURNS, "conversation_id"),
                            (CONVERSATION_SUMMARIES, "conversation_id"), (CONVERSATION_ORIGINS, "conversation_id"),
                            (CONVERSATION_NOTICES, "conversation_id"), (VERIFICATION_SESSIONS, "_id"),
                            (AMIIGO_RECEIPTS, "conversation_id")):
            collection = self._collection(name)
            found.update(self._guard("distinct", lambda: collection.distinct(field, {"user_key": user_key})))
        return sorted(found)

    def delete_person(self, user_key: str, dry_run: bool = False) -> Dict[str, int]:
        """The right to erasure: everything held about one person.

        Every conversation that is theirs, whole: turns recorded before they
        signed in carry no user key, so conversations are found by any record
        that does, then removed by id. Their idempotency receipts go too,
        their verified sessions (found by the phone) and the chat socket's
        receipts of their messages, each removed with its conversation. With
        `dry_run`, counts what would go and deletes nothing.
        """
        counts = {name: 0 for name in (CONVERSATIONS, TRANSCRIPT_TURNS, CONVERSATION_SUMMARIES, IDEMPOTENCY_KEYS, MEDIA,
                                       CONVERSATION_ORIGINS, VERIFICATION_SESSIONS, CONVERSATION_NOTICES,
                                       AMIIGO_RECEIPTS)}
        for conversation_id in self.conversations_of(user_key):
            for name, count in self.delete_conversation(conversation_id, dry_run=dry_run).items():
                counts[name] += count
        return counts

    def delete_conversation(self, conversation_id: str, dry_run: bool = False) -> Dict[str, int]:
        """One conversation, whoever it belongs to: for a chat never tied to a
        verified person, found by its id."""
        return {
            CONVERSATIONS: self._remove(CONVERSATIONS, {"_id": conversation_id}, dry_run),
            TRANSCRIPT_TURNS: self._remove(TRANSCRIPT_TURNS, {"conversation_id": conversation_id}, dry_run),
            CONVERSATION_SUMMARIES: self._remove(CONVERSATION_SUMMARIES, {"conversation_id": conversation_id}, dry_run),
            IDEMPOTENCY_KEYS: self._remove(IDEMPOTENCY_KEYS, _receipts_of(conversation_id), dry_run),
            MEDIA: self._remove(MEDIA, {"conversation_id": conversation_id}, dry_run),
            CONVERSATION_ORIGINS: self._remove(CONVERSATION_ORIGINS, {"conversation_id": conversation_id}, dry_run),
            VERIFICATION_SESSIONS: self._remove(VERIFICATION_SESSIONS, {"_id": conversation_id}, dry_run),
            CONVERSATION_NOTICES: self._remove(CONVERSATION_NOTICES, {"conversation_id": conversation_id}, dry_run),
            # The chat socket's receipts (Ruling 14): their ids name the phone.
            AMIIGO_RECEIPTS: self._remove(AMIIGO_RECEIPTS, {"conversation_id": conversation_id}, dry_run),
        }

    def _remove(self, name: str, query: Dict[str, Any], dry_run: bool) -> int:
        collection = self._collection(name)
        if dry_run:
            return self._guard("count_documents", lambda: collection.count_documents(query))
        return self._guard("delete_many", lambda: collection.delete_many(query)).deleted_count

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history


class MongoIdempotencyStore:
    """The registry's idempotency contract, shared by every server.

    Claim-before-execute: inserting a pending receipt is the claim, and a
    duplicate `_id` means someone else holds it. A pending claim older than
    the lease belongs to a server that died mid-call and is taken over.
    """

    def __init__(
        self, db: Any, now: Callable[[], datetime] = _utc_now, ttl_days: int = 7, lease_seconds: int = CLAIM_LEASE_SECONDS
    ) -> None:
        self._receipts = db[IDEMPOTENCY_KEYS]
        self._now = now
        self._ttl = timedelta(days=ttl_days)
        self._lease = timedelta(seconds=lease_seconds)

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    def claim(self, key: str) -> Optional[Dict[str, Any]]:
        now = self._now()
        try:
            self._guard("insert_one", lambda: self._receipts.insert_one(
                {"_id": key, "status": "pending", "claimed_at": now, "expires_at": now + self._ttl}))
            return None
        except DuplicateKeyError:
            pass
        taken_over = self._guard("find_one_and_update", lambda: self._receipts.find_one_and_update(
            {"_id": key, "status": "pending", "claimed_at": {"$lt": now - self._lease}},
            {"$set": {"claimed_at": now, "expires_at": now + self._ttl}}))
        if taken_over is not None:
            return None
        existing = self.get(key)
        return existing if existing is not None else write_in_progress()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        doc = self._guard("find_one", lambda: self._receipts.find_one({"_id": key}))
        if not doc or doc.get("status") != "done":
            return None
        return json.loads(doc["envelope"])

    def put(self, key: str, envelope: Dict[str, Any]) -> None:
        doc = {"_id": key, "status": "done", "envelope": json.dumps(envelope, default=str),
               "expires_at": self._now() + self._ttl}
        self._guard("replace_one", lambda: self._receipts.replace_one({"_id": key}, doc, upsert=True))

    def release(self, key: str) -> None:
        # Only a pending claim is released; a finished receipt is never undone.
        self._guard("delete_one", lambda: self._receipts.delete_one({"_id": key, "status": "pending"}))


class MongoVerifiedSessions:
    """The proved numbers of web chats (VerifiedSessions in tools/verification.py).

    One document per conversation: `_id` the conversation id, the phone, its
    `user_key` for erasure, `verified_on` (the wall-clock time it was proved,
    for an erasure request's proof) and `expires_at`, behind a TTL index.
    `get` checks `expires_at` itself, since the TTL monitor runs about once a
    minute and a lapsed proof must not count in between.
    """

    def __init__(self, db: Any, now: Callable[[], datetime] = _utc_now) -> None:
        self._sessions = db[VERIFICATION_SESSIONS]
        self._now = now

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    def save(self, conversation_id: str, phone: str, verified_on: str, expires_at: datetime) -> None:
        doc = {"_id": conversation_id, "phone": phone, "user_key": "PHONE#" + phone,
               "verified_on": verified_on, "expires_at": expires_at}
        self._guard("replace_one", lambda: self._sessions.replace_one({"_id": conversation_id}, doc, upsert=True))

    def get(self, conversation_id: str) -> Optional[Tuple[str, str]]:
        doc = self._guard("find_one", lambda: self._sessions.find_one({"_id": conversation_id}))
        if not doc:
            return None
        expires_at = doc["expires_at"]
        if expires_at.tzinfo is None:  # the driver hands back naive UTC
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= self._now():
            return None
        return doc["phone"], doc.get("verified_on") or ""

    def delete(self, conversation_id: str) -> None:
        self._guard("delete_one", lambda: self._sessions.delete_one({"_id": conversation_id}))

    def has_ttl_index(self) -> bool:
        """Whether mongo_setup.py has made the index that removes a session at
        its `expires_at`. Only that script makes it, and a deploy never runs it,
        so without it a proved number would stay for ever: the service then
        keeps proofs in memory instead (wiring.build_stores). Raises
        StoreUnavailable when the index list cannot be read."""
        info = self._guard("index_information", lambda: self._sessions.index_information())
        return any([name for name, _ in spec.get("key", [])] == ["expires_at"]
                   and spec.get("expireAfterSeconds") == 0 for spec in info.values())


def _aware(moment: Any) -> Any:
    """A time read back from the driver, which hands back naive UTC."""
    if isinstance(moment, datetime) and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


class MongoAmiigoReceipts:
    """The chat socket's receipts (amiigo/receipts.py) for every server,
    held to the same behaviour as InMemoryAmiigoReceipts
    (tests/test_amiigo_socket.py).

    The claim is the insert: a duplicate `_id` is the same message already
    accepted, and the partial unique index on `conversation_id` refuses a
    second processing message of one conversation, from any server. A claim
    past its lease is set `abandoned` first, so it holds the conversation no
    longer, and its own message may be claimed again. Reads check
    `expires_at` themselves, since the TTL monitor runs about once a minute.
    """

    def __init__(self, db: Any, now: Callable[[], datetime] = _utc_now, lease: timedelta = LEASE,
                 ttl: timedelta = RECEIPT_TTL) -> None:
        self._receipts = db[AMIIGO_RECEIPTS]
        self._now = now
        self._lease = lease
        self._ttl = ttl

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    def _read(self, rid: str, now: datetime) -> Optional[Dict[str, Any]]:
        doc = self._guard("find_one", lambda: self._receipts.find_one({"_id": rid}))
        if doc is None:
            return None
        doc["at"], doc["expires_at"] = _aware(doc["at"]), _aware(doc["expires_at"])
        return doc if doc["expires_at"] > now else None

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        return self._read(rid, self._now())

    def answering(self, rid: str) -> Optional[Dict[str, Any]]:
        """The receipt when it answers for its message (done, or running
        within its lease), else None. One read."""
        now = self._now()
        doc = self._read(rid, now)
        return doc if answers(doc, now, self._lease) else None

    def in_flight(self, conversation_id: str) -> Optional[str]:
        now = self._now()
        doc = self._guard("find_one", lambda: self._receipts.find_one(
            {"conversation_id": conversation_id, "state": PROCESSING, "at": {"$gt": now - self._lease}},
            projection={"user_key": 1}))
        return doc["user_key"] if doc else None

    def claim(self, rid: str, user_key: str, conversation_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        now = self._now()
        # A claim past its lease belongs to a turn that died with its server.
        self._guard("update_many", lambda: self._receipts.update_many(
            {"conversation_id": conversation_id, "state": PROCESSING, "at": {"$lte": now - self._lease}},
            {"$set": {"state": ABANDONED}}))
        doc = self._read(rid, now)
        if answers(doc, now, self._lease):
            return DUPLICATE, doc
        fresh = new_receipt(rid, user_key, conversation_id, now, self._ttl)
        try:
            if doc is None:
                # Expired but not yet removed by the TTL monitor: gone.
                self._guard("delete_one", lambda: self._receipts.delete_one({"_id": rid, "expires_at": {"$lte": now}}))
                self._guard("insert_one", lambda: self._receipts.insert_one(fresh))
                return CLAIMED, None
            # Abandoned, or past its lease: taken over, unless someone else
            # took it since it was read.
            taken = self._guard("replace_one", lambda: self._receipts.replace_one(
                {"_id": rid, "state": doc["state"], "at": doc["at"]}, fresh))
            if taken.matched_count:
                return CLAIMED, None
        except DuplicateKeyError:
            pass
        # Someone else claimed it, or the conversation, first.
        doc = self._read(rid, now)
        return (DUPLICATE, doc) if answers(doc, now, self._lease) else (BUSY, None)

    def finish(self, rid: str, ack: Dict[str, Any], reply: Dict[str, Any]) -> bool:
        result = self._guard("update_one", lambda: self._receipts.update_one(
            {"_id": rid}, {"$set": {"state": DONE, "ack": ack, "reply": reply}}))
        return result.matched_count > 0

    def release(self, rid: str) -> None:
        # Only a processing claim is released; a finished receipt is never undone.
        self._guard("delete_one", lambda: self._receipts.delete_one({"_id": rid, "state": PROCESSING}))

    def has_indexes(self) -> bool:
        """Whether mongo_setup.py has made the two indexes the receipts rest
        on: the one that removes a receipt at its `expires_at`, and the one
        that holds a conversation busy across servers. Without either the
        service keeps receipts in memory instead (wiring.build_stores).
        Raises StoreUnavailable when the index list cannot be read."""
        info = self._guard("index_information", lambda: self._receipts.index_information()).values()
        expires = any([name for name, _ in spec.get("key", [])] == ["expires_at"]
                      and spec.get("expireAfterSeconds") == 0 for spec in info)
        busy = any([name for name, _ in spec.get("key", [])] == ["conversation_id"] and spec.get("unique")
                   and spec.get("partialFilterExpression") == {"state": PROCESSING} for spec in info)
        return expires and busy


class MongoTicketStore:
    """The ticket record (tickets/record.py) for every server.

    The next reference is one atomic `$inc` on a `counters` document. A save
    is conditional on the worker's lease, and on the wake count when asked,
    and never upserts: a record removed, or taken by another worker, is
    dropped rather than written back. Held to the same contract as
    InMemoryTicketStore (tests/ticket_store_contract.py).
    """

    def __init__(self, db: Any) -> None:
        self._tickets = db[TICKETS]
        self._counters = db[COUNTERS]

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    # -- recording ---------------------------------------------------------------

    def next_reference(self) -> str:
        for attempt in (1, 2):
            try:
                doc = self._guard("find_one_and_update", lambda: self._counters.find_one_and_update(
                    {"_id": TICKET_COUNTER}, {"$inc": {"seq": 1}}, upsert=True,
                    return_document=ReturnDocument.AFTER))
                break
            except DuplicateKeyError:
                # Two servers upserting the first-ever counter at once: the
                # loser collides on _id, and the document exists by now.
                if attempt == 2:
                    raise StoreUnavailable("MongoDB find_one_and_update failed (DuplicateKeyError)") from None
        return desk_reference(FIRST_DESK_NUMBER - 1 + int(doc["seq"]))

    def insert(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The record, or the one already recorded under its source key. The
        unique index decides, so two servers racing make one."""
        try:
            self._guard("insert_one", lambda: self._tickets.insert_one(copy.deepcopy(record)))
        except DuplicateKeyError:
            existing = self.by_source_key(record["source_key"])
            if existing is None:
                # The reference itself was taken: refuse rather than overwrite.
                raise StoreUnavailable("MongoDB insert_one failed (DuplicateKeyError)") from None
            return existing
        return copy.deepcopy(record)

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        return self._guard("find_one", lambda: self._tickets.find_one({"_id": reference}))

    def ticket_status(self, reference: str) -> Optional[Dict[str, Any]]:
        """{"reference", "status", "closed_at"} as the rider sees it, or None
        when no record has this reference (the mock's tickets)."""
        record = self._guard("find_one", lambda: self._tickets.find_one(
            {"_id": reference}, {"support_status": 1, "closed_at": 1}))
        return support_status(record) if record is not None else None

    def by_source_key(self, source_key: str) -> Optional[Dict[str, Any]]:
        return self._guard("find_one", lambda: self._tickets.find_one({"source_key": source_key}))

    def close_support(self, zoho_ticket_id: Optional[str], closed_at: str) -> Optional[Tuple[Dict[str, Any], bool]]:
        """Support closed this Zoho Desk ticket (amiigo/tickets.py): the record
        sent as it, closed once, in one conditional update, so a repeat or a
        second server never closes it again and `closed_at` stays the first.
        None when no record was sent as this ticket; otherwise the record
        (`_id`, `conversation_id`, `support_status`, `closed_at`) and whether
        this call closed it. Nothing the worker owns is touched."""
        if not zoho_ticket_id:
            return None
        fields = {"conversation_id": 1, "support_status": 1, "closed_at": 1}
        closed = self._guard("find_one_and_update", lambda: self._tickets.find_one_and_update(
            {"zoho.ticket_id": zoho_ticket_id, "support_status": {"$ne": SUPPORT_CLOSED}},
            {"$set": {"support_status": SUPPORT_CLOSED, "closed_at": closed_at}},
            projection=fields, return_document=ReturnDocument.AFTER))
        if closed is not None:
            return closed, True
        found = self._guard("find_one", lambda: self._tickets.find_one({"zoho.ticket_id": zoho_ticket_id}, fields))
        return (found, False) if found is not None else None

    # -- new content -------------------------------------------------------------

    def wake(self, reference: str, now: str) -> bool:
        return self._wake(reference, now, {})

    def add_note(self, reference: str, text: str, now: str) -> bool:
        return self._wake(reference, now, {"$push": {"notes": {"text": text, "at": now}}})

    def _wake(self, reference: str, now: str, extra: Dict[str, Any]) -> bool:
        # The count first, then the state. A worker finishing in between
        # cannot save over this wake: its last save expects the count it read.
        update = dict(extra)
        update["$inc"] = {"wake": 1}
        update["$min"] = {"next_attempt_at": now}
        result = self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "state": {"$ne": GONE}}, update))
        if result.matched_count == 0:
            return False
        self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "state": SENT},
            {"$set": {"state": WAITING, "due_since": now, "next_attempt_at": now}}))
        return True

    def close_runs(self, conversation_id: str, new_started_at: str) -> int:
        result = self._guard("update_many", lambda: self._tickets.update_many(
            {"conversation_id": conversation_id, "started_at": {"$lt": new_started_at}, "ended_at": None},
            {"$set": {"ended_at": new_started_at}}))
        return result.modified_count

    # -- the worker --------------------------------------------------------------

    def take_due(self, now: str, mode: str, lease_seconds: float, token: str) -> Optional[Dict[str, Any]]:
        query = {"state": {"$in": list(OUTSTANDING)}, "mode": mode, "next_attempt_at": {"$lte": now},
                 "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}]}
        return self._guard("find_one_and_update", lambda: self._tickets.find_one_and_update(
            query, {"$set": {"lease_until": plus(now, lease_seconds), "lease_token": token}},
            sort=[("urgent", -1), ("next_attempt_at", 1), ("created_at", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER))

    def renew_lease(self, reference: str, token: str, until: str) -> bool:
        if not token:
            return False
        result = self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "lease_token": token}, {"$set": {"lease_until": until}}))
        return result.matched_count == 1

    def save(
        self,
        reference: str,
        token: str,
        changes: Dict[str, Any],
        add_to_set: Optional[Dict[str, List[Any]]] = None,
        push: Optional[Dict[str, List[Any]]] = None,
        expect_wake: Optional[int] = None,
    ) -> bool:
        if not token:
            return False
        query: Dict[str, Any] = {"_id": reference, "lease_token": token}
        if expect_wake is not None:
            query["wake"] = expect_wake
        update: Dict[str, Any] = {}
        if changes:
            update["$set"] = dict(changes)
        if add_to_set:
            update["$addToSet"] = {path: {"$each": list(values)} for path, values in add_to_set.items()}
        if push:
            update["$push"] = {path: {"$each": list(values)} for path, values in push.items()}
        if not update:
            return self._guard("find_one", lambda: self._tickets.find_one(query, {"_id": 1})) is not None
        # No upsert: a record erased meanwhile is not written back.
        result = self._guard("update_one", lambda: self._tickets.update_one(query, update))
        return result.matched_count == 1

    def mark_stuck(self, reference: str) -> bool:
        """A waiting record becomes stuck, with or without a lease: the check
        for overdue records does not hold one. Nothing else is changed, and
        any other state is refused."""
        result = self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "state": WAITING}, {"$set": {"state": STUCK}}))
        return result.matched_count == 1

    # -- reporting ---------------------------------------------------------------

    def overdue(self, now: str, mode: str) -> List[Dict[str, Any]]:
        query = {"state": {"$in": list(OUTSTANDING)}, "mode": mode, "$or": [
            {"urgent": True, "due_since": {"$lte": plus(now, -URGENT_LATE_SECONDS)}},
            {"urgent": False, "due_since": {"$lte": plus(now, -STUCK_SECONDS)}},
        ]}
        return self._guard("find", lambda: list(self._tickets.find(query).sort([("due_since", 1), ("_id", 1)])))

    def counts(self, mode: str, now: str) -> Dict[str, Any]:
        outstanding = {"$in": list(OUTSTANDING)}

        def count(query: Dict[str, Any]) -> int:
            return self._guard("count_documents", lambda: self._tickets.count_documents(query))

        oldest = self._guard("find_one", lambda: self._tickets.find_one(
            {"state": outstanding, "mode": mode}, {"due_since": 1}, sort=[("due_since", 1)]))
        return {
            "waiting": count({"state": WAITING, "mode": mode}),
            "stuck": count({"state": STUCK, "mode": mode}),
            "held": count({"state": outstanding, "mode": {"$ne": mode}}),
            "oldest_due_seconds": age_seconds(oldest["due_since"], now) if oldest else None,
        }

    def unverified_since(self, since: str, phone: Optional[str] = None) -> int:
        query: Dict[str, Any] = {"identity": "unverified", "urgent": False, "created_at": {"$gte": since}}
        if phone is not None:
            query["phone"] = phone
        return self._guard("count_documents", lambda: self._tickets.count_documents(query))

    def contact_for(self, phone: str) -> Optional[str]:
        docs = self._guard("find", lambda: list(self._tickets.find(
            {"phone": phone, "mode": "live", "identity": "verified"}, {"zoho": 1, "created_at": 1})
            .sort([("created_at", -1), ("_id", -1)])))
        return next((d["zoho"]["contact_id"] for d in docs if (d.get("zoho") or {}).get("contact_id")), None)

    def listing(self, mode: str) -> List[Dict[str, Any]]:
        docs = self._guard("find", lambda: list(self._tickets.find({"state": {"$in": list(OUTSTANDING)}})
                                                .sort([("created_at", 1), ("_id", 1)])))
        return [listing_row(d, mode) for d in docs]

    def has_unique_source_key(self) -> bool:
        """Whether mongo_setup.py has made `source_key` unique. Zoho stays
        off without it (spec section 1)."""
        info = self._guard("index_information", lambda: self._tickets.index_information())
        return any([name for name, _ in spec.get("key", [])] == ["source_key"] and bool(spec.get("unique"))
                   for spec in info.values())
