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
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

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
from .contract import VERIFIED, Attachment, InboundMessage, Reply
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
from .digits import ascii_digits
from .disclosure import apply_disclosure
from .enrichment import ContextEnricher, summarise_past, without_bikes
from .graph import TurnNodes, build_turn_graph
from .guardrails import (
    COVERAGE_BLOCKED_MESSAGE,
    EVIDENCE_BLOCKED_MESSAGE,
    HANDOFF_MESSAGE,
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_GONE_MESSAGE,
    HANDOVER_NO_NUMBER_MESSAGE,
    HANDOVER_NOT_RECORDED_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
    ORDER_BLOCKED_MESSAGE,
    SAFETY_ADDED_MESSAGE,
    SAFETY_ASK_AGAIN_MESSAGE,
    SAFETY_EMERGENCY,
    SAFETY_MESSAGE,
    SAFETY_NO_CONTACT_MESSAGE,
    SAFETY_NOT_RECORDED_MESSAGE,
    SAFETY_STEPS,
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
    ticket_source_key,
)
from .tools.registry import ToolContext, ToolRegistry, is_error
from .tools.verification import (
    FIND_ACCOUNT_BY_CODE,
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    apply_proven_phone,
)
from .tickets.caps import CAP_TEXTS, cap_reached
from .tickets.clock import now_iso, parse, plus
from .tickets.kinds import is_desk_reference, is_urgent
from .tickets.record import GONE
from .triage import TriageAgent, bike_ref, unlisted_as_bike, unlisted_context, which_bike_text
from .verify_first import (
    CODE,
    CONFIRMED,
    INVALID_NUMBER,
    NUMBER,
    VerifyFirst,
    find_code,
    find_phone,
    looks_like_a_number,
    redact,
)
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


def _safety_scan(message: InboundMessage) -> Tuple[List[str], List[str]]:
    """The safety terms a message trips, and the hazard sentences of any clip
    description that tripped them. The typed text gets the plain scan. A
    video's description (Attachment.summary, written by the video analyser at
    ingest) gets the negation-aware one, because an analyser that writes "no
    smoke visible" is describing a safe clip. One function, so the safety gate
    and the store-down reply judge a message the same way."""
    matched = list(check_safety(message.message_text).matched)
    evidence: List[str] = []
    for attachment in message.attachments:
        if attachment.summary:
            verdict = check_safety_in_description(attachment.summary)
            matched += [m for m in verdict.matched if m not in matched]
            if verdict.triggered:
                evidence += _hazard_sentences(attachment.summary)
    return matched, evidence


def _again_note(matched: Sequence[str]) -> str:
    """The note a later safety report in the same run adds to its ticket. Built
    by code, with no customer text: the transcript carries their words."""
    return "Customer reported a safety issue again. Matched safety indicators: %s." % ", ".join(matched)


# Added to the safety reply when its ticket is raised or found.
_PRIORITY_CASE = "I have raised this as a priority safety case, reference %s."


def _safety_idempotency_key(conversation_id: str, started_at: Optional[str]) -> str:
    """The safety branch's key for create_support_ticket: one ticket per run of
    the conversation. Repeats inside a run share it, and a thread that returns
    after its working state expired (48 h; receipts live 7 days) raises a new
    one rather than being quoted the old reference (the person's decision,
    2026-09-29). A run is the id and its start, as for summaries."""
    return "safety:%s:%s" % (conversation_id, started_at or "")


# The purposes of the tickets the runtime's gates record (spec 2026-10-05,
# section 6). Each is the last part of the source key
# "<conversation_id>:<started_at>:<purpose>", so a run gets one ticket per need
# and a retry gets the same one.
PURPOSE_SAFETY = "safety_callback"
PURPOSE_HANDOVER = "handover"
PURPOSE_LOCKOUT = "lockout"
# Added to `state.transitions`, with the cap after a colon, when a cap on
# unverified tickets refused the run's lock-out ticket (Runtime._record_lockout).
LOCKOUT_CAPPED = "lockout_capped"


class Recorded(NamedTuple):
    """What a gate's recording came to: the reference, or None when nothing
    was recorded. When a cap on unverified tickets refused it, `refusal` is
    the text to send instead, so the caller promises nothing."""

    reference: Optional[str]
    refusal: Optional[str] = None


class TypedNumber(NamedTuple):
    """A call-back number read from a customer's message (read_number)."""

    number: Optional[str]  # ten digits: the first valid Indian mobile, or None
    shown: str  # the text as the model's history and the transcript keep it
    attempted: bool  # no valid mobile, but something that looked like a number


# Our ticket, booking and order references, which a customer may quote. They
# are blanked at the same length before a number is read, so a quoted
# EM-1000001 is neither a mobile nor a failed try at one. Read after
# ascii_digits, so [0-9] covers digits of any script. The left side checks for
# ASCII letters and digits on purpose: no \b next to text that may be
# Devanagari.
_QUOTED_REFERENCE = re.compile(r"(?<![A-Za-z0-9])(?:EM|BK|RO)-[0-9]+", re.IGNORECASE)
# A try at a number: seven or more digits, however spaced. This is the shape
# verify_first.looks_like_a_number counts.
_NUMBER_TRY = re.compile(r"\+?[0-9][0-9 \-]{5,}[0-9]")


def read_number(text: str) -> TypedNumber:
    """The call-back number in a customer's message (spec 2026-10-05, section 6).

    Digits in any script are read as ASCII first (Devanagari ९८७६…), quoted
    references are set aside, then the first valid Indian mobile is taken
    (verify_first.find_phone). In the text that is kept, the number becomes
    [phone] and a try that is not a valid Indian mobile becomes [number]. A
    message with neither is kept as typed."""
    plain = ascii_digits(text or "")
    probe = _QUOTED_REFERENCE.sub(lambda m: " " * len(m.group()), plain)
    found = find_phone(probe)
    if found is not None:
        number, span = found
        return TypedNumber(number, redact(plain, span, "[phone]"), False)
    if not looks_like_a_number(probe):
        return TypedNumber(None, text or "", False)
    shown = plain
    tries = [m.span() for m in _NUMBER_TRY.finditer(probe) if sum(ch.isdigit() for ch in m.group()) >= 7]
    for start, end in reversed(tries):
        shown = shown[:start] + "[number]" + shown[end:]
    return TypedNumber(None, shown, True)


def _as_shown(message: InboundMessage, typed: TypedNumber) -> InboundMessage:
    """The message as the model's history and the transcript keep it."""
    return message if typed.shown == (message.message_text or "") else replace(message, message_text=typed.shown)


def _code_hidden(message: InboundMessage) -> InboundMessage:
    """The message with a six-digit verification code in it kept as [code],
    digits of any script read as ASCII first (verify_first.find_code), as the
    verify step keeps one. The message itself when there is none."""
    plain = ascii_digits(message.message_text or "")
    found = find_code(plain)
    if found is None:
        return message
    return replace(message, message_text=redact(plain, found[1], "[code]"))


def _live(*records: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The first ticket record that exists and is not gone (deleted or merged
    in Desk): a gone one is never quoted."""
    return next((record for record in records if record is not None and record.get("state") != GONE), None)


_REFERENCE = re.compile(r"\b[A-Z]{2,4}-\d{3,}\b")


def _ticket_raised(turn: Any) -> bool:
    """Whether any tool this turn returned a ticket (create_support_ticket,
    raise_intake_ticket, submit_warranty_proof)."""
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

# Look-up failures a ticket's coverage names (spec 2026-10-05, section 3).
# Any other failure says nothing about the customer's cover.
LOOKUP_ERRORS = ("no_warranty_record", "oms_unavailable")

# The cover states only a bike from the rider's app has (tools/mocks._coverage
# of tools/amigo.amigo_records). Its record holds the app's username, never the
# OMS customer name, so a ticket about it names nobody.
_APP_BIKE_STATES = ("not_registered", "warranty_unknown", "warranty_unavailable")

# What a turn reads from the customer or a look-up, the stretch of the run
# someone else is writing (Runtime._note_speaker) and where the run's own
# person's stretch began after it (Runtime._owner_start), carried when the turn
# loses a save race and changed it (Runtime._merge_onto_fresh).
TURN_FACT_FIELDS = ("typed_number", "lookup_error", "awaiting_callback", "callback_asks", "last_code_phone",
                    "newcomer_started_at", "newcomer_ticket_id", "owner_started_at")


def _ticket_bike(bikes: Sequence[Dict[str, Any]], selected: Optional[str]) -> Optional[Dict[str, Any]]:
    """The chosen bike by its reference, or the only bike; None with several
    and none chosen."""
    if selected:
        return next((bike for bike in bikes if bike_ref(bike) == selected), None)
    return bikes[0] if len(bikes) == 1 else None


def coverage_fact(state: ConversationState, resolved: Optional[ResolvedIdentity] = None) -> Optional[str]:
    """What a ticket says about cover, worked out in code (spec 2026-10-05,
    section 3).

    The coverage_status of the bike the ticket is about, the chosen bike or
    the only one: from the conversation's last warranty look-up first, then
    from this turn's identity look-up (the safety branch fires before any
    agent has looked). Failing both, the run's last look-up error, then this
    turn's. None when nothing is known, and always None for a bike the
    customer gave as not in their list: the listed bikes' cover says nothing
    about it.
    """
    if state.unlisted_bike:
        return None
    looked_up = ((state.coverage_result or {}).get("data") or {}).get("bikes") or []
    for bikes in (looked_up, list(resolved.bikes) if resolved is not None else []):
        bike = _ticket_bike(bikes, state.selected_frame)
        if bike is not None and bike.get("coverage_status"):
            return bike["coverage_status"]
    if state.lookup_error:
        return state.lookup_error
    if resolved is not None and resolved.error in LOOKUP_ERRORS:
        return resolved.error
    return None


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
        self.verify_gate = (VerifyFirst(self.registry, self.resolver, self.log, record_lockout=self._record_lockout)
                            if verify_first else None)

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
        # Safety reports whose ticket could not be recorded since this server
        # started (safety_ticket_not_recorded), shown on /health when above 0.
        self.safety_not_recorded = 0
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
                callback_gate=self._node_callback,
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

    def _identity_strength(self, conversation_id: str, resolved: ResolvedIdentity) -> str:
        """How well the phone a ticket tool is given is known (spec 2026-10-05,
        section 2). The channel's phone keeps the channel's strength: verified
        for WhatsApp, an app sign-in or a code, asserted for caller ID. With
        no channel phone, the tools get the one this conversation proved by a
        code (Agent._late_facts), which is verified. Nobody proved: the
        identity's own strength."""
        identity = resolved.identity
        if identity.phone or self.phone_resolver is None:
            return identity.strength
        return VERIFIED if self.phone_resolver(conversation_id) else identity.strength

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
        if name == LOOKUP_WARRANTY_RECORD:
            self._remember_coverage(state, envelope)
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
        # The intake tool refuses past the caps itself (tickets/caps.py). The
        # alarmed event is logged here, where there is a log.
        if (name == RAISE_INTAKE_TICKET and is_error(envelope)
                and (envelope.get("error") or {}).get("code") == "unverified_ticket_capped"):
            self.log.emit("unverified_ticket_capped", state.conversation_id, kind="intake", level="error")

    @staticmethod
    def _remember_coverage(state: ConversationState, envelope: Dict[str, Any]) -> None:
        """A warranty look-up's answer. One that worked is the conversation's
        cover and clears any failure; a failure that says something about the
        customer (LOOKUP_ERRORS) is kept for a ticket's coverage, because
        coverage_result keeps only answers that worked. Other failures change
        nothing."""
        if not is_error(envelope):
            state.coverage_result = envelope
            state.lookup_error = None
        elif (envelope.get("error") or {}).get("code") in LOOKUP_ERRORS:
            state.lookup_error = envelope["error"]["code"]

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
            # What the turn may change that a merge must not lose or overwrite.
            facts_loaded = {name: getattr(state, name) for name in TURN_FACT_FIELDS}
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
            owns = self._speaker_owns_run(state, resolved)
            stretch_ended = self._note_speaker(message, state, reply, owns)
            state.escalated = state.escalated or reply.escalated
            this_turn = list(state.history[history_mark:])  # before the save trims anything
            try:
                self.conversations.save(state)
                break
            except ConversationConflict:
                self.log.emit("conversation_conflict", cid, attempt=attempt)
                if self._side_effects_since(log_mark, cid):
                    try:
                        state = self._merge_onto_fresh(
                            state, this_turn, reply, looked_up=state.coverage_result != coverage_loaded,
                            loaded=facts_loaded, owner=owns,
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
        # After the save and before the record: a worker pass in between must
        # never find this run's first turn inside an earlier run's bounds, nor
        # a newcomer's turn inside the run of the person before them, nor the
        # run's own person's turn inside a newcomer's.
        self._close_earlier_runs(state)
        self._end_run_before_this_turn(state, owns, stretch_ended)
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
        self._attach_transcript(state, reply, owns)
        return reply

    def _close_earlier_runs(self, state: ConversationState) -> None:
        """On a run's first turn (a fresh state, or restart_for), the ticket
        seam marks where this conversation's earlier runs ended, so their
        tickets take none of this run's turns or files (spec 2026-10-05,
        section 3)."""
        tickets = getattr(self.registry, "tickets", None)
        if state.turns != 1 or not state.started_at or not hasattr(tickets, "close_runs"):
            return
        try:
            tickets.close_runs(state.conversation_id, state.started_at)
        except Exception as exc:  # the turn happened; the log says the earlier runs stay open
            self.log.emit("close_runs_failed", state.conversation_id, error=type(exc).__name__)

    def _speaker_owns_run(self, state: ConversationState, resolved: Optional[ResolvedIdentity]) -> bool:
        """Whether this turn comes from the person the run belongs to: the run
        has no proven owner, or this turn proved the same person, through the
        channel or by a code in this chat."""
        if state.user_key is None:
            return True
        if resolved is not None and self._user_key(resolved) == state.user_key:
            return True
        proven = self.phone_resolver(state.conversation_id) if self.phone_resolver is not None else None
        return bool(proven) and "PHONE#" + proven == state.user_key

    def _speakers_ticket(self, state: ConversationState, resolved: Optional[ResolvedIdentity]) -> Optional[str]:
        """The ticket of the person writing: the run's, for the run's own
        person; for anyone else, the one recorded in their own stretch of the
        run (_note_speaker), never the run's."""
        if self._speaker_owns_run(state, resolved):
            return state.ticket_id
        return state.newcomer_ticket_id

    def _note_speaker(
        self, message: InboundMessage, state: ConversationState, reply: Reply, owns: bool,
    ) -> Optional[str]:
        """Whose stretch of the run this turn is, kept with the state before it
        is saved (the review of Task 13, Important 1 and 2).

        Someone other than the run's own person: a ticket of theirs is kept
        apart (`newcomer_ticket_id`), never as the run's `ticket_id`, so the
        run's own person's turns never wake it and their history never names
        it. Their stretch keeps the start its first turn set
        (_newcomer_start), so its keys repeat on every turn of it: one safety
        ticket holds for them as for anyone, and their own next turn never
        ends their ticket.

        The run's own person: their ticket is the run's. If someone else wrote
        since, their stretch ends here. Its start and ticket are cleared, and
        so is a wait for a number to call about their report, so this
        person's message is never read as that number. A wait still there was
        not ended by the callback gate: this person's own report answered
        first (the safety gate runs before it). So it ends here, through
        _end_wait, and the other person's safety report, left with no ticket,
        is alarmed and counted as `owner_returned` (Fixer A's concern in the
        final fix wave). The time the stretch
        ends, just after its last turn, is where the run's own person's own
        stretch begins (_owner_start), and is returned for
        _end_run_before_this_turn to end the other person's tickets before
        this turn is recorded. None when no stretch ends."""
        if not owns:
            self._newcomer_start(message, state)
            state.newcomer_ticket_id = reply.ticket_id or state.newcomer_ticket_id
            return None
        state.ticket_id = reply.ticket_id or state.ticket_id
        if state.newcomer_started_at is None:
            return None
        ended = self._owner_start(message, state)
        state.newcomer_started_at = state.newcomer_ticket_id = None
        if state.awaiting_callback is not None:
            self._end_wait(message.conversation_id, state, "owner_returned")
        state.awaiting_callback, state.callback_asks = None, 0
        return ended

    def _attach_transcript(self, state: ConversationState, reply: Reply, owns: bool) -> None:
        """The run's ticket gets the run's thread, this turn included, on every
        turn of the run, not only the one that raised it, so a person sees
        what was said after the ticket too (spec 2026-10-05, section 2).

        Only this run's turns, those numbered after `turn_offset`: an earlier
        run of this conversation id, which may be someone else's
        (restart_for), never reaches it. And only while the person writing is
        the run's own: once their proof lapses, the next visitor's words reach
        no ticket of theirs (_end_run_before_this_turn has ended its Desk
        record's run before them). The next visitor's own ticket is woken
        instead (_wake_newcomers_ticket)."""
        tickets = getattr(self.registry, "tickets", None)
        if not hasattr(tickets, "attach_transcript"):
            return
        if not owns:
            self._wake_newcomers_ticket(state, tickets)
            return
        ticket_id = reply.ticket_id or state.ticket_id
        if not ticket_id:
            return
        cid = state.conversation_id
        try:
            turns = [turn for turn in self.conversations.transcript(cid) if turn.n > state.turn_offset]
            tickets.attach_transcript(ticket_id, render_transcript(turns))
        except Exception as exc:  # the ticket exists either way; say the thread did not reach it
            self.log.emit(
                "transcript_attach_failed", cid,
                ticket_id=ticket_id, error="%s: %s" % (type(exc).__name__, exc),
            )

    def _end_run_before_this_turn(self, state: ConversationState, owns: bool, stretch_ended: Optional[str]) -> None:
        """Where one person's part of the run ends and the next person's
        begins, set on the Desk records after the save and before the turn is
        recorded, so no worker pass in between finds this turn inside the
        wrong person's run (the ruling for Task 13). The worker posts by time,
        so a record's end is what keeps the next person's words and photos
        from it. With Zoho off there is no Desk record to end.

        Someone other than the run's own person is writing, and nobody has
        proved a number since, so restart_for has not begun a run of theirs.
        The run's Desk records end where their stretch of it starts
        (_newcomer_start; spec 2026-10-05, section 3, and the plan's
        cross-check, finding 9). Their own tickets start at that same
        instant, so no turn of their stretch ever ends them.

        The run's own person writing again after them ends that stretch's
        records just after its last turn (`stretch_ended`, from
        _note_speaker), so none of the run's own person's turns reach a
        ticket of the newcomer's (the review of Task 13, Important 1). Their
        own tickets from then on start at that same instant (_owner_start),
        and close_runs ends only records that started before the end it
        sets, so a ticket this very turn recorded for them keeps no end and
        takes their report (the final review, safety-flow Important 1).

        A record that has an end keeps it."""
        ended = stretch_ended if owns else state.newcomer_started_at
        if ended is None or self._desk_store() is None:
            return
        try:
            self.registry.tickets.close_runs(state.conversation_id, ended)
        except Exception as exc:  # the turn happened; the log says the run stays open
            self.log.emit("close_runs_failed", state.conversation_id, error=type(exc).__name__)

    def _newcomer_start(self, message: InboundMessage, state: ConversationState) -> str:
        """Where the run ends for someone who does not own it, and their own
        tickets' run begins: set on the first turn of their stretch of the run
        (_after_last_turn) and kept with the state, so every later turn of the
        stretch has the same start, its tickets the same keys and the same
        bounds (the review of Task 13, Important 2). Whichever comes first in
        the turn sets it: a gate's ticket (_ticket_run_start) or
        _note_speaker. The run's own person writing again clears it."""
        if state.newcomer_started_at is None:
            state.newcomer_started_at = self._after_last_turn(message, state)
        return state.newcomer_started_at

    def _after_last_turn(self, message: InboundMessage, state: ConversationState) -> str:
        """Just after the run's last turn before this one: where one person's
        stretch of the run ends and the next person's begins. Not at this
        turn: the API records a photo sent with a message before the turn runs
        (api._persist_media), so that photo is older than the turn and belongs
        to the person who sent it. With no earlier turn of the run on record,
        when this message arrived.

        Read before this turn is recorded, so every caller in the turn sees
        the same transcript and gets the same time."""
        cid = state.conversation_id
        this_turn = state.turn_offset + state.turns * 2 - 1  # its customer line (transcript_turns)
        try:
            before = [turn for turn in self.conversations.transcript(cid)
                      if state.turn_offset < turn.n < this_turn]
            if before:
                return plus(before[-1].at, 0.000001)
        except Exception as exc:  # the class only; the arrival time stands in
            self.log.emit("transcript_read_failed", cid, error=type(exc).__name__)
        try:
            return plus(message.timestamp, 0)
        except (TypeError, ValueError) as exc:  # the server sets it, so never expected
            self.log.emit("message_time_unreadable", cid, error=type(exc).__name__)
            return now_iso()

    def _ticket_run_start(
        self, message: InboundMessage, state: ConversationState, resolved: Optional[ResolvedIdentity],
    ) -> Optional[str]:
        """The run a ticket belongs to, by its start (the ruling for Task 13):
        a gate's, the safety branch's or one an agent's tool raises. For the
        run's own person, the start of their stretch of it (_owner_start):
        the run's start, or where someone else's stretch of it ended. For
        someone else writing in it before restart_for (a second person on a
        shared browser, once the first person's proof lapsed), the start of
        their own stretch of it, where the run ends for them
        (_newcomer_start). Never the other person's start: the worker posts a
        record's turns by its run's times, and the ticket keys hold the start,
        so neither person's turns reach the other's ticket and neither's
        tickets are found by the other's keys."""
        if self._speaker_owns_run(state, resolved):
            return self._owner_start(message, state)
        return self._newcomer_start(message, state)

    def _owner_start(self, message: InboundMessage, state: ConversationState) -> Optional[str]:
        """Where the run's own person's tickets begin (the final review,
        safety-flow Important 1). The run's start, until someone else's
        stretch of it ends. Then the end of that stretch, just after its last
        turn and never at or before its start, so even a stretch begun and
        ended in one turn ends after its own tickets begin. It is set on the
        turn the run's own person writes again, by whichever comes first: a
        ticket in that turn (_ticket_run_start) or _note_speaker after it, and
        kept with the state. So every ticket of theirs from then on, in that
        turn or a later one, takes none of the other person's turns or
        photos, and their keys, receipts and one safety ticket are per
        stretch, as restart_for makes them per run."""
        began = state.newcomer_started_at
        if began is not None:
            ended = state.owner_started_at
            # Set already in this turn, unless it is the end of an earlier
            # stretch, which is before this one's start.
            if ended is None or parse(ended) <= parse(began):
                state.owner_started_at = max(
                    self._after_last_turn(message, state), plus(began, 0.000001), key=parse)
        return state.owner_started_at or state.started_at

    def _wake_newcomers_ticket(self, state: ConversationState, tickets: Any) -> None:
        """The ticket recorded for someone who does not own the run, in their
        stretch of it (_note_speaker), is woken on every turn of the stretch,
        as the run's own ticket is on every turn of the run, so the worker
        sends their latest turn after this reply rather than later. Desk
        ignores the text: the worker posts the ticket's own turns by its
        run's times. Only a record whose start is the stretch's own is woken,
        so the run's own ticket never is."""
        ticket_id, began = state.newcomer_ticket_id, state.newcomer_started_at
        store = self._desk_store()
        if store is None or not is_desk_reference(ticket_id) or began is None:
            return
        try:
            record = store.get(ticket_id)
            if record is not None and record.get("started_at") == began:
                tickets.attach_transcript(ticket_id, "")
        except Exception as exc:  # the ticket exists either way; the worker's first pass sends it
            self.log.emit("transcript_attach_failed", state.conversation_id,
                          ticket_id=ticket_id, error=type(exc).__name__)

    def _merge_onto_fresh(
        self, ours: ConversationState, this_turn: List[Dict[str, Any]], reply: Reply, looked_up: bool = False,
        loaded: Optional[Dict[str, Any]] = None, owner: bool = True,
    ) -> ConversationState:
        """Add a turn that lost the save race, and did something, to the state
        the other server saved. Its words and its ticket are kept; the routing
        the other server saved stands. One attempt: a second clash hands over.
        The ticket is the run's only when the run's own person wrote this turn
        (`owner`); anyone else's is kept apart with their stretch of the run,
        among the turn's facts (_note_speaker)."""
        fresh = self.conversations.get(ours.conversation_id)
        fresh.turns += 1
        # The other server's copy gets the same window this turn's did.
        fresh.history[:] = trim_history(fresh.history, HISTORY_TURNS - 1)
        fresh.history.extend(this_turn)
        fresh.escalated = fresh.escalated or ours.escalated
        if owner:
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
        # What this turn read and kept (spec 2026-10-05, section 6): a number
        # the customer typed, a failed look-up, a wait for a number to call,
        # the number a code went to. Where this turn changed one, its value
        # stands; where it did not, the other server's does. With nothing
        # loaded to compare (a direct caller), this turn's stands.
        for name in TURN_FACT_FIELDS:
            if loaded is None or getattr(ours, name) != loaded.get(name):
                setattr(fresh, name, getattr(ours, name))
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
        ticket the turn already raised is named, so the customer can quote it.

        A safety report is the exception (spec 2026-10-05, section 6). With
        the store down no safety ticket could be recorded, so it gets the
        safety steps and 112 and no promise of a call. Unless the turn already
        raised a ticket, this is logged as safety_ticket_not_recorded, which
        is alarmed."""
        cid = message.conversation_id
        self.log.emit("store_unavailable", cid, error=str(exc))
        reference = "\n\nYour reference is %s." % ticket_id if ticket_id else ""
        matched, _ = _safety_scan(message)
        metadata: Dict[str, Any] = {}
        if matched:
            if ticket_id:
                self.log.escalation(cid, "store_unavailable", ticket_id)
            else:
                self._note_safety_not_recorded(cid, "store_unavailable")
            text, escalated, metadata = SAFETY_NOT_RECORDED_MESSAGE + reference, bool(ticket_id), {"matched": matched}
        else:
            self.log.escalation(cid, "store_unavailable", ticket_id)
            text, escalated = HANDOVER_TEXT + reference, True
        # A throwaway state, so the AI disclosure is always added: we cannot
        # know whether this person has already seen it.
        text = apply_disclosure(text, ConversationState(conversation_id=cid), message.channel)
        return Reply(conversation_id=cid, text=text, handled_by="store_unavailable",
                     escalated=escalated, ticket_id=ticket_id, metadata=metadata)

    # -- graph nodes (graph.py says what follows what) -----------------------

    def _node_prepare(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message = turn["message"]
        state = turn["conversation"]  # loaded by handle(), saved after the graph
        state.turns += 1
        self._note_typed_number(message, state)
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
                    last_contact = summarise_past(
                        self.conversations.recent_summaries(
                            state.user_key, limit=3, exclude=summary_key(state.conversation_id, state.started_at)),
                        # With Zoho on, an old mock number exists nowhere,
                        # so it never reaches the model (spec 2026-10-05).
                        drop_mock_tickets=self._records_real_tickets(),
                    )
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

    @staticmethod
    def _note_typed_number(message: InboundMessage, state: ConversationState) -> None:
        """The latest Indian mobile the customer typed in this run, ten digits
        (spec 2026-10-05, section 6): the number a ticket is called back on
        when nobody proved one. Digits of any script are read as ASCII first,
        and a code the conversation already spent is not a number. Customers
        only: a dealer typing a customer's number is not giving their own."""
        if message.persona != "customer":
            return
        text = ascii_digits(message.message_text or "")
        if text.strip() in state.consumed_codes:
            return
        found = find_phone(text)
        if found is not None:
            state.typed_number = found[0]

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
        matched, evidence = _safety_scan(message)
        if matched:
            return {"reply": self._handle_safety(message, resolved, state, matched, evidence)}
        return {}

    def _node_callback(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 1a. The call-back number (spec 2026-10-05, section 6). While a
        #     handover or a safety report waits for a number, code reads the
        #     next message for one here. This runs after safety and before
        #     going back, the handover, erasure and the verify step, so a
        #     number typed for a call is never taken as a number to verify.
        #     Fixed replies, no model.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        waiting = state.awaiting_callback
        if waiting is None:
            return {}
        cid = message.conversation_id
        if resolved.persona != "customer" or self._desk_store() is None:
            # Zoho switched off since the question: nothing can be recorded,
            # so the wait goes and the turn carries on as any other.
            self._end_wait(cid, state, "not_recordable")
            return {}
        if resolved.identity.phone:
            # A number we know now: the run's own person writing again after
            # someone else's stretch of it, whose wait this was, or a sign-in
            # since the question. The safety and handover gates record with
            # that number, so this message is never read as the one to call.
            self._end_wait(cid, state, "known_phone")
            return {}
        if wants_start_over(message.message_text or ""):
            self._end_wait(cid, state, "start_over")
            return {}
        typed = read_number(message.message_text or "")
        if typed.number is not None:
            return {"reply": self._callback_number(message, state, resolved, waiting, typed)}
        shown = _as_shown(message, typed)
        metadata: Dict[str, Any] = {"purpose": waiting}
        if shown is not message:
            metadata["transcript_text"] = shown.message_text
        if typed.attempted:
            # Not a valid Indian mobile (too short, or +34 and the like). The
            # gate says so and keeps waiting; the try is kept as [number].
            self.log.emit("callback_number_invalid", cid, purpose=waiting)
            return {"reply": self._finish(shown, state, INVALID_NUMBER, "guardrail:callback:invalid_number",
                                          metadata=metadata)}
        if state.verify_step == CODE and find_code(ascii_digits(message.message_text or "")):
            # The verification code, typed while one is pending (the review of
            # Task 14, Important 1). The verify step takes it, spends it and
            # keeps it as [code], so the model never sees it. Read after a
            # number and a try at one, as the verify step reads a number first.
            # The wait ends; a safety report then has no ticket, so it is
            # counted as every such report is (_end_wait).
            self._end_wait(cid, state, "code")
            return {}
        if waiting == "handover":
            self._end_wait(cid, state, "no_number")
            self.log.escalation(cid, "customer_requested_human", None)
            return {"reply": self._finish(shown, state, HANDOVER_NO_NUMBER_MESSAGE, "guardrail:callback:no_number",
                                          escalated=True, metadata=metadata)}
        if state.callback_asks < 1:
            # A safety report: asked once more.
            state.callback_asks += 1
            return {"reply": self._finish(shown, state, SAFETY_ASK_AGAIN_MESSAGE, "guardrail:callback:ask_again",
                                          metadata=metadata)}
        self._end_wait(cid, state, "no_number")  # counted there, as a safety report with no ticket
        return {"reply": self._finish(shown, state, SAFETY_NOT_RECORDED_MESSAGE, "guardrail:callback:no_number",
                                      metadata=metadata)}

    def _end_wait(self, conversation_id: str, state: ConversationState, why: str) -> None:
        """The wait for a call-back number ends, and the log says why. A wait
        that ends here ends with no ticket (a recorded one clears the wait
        itself), so a safety report's is alarmed and counted on /health, as
        every safety report with no ticket is, however the wait ended: no
        number, the verification code, a number we know now, starting over,
        Zoho off since the question (the final review, safety-flow
        Important 4), or the run's own person back with a report of their own
        (_note_speaker, `owner_returned`)."""
        waiting = state.awaiting_callback
        self.log.emit("callback_wait_ended", conversation_id, purpose=waiting, why=why)
        state.awaiting_callback, state.callback_asks = None, 0
        if waiting == "safety":
            self._note_safety_not_recorded(conversation_id, why)

    def _callback_number(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity, waiting: str,
        typed: TypedNumber,
    ) -> Reply:
        """The number a handover or a safety report waited for. It is recorded
        unverified, straight through the seam, so no bike is looked up for it
        (spec 2026-10-05, section 6). The ticket's run is the writer's own
        (_record_ticket)."""
        cid = message.conversation_id
        shown = _as_shown(message, typed)
        metadata: Dict[str, Any] = {"purpose": waiting, "transcript_text": typed.shown}
        state.typed_number = typed.number
        phone = "+91" + typed.number
        if waiting == "handover" and self._handover_gone(message, state, resolved):
            # A wait that outlived the run's handover ticket: its key records
            # no other, so the wait ends here rather than refusing every number.
            self._end_wait(cid, state, "ticket_gone")
            self.log.emit("handover_ticket_not_recorded", cid, why="ticket_gone")
            return self._finish(shown, state, HANDOVER_GONE_MESSAGE, "guardrail:callback:not_recorded",
                                metadata=dict(metadata, why="ticket_gone"))
        if waiting == "safety":
            recorded = self._record_ticket(
                message, state, resolved, kind="safety", purpose=PURPOSE_SAFETY, phone=phone, verified=False,
                description=("Automatic safety escalation. The customer reported a safety issue in the AI chat "
                             "and gave this number when asked. The number is not verified. Their words are "
                             "in the transcript."),
                cluster_id=resolved.cluster_id, category="battery_safety", severity="critical",
            )
        else:
            recorded = self._record_ticket(
                message, state, resolved, kind="handover", purpose=PURPOSE_HANDOVER, phone=phone, verified=False,
                description="Customer asked for a person and gave this number when asked.",
                cluster_id=resolved.cluster_id,
            )
        if recorded.refusal is not None:
            # A cap on unverified tickets refused it: say so, promise nothing.
            state.awaiting_callback, state.callback_asks = None, 0
            return self._finish(shown, state, recorded.refusal, "guardrail:callback:capped", metadata=metadata)
        if recorded.reference is None:
            # The wait stays, so the number sent again is still read here,
            # never by the verify step.
            if waiting == "safety":
                self._note_safety_not_recorded(cid, "not_recorded")
                return self._finish(shown, state, SAFETY_NOT_RECORDED_MESSAGE, "guardrail:callback:not_recorded",
                                    metadata=metadata)
            self.log.emit("handover_ticket_not_recorded", cid, why="not_recorded")
            return self._finish(shown, state, HANDOVER_NOT_RECORDED_MESSAGE, "guardrail:callback:not_recorded",
                                metadata=metadata)
        state.awaiting_callback, state.callback_asks = None, 0
        self.log.escalation(cid, "battery_safety" if waiting == "safety" else "customer_requested_human",
                            recorded.reference)
        return self._finish(
            shown, state, NUMBER_RECEIVED_MESSAGE.format(reference=recorded.reference),
            "guardrail:callback:recorded", escalated=True, ticket_id=recorded.reference, metadata=metadata,
        )

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
                self.log.escalation(cid, "verification_locked", gate.ticket_id)
            shown = replace(message, message_text=gate.model_text)
            return {"reply": self._finish(
                shown, state, gate.text, "verify_first:" + gate.outcome, escalated=gate.escalated,
                ticket_id=gate.ticket_id, metadata={"transcript_text": gate.model_text},
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
        # 2. Human handoff, reachable at any point, no friction. With Zoho
        #    on, a customer's request records a handover ticket, or asks for
        #    the number to record it with (spec 2026-10-05, section 6). With
        #    Zoho off, or a dealer asking for their account manager, it is
        #    exactly as before and nothing is recorded (dealer tickets wait
        #    for W1).
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        handoff = check_human_handoff(message.message_text)
        if handoff.triggered:
            self.log.guardrail(message.conversation_id, "human_handoff", handoff.matched)
            if resolved.persona == "customer" and self._desk_store() is not None:
                return {"reply": self._handover_ticket(message, state, resolved, handoff.matched)}
            self.log.escalation(message.conversation_id, "customer_requested_human", None)
            return {"reply": self._finish(
                message, state, HANDOFF_MESSAGE, "guardrail:human_handoff",
                escalated=True, metadata={"matched": handoff.matched},
            )}
        return {}

    def _handover_ticket(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity, matched: List[str],
    ) -> Reply:
        """Talk to a person, for a customer, with Zoho on (spec 2026-10-05,
        section 6). One ticket per need in a run: a Desk ticket the person
        writing already holds in the run gets the note and is quoted. If the
        run's handover ticket went from Desk, nothing is asked for or
        recorded (_handover_gone). Otherwise a number typed in this message,
        or failing that the number we know, records a handover ticket. With
        neither, the number is asked for and the callback gate waits for it.
        "Call me" is itself a trigger, so the number is read from this message
        first."""
        cid = message.conversation_id
        typed = read_number(message.message_text or "")
        shown = _as_shown(message, typed)
        if state.verify_step == CODE:
            # A code typed with the request, while one is pending. This gate
            # answers before the verify step, so the code is not spent, and
            # it is kept as [code] so no later model turn sees it (the final
            # review, safety-flow Minor 11).
            shown = _code_hidden(shown)
        metadata: Dict[str, Any] = {"matched": matched}
        if shown is not message:
            metadata["transcript_text"] = shown.message_text
        if typed.number:
            state.typed_number = typed.number
        held = self._live_run_ticket(message, state, resolved)
        if held is not None:
            self._add_note(cid, held, "Customer asked for a person.")
            self.log.escalation(cid, "customer_requested_human", held)
            return self._finish(shown, state, HANDOVER_RECORDED_MESSAGE.format(reference=held),
                                "guardrail:human_handoff", escalated=True, ticket_id=held,
                                metadata=dict(metadata, handover="note_added"))
        if self._handover_gone(message, state, resolved):
            # Asking for a number would only lead to a refusal on every number
            # sent (the review of Task 14, Important 2).
            self.log.emit("handover_ticket_not_recorded", cid, why="ticket_gone")
            return self._finish(shown, state, HANDOVER_GONE_MESSAGE, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="ticket_gone"))
        known = resolved.identity.phone
        phone = "+91" + typed.number if typed.number else known
        if phone is None:
            state.awaiting_callback, state.callback_asks = "handover", 0
            self.log.emit("callback_asked", cid, purpose="handover")
            return self._finish(shown, state, HANDOVER_ASK_NUMBER_MESSAGE, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="asked_for_number"))
        # Verified only when it is the number the channel or a code proved.
        verified = resolved.identity.may_disclose and phone == known
        recorded = self._record_ticket(
            message, state, resolved, kind="handover", purpose=PURPOSE_HANDOVER, phone=phone, verified=verified,
            description="Customer asked for a person.", cluster_id=resolved.cluster_id,
            # What a ticket the agent raises carries (the plan's cross-check,
            # finding 30), kept by _record_ticket only for a proved number.
            bike=self._handover_bike(resolved, state), coverage=coverage_fact(state, resolved),
            customer_name=self._handover_name(resolved, state),
        )
        if recorded.refusal is not None:
            # A cap on unverified tickets refused it: say so, promise nothing.
            return self._finish(shown, state, recorded.refusal, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="capped"))
        if recorded.reference is None:
            self.log.emit("handover_ticket_not_recorded", cid, why="not_recorded")
            if typed.number:
                # The number sent again is read by the callback gate, not the verify step.
                state.awaiting_callback, state.callback_asks = "handover", 0
            return self._finish(shown, state, HANDOVER_NOT_RECORDED_MESSAGE, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="not_recorded"))
        self.log.escalation(cid, "customer_requested_human", recorded.reference)
        return self._finish(
            shown, state, HANDOVER_RECORDED_MESSAGE.format(reference=recorded.reference),
            "guardrail:human_handoff", escalated=True, ticket_id=recorded.reference,
            metadata=dict(metadata, handover="recorded"),
        )

    def _live_run_ticket(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity,
    ) -> Optional[str]:
        """The Desk ticket the person writing already holds in this run
        (_speakers_ticket): the run's, for the run's own person; for anyone
        else, their own stretch's, never the run's. None when it is a mock
        ticket, or gone (deleted or merged in Desk) and so never quoted. A new
        run starts with a fresh state, and restart_for clears both."""
        ticket_id = self._speakers_ticket(state, resolved)
        if self._desk_store() is None or not is_desk_reference(ticket_id):
            return None
        if self._is_gone(message.conversation_id, ticket_id):
            return None
        return ticket_id

    def _handover_gone(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity,
    ) -> bool:
        """Whether the writer's handover ticket in this run was deleted or
        merged in Desk. Its key, one per run or stretch of a run
        (_ticket_run_start), returns that record for the rest of it (the
        plan's cross-check, finding 18), so no number can record another, as
        _safety_without_phone finds for safety. A store that cannot answer
        counts as not gone: the record then succeeds or fails on its own."""
        store = self._desk_store()
        if store is None:
            return False
        cid = message.conversation_id
        key = self._gate_key(cid, self._ticket_run_start(message, state, resolved), PURPOSE_HANDOVER)
        try:
            record = store.by_source_key(key)
        except StoreUnavailable as exc:
            self.log.emit("ticket_read_failed", cid, kind="handover", error=type(exc).__name__)
            return False
        return record is not None and record.get("state") == GONE

    def _handover_bike(self, resolved: ResolvedIdentity, state: ConversationState) -> Optional[Dict[str, Any]]:
        """The bike on a verified handover ticket: the chosen one, or the only
        one. Never an unlisted bike, which is a claim and not a record."""
        if state.unlisted_bike:
            return None
        bike = self._selected_bike(resolved, state)
        if not bike or not bike.get("frame_number"):
            return None
        return {"frame_number": bike.get("frame_number"), "frame_number_source": bike.get("frame_number_source"),
                "bike_model": bike.get("product_name")}

    def _handover_name(self, resolved: ResolvedIdentity, state: ConversationState) -> Optional[str]:
        """The OMS customer name on a verified handover ticket, as a ticket the
        agent raises carries it (tools/mocks._record_name). Only with the
        ticket's bike, and only for a bike the OMS holds: a bike only the
        rider's app knows has the app's username on its record, never the
        customer's name."""
        bike = None if state.unlisted_bike else self._selected_bike(resolved, state)
        if not bike or not bike.get("frame_number") or bike.get("coverage_status") in _APP_BIKE_STATES:
            return None
        return (resolved.profile or {}).get("name") or None

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
            self.log.escalation(message.conversation_id, "verification_locked", gate.ticket_id)
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
            ticket_id=gate.ticket_id, metadata={"transcript_text": gate.model_text},
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
            persona=resolved.persona,
            started_at=state.started_at,
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
        spent), which a rerun would repeat or break. Also a ticket a gate
        recorded or noted straight through the seam (ticket_recorded,
        ticket_note_added). A turn that recorded one is merged, never run
        again, and a rerun would add the note twice (spec 2026-10-05,
        section 6)."""
        done: List[Dict[str, Any]] = []
        for event in self.log.events[log_mark:]:
            if event.get("conversation_id") != conversation_id:
                continue
            if event.get("event") in ("ticket_recorded", "ticket_note_added"):
                done.append({"tool": event["event"], "ticket_id": event.get("ticket_id")})
                continue
            if event.get("event") != "tool_call":
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

    def _records_real_tickets(self) -> bool:
        """Whether customer tickets go to Zoho Desk (api.py wired a
        TicketRouter) rather than the mock. The only "is Zoho on" check.
        `is True`, so a test double whose attributes are all truthy never
        counts."""
        tickets = getattr(self.registry, "tickets", None)
        return getattr(tickets, "records_real_tickets", False) is True

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
                # This run of the conversation, or the writer's stretch of it
                # (_ticket_run_start). Write receipts and tickets are scoped
                # by it (tools/registry.py), read when the tool runs.
                "started_at": lambda: self._ticket_run_start(message, state, resolved),
                "evidence_seen": lambda: state.evidence_seen,
                "coverage_result": lambda: state.coverage_result,
                # The ticket tool's check on a frame number the rider reads
                # off the sticker: only for the bike this conversation chose.
                "selected_bike": lambda: state.selected_frame,
                # The knowledge filter's bike: the one the router and the
                # narrow path already use, so the full agent's search applies
                # the same applies_to and excludes (6 October 2026).
                "knowledge_bike": lambda: self._selected_bike(resolved, state),
                # A bike the customer gave because it is not in their list:
                # the ticket tool puts its frame number on a ticket.
                "unlisted_bike": lambda: state.unlisted_bike,
                # How well the ticket tools know who this is (spec 2026-10-05,
                # section 2): a ticket is verified only on a proven phone, and
                # a caller ID's phone never has a bike looked up for it.
                "identity_strength": lambda: self._identity_strength(message.conversation_id, resolved),
                # The customer's own words, for the address backstop: an
                # address is accepted only if it matches the record or
                # something the customer actually typed in this conversation.
                # A code `verify_identity` has already consumed is a spent
                # secret, not an address, so it is left out.
                "customer_messages": lambda: [
                    m for m in customer_texts(state.history) if m.strip() not in state.consumed_codes
                ],
                # What the ticket record needs, worked out in code (spec
                # 2026-10-05, section 2): this turn's channel, the cover of
                # the ticket's bike and the number the customer last typed.
                # The run is started_at, above.
                "channel": lambda: message.channel,
                "coverage": lambda: coverage_fact(state, resolved),
                "typed_number": lambda: state.typed_number,
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
            if call["tool"] == LOOKUP_WARRANTY_RECORD:
                self._remember_coverage(state, call["result"])

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
        without one (spec 2026-10-01). The ticket id, or None.

        The run is the ticket's own (_ticket_run_start): the run's start for
        its own person, a start of their own for anyone else."""
        started_at = self._ticket_run_start(message, state, resolved)
        arguments: Dict[str, Any] = {
            "category": "battery_safety",
            "severity": "critical",
            "description": description,
            "idempotency_key": _safety_idempotency_key(message.conversation_id, started_at),
        }
        # With several bikes the ticket needs one named, and triage may not
        # have run yet — the safety branch fires before it.
        # The chosen bike goes as the conversation's choice, not as a
        # frame number: if this turn's bike list no longer holds it (Amigo
        # stopped answering since it was chosen), the ticket is raised
        # without a bike, naming it here, rather than refused.
        late: Dict[str, Any] = {
            # Set here and nowhere else: the model never chooses a ticket's
            # kind, and anything it sends under this name is dropped.
            "ticket_kind": lambda: "safety",
            "identity_strength": lambda: self._identity_strength(message.conversation_id, resolved),
            # The same facts an agent's ticket gets (spec 2026-10-05,
            # section 2). The run is on the ToolContext below.
            "channel": lambda: message.channel,
            "coverage": lambda: coverage_fact(state, resolved),
            "typed_number": lambda: state.typed_number,
        }
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
                persona=resolved.persona,
                started_at=started_at,
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
        nothing (staging, 2026-10-01). None when it cannot be backed (not a
        customer and no number known, or the ticket failed): the caller then
        hands over. A customer with no number known gets a reply that
        promises nothing instead (_backstop_without_phone)."""
        if not resolved.identity.phone:
            return self._backstop_without_phone(message, resolved, state, turn, hazards)
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
        if not ticket_id or self._is_gone(message.conversation_id, ticket_id):
            self.log.emit("safety_backstop_failed", message.conversation_id,
                          error="gone" if ticket_id else "no_ticket")
            return None
        self.log.guardrail(message.conversation_id, "safety_backstop", hazards)
        self.log.escalation(message.conversation_id, "safety_backstop", ticket_id)
        text = turn.text or ""
        if ticket_id not in text:
            text = text.rstrip() + "\n\n" + _PRIORITY_CASE % ticket_id
            replace_turn_text(state.history, text)
        return replace(turn, text=text, ticket_id=ticket_id, escalate=True)

    def _backstop_without_phone(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState, turn: Any,
        hazards: List[str],
    ) -> Any:
        """The backstop for a customer with no number we know (the plan's
        cross-check, finding 13). The model's claim of a ticket is replaced by
        what the safety gate says in the same place: with Zoho on, the steps,
        a request for a number and 112, and the callback gate waits for the
        number; with Zoho off, the steps and 112, and the failure is alarmed.
        Neither promises a call or a hand-over, because nothing is recorded.
        No model is called. None for anyone but a customer: the caller hands
        over, as before."""
        if resolved.persona != "customer":
            return None
        cid = message.conversation_id
        self.log.guardrail(cid, "safety_backstop", hazards)
        if self._desk_store() is not None:
            text = SAFETY_NO_CONTACT_MESSAGE
            state.awaiting_callback, state.callback_asks = "safety", 0
            self.log.emit("callback_asked", cid, purpose="safety")
        else:
            text = SAFETY_NOT_RECORDED_MESSAGE
            self._note_safety_not_recorded(cid, "backstop_no_contact")
        replace_turn_text(state.history, text)
        return replace(turn, text=text, ticket_id=None, escalate=False)

    def _handle_safety(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        matched: List[str],
        evidence: Optional[List[str]] = None,
    ) -> Reply:
        """The safety branch, never a model call (spec 2026-10-05, section 6).
        The reply promises a call only when a ticket is behind it."""
        self.log.guardrail(message.conversation_id, "battery_safety", matched)
        if resolved.identity.phone:
            return self._safety_with_phone(message, resolved, state, matched, evidence)
        if resolved.persona == "customer" and self._desk_store() is not None:
            return self._safety_without_phone(message, resolved, state, matched, evidence)
        # Zoho off, or not a customer, and no number we know: nothing can be
        # recorded. So the steps and 112, with no question and no promise.
        self.log.emit("safety_without_contact", message.conversation_id)
        return self._safety_reply(message, message, state, SAFETY_NOT_RECORDED_MESSAGE, matched, evidence,
                                  outcome="no_contact")

    def _safety_with_phone(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState,
        matched: List[str], evidence: Optional[List[str]],
    ) -> Reply:
        """A number we know (WhatsApp, the app, a code proved, caller ID). The
        ticket goes through create_support_ticket with the run's safety key,
        as before. One safety ticket per run (the plan's cross-check, finding
        11): with Zoho on, a ticket the run already holds under either safety
        key gets a note and is quoted, and no second is raised. If no ticket
        can be raised, the reply promises nothing."""
        cid = message.conversation_id
        try:
            gate, tool = self._run_safety_records(message, state, resolved)
        except StoreUnavailable as exc:
            # The tool decides: a second safety ticket is a lesser harm than
            # none. Logged, so a second one has a trace of why (the review of
            # Task 13, Important 3).
            self.log.emit("ticket_read_failed", cid, kind="safety", error=type(exc).__name__)
            gate = tool = None
        held = _live(gate, tool)
        if held is not None:
            return self._safety_added(message, message, state, matched, evidence, held["_id"],
                                      SAFETY_MESSAGE + "\n\n" + _PRIORITY_CASE % held["_id"])
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
        # The writer's ticket before this report: the run's, or for someone
        # who does not own the run, their own. With the mock (Zoho off),
        # getting the same id back means its receipt returned that safety
        # ticket: this is a repeat.
        before = self._speakers_ticket(state, resolved)
        try:
            ticket_id = self._raise_safety_ticket(message, state, resolved, description)
        except Exception as exc:  # the class only; the customer still gets the steps
            self.log.emit("safety_ticket_failed", cid, error=type(exc).__name__)
            ticket_id = None
        if not ticket_id:
            return self._safety_not_recorded(message, message, state, matched, evidence, why="tool_error")
        if self._is_gone(cid, ticket_id):
            # Its key returned a ticket deleted or merged in Desk.
            return self._safety_not_recorded(message, message, state, matched, evidence, why="ticket_gone")
        text = SAFETY_MESSAGE + "\n\n" + _PRIORITY_CASE % ticket_id
        if ticket_id == before:
            return self._safety_added(message, message, state, matched, evidence, ticket_id, text)
        self.log.escalation(cid, "battery_safety", ticket_id)
        return self._safety_reply(message, message, state, text, matched, evidence, outcome="recorded",
                                  escalated=True, ticket_id=ticket_id)

    def _safety_without_phone(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState,
        matched: List[str], evidence: Optional[List[str]],
    ) -> Reply:
        """A customer with no number we know (an anonymous web visitor), with
        Zoho on (spec 2026-10-05, section 6). A number in this message records
        the urgent safety ticket at once, unverified and with no bike looked
        up. Without one, the safety steps ask for a number instead of
        promising a call, and the callback gate waits for it. A report after
        the run's safety ticket was recorded, under either safety key, adds a
        note to that ticket."""
        cid = message.conversation_id
        typed = read_number(message.message_text or "")
        shown = _as_shown(message, typed)
        try:
            gate, tool = self._run_safety_records(message, state, resolved)
        except StoreUnavailable:
            return self._safety_not_recorded(message, shown, state, matched, evidence, why="store_unavailable")
        held = _live(gate, tool)
        if held is not None:
            text = "\n\n".join((SAFETY_STEPS, SAFETY_ADDED_MESSAGE.format(reference=held["_id"]), SAFETY_EMERGENCY))
            return self._safety_added(message, shown, state, matched, evidence, held["_id"], text)
        if gate is not None:
            # The run's safety ticket was deleted or merged in Desk, and its key
            # records no other: quoting it would promise nothing real.
            return self._safety_not_recorded(message, shown, state, matched, evidence, why="ticket_gone")
        if typed.number is None:
            state.awaiting_callback, state.callback_asks = "safety", 0
            self.log.emit("callback_asked", cid, purpose="safety")
            return self._safety_reply(message, shown, state, SAFETY_NO_CONTACT_MESSAGE, matched, evidence,
                                      outcome="asked_for_number")
        state.typed_number = typed.number
        description = (
            "Automatic safety escalation. Customer reported: %s. Matched safety indicators: %s. "
            "No troubleshooting was offered. The number to call was typed in the chat and is not verified."
            % (typed.shown, ", ".join(matched))
        )
        if evidence:
            description += " Seen in the customer's photo or video: %s" % " ".join(evidence)
        recorded = self._record_ticket(
            message, state, resolved, kind="safety", purpose=PURPOSE_SAFETY, phone="+91" + typed.number,
            verified=False, description=description, cluster_id=resolved.cluster_id,
            category="battery_safety", severity="critical",
        )
        if recorded.reference is None:
            return self._safety_not_recorded(message, shown, state, matched, evidence, why="not_recorded")
        state.awaiting_callback, state.callback_asks = None, 0
        self.log.escalation(cid, "battery_safety", recorded.reference)
        text = "\n\n".join((SAFETY_STEPS, NUMBER_RECEIVED_MESSAGE.format(reference=recorded.reference),
                            SAFETY_EMERGENCY))
        return self._safety_reply(message, shown, state, text, matched, evidence, outcome="recorded",
                                  escalated=True, ticket_id=recorded.reference)

    def _safety_added(
        self, message: InboundMessage, shown: InboundMessage, state: ConversationState, matched: List[str],
        evidence: Optional[List[str]], reference: str, text: str,
    ) -> Reply:
        """A later safety report in a run whose safety ticket exists: a note
        on that ticket, and its reference (one safety ticket per run, spec
        2026-10-05, section 6). Any wait for a number ends: the ticket has
        one."""
        cid = message.conversation_id
        self._add_note(cid, reference, _again_note(matched))
        state.awaiting_callback, state.callback_asks = None, 0
        self.log.escalation(cid, "battery_safety", reference)
        return self._safety_reply(message, shown, state, text, matched, evidence, outcome="note_added",
                                  escalated=True, ticket_id=reference)

    def _safety_reply(
        self, message: InboundMessage, shown: InboundMessage, state: ConversationState, text: str,
        matched: List[str], evidence: Optional[List[str]], *, outcome: str, escalated: bool = False,
        ticket_id: Optional[str] = None,
    ) -> Reply:
        """Every safety reply. `shown` is the message as the model's history
        and the transcript keep it, with a typed number replaced by [phone].
        `escalated` only when a ticket is behind the reply (spec 2026-10-05,
        section 6)."""
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
            kept = [a for a in shown.attachments if a.summary or _is_image(a)]
            content = user_content(replace(shown, attachments=kept), self.fetch)
            state.history.append({"role": "user", "content": content})
            self._note_customer_turn(shown, state, content)
        metadata: Dict[str, Any] = {"matched": matched, "safety": outcome}
        if shown is not message:
            metadata["transcript_text"] = shown.message_text
        return self._finish(
            shown, state, text, "guardrail:battery_safety",
            escalated=escalated, ticket_id=ticket_id, metadata=metadata,
            already_in_history=bool(evidence),
        )

    def _safety_not_recorded(
        self, message: InboundMessage, shown: InboundMessage, state: ConversationState,
        matched: List[str], evidence: Optional[List[str]], why: str,
    ) -> Reply:
        """No safety ticket could be recorded: the steps and 112, and no
        promise of a call. Logged at error level and alarmed (spec section 8)."""
        self._note_safety_not_recorded(message.conversation_id, why)
        return self._safety_reply(message, shown, state, SAFETY_NOT_RECORDED_MESSAGE, matched, evidence,
                                  outcome="not_recorded")

    def _note_safety_not_recorded(self, conversation_id: str, why: str) -> None:
        """A safety report whose ticket could not be recorded: alarmed by its
        event (infra/zoho-alarms.yaml) and counted on /health (spec
        2026-10-05, section 6). Every such report comes through here."""
        self.safety_not_recorded += 1
        self.log.emit("safety_ticket_not_recorded", conversation_id, why=why, level="error")

    def _desk_store(self) -> Any:
        """The ticket store when Zoho is on, else None. Zoho is on exactly
        when api.py wired a TicketRouter (_records_real_tickets). The
        playground, the CLI, the live evaluation and Zoho off keep the mock,
        and the gates record nothing (spec 2026-10-05, section 6)."""
        return self.registry.tickets.store if self._records_real_tickets() else None

    @staticmethod
    def _gate_key(conversation_id: str, started_at: Optional[str], purpose: str) -> str:
        """A gate ticket's source key: one per run and purpose (spec section 2)."""
        return "%s:%s:%s" % (conversation_id, started_at or "", purpose)

    def _run_safety_records(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The run's safety ticket records, under the gate's key and under the
        safety branch's create_support_ticket key, gone or not (the plan's
        cross-check, finding 11). Both None with Zoho off, or for anyone but a
        customer, whose tickets never go to Desk. Raises StoreUnavailable."""
        store = self._desk_store()
        if store is None or resolved.persona != "customer":
            return None, None
        cid = message.conversation_id
        started_at = self._ticket_run_start(message, state, resolved)
        gate = store.by_source_key(self._gate_key(cid, started_at, PURPOSE_SAFETY))
        tool = store.by_source_key(ticket_source_key(
            cid, started_at, CREATE_SUPPORT_TICKET, _safety_idempotency_key(cid, started_at)))
        return gate, tool

    def _is_gone(self, conversation_id: str, ticket_id: str) -> bool:
        """Whether a Desk ticket was deleted or merged in Desk (gone), so it is
        never quoted. A store that cannot answer counts as not gone: the
        ticket was just returned for this run, so it exists."""
        store = self._desk_store()
        if store is None or not is_desk_reference(ticket_id):
            return False
        try:
            record = store.get(ticket_id)
        except StoreUnavailable as exc:
            self.log.emit("ticket_read_failed", conversation_id, ticket_id=ticket_id, error=type(exc).__name__)
            return False
        return record is not None and record.get("state") == GONE

    def _record_ticket(
        self,
        message: InboundMessage,
        state: ConversationState,
        resolved: Optional[ResolvedIdentity],
        *,
        kind: str,
        purpose: str,
        phone: Optional[str],
        verified: bool,
        description: str,
        cluster_id: Optional[str] = None,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        bike: Optional[Dict[str, Any]] = None,
        coverage: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> Recorded:
        """A ticket a gate writes straight to the seam (spec sections 2 and 6):
        safety with no number we know, the call-back number, the handover and
        the lock-out. It never goes through create_support_ticket, so no bike
        is looked up for a number nobody proved. There is one per run and
        purpose: the same source key always returns the same ticket, so a
        retry or a rerun records nothing new.

        The run is the ticket's own (_ticket_run_start, from this turn's
        `resolved`): every gate's ticket has a start, and never the start of
        a run that belongs to someone else.

        Called only for a customer, with Zoho on (_desk_store). It logs
        ticket_recorded, which a save conflict counts as a side effect. A
        failure is logged and comes back as no reference, and the caller then
        promises nothing. So does a ticket gone in Desk that its key returned
        (the plan's cross-check, finding 18).

        The bike, the cover and the name are kept only for a number someone
        proved, as on a ticket the agent raises (tools/mocks): a typed number
        could be anyone's."""
        cid = message.conversation_id
        started_at = self._ticket_run_start(message, state, resolved)
        source_key = self._gate_key(cid, started_at, purpose)
        if not verified and not is_urgent(kind, category):
            refusal = self._cap_refusal(cid, kind, phone, source_key)
            if refusal is not None:
                return Recorded(None, refusal)
        fields: Dict[str, Any] = dict(
            kind=kind, conversation_id=cid, started_at=started_at,
            cluster_id=cluster_id or state.cluster_id, channel=message.channel, phone=phone,
            identity="verified" if verified else "unverified", category=category, severity=severity,
            description=description,
        )
        if verified:
            fields.update(bike or {}, coverage=coverage, customer_name=customer_name)
        try:
            created = self.registry.tickets.create(source_key=source_key, persona="customer", **fields)
        except Exception as exc:  # StoreUnavailable included; the class only, never str(exc)
            self.log.emit("ticket_record_failed", cid, kind=kind, error=type(exc).__name__)
            return Recorded(None)
        reference = (created or {}).get("ticket_id")
        if not reference:
            self.log.emit("ticket_record_failed", cid, kind=kind, error="no_ticket")
            return Recorded(None)
        if self._is_gone(cid, reference):
            self.log.emit("ticket_record_failed", cid, kind=kind, error="gone")
            return Recorded(None)
        self.log.emit("ticket_recorded", cid, ticket_id=reference, kind=kind, urgent=is_urgent(kind, category))
        return Recorded(reference)

    def _cap_refusal(self, conversation_id: str, kind: str, phone: Optional[str], source_key: str) -> Optional[str]:
        """The caps on unverified tickets that are not urgent (spec
        2026-10-05, section 6), through the one helper the intake tool also
        uses. Returns the text to send instead, or None. A store that cannot
        answer does not cap: the record then fails or succeeds on its own."""
        try:
            cap = cap_reached(self._desk_store(), phone=phone, source_key=source_key, now=now_iso())
        except StoreUnavailable as exc:
            self.log.emit("ticket_cap_unchecked", conversation_id, kind=kind, error=type(exc).__name__)
            return None
        if cap is None:
            return None
        self.log.emit("unverified_ticket_capped", conversation_id, kind=kind, cap=cap, level="error")
        return CAP_TEXTS[cap]

    def _record_lockout(
        self, message: InboundMessage, state: ConversationState, phone: Optional[str], outcome: str
    ) -> Optional[Recorded]:
        """verify_first's lock-out ticket (spec 2026-10-05, section 6).
        VerifyFirst runs only for an anonymous customer, so the ticket is
        always unverified and never has a bike, and the run is the one an
        anonymous writer has (`resolved` None in _ticket_run_start).

        None with Zoho off, where no ticket is ever recorded and the lock-out
        is as before. With Zoho on, no reference when nothing was recorded
        (no number to call, or the write failed), logged as
        lockout_ticket_not_recorded with why it was not, and VerifyFirst then
        promises nothing (the final review, safety-flow Important 2).

        A cap that refuses it is kept in `state.transitions`, so the run's
        later locked messages say the same and neither try the store again
        nor sound the alarm again on every message."""
        cid = message.conversation_id
        if self._desk_store() is None:
            return None
        for entry in state.transitions:
            marker, _, cap = entry.partition(":")
            if marker == LOCKOUT_CAPPED and cap in CAP_TEXTS:
                return Recorded(None, CAP_TEXTS[cap])
        if phone is None:
            self.log.emit("lockout_ticket_not_recorded", cid, why="no_number")
            return Recorded(None)
        why = "five wrong codes" if outcome == "locked" else "a fourth code was asked for"
        recorded = self._record_ticket(
            message, state, None, kind="lockout", purpose=PURPOSE_LOCKOUT, phone=phone, verified=False,
            description="The customer could not verify their number in the AI chat: %s." % why,
        )
        if recorded.refusal is not None:
            cap = next(name for name, text in CAP_TEXTS.items() if text == recorded.refusal)
            state.transitions.append("%s:%s" % (LOCKOUT_CAPPED, cap))
        elif recorded.reference is None:
            # _record_ticket logged the failure's class (ticket_record_failed).
            self.log.emit("lockout_ticket_not_recorded", cid, why="not_recorded")
        return recorded

    def _add_note(self, conversation_id: str, ticket_id: str, text: str) -> bool:
        """A line on a ticket the run already holds (spec section 2). A failed
        note is logged. The ticket stands either way."""
        tickets = getattr(self.registry, "tickets", None)
        if not hasattr(tickets, "add_note"):
            return False
        try:
            tickets.add_note(ticket_id, text)
        except Exception as exc:  # the class only
            self.log.emit("ticket_note_failed", conversation_id, ticket_id=ticket_id, error=type(exc).__name__)
            return False
        self.log.emit("ticket_note_added", conversation_id, ticket_id=ticket_id)
        return True

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
