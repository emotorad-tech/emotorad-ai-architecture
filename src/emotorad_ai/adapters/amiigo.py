"""Amiigo app adapter (build plan §3.3).

The app authenticates on phone number, so a logged-in session is a verified
identity — the same strength WhatsApp gives us, by a different route. That makes
this and WhatsApp the two channels where the agent may speak personally without
any extra step.
"""

from __future__ import annotations

from typing import Any, Dict

from ..contract import Attachment, InboundMessage, new_conversation_id
from .base import ChannelAdapter


class AmiigoAdapter(ChannelAdapter):
    """Expects:

        {
          "session_token": "...",       # app session; maps to a verified phone
          "text": "battery not charging",
          "conversation_id": "...",     # optional
          "screen": "battery_health",   # optional: where in the app they tapped from
          "attachments": [{"kind": "image", "url": "..."}]
        }
    """

    channel = "amiigo_app"

    def to_message(self, event: Dict[str, Any]) -> InboundMessage:
        # No cookie in a native app — identity comes from the session alone.
        persona, identity = self.resolver.resolve_website(
            em_aid=None, session_token=event.get("session_token")
        )

        entry_metadata: Dict[str, Any] = {}
        if event.get("pill"):
            entry_metadata["pill_clicked"] = event["pill"]
        if event.get("screen"):
            # Where they tapped from is intent evidence the web widget does not
            # have: someone opening chat from the battery-health screen is very
            # probably asking about the battery.
            entry_metadata["screen"] = event["screen"]

        attachments = [
            Attachment(kind=a.get("kind", "image"), url=a["url"], mime_type=a.get("mime_type"))
            for a in event.get("attachments", [])
            if a.get("url")
        ]

        return InboundMessage(
            conversation_id=event.get("conversation_id") or new_conversation_id(),
            persona=persona,
            identity=identity,
            channel=self.channel,
            message_text=(event.get("text") or "").strip(),
            entry_metadata=entry_metadata,
            attachments=attachments,
        )
