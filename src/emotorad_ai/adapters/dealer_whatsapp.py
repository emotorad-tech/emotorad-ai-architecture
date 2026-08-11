"""Dealer WhatsApp adapter (build plan §3.3, W1–W12).

A **separate WhatsApp line** from the customer one, which is what makes dealer
identity unambiguous: a message arriving here is from a dealer or it is a
mistake, and there is no "is this a customer or a dealer?" problem to solve.

That separation is not cosmetic. Dealers register most warranties under their own
number, so the same phone can appear across dozens of customers' warranty rows.
Resolving dealers through the customer path would hand them every one of those
bikes as though they owned them.
"""

from __future__ import annotations

from typing import Any, Dict

from ..contract import Attachment, InboundMessage, new_conversation_id
from .base import ChannelAdapter


class DealerWhatsAppAdapter(ChannelAdapter):
    """Expects the normalised webhook shape:

        {
          "from": "919000000001",       # dealer's number, digits, no "+"
          "text": "need 5 EMX Plus",
          "conversation_id": "...",     # optional
          "template_reply": "place_order"  # optional: tapped template button
        }
    """

    channel = "dealer_app"

    def to_message(self, event: Dict[str, Any]) -> InboundMessage:
        persona, identity = self.resolver.resolve_dealer((event.get("from") or "").strip())

        entry_metadata: Dict[str, Any] = {}
        if event.get("template_reply"):
            entry_metadata["pill_clicked"] = event["template_reply"]

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
