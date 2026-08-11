"""Emotorad agentic AI platform — the shared skeleton, proven on one use case.

Built in the order set out in `docs/Emotorad_Platform_Build_Plan.md` §5:
message contract and tool interfaces first, every tool mocked, then the agent
loop. Nothing here talks to a real OMS, ERP or ticketing system yet.
"""

from .contract import Identity, InboundMessage, Reply, new_conversation_id
from .runtime import Runtime

__all__ = ["Identity", "InboundMessage", "Reply", "Runtime", "new_conversation_id"]
__version__ = "0.1.0"
