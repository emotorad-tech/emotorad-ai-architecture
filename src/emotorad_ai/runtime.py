"""The skeleton, wired end to end.

    channel adapter -> message contract -> identity resolution -> enrichment
    -> guardrails -> triage -> sub-agent -> tools -> post-checks

`handle` is the single entry point a channel adapter calls, and the *order* of
the steps is the design:

* identity and enrichment run before the prompt is built, so the agent never has
  to ask a customer what they own;
* both pre-guardrails run before the model is called at all, so no prompt change
  can route around them;
* the coverage post-check runs after the model, because that is the only place a
  wrong claim can be caught — the tool call succeeding proves nothing about what
  the reply then said;
* the AI disclosure is applied to whatever text finally leaves, on every path
  including the guardrail short-circuits, because a legal obligation must not
  depend on which branch a conversation took.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .agents.base import Agent, AgentDefinition
from .agents.battery_support import AGENT_NAME as BATTERY_SUPPORT
from .agents.battery_support import DEFINITION as BATTERY_SUPPORT_DEFINITION
from .agents.dealer_orders import AGENT_NAME as DEALER_ORDERS
from .agents.dealer_orders import DEFINITION as DEALER_ORDERS_DEFINITION
from .agents.late_warranty import AGENT_NAME as LATE_WARRANTY
from .agents.late_warranty import DEFINITION as LATE_WARRANTY_DEFINITION
from .agents.motor_support import AGENT_NAME as MOTOR_SUPPORT
from .agents.motor_support import DEFINITION as MOTOR_SUPPORT_DEFINITION
from .config import Settings, load_settings
from .contract import Attachment, InboundMessage, Reply
from .conversation import ConversationState, ConversationStore
from .disclosure import apply_disclosure
from .enrichment import ContextEnricher
from .guardrails import (
    COVERAGE_BLOCKED_MESSAGE,
    HANDOFF_MESSAGE,
    SAFETY_MESSAGE,
    check_safety,
    check_coverage_claim,
    check_human_handoff,
)
from .identity import IdentityResolver, ResolvedIdentity
from .llm import BedrockClaude
from .observability import EventLog
from .tools.mocks import CREATE_SUPPORT_TICKET, build_registry
from .tools.registry import ToolContext, ToolRegistry, is_error
from .triage import TriageAgent

UNSUPPORTED_MESSAGE = (
    "I am not able to help with this from here. Let me pass you to a member of our support "
    "team who can."
)

# Topic -> sub-agent, **scoped per persona**. Never one router over everything:
# a dealer and a customer asking the same words mean different things, and a
# shared agent set is how a dealer reaches a customer-only tool.
TOPIC_AGENTS = {"battery": BATTERY_SUPPORT, "motor": MOTOR_SUPPORT}
DEALER_AGENTS = {"order": DEALER_ORDERS}


class Runtime:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[ToolRegistry] = None,
        llm: Any = None,
        log: Optional[EventLog] = None,
        resolver: Optional[IdentityResolver] = None,
        diagnostics_available: bool = False,
    ) -> None:
        self.settings = settings or load_settings()
        self.registry = registry or build_registry(diagnostics_available=diagnostics_available)
        self.log = log or EventLog(path=self.settings.log_path, to_stdout=self.settings.log_to_stdout)
        self.llm = llm if llm is not None else BedrockClaude(self.settings)
        self.resolver = resolver or IdentityResolver(self.registry)
        self.conversations = ConversationStore()
        self.enricher = ContextEnricher()
        self.triage = TriageAgent(TOPIC_AGENTS)

        definitions: Dict[str, AgentDefinition] = {
            BATTERY_SUPPORT: BATTERY_SUPPORT_DEFINITION,
            MOTOR_SUPPORT: MOTOR_SUPPORT_DEFINITION,
            LATE_WARRANTY: LATE_WARRANTY_DEFINITION,
            DEALER_ORDERS: DEALER_ORDERS_DEFINITION,
        }
        self.agents = {
            name: Agent(definition, self.registry, self.llm, self.log, self.settings)
            for name, definition in definitions.items()
        }

    # -- entry point ---------------------------------------------------------

    def handle(self, message: InboundMessage) -> Reply:
        self.log.inbound(message)
        state = self.conversations.get(message.conversation_id)
        state.turns += 1
        history = state.history

        resolved = self.resolver.hydrate(message)
        self.log.identity_resolved(
            message.conversation_id,
            resolved.persona,
            resolved.method,
            # The audit trail for disclosure: when a customer asks why the bot
            # knew their name, this records who we thought they were and what
            # proved it.
            cluster_id=resolved.cluster_id,
            strength=resolved.identity.strength,
            error=resolved.error,
        )

        # Built once per conversation and cached on the state. Rebuilding it every
        # turn costs queries, moves the block in the prompt (defeating prefix
        # caching), and cannot change the answer — a customer's bikes and history
        # do not move mid-chat.
        if state.context_block is None:
            context = self.enricher.build(resolved)
            state.context_block = context.render()
            self.log.emit(
                "context_built", message.conversation_id,
                kept=list(context.sections), dropped=context.dropped, tokens=context.tokens,
            )

        # 1. Safety. A keyword gate ahead of the agent turn, not something the
        #    model has to notice. Allowed to over-trigger.
        safety = check_safety(message.message_text)
        if safety.triggered:
            return self._handle_safety(message, resolved, state, safety.matched)

        # 2. Human handoff, reachable at any point, no friction.
        handoff = check_human_handoff(message.message_text)
        if handoff.triggered:
            self.log.guardrail(message.conversation_id, "human_handoff", handoff.matched)
            self.log.escalation(message.conversation_id, "customer_requested_human", None)
            return self._finish(
                message, state, HANDOFF_MESSAGE, "guardrail:human_handoff",
                escalated=True, metadata={"matched": handoff.matched},
            )

        if resolved.persona == "dealer":
            # Dealers bypass customer triage entirely. There is no bike to
            # disambiguate and no customer record to enrich from — and routing
            # them through the customer path is precisely how a dealer would end
            # up holding someone else's warranty data.
            state.route_to(DEALER_ORDERS)
            self.log.routed(message.conversation_id, DEALER_ORDERS, "persona:dealer")
            return self._run_agent(DEALER_ORDERS, message, resolved, state)

        if resolved.persona != "customer":
            return self._finish(
                message, state, UNSUPPORTED_MESSAGE, "router",
                escalated=True, metadata={"persona": resolved.persona},
            )

        # 3. A customer with no bike on record goes straight to registration —
        #    triage has nothing to disambiguate and no issue it can act on.
        if resolved.method in ("no_warranty_record",) and LATE_WARRANTY in self.agents:
            state.route_to(LATE_WARRANTY)
            return self._run_agent(LATE_WARRANTY, message, resolved, state)

        # 4. Triage: which bike, what issue, which agent.
        if state.agent is None:
            outcome = self.triage.handle(message, resolved, state)
            self.log.routed(message.conversation_id, outcome.agent or "triage", outcome.reason)
            # Every classification, with the raw text that produced it. This is the
            # labelled set that makes a semantic router worth building later, and
            # it is worthless if collection starts late — so it starts now.
            self.log.emit(
                "classification", message.conversation_id,
                text=message.message_text, phase=state.phase,
                topic=state.pending_topic, agent=outcome.agent, reason=outcome.reason,
            )
            if not outcome.is_handoff:
                return self._finish(
                    message, state, outcome.reply or UNSUPPORTED_MESSAGE, "triage",
                    metadata=dict(outcome.metadata, reason=outcome.reason),
                )

        return self._run_agent(state.agent or BATTERY_SUPPORT, message, resolved, state)

    # -- steps ---------------------------------------------------------------

    def _run_agent(
        self,
        agent_name: str,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
    ) -> Reply:
        turn = self.agents[agent_name].run(
            message, resolved, state.history, state.context_block or ""
        )
        if turn.escalate:
            self.log.escalation(message.conversation_id, "agent_requested_handover", turn.ticket_id)

        # The post-check: calling the warranty tool proved the tool ran, not that
        # the reply matches what it returned.
        results = [call["result"] for call in turn.tool_calls]
        coverage = check_coverage_claim(turn.text, results)
        if coverage.blocked:
            self.log.guardrail(
                message.conversation_id, "coverage_post_check",
                {"reason": coverage.reason, "claimed": coverage.claimed, "actual": coverage.actual},
            )
            self.log.escalation(message.conversation_id, "coverage_claim_blocked", turn.ticket_id)
            return self._finish(
                message, state, COVERAGE_BLOCKED_MESSAGE, "guardrail:coverage_post_check",
                escalated=True, ticket_id=turn.ticket_id,
                metadata={"blocked_reason": coverage.reason, "suppressed_text": turn.text},
                already_in_history=True,
            )

        self.log.outcome(message.conversation_id, turn.agent, turn.escalate, turn.ticket_id, turn.text)
        return Reply(
            conversation_id=message.conversation_id,
            text=self._outbound(turn.text, state, message.channel),
            handled_by=turn.agent,
            escalated=turn.escalate,
            ticket_id=turn.ticket_id,
            attachments=[
                Attachment(kind="image", url=item["url"], mime_type=item.get("mime_type"))
                for item in turn.attachments
                if item.get("url")
            ],
            metadata={"tool_calls": [c["tool"] for c in turn.tool_calls], "iterations": turn.iterations},
        )

    def _handle_safety(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        matched: List[str],
    ) -> Reply:
        self.log.guardrail(message.conversation_id, "battery_safety", matched)

        ticket_id: Optional[str] = None
        if resolved.identity.phone:
            # Deterministic: code decides this ticket exists, not the model.
            arguments: Dict[str, Any] = {
                "category": "battery_safety",
                "severity": "critical",
                "description": (
                    "Automatic safety escalation. Customer reported: %s. Matched safety "
                    "indicators: %s. No troubleshooting was offered."
                    % (message.message_text, ", ".join(matched))
                ),
                "idempotency_key": "safety:%s" % message.conversation_id,
            }
            # With several bikes the ticket needs one named, and triage may not
            # have run yet — the safety branch fires before it.
            if state.selected_frame:
                arguments["frame_number"] = state.selected_frame
            elif resolved.single_bike:
                arguments["frame_number"] = resolved.single_bike["frame_number"]

            envelope = self.registry.call(
                CREATE_SUPPORT_TICKET,
                arguments,
                ToolContext(
                    conversation_id=message.conversation_id,
                    phone=resolved.identity.phone,
                    cluster_id=resolved.cluster_id,
                ),
            )
            self.log.tool_call(
                message.conversation_id, CREATE_SUPPORT_TICKET, {"category": "battery_safety"}, envelope
            )
            if not is_error(envelope):
                ticket_id = envelope["data"]["ticket_id"]

        text = SAFETY_MESSAGE
        if ticket_id:
            text += "\n\nI have raised this as a priority safety case, reference %s." % ticket_id

        self.log.escalation(message.conversation_id, "battery_safety", ticket_id)
        return self._finish(
            message, state, text, "guardrail:battery_safety",
            escalated=True, ticket_id=ticket_id, metadata={"matched": matched},
        )

    # -- outbound ------------------------------------------------------------

    def _outbound(self, text: str, state: ConversationState, channel: str) -> str:
        """Everything the customer ever sees passes through here.

        One choke point so the disclosure cannot be missed on a branch someone
        adds later — including guardrail short-circuits, which are exactly the
        replies a customer is most likely to receive first.
        """
        return apply_disclosure(text, state, channel)

    def _finish(
        self,
        message: InboundMessage,
        state: ConversationState,
        text: str,
        handled_by: str,
        escalated: bool = False,
        ticket_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        already_in_history: bool = False,
    ) -> Reply:
        outbound = self._outbound(text, state, message.channel)
        if not already_in_history:
            # Short-circuited turns still belong in the transcript, so a human
            # picking the conversation up sees what the customer saw.
            state.history.append({"role": "user", "content": message.message_text})
        state.history.append({"role": "assistant", "content": [{"type": "text", "text": outbound}]})
        self.log.outcome(message.conversation_id, handled_by, escalated, ticket_id, outbound)
        return Reply(
            conversation_id=message.conversation_id,
            text=outbound,
            handled_by=handled_by,
            escalated=escalated,
            ticket_id=ticket_id,
            metadata=metadata or {},
        )
