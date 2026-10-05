"""Which models a process talks to, chosen once from Settings.mode.

One function so the CLI and the API cannot drift apart on what a mode means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import Settings
from .jev import JevClient
from .llm import SINGLE_MODEL_MODES
from .llm import OpenRouterChat, select_llm
from .openrouter import OpenRouterTransport


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
    # openrouter: one transport, one key, three models.
    transport = transport or OpenRouterTransport(
        base_url=settings.openrouter_base_url, timeout=settings.openrouter_timeout
    )
    return Models(
        llm=OpenRouterChat(settings.fallback_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        narrow_llm=OpenRouterChat(settings.narrow_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        jev=JevClient(transport, model=settings.jev_model, timeout=settings.jev_timeout),
    )


@dataclass
class Stores:
    conversations: Any
    idempotency: Any
    # The ticket record (tickets/store.py, stores/mongo.py). Written only when
    # Zoho Desk is on; the mock ticket system never touches it.
    tickets: Any = None


def build_stores(settings: Settings, log: Any = None, client: Any = None) -> Stores:
    """Where conversations and write receipts live, chosen from Settings.store.

    `mongodb` reads the connection string from EMOTORAD_MONGO_URI (never from
    Settings) and fails loudly without one, rather than quietly falling back to
    memory and losing every conversation on the next restart.
    """
    if settings.store == "memory":
        from .conversation import InMemoryConversationStore
        from .tools.registry import IdempotencyStore

        from .tickets.store import InMemoryTicketStore

        return Stores(conversations=InMemoryConversationStore(), idempotency=IdempotencyStore(),
                      tickets=InMemoryTicketStore())
    from .stores.mongo import MongoConversationStore, MongoIdempotencyStore, MongoTicketStore, connect

    db = connect(db_name=settings.mongo_db, client=client)
    return Stores(
        conversations=MongoConversationStore(db, state_ttl_hours=settings.state_ttl_hours, log=log),
        idempotency=MongoIdempotencyStore(db, ttl_days=settings.idempotency_ttl_days),
        tickets=MongoTicketStore(db),
    )
