"""WhatsApp adapter (build plan §3.3).

WhatsApp hands us the sender's phone number natively, so identity is verified
without an OTP — the platform already proved it. That makes this the cheapest
high-quality identity we get anywhere.

The `ref:` code is the other half. A visitor taps "chat on WhatsApp" on the
website, and the prefilled message carries a short code mapped to their `em_aid`.
Parsing it here turns an anonymous browser into a verified phone number *with the
browsing history already attached* — the single highest-value stitch in the
identity design, and the reason this adapter looks at message text at all.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

from ..contract import Attachment, InboundMessage, new_conversation_id
from .base import ChannelAdapter

# "[ref:7KQ2M9]" — a short code, not the raw UUID, because a UUID in a
# customer-visible message looks broken and gets deleted before sending.
REF_PATTERN = re.compile(r"\[?ref[:=]\s*([A-Za-z0-9]{4,12})\]?", re.IGNORECASE)


def extract_ref(text: str) -> Optional[str]:
    match = REF_PATTERN.search(text or "")
    return match.group(1).upper() if match else None


def strip_ref(text: str) -> str:
    """Remove the code before the text reaches the model.

    It is plumbing, not something the customer said, and leaving it in invites
    the model to comment on it or echo it back.
    """
    return REF_PATTERN.sub("", text or "").strip()


class WhatsAppAdapter(ChannelAdapter):
    """Expects the normalised webhook shape:

        {
          "from": "919876543210",        # sender, digits, no "+"
          "text": "Hi, my battery ...",
          "conversation_id": "...",      # optional
          "attachments": [{"kind": "image", "url": "..."}],
          "template_reply": "battery_issue"   # optional: which template button
        }
    """

    channel = "whatsapp"

    def __init__(self, resolver, ref_lookup: Optional[Callable[[str], Optional[str]]] = None) -> None:
        super().__init__(resolver)
        # code -> em_aid, with a TTL. Provided by the Nest identity service; a
        # miss is normal (codes expire) and must degrade to phone-only, never fail.
        self.ref_lookup = ref_lookup or (lambda code: None)

    def to_message(self, event: Dict[str, Any]) -> InboundMessage:
        sender = (event.get("from") or "").strip()
        raw_text = event.get("text") or ""

        em_aid = None
        code = extract_ref(raw_text)
        if code:
            em_aid = self.ref_lookup(code)

        persona, identity = self.resolver.resolve_whatsapp(sender, ref_code_em_aid=em_aid)

        entry_metadata: Dict[str, Any] = {}
        if event.get("template_reply"):
            # A tapped template button is the intent, already stated — no
            # classification needed.
            entry_metadata["pill_clicked"] = event["template_reply"]
        if code:
            entry_metadata["ref_code"] = code
            entry_metadata["ref_resolved"] = em_aid is not None

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
            message_text=strip_ref(raw_text),
            entry_metadata=entry_metadata,
            attachments=attachments,
        )
