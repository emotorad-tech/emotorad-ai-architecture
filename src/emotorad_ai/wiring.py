"""Which models a process talks to, chosen once from Settings.mode.

One function so the CLI and the API cannot drift apart on what a mode means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import Settings
from .jev import JevClient
from .llm import BedrockClaude, OfflinePlanner, OpenRouterChat
from .openrouter import OpenRouterTransport


@dataclass
class Models:
    llm: Any
    narrow_llm: Any = None
    jev: Any = None


def build_models(settings: Settings, transport: Optional[Any] = None) -> Models:
    if settings.mode == "offline":
        return Models(llm=OfflinePlanner())
    if settings.mode == "bedrock":
        return Models(llm=BedrockClaude(settings))
    # openrouter: one transport, one key, three models.
    transport = transport or OpenRouterTransport(
        base_url=settings.openrouter_base_url, timeout=settings.openrouter_timeout
    )
    return Models(
        llm=OpenRouterChat(settings.fallback_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        narrow_llm=OpenRouterChat(settings.narrow_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        jev=JevClient(transport, model=settings.jev_model, timeout=settings.jev_timeout),
    )
