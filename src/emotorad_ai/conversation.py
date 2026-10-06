"""Conversation state (build plan §3.5).

Triage is a conversation, not a single classification: a customer arrives, we
work out which bike and what is wrong, and only then hand to a sub-agent. That
spans turns, so it needs state — and the state has to live in code rather than
being re-derived from the transcript by the model every turn, or two turns of the
same conversation can disagree about which bike is being discussed.

Deliberately small. It holds what the *platform* decided, never what the model
believes: the selected frame number, the phase we are in, and which sub-agent
currently owns the conversation. Everything else is transcript.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import erasure as erasure_rules
from .attachments import MACHINE_TEXT_PREFIXES
from .contract import InboundMessage, Reply
from .evidence_check import FAULT_AGENTS
from .observability import redact_pii

# Phases. A conversation moves forward through these, and can move back — a
# customer who says "actually, my other bike" returns to bike selection from a
# routed state, which is why this is a field rather than a one-way sequence.
GREETING = "greeting"
AWAITING_BIKE_SELECTION = "awaiting_bike_selection"
AWAITING_ISSUE = "awaiting_issue"
# The customer said their bike is not in the list: collecting its frame number
# and model (spec 2026-10-01, unlisted bike).
AWAITING_UNLISTED_BIKE = "awaiting_unlisted_bike"
# The unlisted bike, or the listed one whose frame number they gave, waiting
# for the customer's yes (spec 2026-10-02, confirming the bike once).
AWAITING_BIKE_CONFIRMATION = "awaiting_bike_confirmation"
ROUTED = "routed"
PHASES = (GREETING, AWAITING_BIKE_SELECTION, AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION, AWAITING_ISSUE,
          ROUTED)


@dataclass
class ConversationState:
    """What the platform knows about a conversation in progress."""

    conversation_id: str
    phase: str = GREETING
    # The identity-graph cluster that started this conversation, recorded the
    # first time /message sees it. Lets a later presign under the same
    # conversation id be checked against who actually owns it — see
    # api.post_upload. None until a resolvable session has sent a message.
    cluster_id: Optional[str] = None
    # The bike under discussion. A frame number, always taken from the owned set
    # — never from what the customer typed, and never guessed when several exist.
    selected_frame: Optional[str] = None
    # The chosen bike's model and colour, as the rider saw it in the list. For
    # a safety ticket raised when that bike has since left the bike list.
    selected_bike_label: Optional[str] = None
    # A bike the customer says is not in the list (spec 2026-10-01, unlisted
    # bike): {"frame_number", "model"} as they gave them, either None if they
    # never did, and the asks made while collecting them.
    unlisted_bike: Optional[Dict[str, Optional[str]]] = None
    unlisted_asks: int = 0
    # The question waiting for that yes (triage.TriageAgent._resolve_confirmation):
    # {"kind": "unlisted" or "listed", "ref": the listed bike's reference or None,
    # "step": "confirm" or "which_wrong", "unclear": answers that were neither}.
    bike_confirmation: Optional[Dict[str, Any]] = None
    agent: Optional[str] = None
    # Topic understood before we knew which bike it was about. A customer taps
    # "Battery issue" and *then* picks a bike from three; without this the intent
    # is lost between turns and they get asked what is wrong all over again.
    pending_topic: Optional[str] = None
    pending_topic_source: Optional[str] = None
    # The verify-first step (verify_first.py): which answer it is waiting for,
    # "number" or "code", and the masked number the code went to, for its
    # replies. Both None when the step is not running.
    verify_step: Optional[str] = None
    verify_masked: Optional[str] = None
    # Codes the step has sent in this run, capped (verify_first.MAX_CODES).
    verify_sends: int = 0
    # Rendered enrichment block. Built once per conversation, not per turn: a
    # customer's bikes and history do not change mid-chat, and rebuilding it every
    # turn also moves it in the prompt, which defeats prefix caching.
    context_block: Optional[str] = None
    turns: int = 0
    disclosed: bool = False
    # Whether the model has been shown any photo or video in this conversation
    # (set by Runtime._note_customer_turn; one that could not be read does not
    # count). Held per
    # conversation, not per turn: a customer who sent the picture three turns ago
    # must not be asked for it again because the model concluded later.
    evidence_seen: bool = False
    # Video first (spec 2026-10-01-video-first-evidence-design.md): asks for a
    # video or photo since one last reached the model, the evidence post-check's
    # included, and whether the customer said a video is not possible. Three
    # asks with nothing back are sent; the fourth hands over to a person.
    evidence_asks: int = 0
    video_declined: bool = False
    # The evidence check before a ticket (evidence_check.py, 6 October 2026),
    # kept only with it switched on: the latest verdict on the customer's
    # photos and videos ({"passed", "seen", "missing", "error", "at"}), and
    # how many checks in this run could not be made. A verdict that passed
    # stays until the run ends or the bike changes; the first error in a run
    # earns one extra ask.
    evidence_verdict: Optional[Dict[str, Any]] = None
    evidence_check_errors: int = 0
    # The bike fault this run was last about, "battery" or "motor"
    # (evidence_check.fault_component): set when the chat is routed to the
    # battery or motor agent, or a request for a person names the fault, and
    # kept for the run. Going back to the list or to another number clears the
    # agent and the topic, never this: only a new run (restart_for) does.
    fault_topic: Optional[str] = None
    # The most recent warranty lookup, kept for the conversation for the same
    # reason `evidence_seen` is. Coverage is looked up once and then relied on;
    # the post-check that guards coverage claims was fed the current turn's tool
    # results alone, so a correct "that's covered" three turns after the lookup
    # was blocked as unsupported and the customer escalated to a human to check
    # a warranty that was already checked. The check itself is unchanged — a
    # claim must still match what a tool returned — this is only how long the
    # tool's answer is remembered.
    coverage_result: Optional[Dict[str, Any]] = None
    # Every order this conversation has placed, kept for the same reason
    # `coverage_result` is. The order post-check only ever saw this turn's tool
    # results, so a correct "it was RO-00001" a turn after the order was placed
    # was blocked as unsupported and the customer escalated to check an order
    # that had already gone through. The check itself is unchanged — a claimed
    # order id must still be one a tool actually placed, in this conversation.
    placed_order_ids: List[str] = field(default_factory=list)
    # Codes the customer typed that `verify_identity` accepted; they are secrets that were spent, not address text.
    consumed_codes: List[str] = field(default_factory=list)
    # The knowledge record the narrow agent is working through. Held across
    # turns so a follow-up like "yes, the light is red now", which scores low
    # on everything, stays on the same record (decisions.route rule 4).
    sub_category: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    # Every phase change, for debugging a conversation that went sideways. The
    # transcript says what was said; this says what the platform decided.
    transitions: List[str] = field(default_factory=list)
    # Persistence, for a durable store. `version` guards concurrent saves;
    # `user_key` ties the conversation to a person for memory; the last two
    # feed the per-user summary. `turn_offset` is how many transcript turns an
    # earlier, expired run of this conversation id left: the permanent record
    # carries on numbering from there rather than overwriting it.
    version: int = 0
    turn_offset: int = 0
    user_key: Optional[str] = None
    started_at: Optional[str] = None
    # Where this run came from (origin.py): the place fields and the person
    # once known. Set from the run's first message; only filled in after.
    origin: Optional[Dict[str, Any]] = None
    # Self-service erasure (erasure.py): "wanted" or "cancel_wanted" while the
    # verify step runs, "confirming" while the bot waits for DELETE.
    erasure_step: Optional[str] = None
    # The turn that asked for DELETE: only the next one may answer it.
    erasure_turn: Optional[int] = None
    # The latest Indian mobile the customer typed in this run, ten digits
    # (Runtime._note_typed_number): the number a ticket is called back on when
    # nobody proved one (spec 2026-10-05, section 6). Never the model's, and it
    # goes with the run.
    typed_number: Optional[str] = None
    # The run's last failed warranty look-up, "no_warranty_record" or
    # "oms_unavailable", for a ticket's coverage: coverage_result keeps only a
    # look-up that worked. Cleared by one that works, and with the bike.
    lookup_error: Optional[str] = None
    # The call-back gate (spec 2026-10-05, section 6): "handover" or "safety"
    # while it waits for a number to call, and the asks it has made.
    awaiting_callback: Optional[str] = None
    callback_asks: int = 0
    # The last number a verification code was sent to, for a lock-out ticket.
    last_code_phone: Optional[str] = None
    # Someone other than the run's own person writing in it before
    # restart_for (a second person on a shared browser, once the first
    # person's proof lapsed): where their stretch of the run began
    # (Runtime._newcomer_start) and the ticket recorded for them in it. Their
    # tickets take this start, never the run's, and `ticket_id` stays the
    # run's own person's. Both are cleared when the run's own person writes
    # again, which ends the stretch's tickets (Runtime._note_speaker).
    newcomer_started_at: Optional[str] = None
    newcomer_ticket_id: Optional[str] = None
    # Where the run's own person's stretch of the run began, once someone
    # else's stretch has ended (Runtime._owner_start): their tickets after it
    # take this start, never the run's, so none of them takes in the other
    # person's turns or photos. None until then: the run's start stands.
    owner_started_at: Optional[str] = None
    channel: Optional[str] = None
    escalated: bool = False
    ticket_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError("unknown conversation phase: %r" % (self.phase,))

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "ConversationState":
        """Tolerant of fields a newer version wrote, so a rolling deploy with two
        versions running cannot make old code fail to load a conversation."""
        data = json.loads(raw)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def restart_for(self, user_key: str, started_at: str) -> None:
        """A new run of this conversation for a different person.

        Reached when someone proves a number on a conversation that belonged to
        somebody else, whose session had expired (verify first, 2026-09-30).
        Nothing of the first person's may carry over: not their history, their
        bikes, tickets or orders, their memory, or their summary. So this is
        what an expired conversation does: a fresh state, numbered on from the
        turns already recorded, with a new `started_at` so its summary is a new
        one. The object is reset in place, because the turn holds it.
        """
        fresh = ConversationState(
            conversation_id=self.conversation_id,
            # This turn is the new run's first; the ones before it are recorded.
            turn_offset=self.turn_offset + (self.turns - 1) * 2,
            turns=1,
            started_at=started_at,
            user_key=user_key,
            version=self.version,
            channel=self.channel,
            cluster_id=self.cluster_id,
            consumed_codes=list(self.consumed_codes),
        )
        for f in dataclasses.fields(self):
            setattr(self, f.name, getattr(fresh, f.name))

    def move_to(self, phase: str, reason: str = "") -> None:
        if phase not in PHASES:
            raise ValueError("unknown conversation phase: %r" % (phase,))
        self.transitions.append("%s->%s%s" % (self.phase, phase, ":" + reason if reason else ""))
        self.phase = phase

    def select_bike(self, frame_number: str, label: Optional[str] = None) -> None:
        """Record which bike this conversation is about.

        Changing it mid-conversation is legitimate — customers do say "no, the
        other one" — but it must reset the routed agent, because troubleshooting
        already done applies to a different bike.
        """
        if self.selected_frame and self.selected_frame != frame_number:
            self.agent = None
            self.sub_category = None
            # Evidence that passed was about the other bike.
            self.evidence_verdict = None
            self.transitions.append("bike_changed:%s->%s" % (self.selected_frame, frame_number))
        self.selected_frame = frame_number
        self.selected_bike_label = label

    def forget_bike(self) -> None:
        """Back to before a bike was chosen (navigation, spec 2026-10-02). The
        bike goes, with any unlisted one and its confirmation, the agent, and
        what was learnt about that bike, which does not hold for another: its
        warranty lookup and a failed one, the evidence seen and the asks for
        it. Orders placed stay: they were placed. A number the customer typed
        stays: it is theirs, not the bike's. The topic is the caller's to keep
        or clear."""
        if self.selected_frame:
            self.transitions.append("bike_forgotten:%s" % self.selected_frame)
        self.selected_frame = None
        self.selected_bike_label = None
        self.unlisted_bike, self.unlisted_asks, self.bike_confirmation = None, 0, None
        self.agent = None
        self.sub_category = None
        self.coverage_result = None
        self.lookup_error = None
        self.evidence_seen = False
        self.evidence_asks, self.video_declined = 0, False
        self.evidence_verdict = None

    def route_to(self, agent: str) -> None:
        self.agent = agent
        self.fault_topic = FAULT_AGENTS.get(agent, self.fault_topic)
        self.move_to(ROUTED, agent)

    def hand_back(self, reason: str) -> None:
        """A sub-agent returning control to triage.

        The issue is cleared but the bike is kept: "my battery is fine now, but
        the motor is making a noise" is a new issue on the same bike, and asking
        which bike again would be maddening.
        """
        self.agent = None
        self.sub_category = None
        self.move_to(AWAITING_ISSUE, "handback:" + reason)


# How many customer turns the model is shown. A battery diagnosis runs fifteen
# to twenty turns, but the ones that matter are recent: what the customer just
# said, what was just tried, what the last tool returned. Identity is not in
# here at all — it lives in VerificationStore — so a window can never cost
# someone their verification.
HISTORY_TURNS = 12


def _is_customer_turn(entry: Dict[str, Any]) -> bool:
    """Whether this entry begins a customer turn, and so is safe to cut before.

    Both a customer's message and a batch of tool results are `user` entries.
    What separates them is the content: tool results are always blocks of type
    `tool_result`, and everything else from the customer — plain text, or text
    with a photo attached — is not.

    Testing for a plain string instead was right until a customer could send a
    photo. Their turn then arrives as a list of blocks, stops being recognised
    as a boundary, and the window cuts in the wrong place.
    """
    if entry.get("role") != "user":
        return False
    content = entry.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
    return False


def customer_texts(history: List[Dict[str, Any]]) -> List[str]:
    """The plain text of every customer message in this conversation.

    A customer's turn is either a bare string or a list of blocks (an image
    plus text); either shape can carry an address they typed. Tool-result
    entries are `user` role too but are never customer text — the same
    distinction `_is_customer_turn` draws, reused here for the address
    backstop on `place_replacement_order`: it needs the customer's own words,
    not a tool's or the model's.

    Text the platform wrote into the turn about their media — the video
    analyser's description, a transcript, a "could not be read" note — is
    skipped by its opening marker (`attachments.MACHINE_TEXT_PREFIXES`). An
    address read off a sticker in a clip is not an address the customer gave.
    """
    texts: List[str] = []
    for entry in history:
        if not _is_customer_turn(entry):
            continue
        content = entry.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text.startswith("[") and text.startswith(MACHINE_TEXT_PREFIXES):
                        continue
                    texts.append(text)
    return texts


_ADDRESS_TOKEN = re.compile(r"[a-z0-9]+")


def address_tokens(text: str) -> Set[str]:
    """The comparable words of an address: lowercase, alphanumeric only.

    Punctuation, spacing and case are the model's formatting. The words and
    numbers are the customer's, and those are what the backstop checks. A
    set, because the customer may give the street in one message and the
    pincode in another, in either order, and the model may reorder them into
    a postal shape.
    """
    return set(_ADDRESS_TOKEN.findall((text or "").lower()))


def trim_history(history: List[Dict[str, Any]], max_turns: int = HISTORY_TURNS) -> List[Dict[str, Any]]:
    """Keep the last `max_turns` customer turns, and cut nowhere else.

    Left untrimmed, every turn resent the whole transcript, so cost grew with
    the square of the conversation length and a long enough one would have hit
    the context window with nothing handling it.

    The cut point is the only subtle part; see `_is_customer_turn`. Cutting
    anywhere else can leave a `tool_result` above the `tool_use` it answers,
    which the API rejects outright — so a naive window would turn a conversation
    that costs too much into one that fails.
    """
    starts = [i for i, entry in enumerate(history) if _is_customer_turn(entry)]
    if len(starts) <= max_turns:
        return history
    return history[starts[-max_turns] :]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# What `owner_of` answers for a conversation whose runs belong to more than
# one person: a web chat on a shared browser, where a second person proved
# their number after the first person's proof lapsed (restart_for). No user
# key equals it ("PHONE#..." or "DEALER#..."), so neither person's history,
# and no rider's socket, can open the conversation and read the other's turns.
SHARED_OWNER = "#shared"


def notice_doc(conversation_id: str, seq: int, user_key: Optional[str], kind: str, text: str,
               at: str) -> Dict[str, Any]:
    """A notice in a chat (a ticket closed in Zoho Desk), with exactly the
    fields the Amiigo history reads (amiigo/history.py): `_id`
    `<conversation_id>#N<seq:05d>`, `conversation_id`, `user_key` (the person
    whose chat it is, or None), `kind`, `text` and `at` (ISO 8601 UTC, as a
    transcript turn's)."""
    return {"_id": "%s#N%05d" % (conversation_id, seq), "conversation_id": conversation_id, "user_key": user_key,
            "kind": kind, "text": text, "at": at}


def notice_seq(notice_id: str) -> int:
    """A notice's number in its chat, from its `_id`; 0 when it has none."""
    seq = str(notice_id).rsplit("#N", 1)[-1]
    return int(seq) if seq.isascii() and seq.isdigit() else 0


class ConversationConflict(Exception):
    """Another server saved this conversation after we loaded it."""


class StoreUnavailable(Exception):
    """The store could not be read or written. Never treated as an empty state."""


@dataclass(frozen=True)
class TranscriptTurn:
    """What was said, and nothing the model saw besides: no tool results."""

    n: int
    role: str  # "customer" | "bot"
    text: str
    at: str
    attachments: Tuple[Dict[str, str], ...] = ()
    handled_by: str = ""
    path: str = ""
    # The conversation the turn belongs to, as a store reads it back, so the
    # turn can name its message id (`<conversation_id>#<n:05d>`, the Amiigo
    # history, amiigo/history.py). Not compared: two readings of one turn are
    # the same turn.
    conversation_id: str = field(default="", compare=False)


@dataclass(frozen=True)
class ConversationSummaryItem:
    """One line of a person's history, built by code for the bot's memory."""

    conversation_id: str
    user_key: str
    started_at: str
    last_at: str
    channel: str = ""
    title: str = ""
    frame_number: Optional[str] = None
    product_name: Optional[str] = None
    agent: Optional[str] = None
    sub_category: Optional[str] = None
    outcome: str = "open"  # "open" | "escalated"
    ticket_id: Optional[str] = None
    turns: int = 0


def recorded_url(url: str) -> str:
    """An attachment's link as the permanent record keeps it. An inline photo is
    a `data:` URL holding the whole image, and a signed link carries a
    credential in its query string: neither belongs in a record kept for ever."""
    if (url or "").startswith("data:"):
        return "data:(inline, not kept)"
    return (url or "").split("?", 1)[0]


def transcript_turns(
    state: ConversationState, inbound: InboundMessage, reply: Reply, at: str
) -> Tuple[TranscriptTurn, TranscriptTurn]:
    """The customer's message and the bot's reply for this turn, redacted.

    Numbered from the turn counter, so recording the same turn twice (a retry)
    overwrites rather than duplicates, and after any earlier run's turns.
    """
    n = state.turn_offset + state.turns * 2 - 1
    customer = TranscriptTurn(
        n=n, role="customer", text=redact_pii(inbound.message_text or ""), at=at,
        attachments=tuple({"kind": a.kind, "url": recorded_url(a.url)} for a in inbound.attachments),
        conversation_id=state.conversation_id,
    )
    bot = TranscriptTurn(
        n=n + 1, role="bot", text=redact_pii(reply.text or ""), at=at,
        attachments=tuple({"kind": a.kind, "url": recorded_url(a.url)} for a in reply.attachments),
        handled_by=reply.handled_by or "", path=str(reply.metadata.get("route") or ""),
        conversation_id=state.conversation_id,
    )
    return customer, bot


def summary_key(conversation_id: str, started_at: Optional[str]) -> str:
    """One summary per run of a conversation id: a WhatsApp thread that goes
    quiet past the working-state expiry and starts again is two conversations
    in the person's history, not one overwritten."""
    return "%s#%s" % (conversation_id, started_at or "")


def render_transcript(turns: Sequence[TranscriptTurn]) -> str:
    """The thread as a person reads it on a ticket: time, speaker, words."""
    lines = []
    for turn in turns:
        try:
            clock = datetime.fromisoformat(turn.at).strftime("%H:%M")
        except (TypeError, ValueError):
            clock = "--:--"
        # A photo sent on its own must not read as an empty line on a ticket.
        marks = " ".join("[%s]" % {"image": "photo"}.get(a.get("kind"), a.get("kind") or "attachment") for a in turn.attachments)
        said = " ".join(part for part in (turn.text, marks) if part)
        lines.append("[%s] %s: %s" % (clock, "Customer" if turn.role == "customer" else "Bot", said))
    return "\n".join(lines)


class InMemoryConversationStore:
    """One process, lost on restart. The default, and what every test uses.

    `get` hands back the same object each time, so there is nothing to conflict
    with; `save` only advances the version, as a durable store must. A durable
    store implements the same methods (tests/store_contract.py holds it to
    them) and must raise ConversationConflict on a stale save.
    """

    def __init__(self, clock: Callable[[], str] = utc_now_iso, max_conversations: int = 10_000,
                 receipts: Any = None) -> None:
        self._clock = clock
        # The chat socket's receipts in this process (amiigo/receipts.py), when
        # wired: erased with the person or the conversation (Ruling 14).
        self.receipts = receipts
        # Bounded, so a long-running process does not grow without limit: past
        # `max_conversations`, the least recently used conversation goes, whole
        # (state, transcript, summaries). Everything here is lost on restart
        # anyway; the durable store is MongoDB.
        self.max_conversations = max_conversations
        self._states: "OrderedDict[str, ConversationState]" = OrderedDict()
        self._turns: Dict[str, Dict[int, TranscriptTurn]] = {}
        self._summaries: Dict[str, Dict[str, ConversationSummaryItem]] = {}
        # Media records, permanent like the transcript: conversation id -> S3 key -> record.
        self._media: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Where each run came from (origin.py), permanent: conversation id -> run key -> record.
        self._origins: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Notices in a chat (a ticket closed in Zoho Desk), permanent like the
        # transcript: conversation id -> notice id -> notice. Written under
        # the lock, so one notice is never written twice.
        self._notices: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._notice_lock = threading.Lock()
        # Self-service erasure requests (erasure.py): reference -> request.
        self._erasures: Dict[str, Dict[str, Any]] = {}
        # One pending request per person, even for two requests at once.
        self._erasure_lock = threading.Lock()
        # The erasure_log audit records erasure_admin and delete_person.py write.
        self.erasure_log: List[Dict[str, Any]] = []

    def get(self, conversation_id: str) -> ConversationState:
        state = self._states.get(conversation_id)
        if state is None:
            state = ConversationState(conversation_id=conversation_id, started_at=self._clock(),
                                      turn_offset=max(self._turns.get(conversation_id, {}), default=0))
            self._states[conversation_id] = state
            while len(self._states) > self.max_conversations:
                oldest = next(iter(self._states))
                self.delete_conversation(oldest)
        else:
            self._states.move_to_end(conversation_id)
        return state

    def peek(self, conversation_id: str) -> Optional[ConversationState]:
        """Like `get`, but never creates a state — a presign checking who owns
        a conversation must not itself count as that conversation starting."""
        return self._states.get(conversation_id)

    def save(self, state: ConversationState) -> None:
        state.version += 1
        self._states[state.conversation_id] = state
        self._states.move_to_end(state.conversation_id)

    def record_turn(
        self,
        state: ConversationState,
        inbound: InboundMessage,
        reply: Reply,
        summary: Optional[ConversationSummaryItem] = None,
    ) -> None:
        turns = self._turns.setdefault(state.conversation_id, {})
        for turn in transcript_turns(state, inbound, reply, self._clock()):
            turns[turn.n] = turn
        if summary is not None and state.user_key:
            self._summaries.setdefault(state.user_key, {})[summary_key(summary.conversation_id, summary.started_at)] = summary

    def transcript(self, conversation_id: str) -> List[TranscriptTurn]:
        return [turn for _, turn in sorted(self._turns.get(conversation_id, {}).items())]

    # -- the Amiigo history (amiigo/history.py) ------------------------------

    def runs_of(self, user_key: str, channel: Optional[str] = None) -> List[ConversationSummaryItem]:
        """Every run of every conversation of one person: one summary each.
        Grouped into conversations by amiigo/history.py."""
        return [s for s in self._summaries.get(user_key, {}).values() if channel is None or s.channel == channel]

    def owner_of(self, conversation_id: str) -> Optional[str]:
        """The user key of the conversation's summaries: None when it has
        none, SHARED_OWNER when they name more than one person."""
        keys = {s.user_key for items in self._summaries.values() for s in items.values()
                if s.conversation_id == conversation_id}
        if not keys:
            return None
        return keys.pop() if len(keys) == 1 else SHARED_OWNER

    def turns_of(self, conversation_id: str) -> List[TranscriptTurn]:
        return self.transcript(conversation_id)

    def count_turns(self, conversation_id: str) -> int:
        return len(self._turns.get(conversation_id, {}))

    def notices_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        notices = self._notices.get(conversation_id, {}).values()
        return [dict(n) for n in sorted(notices, key=lambda n: (n["at"], n["_id"]))]

    def add_notice(self, conversation_id: str, user_key: Optional[str], kind: str, text: str,
                   at: str) -> Tuple[Dict[str, Any], bool]:
        """A notice in the chat, numbered after its others, and whether this
        call wrote it. A notice of the same kind and text already in the chat
        is returned as it is: a chat says one thing once."""
        with self._notice_lock:
            mine = self._notices.setdefault(conversation_id, {})
            same = next((n for n in mine.values() if n["kind"] == kind and n["text"] == text), None)
            if same is not None:
                return dict(same), False
            notice = notice_doc(conversation_id, max(map(notice_seq, mine), default=0) + 1, user_key, kind, text, at)
            mine[notice["_id"]] = notice
            return dict(notice), True

    def record_media(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (the S3 key): recording the same object twice
        (a retried claim) replaces its record rather than duplicating it."""
        self._media.setdefault(record["conversation_id"], {})[record["_id"]] = record

    def media_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        records = self._media.get(conversation_id, {}).values()
        return sorted(records, key=lambda r: r["stored_at"])

    def record_origin(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (one per run): filling in a run's country or person
        replaces its record."""
        self._origins.setdefault(record["conversation_id"], {})[record["_id"]] = dict(record)

    def origins_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        return sorted(self._origins.get(conversation_id, {}).values(), key=lambda r: r["started_at"])

    def conversations_of(self, user_key: str) -> List[str]:
        """Every conversation id tied to one person."""
        mine = {cid for cid, state in self._states.items() if state.user_key == user_key}
        mine |= {s.conversation_id for s in self._summaries.get(user_key, {}).values()}
        mine |= {cid for cid, runs in self._origins.items()
                 if any(r.get("user_key") == user_key for r in runs.values())}
        mine |= {cid for cid, notices in self._notices.items()
                 if any(n.get("user_key") == user_key for n in notices.values())}
        if self.receipts is not None:
            mine |= self.receipts.conversations_of(user_key)
        return sorted(mine)

    def pending_erasure_of(self, user_key: str) -> Optional[Dict[str, Any]]:
        return next((dict(r) for r in self._erasures.values()
                     if r.get("user_key") == user_key and r["status"] == "pending"), None)

    def request_erasure(self, user_key: str, channel: str, conversation_id: Optional[str], now: str,
                        proof: Optional[Dict[str, str]] = None) -> str:
        """One pending request per person: asking again returns it."""
        with self._erasure_lock:
            pending = self.pending_erasure_of(user_key)
            if pending is not None:
                return pending["_id"]
            reference = erasure_rules.new_reference()
            while reference in self._erasures:
                reference = erasure_rules.new_reference()
            self._erasures[reference] = {"_id": reference, "user_key": user_key, "status": "pending",
                                         "requested_at": now, "channel": channel,
                                         "conversation_id": conversation_id, "attempts": 0, "last_error": None,
                                         "proof": proof}
            return reference

    def cancel_erasure(self, user_key: str, now: str) -> Optional[str]:
        pending = self.pending_erasure_of(user_key)
        if pending is None:
            return None
        self.close_erasure(pending["_id"], "cancelled", None, None, now)
        return pending["_id"]

    def pending_erasures(self) -> List[Dict[str, Any]]:
        pending = [dict(r) for r in self._erasures.values() if r["status"] == "pending"]
        return sorted(pending, key=lambda r: r["requested_at"])

    def record_erasure_failure(self, reference: str, error: str) -> int:
        record = self._erasures[reference]
        record["attempts"] += 1
        record["last_error"] = error
        return record["attempts"]

    def close_erasure(self, reference: str, status: str, counts: Optional[Dict[str, int]],
                      error: Optional[str], now: str, by: Optional[str] = None) -> None:
        """Closed: the person's key is replaced by its hash. Reviews, a hold
        and who closed it stay: names and times only."""
        record = self._erasures[reference]
        user_key = record.pop("user_key", None)
        if user_key:
            record["key_sha256"] = erasure_rules.key_sha256(user_key)
        record.update(status=status, processed_at=now, counts=counts)
        if error is not None:
            record["last_error"] = error
        if by is not None:
            record["by"] = by

    def record_erasure_review(self, reference: str, by: str, at: str, totals: Dict[str, int]) -> None:
        """Who read a request before deciding (erasure_admin show), and what it then held."""
        self._erasures[reference].setdefault("reviews", []).append({"by": by, "at": at, "totals": dict(totals)})

    def hold_erasure(self, reference: str, by: str, at: str, note: str) -> None:
        self._erasures[reference]["held"] = {"by": by, "at": at, "note": note}

    def erasure_history(self, user_key: str) -> List[Dict[str, Any]]:
        """Every request of one person, open or closed (a closed one by its hash)."""
        digest = erasure_rules.key_sha256(user_key)
        mine = [dict(r) for r in self._erasures.values()
                if r.get("user_key") == user_key or r.get("key_sha256") == digest]
        return sorted(mine, key=lambda r: r["requested_at"])

    def erasure_record(self, reference: str) -> Optional[Dict[str, Any]]:
        record = self._erasures.get(reference)
        return dict(record) if record else None

    def log_erasure(self, entry: Dict[str, Any]) -> None:
        self.erasure_log.append(dict(entry))

    def recent_summaries(
        self, user_key: str, limit: int = 3, exclude: Optional[str] = None
    ) -> List[ConversationSummaryItem]:
        items = [s for key, s in self._summaries.get(user_key, {}).items() if key != exclude]
        return sorted(items, key=lambda s: s.started_at, reverse=True)[:limit]

    def delete_person(self, user_key: str, dry_run: bool = False) -> Dict[str, int]:
        """Everything held about one person: the right to erasure (DPDP, GDPR).

        Every conversation that is theirs, whole: the working state, every
        transcript turn (those from before they signed in too) and the
        summaries. The counts say what went, for the person running the
        deletion to confirm. With `dry_run`, counts what would go and deletes
        nothing (erasure_admin's review).
        """
        mine = set(self.conversations_of(user_key))
        if dry_run:
            return {
                "conversations": sum(1 for cid in mine if cid in self._states),
                "transcript_turns": sum(len(self._turns.get(cid, {})) for cid in mine),
                "media": sum(len(self._media.get(cid, {})) for cid in mine),
                "conversation_origins": sum(len(self._origins.get(cid, {})) for cid in mine),
                "conversation_notices": sum(len(self._notices.get(cid, {})) for cid in mine),
                "amiigo_receipts": sum(self._receipts_of(cid, dry_run=True) for cid in mine),
                "conversation_summaries": len(self._summaries.get(user_key, {})) + sum(
                    1 for key, items in self._summaries.items() if key != user_key
                    for item in items.values() if item.conversation_id in mine),
            }
        counts = {"conversations": 0, "transcript_turns": 0, "media": 0, "conversation_origins": 0,
                  "conversation_notices": 0, "amiigo_receipts": 0,
                  "conversation_summaries": len(self._summaries.pop(user_key, {}))}
        for cid in mine:
            for name, count in self.delete_conversation(cid).items():
                counts[name] += count
        return counts

    def delete_conversation(self, conversation_id: str) -> Dict[str, int]:
        """One conversation, whoever it belongs to: for a chat that was never
        tied to a verified person, found by its id."""
        summaries = 0
        for items in self._summaries.values():
            for key in [k for k, s in items.items() if s.conversation_id == conversation_id]:
                del items[key]
                summaries += 1
        return {
            "conversations": 1 if self._states.pop(conversation_id, None) is not None else 0,
            "transcript_turns": len(self._turns.pop(conversation_id, {})),
            "conversation_summaries": summaries,
            "media": len(self._media.pop(conversation_id, {})),
            "conversation_origins": len(self._origins.pop(conversation_id, {})),
            "conversation_notices": len(self._notices.pop(conversation_id, {})),
            "amiigo_receipts": self._receipts_of(conversation_id),
        }

    def _receipts_of(self, conversation_id: str, dry_run: bool = False) -> int:
        if self.receipts is None:
            return 0
        return self.receipts.delete_conversation(conversation_id, dry_run=dry_run)

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history

    def __len__(self) -> int:
        return len(self._states)


# The name every caller already imports.
ConversationStore = InMemoryConversationStore
