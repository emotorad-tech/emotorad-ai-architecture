"""Channel adapter base (build plan §3.3).

An adapter's only job: take whatever a channel sends, resolve persona and
identity deterministically, and emit the §3.1 contract. No business logic, no
model calls. WhatsApp, voice, dealer-app and internal-portal adapters are the
same pattern applied later — they plug into this interface without changing
anything downstream.
"""

from __future__ import annotations

from typing import Any, Dict

from ..contract import InboundMessage
from ..identity import IdentityResolver


class ChannelAdapter:
    channel = "unset"

    def __init__(self, resolver: IdentityResolver) -> None:
        self.resolver = resolver

    def to_message(self, event: Dict[str, Any]) -> InboundMessage:
        raise NotImplementedError
