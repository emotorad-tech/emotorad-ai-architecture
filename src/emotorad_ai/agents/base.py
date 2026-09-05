"""The sub-agent loop: one turn of tool-calling conversation.

Persona-agnostic on purpose. The Battery Support agent is the first thing to run
through it; dealer and internal sub-agents get the same loop with a different
system prompt and a different slice of the tool registry.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ..config import Settings
from ..contract import Attachment, InboundMessage
from ..identity import ResolvedIdentity
from ..observability import EventLog
from ..tools.registry import ToolContext, ToolRegistry, is_error

HANDOVER_TEXT = (
    "I am not able to get to the bottom of this from here. Let me pass you to someone on our "
    "support team who can help properly."
)

# Tool names whose successful result carries a ticket the customer must be told about.
TICKET_PRODUCING_TOOLS = ("create_support_ticket",)


def user_content(message: InboundMessage) -> Union[str, List[Dict[str, Any]]]:
    """What this turn contributes to the model history.

    A plain string when there are no attachments — unchanged from before, so
    text-only conversations keep their exact history shape. Otherwise a list of
    blocks with the text last, so the model reads the picture before the
    question about it.
    """
    blocks: List[Dict[str, Any]] = []
    for attachment in message.attachments:
        block = _attachment_block(attachment)
        if block is not None:
            blocks.append(block)
    if not blocks:
        return message.message_text
    # The API rejects an empty text block.
    blocks.append({"type": "text", "text": message.message_text or "(attachment)"})
    return blocks


def _attachment_block(attachment: Attachment) -> Optional[Dict[str, Any]]:
    mime_type = attachment.mime_type or ""
    is_data_url = attachment.url.startswith("data:")
    if is_data_url:
        header, _, data = attachment.url.partition(",")
        mime_type = mime_type or header[len("data:"):].split(";")[0]
        source: Dict[str, Any] = {"type": "base64", "media_type": mime_type, "data": data}
    else:
        source = {"type": "url", "url": attachment.url}
        # Every adapter builds its default payload with mime_type=None, so an
        # ordinary http image or document link would otherwise vanish from the
        # model's history for no reason but a missing header. Guess from the
        # URL before giving up on it.
        mime_type = mime_type or mimetypes.guess_type(attachment.url)[0] or ""

    if mime_type.startswith("image/"):
        return {"type": "image", "source": source}
    if mime_type == "application/pdf":
        return {"type": "document", "source": source}
    if not mime_type and not is_data_url:
        # Still nothing to go on — trust what the adapter said this attachment
        # is, rather than dropping a photo it never had a chance to describe.
        if attachment.kind == "image":
            return {"type": "image", "source": source}
        if attachment.kind == "document":
            return {"type": "document", "source": source}
    # A known-but-unsupported mime type (e.g. text/csv) is dropped — sending it
    # beats nothing, but a 400 from the API beats sending it.
    return None


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    tool_names: Sequence[str]
    build_system_prompt: Callable[..., str]


@dataclass
class AgentTurn:
    text: str
    agent: str
    ticket_id: Optional[str] = None
    escalate: bool = False
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    # Media from knowledge records the turn retrieved. Collected here rather than
    # left to the model, which cannot see images and would have to be trusted to
    # copy a URL correctly.
    attachments: List[Dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        definition: AgentDefinition,
        registry: ToolRegistry,
        llm: Any,
        log: EventLog,
        settings: Settings,
    ) -> None:
        self.definition = definition
        self.registry = registry
        self.llm = llm
        self.log = log
        self.settings = settings

    def run(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        history: List[Dict[str, Any]],
        context: str = "",
    ) -> AgentTurn:
        system = self.definition.build_system_prompt(message, resolved, context)
        tools = self.registry.schemas_for(
            [name for name in self.definition.tool_names if name in self.registry.specs]
        )
        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
        )

        history.append({"role": "user", "content": user_content(message)})

        turn = AgentTurn(text="", agent=self.definition.name)
        # Same tool, same arguments, twice: the model is stuck, and the remaining
        # iterations will burn tokens and latency to arrive at the same place.
        # Breaking early and handing over is cheaper and more honest than looping
        # to the cap and then apologising.
        seen_calls: set = set()

        for iteration in range(1, self.settings.max_agent_iterations + 1):
            turn.iterations = iteration
            response = self.llm.create(system=system, messages=history, tools=tools)
            self.log.llm_turn(
                message.conversation_id,
                self.definition.name,
                iteration,
                response.stop_reason,
                response.usage,
            )
            history.append({"role": "assistant", "content": response.api_content})

            if not response.wants_tools:
                turn.text = response.text
                return turn

            # All results for one assistant turn go back in a single user
            # message — splitting them teaches the model to stop batching calls.
            results: List[Dict[str, Any]] = []
            for tool_use in response.tool_uses:
                arguments = self._with_idempotency_key(tool_use, message.conversation_id, iteration)

                signature = (tool_use.name, json.dumps(arguments, sort_keys=True, default=str))
                if signature in seen_calls:
                    self.log.emit(
                        "stuck_agent", message.conversation_id,
                        agent=self.definition.name, tool=tool_use.name, iteration=iteration,
                    )
                    turn.escalate = True
                    turn.text = HANDOVER_TEXT
                    return turn
                seen_calls.add(signature)
                envelope = self.registry.call(tool_use.name, arguments, context)
                self.log.tool_call(message.conversation_id, tool_use.name, arguments, envelope)
                turn.tool_calls.append({"tool": tool_use.name, "arguments": arguments, "result": envelope})

                if tool_use.name in TICKET_PRODUCING_TOOLS and not is_error(envelope):
                    turn.ticket_id = envelope["data"].get("ticket_id", turn.ticket_id)

                if not is_error(envelope):
                    for passage in (envelope.get("data") or {}).get("passages", []) or []:
                        for item in passage.get("media", []) or []:
                            if item not in turn.attachments:
                                turn.attachments.append(item)

                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(envelope, default=str),
                        "is_error": is_error(envelope),
                    }
                )
            history.append({"role": "user", "content": results})

        # Ran out of tool-calling round trips without an answer. Hand over rather
        # than let the loop run on a customer's time.
        turn.text = (
            "I am having trouble getting to the bottom of this from here. Let me pass you to "
            "a member of our support team who can help."
        )
        turn.escalate = True
        return turn

    def _with_idempotency_key(self, tool_use: Any, conversation_id: str, iteration: int) -> Dict[str, Any]:
        """Backstop the model on write tools.

        The schema asks for an idempotency key and the registry refuses writes
        without one, but a missing key should not surface to the customer as a
        failed ticket — derive a stable one from the call itself instead.
        """
        arguments = dict(tool_use.arguments)
        spec = self.registry.specs.get(tool_use.name)
        if spec is None or not spec.write or arguments.get("idempotency_key"):
            return arguments

        payload = json.dumps(
            {k: v for k, v in sorted(arguments.items()) if k != "idempotency_key"},
            default=str,
        )
        digest = hashlib.sha256(("%s|%s|%s" % (conversation_id, tool_use.name, payload)).encode()).hexdigest()
        arguments["idempotency_key"] = digest[:32]
        return arguments
