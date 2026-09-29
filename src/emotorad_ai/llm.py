"""Model access: Claude on AWS Bedrock, reply models on OpenRouter, and scripted stand-ins for tests.

The agent loop talks to the small interface in this module rather than to the
Anthropic SDK directly, so the whole conversational flow can be exercised
offline — no AWS credentials, no network, no tokens spent — while production
runs the identical loop against Bedrock.
"""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .config import Settings
from .openrouter import CHAT_PATH, OpenRouterBadResponse

# Bedrock model IDs carry the `anthropic.` prefix; the first-party Anthropic
# API does not. Settings.model (config.py, not changed here) defaults to the
# Bedrock shape, so flipping EMOTORAD_AI_MODE=bedrock alone, with
# EMOTORAD_AI_MODEL pinned to the Anthropic id for the anthropic path, would
# otherwise send the unprefixed id to Bedrock. Each mode gets its own default.
DEFAULT_MODELS: Dict[str, str] = {"anthropic": "claude-opus-5", "bedrock": "anthropic.claude-opus-5"}


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
    # The model id the API reports it answered with; None from the scripted and
    # offline stand-ins, so a trace never prices a fake turn.
    model: Optional[str] = None

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
        model=getattr(response, "model", None),
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


def _direct_model_id(model: str) -> str:
    """Bedrock model id -> the id the Anthropic API answers to.

    `settings.model` is "anthropic.claude-opus-5", which is how Bedrock names
    it. The direct API wants "claude-opus-5" and 404s on the prefixed form, the
    same name the playground has always passed. Stripping it here rather than
    changing the setting keeps one source of truth: Bedrock is still the target
    and its id stays canonical, and this transport adapts to it.
    """
    return model[len("anthropic."):] if model.startswith("anthropic.") else model


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
    shape as BedrockClaude on purpose — both go through `_create` — so switching
    between them is a change of EMOTORAD_AI_MODE and nothing else. The key is
    held in memory for the process and never logged.

    A deviation from the architecture, and a deliberate, temporary one. CLAUDE.md
    says model access goes through Bedrock so LLM traffic stays inside Emotorad's
    AWS boundary, and `BedrockClaude` above is that path; it needs AWS access
    that is not wired up yet. Meanwhile the playground has been calling this
    endpoint directly for weeks, so every prompt we have tuned was tuned against
    this transport. Swap back with EMOTORAD_AI_MODE=bedrock once AWS access
    lands. Nothing else has to change.
    """

    def __init__(
        self,
        settings: Settings,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self.settings = settings
        self.model = _direct_model_id(model or settings.model)
        if client is not None:
            self._client = client
            return
        import anthropic  # imported lazily: tests never need it

        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            # Loudly, at construction. An agent that starts without a key fails
            # on the customer's first message instead, which reads as the bot
            # being broken rather than as the service being misconfigured.
            raise LLMConfigError(
                "ANTHROPIC_API_KEY is not set, and EMOTORAD_AI_MODE=anthropic needs it. "
                "Set it, or run with EMOTORAD_AI_MODE=offline for the fixed planner."
            )
        self._client = anthropic.Anthropic(api_key=key)

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
        model = env.get("EMOTORAD_AI_MODEL") or DEFAULT_MODELS["anthropic"]
        return AnthropicClaude(settings, api_key=key, model=model, client=client)
    if mode == "bedrock":
        model = env.get("EMOTORAD_AI_MODEL") or DEFAULT_MODELS["bedrock"]
        return BedrockClaude(replace(settings, model=model), client=client)
    raise LLMConfigError("unknown EMOTORAD_AI_MODE %r; expected one of %s" % (mode, ", ".join(MODES)))



class OpenRouterChat:
    """A reply model on OpenRouter, behind the same interface as BedrockClaude.

    Everything upstream keeps the Anthropic-shaped history it has always used:
    text, tool_use and tool_result blocks. Translation to OpenRouter's
    OpenAI-shaped messages happens here and only here, so the agent loop, the
    playground and ScriptedClaude do not know which provider answered.
    """

    def __init__(self, model: str, transport: Any, max_tokens: int = 4096, zdr: bool = True) -> None:
        self.model = model
        self._transport = transport
        self.max_tokens = max_tokens
        self.zdr = zdr
        # Prompt caching is an explicit breakpoint on Anthropic models and
        # automatic on the others, so only Anthropic gets the marker.
        self.cache_system = model.startswith("anthropic/")

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, messages, cache_system=self.cache_system),
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        return from_openai_response(self._transport.post(CHAT_PATH, body))


def to_openai_messages(
    system: str, messages: Sequence[Dict[str, Any]], cache_system: bool = False
) -> List[Dict[str, Any]]:
    """Anthropic-shaped history to OpenAI-shaped messages.

    Thinking blocks are dropped: they belong to the model that wrote them and
    another provider cannot verify their signatures.
    """
    if cache_system:
        system_message: Dict[str, Any] = {
            "role": "system",
            "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }
    else:
        system_message = {"role": "system", "content": system}
    out: List[Dict[str, Any]] = [system_message]

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = [b for b in (content or []) if isinstance(b, dict)]
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")]

        if role == "assistant":
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})},
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            entry: Dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
            if calls:
                entry["tool_calls"] = calls
            elif entry["content"] is None:
                entry["content"] = ""
            out.append(entry)
            continue

        # A user turn: tool results become `tool` messages, which must follow
        # the assistant turn that asked for them, and any text comes after.
        for block in blocks:
            if block.get("type") == "tool_result":
                result = block.get("content")
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": result if isinstance(result, str) else json.dumps(result, default=str),
                    }
                )
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def to_openai_tools(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


_FINISH_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def from_openai_response(body: Dict[str, Any]) -> LLMResponse:
    """Any body we cannot read is a typed OpenRouterBadResponse, never a bare
    TypeError: the runtime falls back or hands over on the typed error, and an
    untyped one would crash the turn."""
    try:
        return _from_openai_response(body)
    except OpenRouterBadResponse:
        raise
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as exc:
        raise OpenRouterBadResponse("unreadable OpenRouter response (%s)" % type(exc).__name__) from None


def _from_openai_response(body: Dict[str, Any]) -> LLMResponse:
    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        raise OpenRouterBadResponse("OpenRouter returned no choices")
    choice = choices[0] or {}
    message = choice.get("message") or {}

    content = message.get("content")
    if isinstance(content, list):
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    else:
        text = content or ""
    text = text.strip()

    tool_uses: List[ToolUse] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments")
        try:
            # Some providers send the arguments as an object rather than the
            # JSON string the OpenAI shape specifies. Both mean the same call.
            arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            # The registry answers missing_arguments and the loop carries on,
            # which beats failing the whole turn over one malformed call.
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        tool_uses.append(
            ToolUse(id=call.get("id") or "call_%d" % len(tool_uses), name=function.get("name", ""), arguments=arguments)
        )

    api_content: List[Dict[str, Any]] = []
    if text:
        api_content.append({"type": "text", "text": text})
    for tool_use in tool_uses:
        api_content.append({"type": "tool_use", "id": tool_use.id, "name": tool_use.name, "input": tool_use.arguments})

    # Some providers report "stop" alongside tool calls; the calls are the truth.
    if tool_uses:
        stop_reason = "tool_use"
    else:
        stop_reason = _FINISH_REASONS.get(choice.get("finish_reason") or "stop", "end_turn")

    return LLMResponse(
        stop_reason=stop_reason,
        text=text,
        tool_uses=tool_uses,
        api_content=api_content,
        usage=_openrouter_usage(body.get("usage")),
        # The id OpenRouter says answered, for the trace and the cost report.
        model=body.get("model") if isinstance(body.get("model"), str) else None,
    )


def _openrouter_usage(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    usage: Dict[str, Any] = {
        "input_tokens": int(raw.get("prompt_tokens") or 0),
        "output_tokens": int(raw.get("completion_tokens") or 0),
    }
    if raw.get("cost") is not None:
        usage["cost"] = float(raw["cost"])
    cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached:
        usage["cache_read_input_tokens"] = int(cached)
    return usage


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
