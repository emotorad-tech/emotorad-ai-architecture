"""Conversations and idempotency receipts in MongoDB (database `emotorad_ai`).

Spec: docs/superpowers/specs/2026-09-29-mongodb-conversation-store-design.md.

Five collections. `transcript_turns`, `conversation_summaries` and `media`
are the conversation record and are kept permanently: no TTL index, and
deletion on request through `delete_person` or `delete_conversation`.
`conversations` (working state) and `idempotency_keys` expire through TTL
indexes.

Every driver failure becomes a typed error the runtime handles:
ConversationConflict when another server saved first, StoreUnavailable for
anything else. Nothing here ever continues on an empty state after a failed
read, which would silently lose the customer's conversation.

The connection string holds a password. It is read from EMOTORAD_MONGO_URI
here and nowhere else, and never logged or put into an error message.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pymongo.errors import DuplicateKeyError, PyMongoError

from ..contract import InboundMessage, Reply
from ..conversation import (
    ConversationConflict,
    ConversationState,
    ConversationSummaryItem,
    StoreUnavailable,
    TranscriptTurn,
    _is_customer_turn,
    summary_key,
    transcript_turns,
    utc_now_iso,
)
from ..tools.registry import CLAIM_LEASE_SECONDS, write_in_progress

MONGO_URI_ENV = "EMOTORAD_MONGO_URI"

CONVERSATIONS = "conversations"
TRANSCRIPT_TURNS = "transcript_turns"
CONVERSATION_SUMMARIES = "conversation_summaries"
IDEMPOTENCY_KEYS = "idempotency_keys"
MEDIA = "media"

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
    ],
    IDEMPOTENCY_KEYS: [
        ([("expires_at", 1)], {"name": "expires_at_ttl", "expireAfterSeconds": 0}),
    ],
    # Every photo and video kept for a conversation. Permanent, like the
    # transcript: no TTL index, and removed only through delete_conversation.
    MEDIA: [
        ([("conversation_id", 1)], {"name": "conversation"}),
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
    """Idempotency receipts are keyed `<conversation id>:<tool>:<key>` and can
    hold what a tool returned about the person (a booking's customer id)."""
    return {"_id": {"$regex": "^%s:" % re.escape(conversation_id)}}


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
            )
            for d in docs
        ]

    def record_media(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (the S3 key): recording the same object twice
        (a retried claim) replaces its record rather than duplicating it."""
        media = self._collection(MEDIA)
        self._guard("replace_one", lambda: media.replace_one({"_id": record["_id"]}, record, upsert=True))

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
        fields = set(ConversationSummaryItem.__dataclass_fields__)
        return [ConversationSummaryItem(**{k: v for k, v in d.items() if k in fields}) for d in docs]

    def conversations_of(self, user_key: str) -> List[str]:
        """Every conversation id tied to one person, in any collection."""
        found = set()
        for name, field in ((CONVERSATIONS, "_id"), (TRANSCRIPT_TURNS, "conversation_id"),
                            (CONVERSATION_SUMMARIES, "conversation_id")):
            collection = self._collection(name)
            found.update(self._guard("distinct", lambda: collection.distinct(field, {"user_key": user_key})))
        return sorted(found)

    def delete_person(self, user_key: str, dry_run: bool = False) -> Dict[str, int]:
        """The right to erasure: everything held about one person.

        Every conversation that is theirs, whole: turns recorded before they
        signed in carry no user key, so conversations are found by any record
        that does, then removed by id. Their idempotency receipts go too.
        With `dry_run`, counts what would go and deletes nothing.
        """
        counts = {name: 0 for name in (CONVERSATIONS, TRANSCRIPT_TURNS, CONVERSATION_SUMMARIES, IDEMPOTENCY_KEYS, MEDIA)}
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
