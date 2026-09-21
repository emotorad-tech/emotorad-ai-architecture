"""Model access: Claude on AWS Bedrock, plus a scripted stand-in for tests.

The agent loop talks to the small interface in this module rather than to the
Anthropic SDK directly, so the whole conversational flow can be exercised
offline — no AWS credentials, no network, no tokens spent — while production
runs the identical loop against Bedrock.
"""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .config import Settings


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    stop_reason: str
    text: str
    tool_uses: List[ToolUse] = field(default_factory=list)
    # The assistant turn exactly as the API returned it, to append to history.
    # Thinking blocks must be echoed back unchanged, so we never rebuild this.
    api_content: List[Dict[str, Any]] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use"


def response_to_llm(response: Any) -> LLMResponse:
    """The SDK message -> our LLMResponse. One mapping for both clients, so the
    Anthropic API path and the Bedrock path cannot disagree about what a tool
    call looks like."""
    api_content: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    tool_uses: List[ToolUse] = []
    for block in response.content:
        api_content.append(block.model_dump(exclude_none=True))
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_uses.append(ToolUse(id=block.id, name=block.name, arguments=dict(block.input or {})))

    return LLMResponse(
        stop_reason=response.stop_reason or "end_turn",
        text="\n".join(part for part in text_parts if part).strip(),
        tool_uses=tool_uses,
        api_content=api_content,
        usage=response.usage.model_dump() if response.usage else None,
    )


def _create(
    client: Any,
    model: str,
    settings: Settings,
    system: str,
    messages: Sequence[Dict[str, Any]],
    tools: Sequence[Dict[str, Any]],
) -> LLMResponse:
    response = client.messages.create(
        model=model,
        max_tokens=settings.max_tokens,
        system=system,
        messages=list(messages),
        tools=list(tools),
        # Adaptive thinking with a low effort default: battery triage is a
        # bounded problem and the turn is in front of a waiting customer.
        thinking={"type": "adaptive"},
        output_config={"effort": settings.effort},
    )
    return response_to_llm(response)


class BedrockClaude:
    """Claude via Bedrock, in Emotorad's own AWS account and region."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        if client is not None:
            self._client = client
        else:
            from anthropic import AnthropicBedrockMantle  # imported lazily: tests never need it

            self._client = AnthropicBedrockMantle(aws_region=settings.aws_region)

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        return _create(self._client, self.settings.model, self.settings, system, messages, tools)


class AnthropicClaude:
    """Claude via the first-party Anthropic API.

    The deploy default since 2026-09-21 (spec: app-config-store). Same request
    shape as BedrockClaude on purpose, so switching between them is a change of
    EMOTORAD_AI_MODE and nothing else. The key is held in memory for the
    process and never logged.
    """

    def __init__(self, settings: Settings, api_key: str, model: Optional[str] = None, client: Any = None) -> None:
        self.settings = settings
        self.model = model or settings.model
        if client is not None:
            self._client = client
        else:
            import anthropic  # imported lazily: tests never need it

            self._client = anthropic.Anthropic(api_key=api_key)

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        return _create(self._client, self.model, self.settings, system, messages, tools)


MODES = ("offline", "anthropic", "bedrock")


class LLMConfigError(Exception):
    """EMOTORAD_AI_MODE names a path this process cannot serve. Raised at startup."""


def select_llm(mode: str, settings: Settings, environ: Optional[Mapping[str, str]] = None, client: Any = None) -> Any:
    """The model client for EMOTORAD_AI_MODE, or a named error before any request.

    A missing key must fail here, at import of api.py, not on the first customer
    message: the health check then fails the deploy instead of the customer
    finding out.
    """
    env = environ if environ is not None else os.environ
    if mode == "offline":
        return OfflinePlanner()
    if mode == "anthropic":
        key = env.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise LLMConfigError(
                "EMOTORAD_AI_MODE=anthropic but ANTHROPIC_API_KEY is not set "
                "(the config store exports it from API_KEY_CLAUDE)"
            )
        return AnthropicClaude(settings, api_key=key, client=client)
    if mode == "bedrock":
        return BedrockClaude(settings, client=client)
    raise LLMConfigError("unknown EMOTORAD_AI_MODE %r; expected one of %s" % (mode, ", ".join(MODES)))


class ScriptedClaude:
    """Returns queued responses in order. Used by tests and `cli --fake`.

    Records every request so tests can assert on what the agent actually asked
    the model — including asserting it was never called at all, which is how the
    safety hard-stop is verified.
    """

    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._queue = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        self.requests.append({"system": system, "messages": list(messages), "tools": list(tools)})
        if not self._queue:
            raise AssertionError("ScriptedClaude ran out of queued responses")
        return self._queue.pop(0)


def say(text: str) -> LLMResponse:
    """Scripted final answer."""
    return LLMResponse(
        stop_reason="end_turn",
        text=text,
        api_content=[{"type": "text", "text": text}],
    )


def call_tool(name: str, arguments: Dict[str, Any], tool_use_id: str = "toolu_test", text: str = "") -> LLMResponse:
    """Scripted tool call."""
    content: List[Dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({"type": "tool_use", "id": tool_use_id, "name": name, "input": arguments})
    return LLMResponse(
        stop_reason="tool_use",
        text=text,
        tool_uses=[ToolUse(id=tool_use_id, name=name, arguments=arguments)],
        api_content=content,
    )


class OfflinePlanner:
    """A fixed, non-model stand-in that always grounds its answer in the manual.

    It exists so the whole path — CLI or HTTP — can run without AWS or a Bedrock
    approval. It is not a model and makes no attempt to be one: it retrieves once,
    then answers from what it got. This is the deployed default until real Bedrock
    access is wired up (see docs/Emotorad_AWS_Deployment_Plan.md).
    """

    def __init__(self) -> None:
        # Imported lazily to avoid a runtime.py <-> llm.py <-> tools.mocks cycle
        # at module load time (tools.mocks does not import llm, but keeping the
        # import local here keeps this class's only special dependency contained).
        from .tools.mocks import SEARCH_BATTERY_KNOWLEDGE

        self._search_tool = SEARCH_BATTERY_KNOWLEDGE
        self._ids = itertools.count(1)
        self.requests: List[Dict[str, Any]] = []

    def create(self, system: str, messages: Sequence[Dict[str, Any]], tools: Sequence[Dict[str, Any]]) -> LLMResponse:
        self.requests.append({"system": system, "messages": list(messages), "tools": list(tools)})
        last = messages[-1]
        content = last.get("content")

        if isinstance(content, list) and content and content[0].get("type") == "tool_result":
            envelope = json.loads(content[0]["content"])
            passages = envelope.get("data", {}).get("passages", [])
            if not passages:
                return say(
                    "I could not find anything specific on that in the battery manual. "
                    "Could you describe what happens when you plug the charger in?"
                )
            passage = passages[0]
            return say(
                "%s. %s\n\nDoes any of that change what you are seeing?"
                % (passage["title"], " ".join(passage["steps"]))
            )

        query = content if isinstance(content, str) else ""
        return call_tool(
            self._search_tool,
            {"query": query},
            tool_use_id="toolu_offline_%d" % next(self._ids),
        )
