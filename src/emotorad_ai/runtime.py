"""The skeleton, wired end to end.

    channel adapter -> message contract -> identity resolution -> enrichment
    -> guardrails -> triage -> Jev path -> sub-agent -> tools -> post-checks

`handle` is the single entry point a channel adapter calls, and the *order* of
the steps is the design. graph.py fixes that order as a LangGraph graph; the
methods below are what each step does:

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

import re
import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence

from .agents.base import HANDOVER_TEXT, Agent, AgentDefinition
from .agents.battery_support import AGENT_NAME as BATTERY_SUPPORT
from .agents.battery_support import DEFINITION as BATTERY_SUPPORT_DEFINITION
from .agents.dealer_orders import AGENT_NAME as DEALER_ORDERS
from .agents.dealer_orders import DEFINITION as DEALER_ORDERS_DEFINITION
from .agents.late_warranty import AGENT_NAME as LATE_WARRANTY
from .agents.late_warranty import DEFINITION as LATE_WARRANTY_DEFINITION
from .agents.motor_support import AGENT_NAME as MOTOR_SUPPORT
from .agents.motor_support import DEFINITION as MOTOR_SUPPORT_DEFINITION
from .agents.narrow_support import AGENT_NAME as NARROW_SUPPORT
from .agents.narrow_support import build_narrow_definition
from .attachments import shows_media, user_content
from .config import Settings, load_settings
from .contract import Attachment, InboundMessage, Reply
from .conversation import (
    HISTORY_TURNS,
    ConversationConflict,
    ConversationState,
    ConversationSummaryItem,
    InMemoryConversationStore,
    StoreUnavailable,
    customer_texts,
    render_transcript,
    summary_key,
    trim_history,
    utc_now_iso,
)
from .decisions import (
    EMPTY_MESSAGE,
    Route,
    Thresholds,
    build_catalogue,
    build_questions,
    build_state,
    load_thresholds,
    route,
)
from .disclosure import apply_disclosure
from .enrichment import ContextEnricher, summarise_past
from .graph import TurnNodes, build_turn_graph
from .guardrails import (
    COVERAGE_BLOCKED_MESSAGE,
    EVIDENCE_BLOCKED_MESSAGE,
    HANDOFF_MESSAGE,
    ORDER_BLOCKED_MESSAGE,
    SAFETY_MESSAGE,
    check_coverage_claim,
    check_evidence,
    check_human_handoff,
    check_order_claim,
    check_safety,
    check_safety_in_description,
)
from .errorcodes import load_table
from .identity import IdentityResolver, ResolvedIdentity
from .jev import JevError
from .knowledge import KnowledgeBase
from .llm import BedrockClaude
from .observability import EventLog
from .openrouter import OpenRouterError
from .standard_responses import StandardResponse, load_standard_responses
from .tools.mocks import (
    CREATE_SUPPORT_TICKET,
    LOOKUP_ERROR_CODE,
    LOOKUP_WARRANTY_RECORD,
    OFFER_LOCATION_SHARE,
    PLACE_REPLACEMENT_ORDER,
    RAISE_INTAKE_TICKET,
    SEND_GUIDE_MEDIA,
    build_registry,
)
from .tools.registry import ToolContext, ToolRegistry, is_error
from .tools.verification import (
    FIND_ACCOUNT_BY_CODE,
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
)
from .triage import TriageAgent

UNSUPPORTED_MESSAGE = (
    "I am not able to help with this from here. Let me pass you to a member of our support "
    "team who can."
)

BUSY_MESSAGE = "Sorry, I'm still working on your last message. Please send that again in a moment."

# The summary title when no knowledge record was chosen. Fixed labels, never
# chat text, so nothing a customer typed is replayed into a future prompt.
AGENT_TITLES = {
    BATTERY_SUPPORT: "Battery issue",
    MOTOR_SUPPORT: "Motor issue",
    LATE_WARRANTY: "Warranty registration",
    DEALER_ORDERS: "Dealer order",
}

# Topic -> sub-agent, **scoped per persona**. Never one router over everything:
# a dealer and a customer asking the same words mean different things, and a
# shared agent set is how a dealer reaches a customer-only tool.
TOPIC_AGENTS = {"battery": BATTERY_SUPPORT, "motor": MOTOR_SUPPORT}
DEALER_AGENTS = {"order": DEALER_ORDERS}

# What a surface needs to establish identity inside the conversation, when the
# channel did not resolve it upstream. `find_account_by_code` is the fallback
# for a customer who cannot recall the number they registered on, and
# `raise_intake_ticket` is what the agent falls back to when nothing resolves,
# so a customer who cannot be identified is still captured for a human rather
# than left in a loop.
SELF_SERVICE_IDENTITY_TOOLS = (
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    FIND_ACCOUNT_BY_CODE,
    RAISE_INTAKE_TICKET,
)

# Tools that exist because of what the surface can render, not who the
# customer is. Added to the slice the same way: only when the registry holds
# them, which it does only when the surface asked for them.
SELF_SERVICE_SURFACE_TOOLS = (OFFER_LOCATION_SHARE,)

# Tools that change something for the customer without being registry writes:
# a code sent by SMS, and a code spent. A conflict retry must not repeat them
# (a second code, or a spent code replayed and refused).
SIDE_EFFECT_TOOLS = (SEND_GUIDE_MEDIA, REQUEST_IDENTITY_VERIFICATION, VERIFY_IDENTITY)


def _hazard_sentences(summary: str) -> List[str]:
    """The lines or sentences of a description that carry a hazard term,
    for the ticket. Split on newlines and full stops so a bullet-point
    description and a prose one both come out as short quotes."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", summary) if p.strip()]
    return [p for p in pieces if check_safety_in_description(p).triggered]


class Runtime:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[ToolRegistry] = None,
        llm: Any = None,
        log: Optional[EventLog] = None,
        resolver: Optional[IdentityResolver] = None,
        diagnostics_available: bool = False,
        jev: Any = None,
        narrow_llm: Any = None,
        thresholds: Optional[Thresholds] = None,
        standard_responses: Optional[Sequence[StandardResponse]] = None,
        conversations: Any = None,
        self_service_identity: bool = False,
        phone_resolver: Optional[Callable[[str], Optional[str]]] = None,
        media_store: Any = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.registry = registry or build_registry(diagnostics_available=diagnostics_available)
        self.log = log or EventLog(path=self.settings.log_path, to_stdout=self.settings.log_to_stdout)
        self.llm = llm if llm is not None else BedrockClaude(self.settings)
        self.resolver = resolver or IdentityResolver(self.registry)
        # In memory by default; the MongoDB store (EMOTORAD_STORE=mongodb) keeps
        # conversations across restarts and servers.
        self.conversations = conversations if conversations is not None else InMemoryConversationStore()
        self.enricher = ContextEnricher()
        self.triage = TriageAgent(TOPIC_AGENTS)

        definitions: Dict[str, AgentDefinition] = {
            BATTERY_SUPPORT: BATTERY_SUPPORT_DEFINITION,
            MOTOR_SUPPORT: MOTOR_SUPPORT_DEFINITION,
            LATE_WARRANTY: LATE_WARRANTY_DEFINITION,
            DEALER_ORDERS: DEALER_ORDERS_DEFINITION,
        }
        if self_service_identity:
            definitions = {
                name: self._with_identity_tools(definition)
                for name, definition in definitions.items()
            }
        # Evidence in S3 is fetched through the instance role and sent as base64;
        # the model never sees a URL.
        self.self_service_identity = self_service_identity
        self.media_store = media_store
        self.fetch = self.media_store.get_bytes if self.media_store is not None else None
        self.phone_resolver = phone_resolver
        self.agents = {
            name: Agent(
                definition,
                self.registry,
                self.llm,
                self.log,
                self.settings,
                phone_resolver=phone_resolver,
                fetch=self.fetch,
            )
            for name, definition in definitions.items()
        }

        # Jev routing. Absent in offline and bedrock modes, where the graph's
        # classify node always answers `full` and the turn is exactly today's.
        self.jev = jev
        self.narrow_llm = narrow_llm if narrow_llm is not None else self.llm
        self.catalogue = None
        self.jev_questions: Dict[str, Any] = {}
        self.thresholds = thresholds or Thresholds()
        if jev is not None:
            self.thresholds = thresholds or load_thresholds()
            self.catalogue = build_catalogue(
                KnowledgeBase(),
                standard=standard_responses if standard_responses is not None else load_standard_responses(),
                error_table=load_table() if LOOKUP_ERROR_CODE in self.registry.specs else None,
            )
            self.jev_questions = build_questions(self.catalogue)

        self.graph = build_turn_graph(
            TurnNodes(
                prepare=self._node_prepare,
                safety_gate=self._node_safety,
                handoff_gate=self._node_handoff,
                persona_route=self._node_persona,
                jev_classify=self._node_classify,
                standard_reply=self._node_standard,
                narrow_agent=self._node_narrow,
                full_agent=self._node_full,
            )
        )

    def _with_identity_tools(self, definition: AgentDefinition) -> AgentDefinition:
        """The agent's own tools, plus the ones a surface needs to establish identity.

        Production never needs these: identity is resolved upstream by the
        channel, so each agent's `TOOL_NAMES` is right to omit them. A website
        visitor who has not signed in is the one case where the agent has to
        establish identity itself, and slicing strictly by `TOOL_NAMES` left the
        verification tools registered but unreachable.

        Added here rather than in the agent module so the production slice stays
        as designed, and only for tools the registry actually holds: naming an
        unregistered tool would advertise one the model cannot call.
        """
        names = list(definition.tool_names)
        for extra in SELF_SERVICE_IDENTITY_TOOLS + SELF_SERVICE_SURFACE_TOOLS:
            if extra in self.registry.specs and extra not in names:
                names.append(extra)
        return replace(definition, tool_names=tuple(names))

    def _remember_lookup(
        self, state: ConversationState, name: str, arguments: Dict[str, Any], envelope: Dict[str, Any]
    ) -> None:
        """Record a tool result as it happens, mid-turn.

        `Agent.run` calls this straight after every tool call, before the loop
        goes round again. That is what lets a model call
        `lookup_warranty_record` and `place_replacement_order` in the same
        assistant turn: without it, `state.coverage_result` was only ever
        written after `run` returned, so the order tool's `coverage_result`
        injection saw the previous turn's lookup, or nothing at all, when the
        model had just done both in one turn.
        """
        if name == LOOKUP_WARRANTY_RECORD and not is_error(envelope):
            state.coverage_result = envelope
        if name == PLACE_REPLACEMENT_ORDER and not is_error(envelope):
            order_id = (envelope.get("data") or {}).get("order_id")
            if order_id and order_id not in state.placed_order_ids:
                state.placed_order_ids.append(order_id)
        # A code the conversation consumed for verification is not customer
        # address text (final fix wave, item 1). Probe:
        # `AddressFromTheConversationTests.test_a_verified_code_is_not_a_pincode`.
        if name == VERIFY_IDENTITY and not is_error(envelope):
            code = str(arguments.get("code", "")).strip()
            if code and code not in state.consumed_codes:
                state.consumed_codes.append(code)

    # -- entry point ---------------------------------------------------------

    def handle(self, message: InboundMessage) -> Reply:
        """One turn: load, run the graph, save, record.

        The save is version-checked. If another server saved this conversation
        in the meantime, a turn that changed nothing for the customer is run
        once more from freshly loaded state, never on top of the losing
        attempt's; a second clash asks the customer to resend rather than
        overwrite anyone's work. A turn that did something (a ticket, a
        booking, a picture sent) is never run again: it is added to the fresh
        state instead, so it is neither repeated nor forgotten.
        """
        # One customer turn is one `inbound` and one `outcome`, whichever path
        # it takes (a retry after a conflict, the busy reply, a storage
        # outage): the metrics count turns by `inbound`, and a Langfuse trace
        # opens on `inbound` and closes on `outcome`. A turn that crashes logs
        # no outcome; the trace is closed, flagged, by the next `inbound`.
        self.log.inbound(message)
        reply = self._handle(message)
        self.log.outcome(message.conversation_id, reply.handled_by, reply.escalated, reply.ticket_id, reply.text)
        return reply

    def _handle(self, message: InboundMessage) -> Reply:
        cid = message.conversation_id
        turn_mark = len(self.log.events)
        for attempt in (1, 2):
            try:
                state = self.conversations.get(cid)
            except StoreUnavailable as exc:
                return self._store_down(message, exc, self._ticket_since(turn_mark, cid))
            # The history window, applied here rather than first inside
            # Agent.run: the marks below (the merge after a conflict, the
            # narrow model's rollback) are positions in this list, and a trim
            # after they are taken would move what they point at. Agent.run
            # trims to the same length, so its own trim then changes nothing.
            state.history[:] = trim_history(state.history, HISTORY_TURNS - 1)
            history_mark, log_mark = len(state.history), len(self.log.events)
            coverage_loaded = state.coverage_result
            try:
                final = self.graph.invoke({"message": message, "conversation": state})
            except StoreUnavailable as exc:
                # Every store call inside the turn handles its own failure; this
                # is the backstop, so one that does not is a handover, not a 500.
                return self._store_down(message, exc, self._ticket_since(turn_mark, cid))
            reply, resolved = final["reply"], final.get("resolved")
            state.escalated = state.escalated or reply.escalated
            state.ticket_id = reply.ticket_id or state.ticket_id
            this_turn = list(state.history[history_mark:])  # before the save trims anything
            try:
                self.conversations.save(state)
                break
            except ConversationConflict:
                self.log.emit("conversation_conflict", cid, attempt=attempt)
                if self._side_effects_since(log_mark, cid):
                    try:
                        state = self._merge_onto_fresh(
                            state, this_turn, reply, looked_up=state.coverage_result != coverage_loaded
                        )
                    except (ConversationConflict, StoreUnavailable) as exc:
                        return self._store_down(message, exc, reply.ticket_id)
                    break
                if attempt == 2:
                    self.log.emit("conversation_busy", cid)
                    return Reply(
                        conversation_id=cid, text=self._outbound(BUSY_MESSAGE, state, message.channel),
                        handled_by="conversation_busy",
                    )
            except StoreUnavailable as exc:
                return self._store_down(message, exc, reply.ticket_id)
        try:
            self.conversations.record_turn(state, message, reply, self._summary_for(state, resolved))
        except StoreUnavailable as exc:
            # The turn happened and the state is saved; only the record of it
            # failed. Said in the log, and the customer still gets the answer.
            self.log.emit("transcript_write_failed", cid, error=str(exc))
        self._attach_transcript(cid, reply)
        return reply

    def _attach_transcript(self, conversation_id: str, reply: Reply) -> None:
        """Every ticket, from an agent or the safety branch, gets the whole
        thread, this turn included, so a person never starts from nothing."""
        tickets = getattr(self.registry, "tickets", None)
        if not reply.ticket_id or not hasattr(tickets, "attach_transcript"):
            return
        try:
            tickets.attach_transcript(reply.ticket_id, render_transcript(self.conversations.transcript(conversation_id)))
        except Exception as exc:  # the ticket exists either way; say the thread did not reach it
            self.log.emit(
                "transcript_attach_failed", conversation_id,
                ticket_id=reply.ticket_id, error="%s: %s" % (type(exc).__name__, exc),
            )

    def _merge_onto_fresh(
        self, ours: ConversationState, this_turn: List[Dict[str, Any]], reply: Reply, looked_up: bool = False
    ) -> ConversationState:
        """Add a turn that lost the save race, and did something, to the state
        the other server saved. Its words and its ticket are kept; the routing
        the other server saved stands. One attempt: a second clash hands over."""
        fresh = self.conversations.get(ours.conversation_id)
        fresh.turns += 1
        # The other server's copy gets the same window this turn's did.
        fresh.history[:] = trim_history(fresh.history, HISTORY_TURNS - 1)
        fresh.history.extend(this_turn)
        fresh.escalated = fresh.escalated or ours.escalated
        fresh.ticket_id = reply.ticket_id or fresh.ticket_id
        fresh.evidence_seen = fresh.evidence_seen or ours.evidence_seen
        fresh.disclosed = fresh.disclosed or ours.disclosed
        # What this turn learnt stands: its lookup, if it made one, is the
        # newer one (otherwise the other server's stands: ours is only what we
        # loaded), and its orders and spent codes are added to the other's.
        if looked_up and ours.coverage_result is not None:
            fresh.coverage_result = ours.coverage_result
        fresh.placed_order_ids += [o for o in ours.placed_order_ids if o not in fresh.placed_order_ids]
        fresh.consumed_codes += [c for c in ours.consumed_codes if c not in fresh.consumed_codes]
        for name in ("user_key", "channel", "context_block", "cluster_id"):
            if getattr(fresh, name) is None:
                setattr(fresh, name, getattr(ours, name))
        fresh.transitions.append("merged_after_conflict")
        self.conversations.save(fresh)
        self.log.emit("conversation_merged", ours.conversation_id, ticket_id=reply.ticket_id)
        return fresh

    @staticmethod
    def _already_done(turn: Any) -> str:
        """What this turn created (tickets, bookings, orders), for a reply a
        post-check replaced: the write happened either way, and the customer
        must not be left without its reference."""
        done: List[str] = []
        for call in turn.tool_calls:
            if call.get("prefetched") or is_error(call.get("result") or {}):
                continue
            data = (call.get("result") or {}).get("data") or {}
            if not isinstance(data, dict):
                continue
            for key, label in (("ticket_id", "ticket"), ("booking_id", "booking"), ("order_id", "order")):
                value = data.get(key)
                if value and "%s %s" % (label, value) not in done:
                    done.append("%s %s" % (label, value))
        return ("\n\nAlready done for you: %s." % ", ".join(done)) if done else ""

    def _ticket_since(self, log_mark: int, conversation_id: str) -> Optional[str]:
        return next((w["ticket_id"] for w in self._side_effects_since(log_mark, conversation_id) if w.get("ticket_id")), None)

    def _store_down(self, message: InboundMessage, exc: Exception, ticket_id: Optional[str] = None) -> Reply:
        """The store cannot be reached: hand over, never start from blank. A
        ticket the turn already raised is named, so the customer can quote it."""
        self.log.emit("store_unavailable", message.conversation_id, error=str(exc))
        self.log.escalation(message.conversation_id, "store_unavailable", ticket_id)
        # A throwaway state, so the AI disclosure is always added: we cannot
        # know whether this person has already seen it.
        text = HANDOVER_TEXT + ("\n\nYour reference is %s." % ticket_id if ticket_id else "")
        text = apply_disclosure(text, ConversationState(conversation_id=message.conversation_id), message.channel)
        return Reply(conversation_id=message.conversation_id, text=text, handled_by="store_unavailable",
                     escalated=True, ticket_id=ticket_id)

    # -- graph nodes (graph.py says what follows what) -----------------------

    def _node_prepare(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message = turn["message"]
        state = turn["conversation"]  # loaded by handle(), saved after the graph
        state.turns += 1
        # Set by the web chat API (api.post_message): the identity cluster that
        # started this conversation, which a later upload is checked against,
        # and an agent the tester pinned. Applied here so they are saved with
        # the turn; an unknown pin is cleared in the persona step.
        meta = message.entry_metadata
        if state.cluster_id is None and meta.get("cluster_id"):
            state.cluster_id = meta["cluster_id"]
        if meta.get("pinned_agent"):
            state.route_to(meta["pinned_agent"])

        resolved = self.resolver.hydrate(message)
        if state.channel is None:
            state.channel = message.channel
        if state.user_key is None:
            state.user_key = self._user_key(resolved)
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
            # Memory: a proven person's last few conversations, as code-built
            # lines. The enricher shows them only to a verified identity.
            last_contact = None
            if state.user_key:
                try:
                    last_contact = summarise_past(self.conversations.recent_summaries(
                        state.user_key, limit=3, exclude=summary_key(state.conversation_id, state.started_at)))
                except StoreUnavailable as exc:
                    # Memory is a nicety; the conversation goes on without it.
                    self.log.emit("memory_unavailable", message.conversation_id, error=str(exc))
            context = self.enricher.build(resolved, last_conversation=last_contact)
            state.context_block = context.render()
            self.log.emit(
                "context_built", message.conversation_id,
                kept=list(context.sections), dropped=context.dropped, tokens=context.tokens,
            )

        # evidence_seen is not set here, on arrival: it is set where the
        # customer's turn is built for the model (_note_customer_turn), and
        # only when that turn shows a photo or video.
        return {"conversation": state, "resolved": resolved}

    def _node_safety(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 1. Safety. A keyword gate ahead of the agent turn, not something the
        #    model has to notice. Allowed to over-trigger.
        #    A video's description (Attachment.summary, written by the video
        #    analyser at ingest) is scanned too: smoke seen on a clip is the
        #    same hard stop as smoke typed in a sentence. The description gets
        #    the negation-aware scan, because an analyser that writes "no
        #    smoke visible" is describing a safe clip, not a hazard.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        safety = check_safety(message.message_text)
        matched = list(safety.matched)
        evidence: List[str] = []
        for attachment in message.attachments:
            if attachment.summary:
                verdict = check_safety_in_description(attachment.summary)
                matched += [m for m in verdict.matched if m not in matched]
                if verdict.triggered:
                    evidence += _hazard_sentences(attachment.summary)
        if matched:
            return {"reply": self._handle_safety(message, resolved, state, matched, evidence)}
        return {}

    def _node_handoff(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 2. Human handoff, reachable at any point, no friction.
        message, state = turn["message"], turn["conversation"]
        handoff = check_human_handoff(message.message_text)
        if handoff.triggered:
            self.log.guardrail(message.conversation_id, "human_handoff", handoff.matched)
            self.log.escalation(message.conversation_id, "customer_requested_human", None)
            return {"reply": self._finish(
                message, state, HANDOFF_MESSAGE, "guardrail:human_handoff",
                escalated=True, metadata={"matched": handoff.matched},
            )}
        return {}

    def _node_persona(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if resolved.persona == "dealer":
            # Dealers bypass customer triage entirely. There is no bike to
            # disambiguate and no customer record to enrich from — and routing
            # them through the customer path is precisely how a dealer would end
            # up holding someone else's warranty data.
            state.route_to(DEALER_ORDERS)
            self.log.routed(message.conversation_id, DEALER_ORDERS, "persona:dealer")
            return {"reply": self._run_agent_or_handover(DEALER_ORDERS, message, resolved, state)}

        if resolved.persona != "customer":
            return {"reply": self._finish(
                message, state, UNSUPPORTED_MESSAGE, "router",
                escalated=True, metadata={"persona": resolved.persona},
            )}

        # 3. A customer with no bike on record goes straight to registration —
        #    triage has nothing to disambiguate and no issue it can act on.
        if resolved.method in ("no_warranty_record",) and LATE_WARRANTY in self.agents:
            state.route_to(LATE_WARRANTY)
            return {"reply": self._run_agent_or_handover(LATE_WARRANTY, message, resolved, state)}

        # A pin naming an agent this runtime does not have is dropped rather
        # than followed. It reaches here from `MessageIn.agent`, which is
        # browser-supplied, and following it raised KeyError on every turn of
        # that conversation forever, including turns that pinned nothing,
        # because the bad name had already been written into the state. The
        # conversation could never recover. Clearing it sends the customer
        # through triage, which is where they would have gone without the pin.
        if state.agent is not None and state.agent not in self.agents:
            self.log.emit("unknown_agent_cleared", message.conversation_id, agent=state.agent)
            state.agent = None

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
                return {"reply": self._finish(
                    message, state, outcome.reply or UNSUPPORTED_MESSAGE, "triage",
                    metadata=dict(outcome.metadata, reason=outcome.reason),
                )}
        return {}

    def _node_classify(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if self.jev is None:
            return {"route": Route(path="full", reasons=("jev_disabled",))}
        if message.entry_metadata.get("pinned_agent") in self.agents:
            # A tester pinned the agent (web chat): follow the pin, with no Jev
            # score to route around it and no narrow path in its place.
            return {"route": Route(path="full", reasons=("pinned_agent",))}

        bike = self._selected_bike(resolved, state)
        decision, error = None, None
        if not message.message_text.strip():
            # A photo on its own: nothing to score. Stay on the record if there is one.
            error = EMPTY_MESSAGE
        else:
            jev_state = build_state(
                message.message_text, state.history, message.channel, bike=bike,
                current_sub_category=state.sub_category, redact=self._redaction_terms(resolved),
            )
            try:
                decision = self.jev.decide(jev_state, self.jev_questions)
            except JevError as exc:
                error = exc.code

        chosen = route(decision, error, self.thresholds, self.catalogue, state.sub_category, bike)
        self.log.jev_decision(message.conversation_id, chosen, decision, error)
        return {"route": chosen}

    def _node_standard(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, chosen = turn["message"], turn["conversation"], turn["route"]
        response = self.catalogue.standard[chosen.standard_response_id]
        text = response.reply_for(chosen.language)
        # Checked at load already; checked again here because a check that only
        # runs at load is a check an edit to the loader can remove.
        if check_coverage_claim(text, []).blocked or check_evidence(text, state.evidence_seen).blocked:
            self.log.guardrail(message.conversation_id, "standard_response_blocked", {"id": response.id})
            return {"route": replace(chosen, path="full", reasons=chosen.reasons + ("standard_blocked",))}
        self._log_turn_path(message.conversation_id, "standard")
        return {"reply": self._finish(
            message, state, text, "standard:%s" % response.id, metadata={"route": "standard"},
        )}

    def _node_narrow(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved, chosen = turn["message"], turn["conversation"], turn["resolved"], turn["route"]
        record = self.catalogue.records[chosen.sub_category]
        state.sub_category = record.id

        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
        )
        prefetched: List[Dict[str, Any]] = []
        for call in chosen.prefetch:
            if call.tool not in self.registry.specs:
                continue
            arguments = dict(call.arguments)
            envelope = self.registry.call(call.tool, arguments, context)
            self.log.tool_call(message.conversation_id, call.tool, arguments, envelope)
            prefetched.append({"tool": call.tool, "arguments": arguments, "result": envelope, "prefetched": True})

        definition = build_narrow_definition(record, prefetched)
        if self.self_service_identity:
            # The same identity tools every other agent gets on the web chat:
            # an anonymous visitor routed narrow must still be able to verify.
            definition = self._with_identity_tools(definition)
        agent = Agent(
            definition, self.registry, self.narrow_llm, self.log, self.settings,
            phone_resolver=self.phone_resolver, fetch=self.fetch,
        )
        mark = len(state.history)
        log_mark = len(self.log.events)
        try:
            reply = self._run(agent, message, resolved, state, prefetched=prefetched, route_path="narrow")
        except OpenRouterError as exc:
            # The loop appended the customer's message before failing. Undo it,
            # or the full agent would be shown the message twice.
            del state.history[mark:]
            self.log.emit("llm_error", message.conversation_id, agent=NARROW_SUPPORT, error=exc.code)
            written = self._side_effects_since(log_mark, message.conversation_id)
            if written:
                # Something already happened for the customer: a ticket, a
                # booking, a picture sent. Rerunning the turn on the full agent
                # would repeat it or hide it, so hand over and say what was done.
                ticket_id = next((w["ticket_id"] for w in written if w.get("ticket_id")), None)
                self.log.escalation(message.conversation_id, "llm_error_after_write", ticket_id)
                self._log_turn_path(message.conversation_id, "narrow")
                text = HANDOVER_TEXT + ("\n\nYour reference is %s." % ticket_id if ticket_id else "")
                return {"reply": self._finish(
                    message, state, text, "llm_error", escalated=True, ticket_id=ticket_id,
                    metadata={"code": exc.code, "side_effects": [w["tool"] for w in written]},
                )}
            return {"route": replace(chosen, path="full", reasons=chosen.reasons + ("narrow_llm_error:%s" % exc.code,))}
        self._log_turn_path(message.conversation_id, "narrow")
        return {"reply": reply}

    def _node_full(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        chosen = turn.get("route")

        # Jev may confidently place the message in a different topic from the
        # one triage picked; that is the category decision the spec gives it.
        topic_agent = TOPIC_AGENTS.get(chosen.category) if chosen and chosen.category else None
        if topic_agent and topic_agent != state.agent:
            state.route_to(topic_agent)
            self.log.routed(message.conversation_id, topic_agent, "jev:%s" % chosen.category)
        if chosen and chosen.category and state.sub_category:
            current = self.catalogue.records.get(state.sub_category) if self.catalogue else None
            if current is None or current.topic != chosen.category:
                state.sub_category = None

        self._log_turn_path(message.conversation_id, "full")
        return {"reply": self._run_agent_or_handover(state.agent or BATTERY_SUPPORT, message, resolved, state)}

    # -- helpers -------------------------------------------------------------

    def _run_agent_or_handover(
        self,
        agent_name: str,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
    ) -> Reply:
        """Every agent node, so an OpenRouter outage in any of them (dealer, late
        warranty, a full topic agent) is a handover, never a crashed turn. Only
        the OpenRouter models raise this; Bedrock behaviour is unchanged."""
        mark = len(state.history)
        log_mark = len(self.log.events)
        try:
            return self._run_agent(agent_name, message, resolved, state)
        except OpenRouterError as exc:
            del state.history[mark:]
            self.log.emit("llm_error", message.conversation_id, agent=agent_name, error=exc.code)
            # A ticket the turn raised before the model failed exists: keep its
            # id, so the conversation remembers it and the customer can quote it.
            ticket_id = self._ticket_since(log_mark, message.conversation_id)
            self.log.escalation(message.conversation_id, "llm_error", ticket_id)
            text = HANDOVER_TEXT + ("\n\nYour reference is %s." % ticket_id if ticket_id else "")
            return self._finish(
                message, state, text, "llm_error", escalated=True, ticket_id=ticket_id, metadata={"code": exc.code},
            )

    def _side_effects_since(self, log_mark: int, conversation_id: str) -> List[Dict[str, Any]]:
        """Tool calls since `log_mark` that changed something for the customer:
        any write tool, plus SIDE_EFFECT_TOOLS (a picture sent, a code sent or
        spent), which a rerun would repeat or break."""
        done: List[Dict[str, Any]] = []
        for event in self.log.events[log_mark:]:
            if event.get("event") != "tool_call" or event.get("conversation_id") != conversation_id:
                continue
            # A failed call changed nothing, except a code tried: that attempt
            # is spent either way, so a rerun would cost the customer another.
            if not event.get("ok") and event.get("tool") != VERIFY_IDENTITY:
                continue
            spec = self.registry.specs.get(event.get("tool"))
            if spec is None or not (spec.write or spec.name in SIDE_EFFECT_TOOLS):
                continue
            data = (event.get("result") or {}).get("data") or {}
            done.append({"tool": spec.name, "ticket_id": data.get("ticket_id") if isinstance(data, dict) else None})
        return done

    def _log_turn_path(self, conversation_id: str, path: str) -> None:
        """The path that actually answered, logged only when Jev routes. The
        jev_decision event records the intended path, which a fallback changes."""
        if self.jev is not None:
            self.log.emit("turn_path", conversation_id, path=path)

    @staticmethod
    def _user_key(resolved: ResolvedIdentity) -> Optional[str]:
        """Who a conversation belongs to, for memory. Only a proven identity:
        a cookie or caller ID never gets a history (the disclosure rule)."""
        identity = resolved.identity
        if resolved.persona == "dealer" and identity.dealer_id:
            return "DEALER#" + identity.dealer_id
        if resolved.persona == "customer" and identity.may_disclose and identity.phone:
            return "PHONE#" + identity.phone
        return None

    def _summary_for(
        self, state: ConversationState, resolved: Optional[ResolvedIdentity]
    ) -> Optional[ConversationSummaryItem]:
        """This conversation's line in the person's history, from code only."""
        if not state.user_key:
            return None
        record = self.catalogue.records.get(state.sub_category) if (self.catalogue and state.sub_category) else None
        title = record.title if record else AGENT_TITLES.get(state.agent or "", "General question")
        bike = (self._selected_bike(resolved, state) if resolved else None) or {}
        return ConversationSummaryItem(
            conversation_id=state.conversation_id,
            user_key=state.user_key,
            started_at=state.started_at or "",
            last_at=utc_now_iso(),
            channel=state.channel or "",
            title=title,
            frame_number=bike.get("frame_number"),
            product_name=bike.get("product_name"),
            agent=state.agent,
            sub_category=state.sub_category,
            outcome="escalated" if state.escalated else "open",
            ticket_id=state.ticket_id,
            turns=state.turns,
        )

    @staticmethod
    def _selected_bike(resolved: ResolvedIdentity, state: ConversationState) -> Optional[Dict[str, Any]]:
        for bike in resolved.bikes:
            if bike.get("frame_number") == state.selected_frame:
                return bike
        return resolved.single_bike

    @staticmethod
    def _redaction_terms(resolved: ResolvedIdentity) -> List[str]:
        """Names and frame numbers that must not reach Jev, even inside recent turns."""
        terms: List[str] = []
        name = (resolved.profile or {}).get("name") or ""
        if name:
            terms.append(name)
            terms.extend(part for part in name.split() if len(part) > 2)
        terms.extend(bike.get("frame_number") or "" for bike in resolved.bikes)
        return [term for term in terms if term]

    # -- steps ---------------------------------------------------------------

    def _run_agent(
        self,
        agent_name: str,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
    ) -> Reply:
        return self._run(self.agents[agent_name], message, resolved, state)

    def _run(
        self,
        agent: Agent,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        prefetched: Sequence[Dict[str, Any]] = (),
        route_path: Optional[str] = None,
    ) -> Reply:
        """One agent turn and every post-check, for whichever agent ran it.

        One place, so the narrow agent cannot skip a check the full agents get.
        """
        turn = agent.run(
            message, resolved, state.history, state.context_block or "",
            # Conversation facts the order tool decides on. Lambdas, because
            # evidence_seen can flip during this very turn when a photo arrives
            # with the message that triggers the order.
            facts={
                "evidence_seen": lambda: state.evidence_seen,
                "coverage_result": lambda: state.coverage_result,
                # The customer's own words, for the address backstop: an
                # address is accepted only if it matches the record or
                # something the customer actually typed in this conversation.
                # A code `verify_identity` has already consumed is a spent
                # secret, not an address, so it is left out.
                "customer_messages": lambda: [
                    m for m in customer_texts(state.history) if m.strip() not in state.consumed_codes
                ],
            },
            # Recorded as the loop runs, not only once the turn ends: a model
            # that calls lookup_warranty_record and place_replacement_order in
            # the same assistant turn needs coverage_result to see the lookup
            # that just happened.
            on_tool_result=lambda name, arguments, envelope: self._remember_lookup(
                state, name, arguments, envelope
            ),
            prefetched=prefetched,
            on_user_turn=lambda content: self._note_customer_turn(message, state, content),
        )
        if turn.escalate:
            self.log.escalation(
                message.conversation_id, turn.escalation_reason or "agent_requested_handover", turn.ticket_id
            )

        # The post-check: calling the warranty tool proved the tool ran, not that
        # the reply matches what it returned.
        #
        # Remember this turn's lookup before checking (prefetched ones included),
        # so a claim made in the same turn as the lookup and a claim made three
        # turns later are judged against the same fact. Latest wins.
        for call in turn.tool_calls:
            if call["tool"] == LOOKUP_WARRANTY_RECORD and not is_error(call["result"]):
                state.coverage_result = call["result"]
        results = [call["result"] for call in turn.tool_calls]
        if state.coverage_result is not None:
            results = results + [state.coverage_result]
        coverage = check_coverage_claim(turn.text, results)
        if coverage.blocked:
            self.log.guardrail(
                message.conversation_id, "coverage_post_check",
                {
                    "reason": coverage.reason,
                    "claimed": coverage.claimed,
                    "actual": coverage.actual,
                    # What the customer nearly got told. Without it a false
                    # positive and a true positive are indistinguishable in the
                    # log.
                    "suppressed_text": turn.text,
                },
            )
            self.log.escalation(message.conversation_id, "coverage_claim_blocked", turn.ticket_id)
            return self._finish(
                message, state, COVERAGE_BLOCKED_MESSAGE + self._already_done(turn), "guardrail:coverage_post_check",
                escalated=True, ticket_id=turn.ticket_id,
                metadata={"blocked_reason": coverage.reason, "suppressed_text": turn.text},
                already_in_history=True,
            )

        # The third post-check, same reason as the first: the order tool ran
        # is not the reply named the order it returned. A placed order is
        # remembered for the conversation, the way coverage_result is, so a
        # claim about an order placed three turns ago is not blocked as
        # unsupported; an id from another conversation still is.
        order = check_order_claim(
            turn.text,
            [call["result"] for call in turn.tool_calls]
            + [{"data": {"order_id": order_id}} for order_id in state.placed_order_ids],
        )
        if order.blocked:
            self.log.guardrail(
                message.conversation_id, "order_post_check",
                {"reason": order.reason, "claimed": order.claimed, "suppressed_text": turn.text},
            )
            self.log.escalation(message.conversation_id, "order_claim_blocked", turn.ticket_id)
            return self._finish(
                message, state, ORDER_BLOCKED_MESSAGE + self._already_done(turn), "guardrail:order_post_check",
                escalated=True, ticket_id=turn.ticket_id,
                metadata={"blocked_reason": order.reason, "suppressed_text": turn.text},
                already_in_history=True,
            )

        # The second post-check, for the same reason as the first: a rule the
        # model was given in prose is a rule it can skip. This one asks only
        # whether a fault was concluded having seen nothing.
        # `handle()` returns before this on a safety trigger, so anything reaching
        # here has already passed check_safety. What check_evidence still watches
        # for is a reply that hands over for safety reasons the regex did not
        # catch — a hazard is never held behind a request for a photograph of it.
        evidence = check_evidence(turn.text, state.evidence_seen)
        if evidence.blocked:
            self.log.guardrail(
                message.conversation_id, "evidence_post_check",
                {"reason": evidence.reason, "matched": evidence.matched},
            )
            # The reply is replaced, but a ticket the turn already raised still
            # exists: keep its id so it is tracked and gets the transcript.
            if state.evidence_asked:
                # Asked for a photo once already, and the reply still concludes
                # with nothing seen: a person takes it from here, rather than the
                # customer being asked the same thing again.
                self.log.escalation(message.conversation_id, "evidence_not_forthcoming", turn.ticket_id)
                return self._finish(
                    message, state, HANDOVER_TEXT + self._already_done(turn), "guardrail:evidence_post_check",
                    escalated=True, ticket_id=turn.ticket_id,
                    metadata={"blocked_reason": evidence.reason, "suppressed_text": turn.text, "handover": True},
                    already_in_history=True,
                )
            state.evidence_asked = True
            return self._finish(
                message, state, EVIDENCE_BLOCKED_MESSAGE + self._already_done(turn), "guardrail:evidence_post_check",
                ticket_id=turn.ticket_id,
                metadata={"blocked_reason": evidence.reason, "suppressed_text": turn.text},
                already_in_history=True,
            )

        return Reply(
            conversation_id=message.conversation_id,
            text=self._outbound(turn.text, state, message.channel),
            handled_by=turn.agent,
            escalated=turn.escalate,
            ticket_id=turn.ticket_id,
            # `kind` comes from the item, not hardcoded: the catalogue carries
            # clips as well as photos, and a video announced as an image renders
            # as a broken picture on every channel that trusts the field.
            attachments=[
                Attachment(
                    kind=item.get("kind") or "image",
                    url=item["url"],
                    mime_type=item.get("mime_type"),
                    caption=item.get("caption"),
                    poster=item.get("poster"),
                )
                for item in turn.attachments
                if item.get("url")
            ],
            actions=list(turn.actions),
            metadata=dict(
                {"tool_calls": [c["tool"] for c in turn.tool_calls], "iterations": turn.iterations},
                **({"route": route_path} if route_path else {}),
            ),
        )

    def _handle_safety(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        matched: List[str],
        evidence: Optional[List[str]] = None,
    ) -> Reply:
        self.log.guardrail(message.conversation_id, "battery_safety", matched)

        ticket_id: Optional[str] = None
        if resolved.identity.phone:
            # Deterministic: code decides this ticket exists, not the model.
            description = (
                "Automatic safety escalation. Customer reported: %s. Matched safety "
                "indicators: %s. No troubleshooting was offered."
                % (message.message_text, ", ".join(matched))
            )
            if evidence:
                # The trigger came from the clip, and the typed text may say
                # nothing alarming: the safety team needs what the analyser
                # saw, not just "video attached".
                description += " Seen in the customer's video: %s" % " ".join(evidence)
            arguments: Dict[str, Any] = {
                "category": "battery_safety",
                "severity": "critical",
                "description": description,
                # One ticket per run of the conversation: repeats inside a run
                # share it, and a thread that returns after its working state
                # expired (48 h; receipts live 7 days) raises a new one rather
                # than being quoted the old reference (the person's decision,
                # 2026-09-29). A run is the id and its start, as for summaries.
                "idempotency_key": "safety:%s:%s" % (message.conversation_id, state.started_at or ""),
            }
            # With several bikes the ticket needs one named, and triage may not
            # have run yet — the safety branch fires before it.
            if state.selected_frame:
                arguments["frame_number"] = state.selected_frame
            elif resolved.single_bike:
                arguments["frame_number"] = resolved.single_bike["frame_number"]

            # Raised even if the receipt store is down: a duplicate safety
            # ticket is a lesser harm than none.
            self.log.tool_request(message.conversation_id, CREATE_SUPPORT_TICKET)
            started = time.monotonic()
            envelope = self.registry.call(
                CREATE_SUPPORT_TICKET,
                arguments,
                ToolContext(
                    conversation_id=message.conversation_id,
                    phone=resolved.identity.phone,
                    cluster_id=resolved.cluster_id,
                ),
                run_without_idempotency=True,
            )
            self.log.tool_call(
                message.conversation_id, CREATE_SUPPORT_TICKET, {"category": "battery_safety"}, envelope,
                duration_ms=int(round((time.monotonic() - started) * 1000)),
            )
            if not is_error(envelope):
                ticket_id = envelope["data"]["ticket_id"]

        text = SAFETY_MESSAGE
        if ticket_id:
            text += "\n\nI have raised this as a priority safety case, reference %s." % ticket_id

        self.log.escalation(message.conversation_id, "battery_safety", ticket_id)
        if evidence:
            # The whole description goes into the transcript, not just the
            # matched lines, so a human reading it later sees what the
            # analyser saw. Same labelled shape the agent path writes, and the
            # same rule for the typed text: a clip sent with no caption must
            # not leave an empty text block behind, because the API rejects it
            # on every later turn of the conversation (staging, 2026-09-22).
            described = replace(message, attachments=[a for a in message.attachments if a.summary])
            content = user_content(described)
            state.history.append({"role": "user", "content": content})
            self._note_customer_turn(message, state, content)
        return self._finish(
            message, state, text, "guardrail:battery_safety",
            escalated=True, ticket_id=ticket_id, metadata={"matched": matched},
            already_in_history=bool(evidence),
        )

    def _note_customer_turn(self, message: InboundMessage, state: ConversationState, content: Any) -> None:
        """Evidence from the customer's turn as it was built for the model.

        Every place that builds that turn calls this with what it built: the
        agent loop (before its first model call), the short-circuit replies
        and the safety branch. A photo or video counts only when the model is
        shown it (attachments.shows_media); one that could not be fetched or
        read, or a PDF, does not (the person's decision, 2026-09-29).
        """
        if shows_media(content):
            state.evidence_seen = True
        elif message.attachments:
            # By kind only: the URL can be a signed link or a customer's key.
            self.log.emit(
                "attachment_not_evidence", message.conversation_id,
                kinds=[attachment.kind for attachment in message.attachments],
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
            # Written the same way the agent path writes a turn: a photo with no
            # caption must become image blocks, not an empty string the API
            # rejects on every later call of the conversation.
            content = user_content(message, self.fetch)
            state.history.append({"role": "user", "content": content})
            self._note_customer_turn(message, state, content)
        state.history.append({"role": "assistant", "content": [{"type": "text", "text": outbound}]})
        return Reply(
            conversation_id=message.conversation_id,
            text=outbound,
            handled_by=handled_by,
            escalated=escalated,
            ticket_id=ticket_id,
            metadata=metadata or {},
        )
