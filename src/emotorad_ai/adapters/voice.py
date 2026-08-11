"""IVR / voice adapter (build plan §3.3).

Two things make voice different from every other channel, and both are handled
here rather than downstream:

* **Caller ID is asserted, not verified.** It is trivially spoofed. So it
  resolves a person and personalises a greeting, but authorises nothing — the
  `asserted` strength on the identity is what stops the agent stating a name,
  a bike or coverage.
* **There is nowhere to put a file.** A flow that needs an invoice cannot run
  here, which the Late Warranty agent checks before promising an upload.

Speech-to-text is bought in and sits *in front* of this adapter: by the time an
event arrives it is already text, with a confidence score worth keeping — a
low-confidence transcript is a good reason to confirm before acting on it.
"""

from __future__ import annotations

from typing import Any, Dict

from ..contract import InboundMessage, new_conversation_id
from .base import ChannelAdapter

# Below this, treat the transcript as unreliable and confirm before acting.
LOW_CONFIDENCE = 0.6


class VoiceAdapter(ChannelAdapter):
    """Expects whatever the telephony layer emits, normalised to:

        {
          "caller_id": "+919876543210",   # asserted by the telco, spoofable
          "transcript": "my battery is not charging",
          "confidence": 0.92,             # STT confidence, 0-1
          "call_id": "...",               # correlation id for the whole call
          "dtmf": "1"                     # optional keypad selection
        }
    """

    channel = "voice"

    def to_message(self, event: Dict[str, Any]) -> InboundMessage:
        persona, identity = self.resolver.resolve_voice(event.get("caller_id"))

        entry_metadata: Dict[str, Any] = {}
        if event.get("call_id"):
            entry_metadata["call_id"] = event["call_id"]

        confidence = event.get("confidence")
        if isinstance(confidence, (int, float)):
            entry_metadata["stt_confidence"] = confidence
            if confidence < LOW_CONFIDENCE:
                # Surfaced as metadata rather than silently dropped: the agent
                # should confirm what it heard, not guess at a mangled sentence
                # or refuse a customer who simply has a noisy line.
                entry_metadata["low_confidence_transcript"] = True

        # A keypad press is an unambiguous selection — better evidence than the
        # transcript, so it wins where both exist.
        if event.get("dtmf"):
            entry_metadata["pill_clicked"] = event["dtmf"]

        return InboundMessage(
            conversation_id=event.get("call_id") or event.get("conversation_id") or new_conversation_id(),
            persona=persona,
            identity=identity,
            channel=self.channel,
            message_text=(event.get("transcript") or "").strip(),
            entry_metadata=entry_metadata,
        )
