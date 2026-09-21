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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

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
    history: List[Dict[str, Any]] = field(default_factory=list)
    # Every phase change, for debugging a conversation that went sideways. The
    # transcript says what was said; this says what the platform decided.
    transitions: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError("unknown conversation phase: %r" % (self.phase,))

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
                    texts.append(block.get("text", ""))
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


class ConversationStore:
    """In-memory conversation state.

    Replace with the session store before anything multi-instance ships — two
    web dynos with separate dicts would put a customer in two different phases
    depending on which one answered. The interface is `get` and nothing else.
    """

    def __init__(self) -> None:
        self._states: Dict[str, ConversationState] = {}

    def get(self, conversation_id: str) -> ConversationState:
        state = self._states.get(conversation_id)
        if state is None:
            state = ConversationState(conversation_id=conversation_id)
            self._states[conversation_id] = state
        return state

    def peek(self, conversation_id: str) -> Optional[ConversationState]:
        """Like `get`, but never creates a state — a presign checking who owns
        a conversation must not itself count as that conversation starting."""
        return self._states.get(conversation_id)

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history

    def __len__(self) -> int:
        return len(self._states)
