"""The sub-agent loop: one turn of tool-calling conversation.

Persona-agnostic on purpose. The Battery Support agent is the first thing to run
through it; dealer and internal sub-agents get the same loop with a different
system prompt and a different slice of the tool registry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..attachments import to_image_blocks, validate
from ..config import Settings
from ..conversation import HISTORY_TURNS, trim_history
from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from ..observability import EventLog
from ..tools.registry import ToolContext, ToolRegistry, is_error

HANDOVER_TEXT = (
    "I am not able to get to the bottom of this from here. Let me pass you to someone on our "
    "support team who can help properly."
)

# Tool names whose successful result carries a ticket the customer must be told about.
TICKET_PRODUCING_TOOLS = ("create_support_ticket",)


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
        phone_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.definition = definition
        self.registry = registry
        self.llm = llm
        self.log = log
        self.settings = settings
        # A surface that establishes identity inside the conversation resolves
        # the phone late, because the model proves it and uses it in the same
        # turn. None for every channel that arrives already resolved.
        self.phone_resolver = phone_resolver

    def _late_identity(self, conversation_id: str) -> Dict[str, Callable[[], Any]]:
        """Identity the conversation may prove while this turn is still running.

        Only consulted when the channel resolved nothing, so it can never
        redirect a lookup away from an identity already established upstream.
        """
        if self.phone_resolver is None:
            return {}
        return {"phone": lambda: self.phone_resolver(conversation_id)}

    @staticmethod
    def _user_content(message: InboundMessage) -> Any:
        """The customer's turn: their words, and any photo they sent with them.

        Plain text when there is no photo, so every channel that has never sent
        one is byte-for-byte unchanged. The image goes first because the text
        usually refers to it ("here is the terminal"), and the words go with it
        rather than being dropped — they are often the half that names the
        symptom.
        """
        if not message.attachments:
            return message.message_text
        blocks = to_image_blocks(validate([a.to_dict() for a in message.attachments]))
        if not blocks:
            return message.message_text
        # A photo sent on its own — tap attach, pick, send, no caption — goes as
        # image blocks alone. The API rejects an empty text block.
        if message.message_text.strip():
            blocks.append({"type": "text", "text": message.message_text})
        return blocks

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
            late=self._late_identity(message.conversation_id),
        )

        # Bound the transcript before adding to it. In place, because this is
        # the conversation's own history list and the store hands out the same
        # object every turn.
        history[:] = trim_history(history, HISTORY_TURNS - 1)
        history.append({"role": "user", "content": self._user_content(message)})

        turn = AgentTurn(text="", agent=self.definition.name)
        # Everything the model writes during the turn, in the order it wrote it.
        #
        # A model narrates before it acts: "Before anything else, check the
        # battery's on/off switch is ON" arrives in the same block as the
        # send_guide_media call that shows the switch. That text was appended to
        # the history the model sees and then dropped, because turn.text was
        # assigned from the final tool-free response alone. The customer got the
        # last sentence of a paragraph they never received — on 20 September, a
        # photo of a switch and the words "The picture should be just above this
        # message."
        said: List[str] = []
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
            if (response.text or "").strip():
                said.append(response.text.strip())

            if not response.wants_tools:
                # A turn that ends with nothing written is a failed turn, not a
                # reply. It happens: the model calls its tools, gets the results,
                # and then ends the turn having said nothing — stop_reason
                # `end_turn`, no text block, nothing truncated. Passing that
                # through sent the customer an empty message, or on a channel
                # that trims it, the AI disclosure on its own.
                #
                # Treated like the other two ways this loop fails to produce an
                # answer — a stuck repeat above, an exhausted budget below — and
                # logged, because how often it happens is what decides whether
                # this is worth recovering from (one more round trip nudging the
                # model to reply) rather than handing over.
                # Nothing written anywhere in the turn, not merely nothing in
                # the last block. A turn that explained itself and then ended
                # without a closing line has still told the customer something,
                # and handing them to a human would throw that away.
                if not said:
                    self.log.emit(
                        "empty_reply", message.conversation_id,
                        agent=self.definition.name,
                        iteration=iteration,
                        stop_reason=response.stop_reason,
                        tools_called=len(turn.tool_calls),
                    )
                    turn.escalate = True
                    turn.text = HANDOVER_TEXT
                    return turn
                turn.text = "\n\n".join(said)
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

                # Media goes out because the model asked for it, never because a
                # search happened to return a record carrying some. Retrieved media
                # used to be attached automatically here, which was the only way to
                # send a picture before `send_guide_media` existed (2026-08-06 vs
                # 2026-09-08). It infers from retrieval rather than from what the
                # reply is about, so it re-sent photos the customer already had and
                # attached a runner-up passage's media for a diagnosis that was
                # never given. `send_guide_media` has none of that: the model picks
                # a catalogue key from an enum, code resolves the URL, and the tool
                # refuses a duplicate within a conversation.
                if not is_error(envelope):
                    for item in (envelope.get("data") or {}).get("media", []) or []:
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
