"""Langfuse tracing as a sink behind the event log.

Every event has already been through `observability.redact_fields` by the time it
reaches a sink, so what Langfuse holds is what the JSONL holds: phones, emails,
codes and attachments masked. That is why this is a sink and not an SDK
integration wrapped round the Anthropic client — an auto-instrumented client
would ship the full prompt before redaction runs.

One trace per turn, grouped into a session by conversation id, so the UI shows
cost per turn and cost per conversation without any code of ours. The trace's
user is the identity-graph cluster, never the phone (a phone in a trace is PII
in every Langfuse view).

Off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set
(`langfuse_sink_from_env`). A failure inside the sink is contained by
`EventLog.emit`, so tracing can never cost a customer a reply.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Mapping, Optional

from .video_summary import PROMPT as VIDEO_PROMPT

# Tools that read the knowledge base rather than a system of record. Typed as
# retrievers so Langfuse's retrieval views and the agent graph tell them apart
# from OMS and ticket calls.
RETRIEVER_TOOLS = frozenset({"search_battery_knowledge", "search_motor_knowledge"})

# Anthropic usage keys -> the names Langfuse prices. Anything else the API
# reports (server_tool_use, service_tier) is not a token count and is dropped.
_USAGE_KEYS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
}


def usage_details(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    if not usage:
        return None
    details = {}
    for source, target in _USAGE_KEYS.items():
        value = usage.get(source)
        if isinstance(value, int):
            details[target] = value
    return details or None


class _Turn:
    """The observations open inside one turn."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.agent: Optional[Any] = None
        self.generation: Optional[Any] = None
        self.tool: Optional[Any] = None

    @property
    def parent(self) -> Any:
        return self.agent if self.agent is not None else self.root

    def close(self) -> None:
        for observation in (self.generation, self.tool, self.agent):
            if observation is not None:
                observation.end()
        self.generation = self.tool = self.agent = None


class LangfuseSink:
    """Maps event-log events onto one Langfuse trace per turn.

    `client` is a `langfuse.Langfuse` or anything with its `start_observation`
    and `flush` methods; tests pass a fake.
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        # conversation_id -> the turn in progress.
        self.open_turns: Dict[str, _Turn] = {}
        # conversation_id -> video summaries described at ingest, before the
        # turn's inbound event exists. Attached to the next turn that opens.
        self._held_videos: Dict[str, List[Dict[str, Any]]] = {}

    def __call__(self, event: Dict[str, Any]) -> None:
        handler = getattr(self, "_on_" + event["event"], None)
        if handler is not None:
            handler(event)

    def flush(self) -> None:
        self.client.flush()

    # Trace lifecycle

    def _on_inbound(self, event: Dict[str, Any]) -> None:
        conversation_id = event["conversation_id"]
        previous = self.open_turns.pop(conversation_id, None)
        if previous is not None:
            # A turn that never reached `outcome` (an unhandled exception in the
            # runtime) would otherwise stay open forever and the SDK would never
            # send it. Close it as-is; the new turn starts clean.
            previous.close()
            previous.root.update(level="WARNING", status_message="turn ended without an outcome")
            previous.root.end()
        name = "%s-turn" % (event.get("persona") or "unknown")
        root = self.client.start_observation(name=name, as_type="span", input=event.get("text"))
        tags = [tag for tag in (event.get("persona"), event.get("channel")) if tag]
        root.update_trace(name=name, session_id=conversation_id, input=event.get("text"), tags=tags)
        self.open_turns[conversation_id] = _Turn(root)
        for held in self._held_videos.pop(conversation_id, []):
            self._attach_video(root, held)

    def _on_identity_resolved(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None:
            return
        cluster_id = event.get("cluster_id")
        if cluster_id:
            turn.root.update_trace(user_id=cluster_id)

    def _on_outcome(self, event: Dict[str, Any]) -> None:
        turn = self.open_turns.pop(event["conversation_id"], None)
        if turn is None:
            return
        turn.close()
        text = event.get("text")
        metadata = {
            "handled_by": event.get("handled_by"),
            "escalated": event.get("escalated"),
            "ticket_id": event.get("ticket_id"),
            "duration_ms": event.get("duration_ms"),
        }
        turn.root.update(output=text, metadata=metadata)
        turn.root.update_trace(output=text, metadata=metadata)
        turn.root.end()

    # The video analyser runs at ingest, before the turn opens.

    def _on_video_summary(self, event: Dict[str, Any]) -> None:
        self._held_videos.setdefault(event["conversation_id"], []).append(event)

    def _on_video_summary_failed(self, event: Dict[str, Any]) -> None:
        self._held_videos.setdefault(event["conversation_id"], []).append(event)

    def _attach_video(self, root: Any, event: Dict[str, Any]) -> None:
        clip = {"key": event.get("key"), "mime": event.get("mime"), "size_bytes": event.get("size_bytes")}
        if event["event"] == "video_summary_failed":
            root.start_observation(
                name="video-summary", as_type="generation", input={"clip": clip},
                level="ERROR", status_message=event.get("error"),
            ).end()
            return
        root.start_observation(
            name="video-summary", as_type="generation",
            model=event.get("model"),
            usage_details=usage_details(event.get("usage")),
            # The prompt is fixed in code, so it is the same for every clip; it
            # is on the observation so the trace reads as what Gemini was asked.
            input={"prompt": VIDEO_PROMPT, "clip": clip},
            output=event.get("text"),
            metadata={"duration_ms": event.get("duration_ms"), "chars": event.get("chars")},
        ).end()

    # Inside the turn

    def _on_routed(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None:
            return
        if turn.agent is not None:
            turn.agent.end()
        turn.agent = turn.root.start_observation(
            name=event.get("agent") or "triage", as_type="agent",
            metadata={"reason": event.get("reason")},
        )

    def _on_llm_request(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None:
            return
        if turn.generation is not None:
            turn.generation.end()
        turn.generation = turn.parent.start_observation(
            name=event.get("agent") or "model", as_type="generation",
            metadata={"iteration": event.get("iteration")},
        )

    def _on_llm_turn(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None or turn.generation is None:
            return
        turn.generation.update(
            model=event.get("model"),
            usage_details=usage_details(event.get("usage")),
            metadata={
                "iteration": event.get("iteration"),
                "stop_reason": event.get("stop_reason"),
                "duration_ms": event.get("duration_ms"),
            },
        )
        turn.generation.end()
        turn.generation = None

    def _on_llm_error(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None or turn.generation is None:
            return
        turn.generation.update(level="ERROR", status_message=event.get("error"))
        turn.generation.end()
        turn.generation = None

    def _on_tool_request(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None:
            return
        if turn.tool is not None:
            turn.tool.end()
        name = event.get("tool") or "tool"
        turn.tool = turn.parent.start_observation(
            name=name, as_type="retriever" if name in RETRIEVER_TOOLS else "tool",
        )

    def _on_tool_call(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None or turn.tool is None:
            return
        fields: Dict[str, Any] = {
            "input": event.get("arguments"),
            "output": event.get("result"),
            "metadata": {"duration_ms": event.get("duration_ms")},
        }
        if not event.get("ok", True):
            error = (event.get("result") or {}).get("error") or {}
            fields["level"] = "ERROR"
            fields["status_message"] = error.get("code") if isinstance(error, dict) else str(error)
        turn.tool.update(**fields)
        turn.tool.end()
        turn.tool = None

    def _on_guardrail_triggered(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None:
            return
        turn.parent.start_observation(
            name=event.get("guardrail") or "guardrail", as_type="guardrail",
            input=event.get("triggered_by"),
        ).end()

    def _on_escalation(self, event: Dict[str, Any]) -> None:
        turn = self._turn(event)
        if turn is None:
            return
        turn.parent.start_observation(
            name="escalation", as_type="event",
            metadata={"reason": event.get("reason"), "ticket_id": event.get("ticket_id")},
        ).end()

    def _turn(self, event: Dict[str, Any]) -> Optional[_Turn]:
        return self.open_turns.get(event["conversation_id"])


# EU cloud: nearest region to India, and the one Spain's data can sit in. A
# self-hosted instance later is this one variable.
DEFAULT_HOST = "https://cloud.langfuse.com"


def langfuse_sink_from_env(
    environ: Optional[Mapping[str, str]] = None,
    factory: Optional[Callable[..., Any]] = None,
) -> Optional[LangfuseSink]:
    """The sink for this process, or None when tracing is not configured.

    Both keys are required; one without the other is treated as unset rather
    than as an error, because a local run without tracing is the normal case.
    The keys come from the config store on staging (runbook: tracing.md).
    """
    env = environ if environ is not None else os.environ
    public_key = (env.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret_key = (env.get("LANGFUSE_SECRET_KEY") or "").strip()
    if not (public_key and secret_key):
        return None
    if factory is None:
        from langfuse import Langfuse  # imported lazily: tests and untraced runs never need it

        factory = Langfuse
    client = factory(
        public_key=public_key,
        secret_key=secret_key,
        host=env.get("LANGFUSE_HOST") or DEFAULT_HOST,
        environment=env.get("EMOTORAD_AI_ENV") or None,
    )
    return LangfuseSink(client)
