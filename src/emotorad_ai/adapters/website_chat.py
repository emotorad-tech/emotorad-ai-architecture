"""Website chat widget adapter — the only adapter use case #1 needs.

Assumes the widget posts events shaped like:

    {
      "conversation_id": "…",        # optional; minted on the first turn
      "em_aid": "…",                 # the first-party cookie; present for every
                                     # visitor, logged in or not
      "session_token": "…",          # the logged-in website session, if any
      "text": "my battery won't charge",
      "pill": "battery_issue",       # optional: the pill the visitor tapped
      "referrer": "https://…",       # optional
      "attachments": [{"kind": "image", "url": "…"}]
    }

Confirm this against the real widget's payload before wiring it up — and confirm
whether the Amiigo app should actually be the first surface instead (build plan
§8, still open).
"""

from __future__ import annotations

from typing import Any, Dict

from ..contract import Attachment, InboundMessage, new_conversation_id
from .base import ChannelAdapter


class WebsiteChatAdapter(ChannelAdapter):
    channel = "website_chat"

    def to_message(self, event: Dict[str, Any]) -> InboundMessage:
        persona, identity = self.resolver.resolve_website(
            event.get("em_aid"), event.get("session_token")
        )

        entry_metadata: Dict[str, Any] = {}
        # Metadata over inference: a tapped pill *is* the intent, so nothing
        # downstream needs to classify it.
        if event.get("pill"):
            entry_metadata["pill_clicked"] = event["pill"]
        if event.get("referrer"):
            entry_metadata["referrer"] = event["referrer"]

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
