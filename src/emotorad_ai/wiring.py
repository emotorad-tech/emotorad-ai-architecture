"""Which models a process talks to, chosen once from Settings.mode.

One function so the CLI and the API cannot drift apart on what a mode means.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .amiigo.receipts import InMemoryAmiigoReceipts
from .config import Settings
from .jev import JevClient
from .llm import SINGLE_MODEL_MODES
from .llm import OpenRouterChat, select_llm
from .openrouter import OpenRouterTransport

_logger = logging.getLogger(__name__)

# The /health "verification_sessions" values: where proved numbers are kept.
SESSIONS_MEMORY = "memory"
SESSIONS_MONGODB = "mongodb"
# MongoDB is the store, but proofs stay in memory (a restart forgets them, as
# before 2026-10-06) because a saved one might never be removed.
SESSIONS_TTL_MISSING = "memory: TTL index missing, run scripts/mongo_setup.py"
SESSIONS_INDEX_UNREADABLE = "memory: TTL index could not be checked"
# The /health "amiigo_receipts" values (the final review's Minor 6): where the
# chat socket's receipts are kept. MongoDB is the store, but receipts stay in
# this process's memory (a restart forgets which messages were answered)
# until both their indexes exist.
RECEIPTS_MEMORY = "memory"
RECEIPTS_MONGODB = "mongodb"
RECEIPTS_INDEXES_MISSING = "memory: indexes missing, run scripts/mongo_setup.py"
RECEIPTS_INDEX_UNREADABLE = "memory: indexes could not be checked"


@dataclass
class Models:
    llm: Any
    narrow_llm: Any = None
    jev: Any = None


def build_models(settings: Settings, transport: Optional[Any] = None) -> Models:
    if settings.mode in SINGLE_MODEL_MODES:
        # offline, anthropic, bedrock: one Claude client. select_llm checks the
        # key at startup and picks each mode's own default model id.
        return Models(llm=select_llm(settings.mode, settings))
    # openrouter: one transport, one key, three models. With the kill switch
    # off (config.jev_switch) there is no Jev client, and the runtime sends
    # every turn to the full agent, as it does in the single-model modes.
    transport = transport or OpenRouterTransport(
        base_url=settings.openrouter_base_url, timeout=settings.openrouter_timeout
    )
    return Models(
        llm=OpenRouterChat(settings.fallback_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        narrow_llm=OpenRouterChat(settings.narrow_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        jev=(JevClient(transport, model=settings.jev_model, timeout=settings.jev_timeout)
             if settings.jev_enabled else None),
    )


@dataclass
class Stores:
    conversations: Any
    idempotency: Any
    # The ticket record (tickets/store.py, stores/mongo.py). Written only when
    # Zoho Desk is on; the mock ticket system never touches it.
    tickets: Any = None
    # The numbers web chats have proved (tools/verification.py), so a restart
    # does not make a verified chat anonymous.
    verified_sessions: Any = None
    # Where they are kept, for /health (SESSIONS_*).
    verified_sessions_status: str = SESSIONS_MEMORY
    # The chat socket's receipts (amiigo/receipts.py): a rider's message is
    # handled once, however often it is sent, for 24 hours.
    amiigo_receipts: Any = None
    # Where they are kept, for /health (RECEIPTS_*).
    amiigo_receipts_status: str = RECEIPTS_MEMORY
    # The replacement order ledger (spec 2026-10-10 replacement orders).
    replacement_orders: Any = None
    replacement_orders_status: str = "memory"


def build_stores(settings: Settings, log: Any = None, client: Any = None) -> Stores:
    """Where conversations and write receipts live, chosen from Settings.store.

    `mongodb` reads the connection string from EMOTORAD_MONGO_URI (never from
    Settings) and fails loudly without one, rather than quietly falling back to
    memory and losing every conversation on the next restart.
    """
    if settings.store == "memory":
        from .conversation import InMemoryConversationStore
        from .tools.registry import IdempotencyStore
        from .tools.verification import InMemoryVerifiedSessions

        from .tickets.store import InMemoryTicketStore

        from .fulfilment import ReplacementOrders

        receipts = InMemoryAmiigoReceipts()
        return Stores(conversations=InMemoryConversationStore(receipts=receipts), idempotency=IdempotencyStore(),
                      tickets=InMemoryTicketStore(), verified_sessions=InMemoryVerifiedSessions(),
                      amiigo_receipts=receipts, replacement_orders=ReplacementOrders())
    from .stores.mongo import (
        MongoAmiigoReceipts, MongoConversationStore, MongoIdempotencyStore, MongoOrderLedger, MongoTicketStore,
        MongoVerifiedSessions, connect,
    )

    db = connect(db_name=settings.mongo_db, client=client)
    sessions, sessions_status = _verified_sessions(MongoVerifiedSessions(db), log)
    receipts, receipts_status = _amiigo_receipts(MongoAmiigoReceipts(db), log)
    ledger = MongoOrderLedger(db)
    if ledger.index_ready():
        orders, orders_status = ledger, "mongodb"
    else:
        from .fulfilment import ReplacementOrders

        orders, orders_status = ReplacementOrders(), "memory: index missing, run scripts/mongo_setup.py"
        if log is not None:
            log.emit("replacement_orders_index_missing", "replacement_orders", level="error")
    return Stores(
        conversations=MongoConversationStore(db, state_ttl_hours=settings.state_ttl_hours, log=log),
        idempotency=MongoIdempotencyStore(db, ttl_days=settings.idempotency_ttl_days),
        tickets=MongoTicketStore(db),
        verified_sessions=sessions,
        verified_sessions_status=sessions_status,
        amiigo_receipts=receipts,
        amiigo_receipts_status=receipts_status,
        replacement_orders=orders,
        replacement_orders_status=orders_status,
    )


def _amiigo_receipts(mongo: Any, log: Any) -> Tuple[Any, str]:
    """MongoDB's receipts only when both their indexes exist, as the
    proved numbers (`_verified_sessions`): without the TTL index a receipt,
    which names the rider's phone in its id, would never be removed, and
    without the one-processing index a conversation is not held busy across
    servers. Until a person runs mongo_setup.py and restarts, receipts stay
    in this process's memory (a restart forgets which messages were
    answered), `amiigo_receipts_ttl_missing` is logged at error level, and
    /health says which (`amiigo_receipts`, RECEIPTS_*)."""
    from .conversation import StoreUnavailable

    error = None
    try:
        if mongo.has_indexes():
            return mongo, RECEIPTS_MONGODB
        status, shown = "indexes_missing", RECEIPTS_INDEXES_MISSING
    except StoreUnavailable as exc:
        status, shown, error = "index_unreadable", RECEIPTS_INDEX_UNREADABLE, type(exc).__name__
    _logger.error("amiigo_receipts_ttl_missing: %s", status)
    if log is not None:
        fields = {"error": error} if error else {}
        log.emit("amiigo_receipts_ttl_missing", "amiigo_receipts", level="error", reason=status, **fields)
    return InMemoryAmiigoReceipts(), shown


def _verified_sessions(mongo: Any, log: Any) -> Tuple[Any, str]:
    """MongoDB's saved sessions only when their TTL index exists.

    Only mongo_setup.py makes it, and a deploy never runs that. A collection
    created by the first write has none, and every proved number would stay
    in it for ever, against the twelve hours the code and CLAUDE.md promise.
    So without the index, or when it cannot be checked, proofs stay in this
    process's memory (a restart asks for the number again, as before) and
    `verification_sessions_ttl_missing` is logged at error level for a
    person to run mongo_setup.py and restart. /health says which. The same
    guard as Zoho's unique `source_key` index (zoho/settings.py).
    """
    from .conversation import StoreUnavailable
    from .tools.verification import InMemoryVerifiedSessions

    error = None
    try:
        if mongo.has_ttl_index():
            return mongo, SESSIONS_MONGODB
        status = SESSIONS_TTL_MISSING
    except StoreUnavailable as exc:
        status, error = SESSIONS_INDEX_UNREADABLE, type(exc).__name__
    _logger.error("verification_sessions_ttl_missing: %s", status)
    if log is not None:
        fields = {"error": error} if error else {}
        log.emit("verification_sessions_ttl_missing", "verification_sessions", level="error", reason=status, **fields)
    return InMemoryVerifiedSessions(), status
