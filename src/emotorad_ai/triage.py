"""Triage (build plan §3.5) — a conversation, not a classifier.

The principle this file exists to enforce: **identity sets the option set, intent
picks from it.** Knowing someone owns three bikes does not tell us which one they
mean, and knowing they own a bike does not mean they want to talk about it — they
may want to buy another, or chase an order. So triage greets with context,
narrows to one bike, captures the issue, and only then hands to a sub-agent.

Everything here that *can* be deterministic is. A tapped pill is already the
routing decision; a customer typing "1" against a numbered list is a selection,
not a classification problem. The model is reached for only when free text has to
be understood, which keeps the common paths cheap, testable and identical every
time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .conversation import (
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    ROUTED,
    ConversationState,
)
from .contract import InboundMessage
from .identity import ResolvedIdentity

# Deterministic issue classification. Keywords are the cheap path; anything they
# do not catch falls through to the model rather than being force-fitted here.
# English plus the Hindi/Hinglish terms that actually appear in support traffic —
# a Devanagari-script message must not be a silent miss.
TOPIC_KEYWORDS: Dict[str, Sequence[str]] = {
    # Note what is NOT here: bare "power". It appears in both "won't power on"
    # (battery) and "power cuts out while riding" (drive), so on its own it
    # classifies nothing — it just makes every drive complaint look like a
    # battery one. The phrases below carry the discrimination instead.
    "battery": (
        "battery", "charge", "charging", "charger", "range", "backup",
        "discharge", "drain", "drains", "draining", "not turning on",
        "won't start", "wont start", "dead", "power on", "powering on",
        "no power at all", "बैटरी", "चार्ज", "batri", "charj",
    ),
    "motor": (
        "motor", "noise", "noisy", "sound", "grinding", "jerk", "jerking",
        "pedal assist", "pas", "throttle", "speed", "vibration",
        "cuts out", "cutting out", "cuts off", "cutting off", "power cut",
        "stops while riding", "while riding",
        "मोटर", "आवाज", "awaz",
    ),
}

# Checked in two passes. "the second one" contains the word "one", so a bare
# cardinal must never outrank a true ordinal — that phrasing is common enough
# that treating it as "bike 1" would misroute a large share of selections.
STRONG_ORDINALS = {
    "1": 0, "first": 0, "1st": 0, "पहली": 0,
    "2": 1, "second": 1, "2nd": 1, "दूसरी": 1,
    "3": 2, "third": 2, "3rd": 2, "तीसरी": 2,
}
WEAK_ORDINALS = {"one": 0, "two": 1, "three": 2}


@dataclass
class TriageOutcome:
    """Either a reply triage is making itself, or a hand-off to a sub-agent."""

    reply: Optional[str] = None
    agent: Optional[str] = None
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_handoff(self) -> bool:
        return self.agent is not None


def classify_issue(text: str) -> Optional[str]:
    """Topic from keywords, or None when the model needs to decide.

    Returning None is a real answer, not a failure: forcing a guess here is how a
    motor complaint ends up in a battery agent, which then troubleshoots the
    wrong component confidently and at length.
    """
    lowered = text.lower()
    matched = [
        topic for topic, words in TOPIC_KEYWORDS.items()
        if any(word in lowered for word in words)
    ]
    if len(matched) != 1:
        # Zero means we do not know. More than one means the message covers two
        # components — "the motor is noisy and the battery drains fast" is two
        # issues, and picking the one with more keyword hits silently drops the
        # other. Both cases end in asking, which is cheap and correct.
        return None
    return matched[0]


def topic_from_pill(pill: Optional[str]) -> Optional[str]:
    """Canonical topic for a channel's own pill vocabulary.

    Every channel names its entry points differently — a WhatsApp template
    `battery_issue`, an Amiigo screen `battery_health`, an IVR keypress `1`. They
    all have to land on the same topic, so the mapping lives here rather than
    each adapter guessing what triage wants to be told.

    Unmapped values fall through to keyword matching on the pill itself, which
    covers most of them without a table entry; anything left returns None and is
    resolved from the message text instead of being force-fitted.
    """
    if not pill:
        return None
    if pill in TOPIC_KEYWORDS:
        return pill
    return classify_issue(pill.replace("_", " "))


def match_bike(text: str, bikes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Which bike the customer just picked, or None if it is not unambiguous.

    None is the safe answer and the common one. Selecting the wrong bike sends a
    whole troubleshooting flow against the wrong machine — and because the reply
    still reads plausibly, nobody notices until the customer does.
    """
    if not bikes:
        return None
    lowered = text.lower().strip()

    # 1. Frame number, whole or the tail customers actually read out.
    for bike in bikes:
        frame = (bike.get("frame_number") or "").lower()
        if frame and (frame in lowered or (len(lowered) >= 4 and lowered in frame)):
            return bike

    # 2. Ordinal against the list as it was presented, strong forms first.
    for table in (STRONG_ORDINALS, WEAK_ORDINALS):
        for token, index in table.items():
            if index < len(bikes) and _contains_token(lowered, token):
                return bikes[index]

    # 3. Model name, and colour to break ties between two of the same model.
    named = [
        bike for bike in bikes
        if (bike.get("product_name") or "").lower() in lowered
        or any(
            part in lowered
            for part in (bike.get("product_name") or "").lower().split()
            if len(part) > 3
        )
    ]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        coloured = [
            bike for bike in named
            if (bike.get("product_color") or "").lower()
            and (bike.get("product_color") or "").lower() in lowered
        ]
        if len(coloured) == 1:
            return coloured[0]
    return None


def _contains_token(haystack: str, token: str) -> bool:
    """Whole-word match that also works outside ASCII.

    `\\b` is defined against `\\w`, which excludes Devanagari combining marks — so
    a word boundary after "तीसरी" (ending in the vowel sign ी) never matches and the
    ordinal is silently missed. English keeps the boundary check because "one" must
    not match inside "money"; non-ASCII tokens fall back to a substring test, where
    the false-positive risk is negligible and a missed match is not.
    """
    if token.isascii():
        return re.search(r"\b%s\b" % re.escape(token), haystack) is not None
    return token in haystack


def bike_ref(bike: Dict[str, Any]) -> Optional[str]:
    """The key a bike is chosen by: its frame number, or an internal reference
    for a bike whose frame number is not on record. Never shown."""
    return bike.get("bike_ref") or bike.get("frame_number")


def bike_name(bike: Dict[str, Any]) -> str:
    """The model and colour, with no identifier."""
    name = bike.get("product_name") or "Your bike"
    if bike.get("product_color"):
        name += " (%s)" % bike["product_color"]
    return name


def describe_bike(bike: Dict[str, Any]) -> str:
    """One line of the list. The whole frame number, so the customer can match
    it to the sticker on the frame (the person's rule, 2026-09-30). A bike
    whose frame number is not on record says so, and never shows its IMEI."""
    name = bike_name(bike)
    frame = bike.get("frame_number")
    if frame:
        return "%s, frame %s" % (name, frame)
    if bike.get("frame_on_record") is False:
        return "%s, frame number not on record" % name
    return name


def which_bike_text(bikes: Sequence[Dict[str, Any]]) -> str:
    """The list and the question, for one bike or several. Shared by triage and
    the verify-first step, so the two never word it differently."""
    count = len(bikes)
    lines = ["I found %d bike%s on this number:" % (count, "" if count == 1 else "s")]
    lines += ["%d. %s" % (index, describe_bike(bike)) for index, bike in enumerate(bikes, start=1)]
    if count == 1 and not bikes[0].get("frame_number"):
        # No frame number on record to match one against, so none is asked for.
        lines.append("Is this the bike that needs help? Reply yes or no.")
    elif count == 1:
        lines.append("Is this the bike that needs help? Reply yes, or send the frame number of the bike you mean.")
    else:
        lines.append("Which one needs help? Reply with its number in the list or its frame number.")
    return "\n".join(lines)


# A yes or a no to "is this the bike?", in English and Hindi. A no is the whole
# reply, or a no followed by a comma or a stop ("no, a different one", "nahi,
# dusri"), and may be polite ("ji nahi"): "no power at all" is an issue, not an
# answer. A yes that goes on to say "the other one" is not a yes. The no is
# checked first, so "ji nahi" is never read as its leading "ji".
_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|ya|haan|haa|han|ha|ji|correct|right|sure|ok|okay|that'?s it|this one|same one|हाँ|हां|जी)(?!\w)"
    r"(?!.*\b(?:other|different|another|dusri|dusra)\b)",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(?:(?:ji|जी)\s+)?(?:no|nope|nah|nahi|nahin|नहीं)\s*(?:[,.!]|$)"
    r"|^\s*(?:not this(?: one)?|not that(?: one)?|wrong(?: bike)?|"
    r"a different one|different bike|another one|another bike|other bike)\s*[.!]*\s*$",
    re.IGNORECASE,
)
# A frame number: a few letters, then six or more digits (EMXP2026001234).
_FRAME = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,5}\d{6,}(?![A-Za-z0-9])")


def says_no(text: str) -> bool:
    return bool(_NO.match(text or ""))


def says_yes(text: str) -> bool:
    return not says_no(text) and bool(_YES.match(text or ""))


def names_a_frame(text: str) -> bool:
    return bool(_FRAME.search(text or ""))


class TriageAgent:
    """Greets, narrows to one bike, captures the issue, hands off."""

    def __init__(self, topic_agents: Dict[str, str], unlisted_agent: Optional[str] = None) -> None:
        # topic -> sub-agent name, e.g. {"battery": "battery_support"}.
        self.topic_agents = topic_agents
        # Where a customer goes who says the one bike listed is not theirs:
        # the bike they mean is not registered on this number.
        self.unlisted_agent = unlisted_agent

    def handle(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
    ) -> TriageOutcome:
        text = message.message_text.strip()

        if state.phase == AWAITING_BIKE_SELECTION:
            return self._resolve_selection(text, resolved, state)

        # A tapped pill is the intent, already stated. It still has to pass
        # through bike selection — knowing they tapped "Battery issue" does not
        # say which of three bikes it is about.
        pill = message.pill_clicked
        topic = topic_from_pill(pill) or classify_issue(text)
        source = "pill:%s" % pill if pill and topic_from_pill(pill) else "text"

        if state.selected_frame is None:
            bikes = resolved.bikes
            if len(bikes) > 1:
                state.move_to(AWAITING_BIKE_SELECTION, "%d bikes" % len(bikes))
                state.pending_topic = topic
                state.pending_topic_source = source
                return TriageOutcome(
                    reply=self._ask_which_bike(bikes),
                    reason="multiple_bikes:%d" % len(bikes),
                    metadata={"bikes": [bike_ref(b) for b in bikes]},
                )
            if len(bikes) == 1:
                state.select_bike(bike_ref(bikes[0]), bike_name(bikes[0]))

        return self._route_or_ask(topic, state, source)

    # -- phases --------------------------------------------------------------

    def _ask_which_bike(self, bikes: Sequence[Dict[str, Any]]) -> str:
        return which_bike_text(bikes)

    def _resolve_selection(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> TriageOutcome:
        bikes = resolved.bikes
        if not bikes:
            # Nothing to choose from (the lookup failed after verification):
            # carry on to the issue rather than asking about an empty list.
            state.move_to(AWAITING_ISSUE, "no_bikes_to_choose")
            return self._route_or_ask(self._take_pending(state)[0], state, "text")

        single = len(bikes) == 1
        bike = bikes[0] if single and says_yes(text) else match_bike(text, bikes)
        if bike is None and single and not bikes[0].get("frame_number") and names_a_frame(text):
            # The only bike has no frame number on record, so a frame number
            # typed now is the rider reading theirs, not a different bike.
            bike = bikes[0]
        if bike is None:
            # One bike listed and the customer says it is not theirs, or sends
            # the frame number of one that is not on the list (as the question
            # invites): the bike they mean is not registered on this number.
            if single and self.unlisted_agent and (says_no(text) or names_a_frame(text)):
                self._take_pending(state)
                state.route_to(self.unlisted_agent)
                return TriageOutcome(agent=self.unlisted_agent, reason="bike_not_listed")
            # Re-ask rather than guess. An unmatched reply usually means the
            # customer answered something else entirely, and picking a bike here
            # would silently attach the whole conversation to the wrong one.
            return TriageOutcome(
                reply="Sorry, I did not catch which bike you meant. " + which_bike_text(bikes),
                reason="selection_unmatched",
            )

        state.select_bike(bike_ref(bike), bike_name(bike))
        state.move_to(AWAITING_ISSUE, "bike_selected")
        topic, source = self._take_pending(state)
        return self._route_or_ask(topic, state, source)

    @staticmethod
    def _take_pending(state: ConversationState) -> Tuple[Optional[str], str]:
        topic, source = state.pending_topic, state.pending_topic_source or "text"
        state.pending_topic = None
        state.pending_topic_source = None
        return topic, source

    def _route_or_ask(
        self, topic: Optional[str], state: ConversationState, source: str = "text"
    ) -> TriageOutcome:
        agent = self.topic_agents.get(topic or "")
        if agent:
            state.route_to(agent)
            # The reason records *how* we knew, not just what we decided — a
            # routing mistake looks very different if it came from a tapped pill
            # than if it came from classifying free text.
            return TriageOutcome(agent=agent, reason="%s->%s" % (source, topic))

        if topic and not agent:
            # Classified, but nothing here handles it. Saying so beats routing it
            # to whichever agent happens to be the default.
            state.move_to(AWAITING_ISSUE, "unsupported_topic")
            return TriageOutcome(
                reply=(
                    "I can help with battery and motor problems from here. For anything else, "
                    "let me put you through to the support team."
                ),
                reason="unsupported_topic:%s" % topic,
            )

        state.move_to(AWAITING_ISSUE, "need_issue")
        return TriageOutcome(
            reply="What is happening with the bike? A short description is enough.",
            reason="issue_unknown",
        )
