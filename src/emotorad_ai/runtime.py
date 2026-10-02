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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
    AWAITING_BIKE_CONFIRMATION,
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    AWAITING_UNLISTED_BIKE,
    GREETING,
    HISTORY_TURNS,
    ROUTED,
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
from .enrichment import ContextEnricher, summarise_past, without_bikes
from .graph import TurnNodes, build_turn_graph
from .guardrails import (
    COVERAGE_BLOCKED_MESSAGE,
    EVIDENCE_BLOCKED_MESSAGE,
    HANDOFF_MESSAGE,
    ORDER_BLOCKED_MESSAGE,
    SAFETY_MESSAGE,
    CoverageCheck,
    EvidenceCheck,
    OrderCheck,
    carries_caution,
    check_coverage_claim,
    check_evidence,
    check_human_handoff,
    check_order_claim,
    check_safety,
    claims_coverage,
    claims_deletion,
    claims_ticket,
    check_safety_in_description,
)
from .errorcodes import load_table
from .identity import IdentityResolver, ResolvedIdentity
from . import origin as origin_place
from . import photo_check
from .jev import JevError
from .knowledge import KnowledgeBase
from .llm import BedrockClaude
from .navigation import (
    APP_SIGN_IN_CHANNELS,
    BACK_TO_LIST,
    NUMBER_FIXED,
    NUMBER_FIXED_APP,
    START_AGAIN,
    names_the_mobile,
    wants_change_bike,
    wants_change_number,
    wants_list,
    wants_start_over,
)
from .observability import EventLog
from .evidence_asks import MAX_EVIDENCE_ASKS, added_line, asks_for_media, declines_video
from .one_step import is_too_long, replace_turn_text
from .one_step import sentences as one_step_sentences
from .one_step import shorten as shorten_reply
from .one_step import words as one_step_words
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
    apply_proven_phone,
)
from .triage import TriageAgent, bike_ref, unlisted_as_bike, unlisted_context, which_bike_text
from .verify_first import CONFIRMED, NUMBER, VerifyFirst
from . import erasure as erasure_rules

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
# The topic an agent works on, kept when the customer changes bike mid-chat.
_TOPIC_OF_AGENT = {agent: topic for topic, agent in TOPIC_AGENTS.items()}
# Where some other number is being asked for or talked about (a frame number,
# an invoice number): a plain "wrong number" there does not mean the mobile.
_NUMBER_ASKED_BY_OTHERS = (AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION, ROUTED)
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

# What a verify-first turn changes, carried whole when it loses a save race
# (Runtime._merge_onto_fresh).
VERIFY_FIRST_FIELDS = (
    "verify_step", "verify_masked", "verify_sends", "phase", "agent", "selected_frame",
    "unlisted_bike", "unlisted_asks", "bike_confirmation",
    # started_at: a new person's run keeps its own summary even after a clash.
    # The turn numbering stays the other server's, which already counts both.
    "pending_topic", "pending_topic_source", "context_block", "user_key", "started_at",
    # origin goes with started_at: it belongs to one run, never another.
    "origin",
)

# Said when a guide picture failed and the reply does not already say so
# (Runtime._admit_unsent_media).
MEDIA_NOT_SENT_TEXT = "I couldn't show you the picture just now, sorry."
_ADMITS_NO_PICTURE = re.compile(
    r"\b(?:can(?:no|')t|could(?: not|n't)|unable to|not able to)\b[^.\n]{0,30}\b(?:show|send|share|display)\b",
    re.IGNORECASE,
)


def _hazard_sentences(summary: str) -> List[str]:
    """The lines or sentences of a description that carry a hazard term,
    for the ticket. Split on newlines and full stops so a bullet-point
    description and a prose one both come out as short quotes."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", summary) if p.strip()]
    return [p for p in pieces if check_safety_in_description(p).triggered]


_REFERENCE = re.compile(r"\b[A-Z]{2,4}-\d{3,}\b")


def _ticket_raised(turn: Any) -> bool:
    """Whether any tool this turn returned a ticket (create_support_ticket,
    raise_intake_ticket)."""
    for call in turn.tool_calls:
        result = call.get("result") or {}
        if isinstance(result, dict) and not is_error(result) and (result.get("data") or {}).get("ticket_id"):
            return True
    return False


def _ticket_known(text: str, state: ConversationState) -> bool:
    """Whether a claim may be about a ticket that exists: the conversation
    holds one, or the reply names a reference its context lists (earlier
    conversations' tickets)."""
    if state.ticket_id:
        return True
    return any(ref in (state.context_block or "") for ref in _REFERENCE.findall(text or ""))


def _is_image(attachment: Any) -> bool:
    return attachment.kind == "image" or (attachment.mime_type or "").startswith("image/")


# The agents whose asks to see something are fault evidence (video first).
_FAULT_AGENTS = (BATTERY_SUPPORT, MOTOR_SUPPORT, NARROW_SUPPORT)


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
        otp_verified_at: Optional[Callable[[str], Optional[str]]] = None,
        media_store: Any = None,
        verify_first: bool = False,
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
        # A customer who says the one bike listed is not theirs goes to
        # registration: the bike they mean is not on this number.
        self.triage = TriageAgent(TOPIC_AGENTS)
        # Verify first (the person's decision, 2026-09-30): an anonymous
        # customer proves their number and picks a bike before triage or any
        # model. Off unless asked for; the web chat API turns it on.
        self.verify_gate = VerifyFirst(self.registry, self.resolver, self.log) if verify_first else None

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
        # When a conversation proved its number by SMS code, for an erasure
        # request's proof (VerificationStore.verified_on).
        self.otp_verified_at = otp_verified_at
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
                navigation_gate=self._node_navigation,
                handoff_gate=self._node_handoff,
                erasure_gate=self._node_erasure,
                verify_gate=self._node_verify,
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
            if state.user_key is None and self.phone_resolver is not None:
                # A number verified during this very turn makes the conversation
                # that person's now, before it is saved: otherwise a visitor who
                # verified in their last message never gets a summary, their
                # next visit has no memory of it, and erasure by phone cannot
                # find it (2026-09-29).
                proven = self.phone_resolver(cid)
                if proven:
                    state.user_key = "PHONE#" + proven
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
            recorded, summary = message, self._summary_for(state, resolved)
            if "transcript_text" in reply.metadata:
                # The verify-first step's turns: the number or code replaced.
                recorded = replace(message, message_text=reply.metadata["transcript_text"])
                if not (resolved and resolved.may_disclose):
                    # Nobody is proved yet (a session that expired): the turn
                    # must not rewrite the earlier person's summary.
                    summary = None
            self.conversations.record_turn(state, recorded, reply, summary)
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
        # This turn's count is the newer one, and a decline stands.
        fresh.evidence_asks = ours.evidence_asks
        fresh.video_declined = fresh.video_declined or ours.video_declined
        if fresh.started_at == ours.started_at:  # an origin belongs to its run
            fresh.origin = fresh.origin or ours.origin
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
        if reply.handled_by.startswith("verify_first:"):
            # The verify-first step's progress is this turn's: a code went out
            # or was tried, so the next message is read against where this
            # turn left the step, not where the other server did. That
            # includes a context block this turn cleared for rebuilding.
            for name in VERIFY_FIRST_FIELDS:
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
        if self.verify_gate is not None and self.phone_resolver is not None:
            # A number this conversation has proved opens the identity here,
            # so the runtime does not depend on the API having done it.
            message = apply_proven_phone(message, self.phone_resolver(message.conversation_id))
        # Set by the web chat API (api.post_message): the identity cluster that
        # started this conversation, which a later upload is checked against,
        # and an agent the tester pinned. Applied here so they are saved with
        # the turn; an unknown pin is cleared in the persona step.
        meta = message.entry_metadata
        if state.cluster_id is None and meta.get("cluster_id"):
            state.cluster_id = meta["cluster_id"]
        choosing = self.verify_gate is not None and state.phase in (
            AWAITING_BIKE_SELECTION, AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION)
        if meta.get("pinned_agent") and not choosing:
            # A pin waits while the bike is being chosen (verify first).
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
        self._note_origin(message, state, resolved)

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
        return {"message": message, "conversation": state, "resolved": resolved}

    def _note_origin(self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity) -> None:
        """Where this run comes from, for reporting (spec 2026-10-01): set from
        its first message, then only filled in. An unknown country takes the
        country of a phone proven later; the record takes the person once
        known, so an erasure by phone finds a run that began anonymous. Never
        stops the turn: a failed write is logged and tried again next turn."""
        phone = resolved.identity.phone if resolved.identity.may_disclose else None
        current = state.origin
        if current is None:
            place = origin_place.choose(origin_place.place_from_dict(message.entry_metadata.get("origin")),
                                        origin_place.from_phone(phone))
            updated = dict(place.as_dict(), user_key=state.user_key)
        else:
            updated = dict(current)
            if current["country"] == origin_place.UNKNOWN.country:
                by_phone = origin_place.from_phone(phone)
                if by_phone is not None:
                    updated.update(by_phone.as_dict())
            if state.user_key and not current.get("user_key"):
                updated["user_key"] = state.user_key
            if updated == current:
                return
        record = dict(updated, _id=summary_key(state.conversation_id, state.started_at),
                      conversation_id=state.conversation_id, started_at=state.started_at, channel=message.channel)
        try:
            self.conversations.record_origin(record)
        except Exception as exc:
            self.log.emit("origin_record_failed", message.conversation_id, error=type(exc).__name__)
            return
        state.origin = updated

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

    def _node_navigation(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 1b. Going back (navigation.py, spec 2026-10-02): another number,
        #     another bike, the list again or a fresh start, from any step.
        #     Fixed replies, no model. Never while a deletion waits for DELETE:
        #     that answer has to be the very next message.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if resolved.persona != "customer" or state.erasure_step == erasure_rules.CONFIRMING:
            return {}
        text = message.message_text or ""
        if wants_change_number(text) and (state.phase not in _NUMBER_ASKED_BY_OTHERS or names_the_mobile(text)):
            # While a frame number is given or confirmed, or an agent is
            # talking, "wrong number" is about whatever was asked; only "my",
            # "mobile" or "phone" wording means the mobile (the final review).
            return self._navigate_number(message, state, resolved)
        bikes = resolved.bikes
        if not bikes or not resolved.may_disclose:
            # Not verified yet (the verify step answers), or no list to go back to.
            return {}
        if wants_start_over(text):
            return self._back_to_list(message, state, bikes, keep_topic=False, why="start_over")
        if state.phase in (AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION) and wants_list(text):
            return self._back_to_list(message, state, bikes, keep_topic=True, why="list")
        chosen = state.selected_frame is not None and state.phase in (AWAITING_ISSUE, ROUTED)
        if chosen and wants_change_bike(text):
            return self._back_to_list(message, state, bikes, keep_topic=True, why="change_bike")
        return {}

    def _navigate_number(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity
    ) -> Dict[str, Any]:
        cid = message.conversation_id
        proven = self.verify_gate is not None and self.phone_resolver is not None and bool(self.phone_resolver(cid))
        if proven:
            # Forget who they proved to be; the verify step starts again.
            self.verify_gate.store.reset(cid)
            state.forget_bike()
            state.pending_topic = state.pending_topic_source = None
            state.context_block = None
            state.verify_step, state.verify_masked = NUMBER, None
            state.move_to(GREETING, "change_number")
            self.log.emit("navigation", cid, to="number")
            # The verify step answers: a number in this message gets its code now.
            gate = self.verify_gate.handle(message, state)
            if gate.escalated:
                self.log.escalation(cid, "verification_locked", None)
            shown = replace(message, message_text=gate.model_text)
            return {"reply": self._finish(
                shown, state, gate.text, "verify_first:" + gate.outcome, escalated=gate.escalated,
                metadata={"transcript_text": gate.model_text},
            )}
        if resolved.may_disclose:
            # Signed in: the number is the sign-in's, with no code step to redo.
            self.log.emit("navigation", cid, to="number_fixed")
            fixed = NUMBER_FIXED_APP if message.channel in APP_SIGN_IN_CHANNELS else NUMBER_FIXED
            return {"reply": self._finish(message, state, fixed, "navigation:number_fixed")}
        # Not verified: the verify step answers (verify_first.VerifyFirst.handle).
        return {}

    def _back_to_list(
        self, message: InboundMessage, state: ConversationState, bikes: Sequence[Dict[str, Any]],
        keep_topic: bool, why: str,
    ) -> Dict[str, Any]:
        """Back to the bike list (spec 2026-10-02). The bike and what was
        learnt about it go; the problem stays unless they start over, so
        troubleshooting starts again for the bike they choose."""
        topic = (state.pending_topic or _TOPIC_OF_AGENT.get(state.agent or "")) if keep_topic else None
        source = (state.pending_topic_source or "text") if topic else None
        state.forget_bike()
        state.pending_topic, state.pending_topic_source = topic, source
        state.move_to(AWAITING_BIKE_SELECTION, why)
        self.log.emit("navigation", message.conversation_id, to="bike_list", why=why)
        lead = START_AGAIN if why == "start_over" else BACK_TO_LIST
        return {"reply": self._finish(message, state, lead + " " + which_bike_text(bikes), "navigation:" + why)}

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

    def _node_erasure(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 2b. Delete my data (erasure.py). A customer's request is recorded,
        #     never carried out here: a person deletes it with erasure_admin.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if resolved.persona != "customer":
            return {}
        text = message.message_text or ""
        # This turn's proven identity only: the conversation's saved owner
        # outlives a lapsed verification (the final review, 2026-10-01).
        user_key = self._user_key(resolved)
        if state.erasure_step == erasure_rules.CONFIRMING:
            # DELETE confirms only as the very next message, from a person
            # still proven. Anything later is an ordinary message.
            answering = user_key is not None and state.turns == (state.erasure_turn or 0) + 1
            state.erasure_step = None
            if answering:
                if erasure_rules.is_confirmation(text):
                    reply, label = self._erasure_request(message, state, user_key)
                    return {"reply": self._finish(message, state, reply, label)}
                self.log.emit("erasure_kept", message.conversation_id)
                return {"reply": self._finish(message, state, erasure_rules.ERASURE_KEPT, "erasure:kept")}
        cancel = erasure_rules.wants_cancel(text)
        if not cancel and not erasure_rules.wants_deletion(text):
            return {}
        if user_key is None:
            # Not verified: the verify step asks for the number, and its
            # verified reply carries this on (_node_verify).
            if self.verify_gate is not None and self.verify_gate.applies(resolved):
                state.erasure_step = erasure_rules.CANCEL_WANTED if cancel else erasure_rules.WANTED
            return {}
        reply, label = (self._erasure_cancel(message, state, user_key) if cancel
                        else self._erasure_offer(message, state, user_key))
        return {"reply": self._finish(message, state, reply, label)}

    def _erasure_offer(self, message: InboundMessage, state: ConversationState, user_key: str) -> Tuple[str, str]:
        try:
            pending = self.conversations.pending_erasure_of(user_key)
        except Exception as exc:
            return self._erasure_failed(message, state, exc)
        if pending is not None:
            return erasure_rules.ERASURE_EXISTING.format(reference=pending["_id"]), "erasure:existing"
        state.erasure_step = erasure_rules.CONFIRMING
        state.erasure_turn = state.turns
        return erasure_rules.ERASURE_CONFIRM, "erasure:confirm"

    def _erasure_request(self, message: InboundMessage, state: ConversationState, user_key: str) -> Tuple[str, str]:
        try:
            reference = self.conversations.request_erasure(
                user_key, message.channel, message.conversation_id, utc_now_iso(),
                proof=erasure_rules.proof_of(self._otp_verified_at(message.conversation_id, user_key)))
        except Exception as exc:
            return self._erasure_failed(message, state, exc)
        self.log.emit("erasure_requested", message.conversation_id, reference=reference)
        return erasure_rules.ERASURE_REQUESTED.format(reference=reference), "erasure:requested"

    def _otp_verified_at(self, conversation_id: str, user_key: str) -> Optional[str]:
        """When this chat proved the asking number by SMS code, or None: the
        identity then came from the app's sign-in."""
        if self.otp_verified_at is None or self.phone_resolver is None:
            return None
        if "PHONE#" + (self.phone_resolver(conversation_id) or "") != user_key:
            return None
        return self.otp_verified_at(conversation_id)

    def _erasure_cancel(self, message: InboundMessage, state: ConversationState, user_key: str) -> Tuple[str, str]:
        try:
            reference = self.conversations.cancel_erasure(user_key, utc_now_iso())
        except Exception as exc:
            return self._erasure_failed(message, state, exc)
        if reference is None:
            return erasure_rules.ERASURE_NOTHING_TO_CANCEL, "erasure:nothing_to_cancel"
        self.log.emit("erasure_cancelled", message.conversation_id, reference=reference)
        return erasure_rules.ERASURE_CANCELLED.format(reference=reference), "erasure:cancelled"

    def _erasure_failed(self, message: InboundMessage, state: ConversationState, exc: Exception) -> Tuple[str, str]:
        state.erasure_step = None
        self.log.emit("erasure_request_failed", message.conversation_id, error=type(exc).__name__)
        return erasure_rules.ERASURE_FAILED, "erasure:failed"

    def _node_verify(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 3. Verify first: an anonymous customer's number, code and bike, by
        #    fixed replies (verify_first.py). No model is called here.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if self.verify_gate is None or not self.verify_gate.applies(resolved):
            return {}
        gate = self.verify_gate.handle(message, state)
        if gate.escalated:
            self.log.escalation(message.conversation_id, "verification_locked", None)
        text = gate.text
        if gate.resolved is not None and state.erasure_step in (erasure_rules.WANTED, erasure_rules.CANCEL_WANTED):
            # Verified for a deletion or a cancel asked before the number:
            # that, not the bike list (spec 2026-10-01).
            wanted, state.erasure_step = state.erasure_step, None
            user_key = self._user_key(gate.resolved)
            if user_key is not None:
                follow, _ = (self._erasure_cancel(message, state, user_key) if wanted == erasure_rules.CANCEL_WANTED
                             else self._erasure_offer(message, state, user_key))
                text = CONFIRMED + " " + follow
        # The history gets the message with the number or code replaced, so
        # no model and no Jev call sees them; the transcript gets the same.
        shown = replace(message, message_text=gate.model_text)
        update: Dict[str, Any] = {"reply": self._finish(
            shown, state, text, "verify_first:" + gate.outcome, escalated=gate.escalated,
            metadata={"transcript_text": gate.model_text},
        )}
        if gate.resolved is not None:
            update["resolved"] = gate.resolved
        return update

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
        if message.entry_metadata.get("photos_unchecked"):
            # A photo the safety check could not answer: a model looks at it,
            # told so (_run), never a standard reply.
            return {"route": Route(path="full", reasons=("photo_unchecked",))}

        bike = self._selected_bike(resolved, state)
        decision, error = None, None
        if not message.message_text.strip():
            # A photo on its own: nothing to score. Stay on the record if there is one.
            error = EMPTY_MESSAGE
        else:
            jev_state = build_state(
                message.message_text, state.history, message.channel, bike=bike,
                current_sub_category=state.sub_category, redact=self._redaction_terms(resolved, state),
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

        # Only the pictures this server can send are listed (media.sendable).
        sendable = getattr(self.registry, "guide_media", {}) if SEND_GUIDE_MEDIA in self.registry.specs else {}
        definition = build_narrow_definition(record, prefetched, sendable=sendable)
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
        if state.unlisted_bike:
            # Not a listed bike, and never the one bike the customer rejected.
            return unlisted_as_bike(state.unlisted_bike)
        for bike in resolved.bikes:
            # By reference: a bike with no frame number on record would
            # otherwise match a conversation with no bike chosen.
            if state.selected_frame and bike_ref(bike) == state.selected_frame:
                return bike
        return resolved.single_bike

    @staticmethod
    def _redaction_terms(resolved: ResolvedIdentity, state: Optional[ConversationState] = None) -> List[str]:
        """Names and frame numbers that must not reach Jev, even inside recent turns."""
        terms: List[str] = []
        name = (resolved.profile or {}).get("name") or ""
        if name:
            terms.append(name)
            terms.extend(part for part in name.split() if len(part) > 2)
        terms.extend(term for bike in resolved.bikes for term in (bike.get("frame_number") or "", bike_ref(bike) or ""))
        if state is not None and state.unlisted_bike:
            # The frame number the customer typed for a bike not in the list.
            terms.append(state.unlisted_bike.get("frame_number") or "")
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
        # The unlisted bike stands in for the listed ones in what the agent is
        # told: never "owns 2 bikes, ask which", nor the rejected bikes' cover
        # (the final review, 2026-10-01).
        agent_view = replace(resolved, bikes=[unlisted_as_bike(state.unlisted_bike)]) if state.unlisted_bike else resolved
        context = ((without_bikes(state.context_block or "") if state.unlisted_bike else (state.context_block or ""))
                   + unlisted_context(state.unlisted_bike))
        if message.entry_metadata.get("photos_unchecked"):
            # This turn only (spec 2026-10-02).
            context += "\n\n" + photo_check.UNCHECKED_NOTE
        turn = agent.run(
            message, agent_view, state.history, context,
            # Conversation facts the order tool decides on. Lambdas, because
            # evidence_seen can flip during this very turn when a photo arrives
            # with the message that triggers the order.
            facts={
                "evidence_seen": lambda: state.evidence_seen,
                "coverage_result": lambda: state.coverage_result,
                # The ticket tool's check on a frame number the rider reads
                # off the sticker: only for the bike this conversation chose.
                "selected_bike": lambda: state.selected_frame,
                # A bike the customer gave because it is not in their list:
                # the ticket tool puts its frame number on a ticket.
                "unlisted_bike": lambda: state.unlisted_bike,
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
        # Remember this turn's lookup before checking (prefetched ones included),
        # so a claim made in the same turn as the lookup and a claim made three
        # turns later are judged against the same fact. Latest wins.
        for call in turn.tool_calls:
            if call["tool"] == LOOKUP_WARRANTY_RECORD and not is_error(call["result"]):
                state.coverage_result = call["result"]

        # The one-step cut runs before the post-checks, so they judge the text
        # the customer is sent. But the post-checks judge the model's own reply
        # first: a cut that dropped an unsupported "that's covered" would
        # otherwise leave no trace of it. When the original would be blocked it
        # is not cut, and the checks below block it exactly as they would
        # have, with the original as the suppressed text.
        if not any(check.blocked for check in self._post_checks(turn.text, turn, state)):
            turn = self._one_step(agent, message, state, turn)
        # After the cut, so the admission is never the part a cut removes.
        turn = self._admit_unsent_media(message, state, turn)
        if turn.escalate:
            self.log.escalation(
                message.conversation_id, turn.escalation_reason or "agent_requested_handover", turn.ticket_id
            )

        # The post-check: calling the warranty tool proved the tool ran, not that
        # the reply matches what it returned.
        coverage, order, evidence = self._post_checks(turn.text, turn, state)
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
        # is not the reply named the order it returned (see _post_checks).
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
        if evidence.blocked:
            self.log.guardrail(
                message.conversation_id, "evidence_post_check",
                {"reason": evidence.reason, "matched": evidence.matched},
            )
            # The reply is replaced, but a ticket the turn already raised still
            # exists: keep its id so it is tracked and gets the transcript.
            if state.evidence_asks >= MAX_EVIDENCE_ASKS:
                state.evidence_asks = 0  # a fresh count after the hand-over
                # Asked three times already, and the reply still concludes
                # with nothing seen: a person takes it from here, rather than the
                # customer being asked the same thing again.
                self.log.escalation(message.conversation_id, "evidence_not_forthcoming", turn.ticket_id)
                return self._finish(
                    message, state, HANDOVER_TEXT + self._already_done(turn), "guardrail:evidence_post_check",
                    escalated=True, ticket_id=turn.ticket_id,
                    metadata={"blocked_reason": evidence.reason, "suppressed_text": turn.text, "handover": True},
                    already_in_history=True,
                )
            state.evidence_asks += 1
            return self._finish(
                message, state, EVIDENCE_BLOCKED_MESSAGE + self._already_done(turn), "guardrail:evidence_post_check",
                ticket_id=turn.ticket_id,
                metadata={"blocked_reason": evidence.reason, "suppressed_text": turn.text},
                already_in_history=True,
            )

        # The backstop (spec 2026-10-01, revised): a reply that says a ticket
        # exists must have one. Advice is never acted on. About a live hazard,
        # the safety ticket is raised here; otherwise, or when it cannot be,
        # a person takes over, so the claim is never sent with nothing behind it.
        if (claims_ticket(turn.text) and not _ticket_raised(turn)
                and not _ticket_known(turn.text, state)):
            hazards = check_safety_in_description(turn.text).matched
            backed = self._safety_backstop(message, resolved, state, turn, hazards) if hazards else None
            if backed is not None:
                turn = backed
            else:
                self.log.guardrail(message.conversation_id, "ticket_promise_unbacked",
                                   {"suppressed_text": turn.text})
                self.log.escalation(message.conversation_id, "ticket_promise_unbacked", turn.ticket_id)
                return self._finish(
                    message, state, HANDOVER_TEXT + self._already_done(turn), "guardrail:ticket_promise_unbacked",
                    escalated=True, ticket_id=turn.ticket_id,
                    metadata={"suppressed_text": turn.text}, already_in_history=True,
                )

        # Only the erasure gate records a deletion and only a person makes one
        # (erasure_admin): a reply that says data was deleted is never sent.
        if claims_deletion(turn.text):
            self.log.guardrail(message.conversation_id, "deletion_claim", {"suppressed_text": turn.text})
            return self._finish(
                message, state, erasure_rules.ERASURE_NOT_BY_MODEL + self._already_done(turn),
                "guardrail:deletion_claim", ticket_id=turn.ticket_id,
                metadata={"suppressed_text": turn.text}, already_in_history=True,
            )

        # Video first (spec 2026-10-01-video-first-evidence-design.md): the
        # model decides when to ask to see something; every ask is a video
        # first, and three asks with nothing back hand the chat to a person.
        # Only the fault agents' asks (an invoice for late registration is not
        # fault evidence), and never a reply that tells the customer to stop:
        # nobody is asked to film a hazard, and a stop instruction is never
        # replaced by a hand-over (the final review, 2026-10-01).
        asked = asks_for_media(turn.text) if turn.agent in _FAULT_AGENTS else None
        if asked is not None and (evidence.reason == "safety_exempt"
                                  or check_safety_in_description(turn.text).triggered):
            asked = None
        if asked is not None:
            if state.evidence_asks >= MAX_EVIDENCE_ASKS:
                # A fresh count after the hand-over: the next message is not
                # handed over again for the same asks.
                state.evidence_asks = 0
                self.log.guardrail(message.conversation_id, "evidence_not_forthcoming",
                                   {"suppressed_text": turn.text})
                self.log.escalation(message.conversation_id, "evidence_not_forthcoming", turn.ticket_id)
                return self._finish(
                    message, state, HANDOVER_TEXT + self._already_done(turn), "guardrail:evidence_not_forthcoming",
                    escalated=True, ticket_id=turn.ticket_id,
                    metadata={"suppressed_text": turn.text, "handover": True}, already_in_history=True,
                )
            state.evidence_asks += 1
            added = added_line(turn.text, asked, state.video_declined)
            if added is not None:
                event, line = added
                text = turn.text.rstrip() + "\n\n" + line
                self.log.emit(event, message.conversation_id)
                replace_turn_text(state.history, text)
                turn = replace(turn, text=text)

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

    @staticmethod
    def _post_checks(
        text: str, turn: Any, state: ConversationState
    ) -> Tuple[CoverageCheck, OrderCheck, EvidenceCheck]:
        """The coverage, order and evidence verdicts on `text`, from this
        turn's tool results and what the conversation remembers.

        One place, because `_run` asks twice: of the model's own reply, to
        decide whether it may be cut at all, and of the text the customer
        would be sent. Pure: nothing is logged or changed here.

        The order check takes the conversation's placed orders as well as
        this turn's, the way coverage takes `state.coverage_result`, so a
        claim about an order placed three turns ago is not blocked as
        unsupported; an id from another conversation still is.
        """
        results = [call["result"] for call in turn.tool_calls]
        if state.coverage_result is not None:
            results = results + [state.coverage_result]
        coverage = check_coverage_claim(text, results)
        if state.unlisted_bike and claims_coverage(text):
            # No record of this bike on the number (spec 2026-10-01, unlisted
            # bike): the listed bikes' cover says nothing about it.
            coverage = CoverageCheck(blocked=True, reason="unlisted_bike", claimed="coverage", actual="no record")
        order = check_order_claim(
            text,
            [call["result"] for call in turn.tool_calls]
            + [{"data": {"order_id": order_id}} for order_id in state.placed_order_ids],
        )
        evidence = check_evidence(text, state.evidence_seen)
        return coverage, order, evidence

    def _raise_safety_ticket(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity, description: str
    ) -> Optional[str]:
        """The priority safety ticket, one per conversation run: the safety
        gate's, and the backstop's when a reply gave safety instructions
        without one (spec 2026-10-01). The ticket id, or None."""
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
        # The chosen bike goes as the conversation's choice, not as a
        # frame number: if this turn's bike list no longer holds it (Amigo
        # stopped answering since it was chosen), the ticket is raised
        # without a bike, naming it here, rather than refused.
        late: Dict[str, Any] = {}
        if state.unlisted_bike:
            late["unlisted_bike"] = lambda: state.unlisted_bike
        if state.selected_frame:
            late["selected_bike"] = lambda: state.selected_frame
            if state.selected_bike_label:
                arguments["description"] += " The rider's bike: %s." % state.selected_bike_label
        elif resolved.single_bike and not state.unlisted_bike:
            # Never the one listed bike when the customer said theirs is not it.
            arguments["frame_number"] = bike_ref(resolved.single_bike)

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
                late=late,
            ),
            run_without_idempotency=True,
        )
        self.log.tool_call(
            message.conversation_id, CREATE_SUPPORT_TICKET, {"category": "battery_safety"}, envelope,
            duration_ms=int(round((time.monotonic() - started) * 1000)),
        )
        return None if is_error(envelope) else envelope["data"]["ticket_id"]

    def _safety_backstop(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState, turn: Any,
        hazards: List[str],
    ) -> Any:
        """A reply claimed a ticket about a live hazard and none exists: raise
        the safety ticket here and add its reference. The model wrote "I'm
        raising an urgent support ticket" about a smoking battery and raised
        nothing (staging, 2026-10-01). None when it cannot be backed (no known
        customer, or the ticket failed): the caller then hands over."""
        if not resolved.identity.phone:
            return None
        description = (
            "Automatic safety escalation: the assistant said a ticket was raised about a safety hazard, "
            "and none was. "
            "Customer wrote: %s. Assistant replied: %s. Matched safety indicators: %s."
            % (message.message_text or "(a photo or video, no text)", turn.text, ", ".join(hazards))
        )
        try:
            ticket_id = self._raise_safety_ticket(message, state, resolved, description)
        except Exception as exc:
            self.log.emit("safety_backstop_failed", message.conversation_id, error=type(exc).__name__)
            return None
        if not ticket_id:
            self.log.emit("safety_backstop_failed", message.conversation_id, error="no_ticket")
            return None
        self.log.guardrail(message.conversation_id, "safety_backstop", hazards)
        self.log.escalation(message.conversation_id, "safety_backstop", ticket_id)
        text = turn.text or ""
        if ticket_id not in text:
            text = text.rstrip() + "\n\nI have raised this as a priority safety case, reference %s." % ticket_id
            replace_turn_text(state.history, text)
        return replace(turn, text=text, ticket_id=ticket_id, escalate=True)

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
                description += " Seen in the customer's photo or video: %s" % " ".join(evidence)
            ticket_id = self._raise_safety_ticket(message, state, resolved, description)

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
            # Every photo, fetched, so it stays in history as a photo (the
            # final review: unfetched, a stored photo read "could not be
            # retrieved" on every later turn); a video only by its text.
            kept = [a for a in message.attachments if a.summary or _is_image(a)]
            content = user_content(replace(message, attachments=kept), self.fetch)
            state.history.append({"role": "user", "content": content})
            self._note_customer_turn(message, state, content)
        return self._finish(
            message, state, text, "guardrail:battery_safety",
            escalated=True, ticket_id=ticket_id, metadata={"matched": matched},
            already_in_history=bool(evidence),
        )

    def _admit_unsent_media(self, message: InboundMessage, state: ConversationState, turn: Any) -> Any:
        """The backstop for GUIDE_MEDIA_RULE: a guide picture the model asked
        for failed and nothing went out, and the reply does not say so. One
        plain sentence is added, so the customer is never left looking for a
        picture that was described but never sent (2026-09-29)."""
        failed = [call["arguments"].get("key") for call in turn.tool_calls
                  if call.get("tool") == SEND_GUIDE_MEDIA and is_error(call.get("result") or {})]
        if (not failed or turn.attachments or turn.escalation_reason == "model_unavailable"
                or _ADMITS_NO_PICTURE.search(turn.text or "")):
            return turn
        text = (turn.text or "").rstrip() + "\n\n" + MEDIA_NOT_SENT_TEXT
        replace_turn_text(state.history, text)
        self.log.emit("guide_media_not_sent", message.conversation_id, keys=failed)
        return replace(turn, text=text)

    def _one_step(self, agent: Agent, message: InboundMessage, state: ConversationState, turn: Any) -> Any:
        """The backstop for the one-step rule (one_step.py): a reply that runs
        over the limits is cut, once, by the model that wrote it.

        Left alone: an agent that opts out (dealer), text written by code (the
        model-outage handover), and a reply that carries a caution or a hazard
        word (guardrails.carries_caution), which goes out whole. Whatever
        happens is logged by counts, never the text; the transcript has the
        text.
        """
        cid = message.conversation_id
        if (
            not agent.definition.one_step
            or turn.escalation_reason == "model_unavailable"
            or not is_too_long(turn.text)
            or carries_caution(turn.text)
        ):
            return turn
        before = {"agent": turn.agent, "words_before": one_step_words(turn.text),
                  "sentences_before": one_step_sentences(turn.text)}
        started = time.monotonic()
        self.log.llm_request(cid, "one_step", 0)
        try:
            cut, reason, response = shorten_reply(agent.llm, turn.text)
        except Exception as exc:
            # The cut is optional; the reply is not. Logged, never silent.
            self.log.emit("reply_too_long", cid, reason="shorten_failed", error=type(exc).__name__, **before)
            return turn
        self.log.llm_turn(
            cid, "one_step", 0, getattr(response, "stop_reason", None), getattr(response, "usage", None),
            model=getattr(response, "model", None) or getattr(agent.llm, "model", None),
            duration_ms=int(round((time.monotonic() - started) * 1000)),
        )
        if cut is None:
            self.log.emit("reply_too_long", cid, reason=reason, **before)
            return turn
        replace_turn_text(state.history, cut)
        self.log.emit("reply_shortened", cid, words_after=one_step_words(cut),
                      sentences_after=one_step_sentences(cut), **before)
        return replace(turn, text=cut)

    def _note_customer_turn(self, message: InboundMessage, state: ConversationState, content: Any) -> None:
        """Evidence from the customer's turn as it was built for the model.

        Every place that builds that turn calls this with what it built: the
        agent loop (before its first model call), the short-circuit replies
        and the safety branch. A photo or video counts only when the model is
        shown it (attachments.shows_media); one that could not be fetched or
        read, or a PDF, does not (the person's decision, 2026-09-29).
        """
        if declines_video(message.message_text):
            state.video_declined = True
        if shows_media(content):
            state.evidence_seen = True
            # Something to see arrived: the asks start again (video first).
            state.evidence_asks = 0
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
