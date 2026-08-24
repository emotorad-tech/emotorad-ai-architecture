"""Model access: Claude on AWS Bedrock, plus a scripted stand-in for tests.

The agent loop talks to the small interface in this module rather than to the
Anthropic SDK directly, so the whole conversational flow can be exercised
offline — no AWS credentials, no network, no tokens spent — while production
runs the identical loop against Bedrock.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

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
        response = self._client.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=system,
            messages=list(messages),
            tools=list(tools),
            # Adaptive thinking with a low effort default: battery triage is a
            # bounded problem and the turn is in front of a waiting customer.
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.effort},
        )

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
