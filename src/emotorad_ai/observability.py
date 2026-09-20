"""Conversation, tool-call and escalation logging (build plan §3.6).

This exists from the first mocked test, not after launch: it is what the golden
regression set in §5 step 9 is built from, and it is the same plumbing every
future sub-agent on any persona will log through.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# `\b` cannot match before a "+", so the leading sign was left behind and the
# log read "+[phone]". A lookbehind that rejects a digit or a sign also stops
# this matching the tail of a longer digit run, which is what the word
# boundary was there for.
_PHONE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
# 16-digit-ish sequences: card numbers pasted into a support chat.
_LONG_DIGITS = re.compile(r"\b\d{12,19}\b")


# A one-time code or pincode typed on its own. Six digits are far too short
# to pattern match inside a sentence without eating pincodes, prices and model
# numbers, so this is anchored: the whole string, and nothing else. That is how
# the code actually arrives, both as the customer's message and as
# verify_identity's argument.
_OTP_ALONE = re.compile(r"^\s*\d{4,8}\s*$")

# Argument and result fields that are removed by name rather than by pattern,
# because what makes them sensitive is what they are, not what they look like.
# `phone_masked` is deliberately absent: the masked form is what the agent reads
# back to the customer, and checking it was masked correctly is a thing the log
# has to be able to answer.
_SENSITIVE_KEYS = frozenset({"code", "otp", "phone", "mobile", "stated_contact"})

# An inline image, which is how a customer's photo arrives. Never written down:
# it is a picture of somebody's bike, their garage and whoever is standing in
# it, and a megabyte of base64 on one line would make the log unreadable for
# the thing it exists for. The fact that a photo was sent still survives, in
# the surrounding fields.
_DATA_URI = re.compile(r"^data:[^;,]*;base64,", re.I)


def redact_pii(text: str) -> str:
    """Strip the identifiers a customer is most likely to type into free text.

    Ownership data already reaches us through identity resolution, so nothing
    downstream needs these to be readable in the log.
    """
    if _OTP_ALONE.match(text):
        # A one-time code or a pincode, typed alone. Both are hidden; the
        # placeholder says only what it can know. Calling every bare
        # six-digit message a code logged a customer's pincode as [code].
        return "[%d digits]" % len(text.strip())
    text = _EMAIL.sub("[email]", text)
    # Phones before long digit runs. "+919876500000" is twelve digits, so
    # _LONG_DIGITS claimed it first and labelled a mobile number as a card. It
    # was redacted either way, but anyone reading the log was told the wrong
    # thing about what had been there. A card number cannot be caught by _PHONE
    # in passing: its word boundaries cannot land inside a longer digit run.
    text = _PHONE.sub("[phone]", text)
    text = _LONG_DIGITS.sub("[number]", text)
    return text


def redact_fields(value: Any, key: Optional[str] = None, parent: Optional[str] = None) -> Any:
    """Walk a logged structure and redact what should not be written down.

    Applied to whole events rather than to the few call sites that looked risky.
    `redact_pii` was on the inbound text alone, so a number the customer typed
    was redacted on the way in and written out in full a few lines later as a
    tool argument. Redacting at the sink means a field added later is covered by
    default instead of being covered only if someone remembers.

    One exemption, by parent: the `code` of an error envelope is the name of
    the error, not a secret. Redacting it hid every refusal reason in the log
    and left only the message text to read.
    """
    sensitive = key is not None and key.lower() in _SENSITIVE_KEYS and isinstance(value, str)
    if sensitive and not (key.lower() == "code" and parent == "error"):
        return "[redacted]"
    if isinstance(value, str):
        if _DATA_URI.match(value):
            return "[attachment]"
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_fields(v, k, key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_fields(v, key, parent) for v in value]
    return value


@dataclass
class EventLog:
    """Append-only JSONL event log, one line per event.

    Swap the sink for CloudWatch/Firehose in deployment; the event shape is the
    part that matters and should not change.
    """

    path: Optional[str] = None
    to_stdout: bool = False
    events: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, event_type: str, conversation_id: str, **fields: Any) -> Dict[str, Any]:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "conversation_id": conversation_id,
        }
        event.update({k: redact_fields(v, k) for k, v in fields.items()})
        with self._lock:
            self.events.append(event)
            line = json.dumps(event, default=str)
            if self.to_stdout:
                print(line)
            if self.path:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        return event

    # Named helpers keep the event vocabulary consistent across agents.

    def inbound(self, message) -> None:
        self.emit(
            "inbound",
            message.conversation_id,
            persona=message.persona,
            channel=message.channel,
            pill_clicked=message.pill_clicked,
            text=redact_pii(message.message_text),
        )

    def identity_resolved(self, conversation_id: str, persona: str, method: str, **fields: Any) -> None:
        self.emit("identity_resolved", conversation_id, persona=persona, method=method, **fields)

    def routed(self, conversation_id: str, agent: str, reason: str) -> None:
        self.emit("routed", conversation_id, agent=agent, reason=reason)

    def guardrail(self, conversation_id: str, name: str, triggered_by: Any) -> None:
        self.emit("guardrail_triggered", conversation_id, guardrail=name, triggered_by=triggered_by)

    def llm_turn(self, conversation_id: str, agent: str, iteration: int, stop_reason: str, usage: Any = None) -> None:
        self.emit(
            "llm_turn",
            conversation_id,
            agent=agent,
            iteration=iteration,
            stop_reason=stop_reason,
            usage=usage,
        )

    def tool_call(self, conversation_id: str, tool: str, arguments: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.emit(
            "tool_call",
            conversation_id,
            tool=tool,
            arguments=arguments,
            ok="error" not in result,
            result=result,
        )

    def escalation(self, conversation_id: str, reason: str, ticket_id: Optional[str] = None) -> None:
        self.emit("escalation", conversation_id, reason=reason, ticket_id=ticket_id)

    def outcome(self, conversation_id: str, handled_by: str, escalated: bool, ticket_id: Optional[str], text: str) -> None:
        self.emit(
            "outcome",
            conversation_id,
            handled_by=handled_by,
            escalated=escalated,
            ticket_id=ticket_id,
            text=redact_pii(text),
        )
