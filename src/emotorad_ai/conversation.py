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
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .attachments import MACHINE_TEXT_PREFIXES
from .contract import InboundMessage, Reply
from .observability import redact_pii

# Phases. A conversation moves forward through these, and can move back — a
# customer who says "actually, my other bike" returns to bike selection from a
# routed state, which is why this is a field rather than a one-way sequence.
GREETING = "greeting"
AWAITING_BIKE_SELECTION = "awaiting_bike_selection"
AWAITING_ISSUE = "awaiting_issue"
ROUTED = "routed"
PHASES = (GREETING, AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ROUTED)


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
    agent: Optional[str] = None
    # Topic understood before we knew which bike it was about. A customer taps
    # "Battery issue" and *then* picks a bike from three; without this the intent
    # is lost between turns and they get asked what is wrong all over again.
    pending_topic: Optional[str] = None
    pending_topic_source: Optional[str] = None
    # Rendered enrichment block. Built once per conversation, not per turn: a
    # customer's bikes and history do not change mid-chat, and rebuilding it every
    # turn also moves it in the prompt, which defeats prefix caching.
    context_block: Optional[str] = None
    turns: int = 0
    disclosed: bool = False
    # Whether any photo or video has arrived in this conversation. Held per
    # conversation, not per turn: a customer who sent the picture three turns ago
    # must not be asked for it again because the model concluded later.
    evidence_seen: bool = False
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

    def move_to(self, phase: str, reason: str = "") -> None:
        if phase not in PHASES:
            raise ValueError("unknown conversation phase: %r" % (phase,))
        self.transitions.append("%s->%s%s" % (self.phase, phase, ":" + reason if reason else ""))
        self.phase = phase

    def select_bike(self, frame_number: str) -> None:
        """Record which bike this conversation is about.

        Changing it mid-conversation is legitimate — customers do say "no, the
        other one" — but it must reset the routed agent, because troubleshooting
        already done applies to a different bike.
        """
        if self.selected_frame and self.selected_frame != frame_number:
            self.agent = None
            self.sub_category = None
            self.transitions.append("bike_changed:%s->%s" % (self.selected_frame, frame_number))
        self.selected_frame = frame_number

    def route_to(self, agent: str) -> None:
        self.agent = agent
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
    )
    bot = TranscriptTurn(
        n=n + 1, role="bot", text=redact_pii(reply.text or ""), at=at,
        attachments=tuple({"kind": a.kind, "url": recorded_url(a.url)} for a in reply.attachments),
        handled_by=reply.handled_by or "", path=str(reply.metadata.get("route") or ""),
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

    def __init__(self, clock: Callable[[], str] = utc_now_iso, max_conversations: int = 10_000) -> None:
        self._clock = clock
        # Bounded, so a long-running process does not grow without limit: past
        # `max_conversations`, the least recently used conversation goes, whole
        # (state, transcript, summaries). Everything here is lost on restart
        # anyway; the durable store is MongoDB.
        self.max_conversations = max_conversations
        self._states: "OrderedDict[str, ConversationState]" = OrderedDict()
        self._turns: Dict[str, Dict[int, TranscriptTurn]] = {}
        self._summaries: Dict[str, Dict[str, ConversationSummaryItem]] = {}

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

    def recent_summaries(
        self, user_key: str, limit: int = 3, exclude: Optional[str] = None
    ) -> List[ConversationSummaryItem]:
        items = [s for key, s in self._summaries.get(user_key, {}).items() if key != exclude]
        return sorted(items, key=lambda s: s.started_at, reverse=True)[:limit]

    def delete_person(self, user_key: str) -> Dict[str, int]:
        """Everything held about one person: the right to erasure (DPDP, GDPR).

        Every conversation that is theirs, whole: the working state, every
        transcript turn (those from before they signed in too) and the
        summaries. The counts say what went, for the person running the
        deletion to confirm.
        """
        mine = {cid for cid, state in self._states.items() if state.user_key == user_key}
        mine |= {s.conversation_id for s in self._summaries.get(user_key, {}).values()}
        counts = {"conversations": 0, "transcript_turns": 0, "conversation_summaries": len(self._summaries.pop(user_key, {}))}
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
        }

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history

    def __len__(self) -> int:
        return len(self._states)


# The name every caller already imports.
ConversationStore = InMemoryConversationStore
