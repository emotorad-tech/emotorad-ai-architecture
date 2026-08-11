"""Model access: Claude on AWS Bedrock, plus a scripted stand-in for tests.

The agent loop talks to the small interface in this module rather than to the
Anthropic SDK directly, so the whole conversational flow can be exercised
offline — no AWS credentials, no network, no tokens spent — while production
runs the identical loop against Bedrock.
"""

from __future__ import annotations

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
