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

_PHONE = re.compile(r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
# 16-digit-ish sequences: card numbers pasted into a support chat.
_LONG_DIGITS = re.compile(r"\b\d{12,19}\b")


def redact_pii(text: str) -> str:
    """Strip the identifiers a customer is most likely to type into free text.

    Ownership data already reaches us through identity resolution, so nothing
    downstream needs these to be readable in the log.
    """
    text = _EMAIL.sub("[email]", text)
    text = _LONG_DIGITS.sub("[number]", text)
    text = _PHONE.sub("[phone]", text)
    return text


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
        event.update(fields)
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

    def jev_decision(self, conversation_id: str, route: Any, decision: Any = None, error: Optional[str] = None) -> None:
        """The path Jev's scores chose, and why. Scores only: the state text is
        never logged here; `inbound` already holds the redacted message."""
        self.emit(
            "jev_decision",
            conversation_id,
            path=route.path,
            reasons=list(route.reasons),
            scores=dict(route.scores),
            sub_category=route.sub_category,
            standard_response=route.standard_response_id,
            prefetch=[call.tool for call in route.prefetch],
            error=error,
            model=getattr(decision, "model", None),
            cost=getattr(decision, "cost", None),
            latency_ms=getattr(decision, "latency_ms", None),
        )

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
