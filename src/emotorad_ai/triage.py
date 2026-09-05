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
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .agents import battery_support, motor_support
from .conversation import (
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    ROUTED,
    ConversationState,
)
from .contract import InboundMessage
from .identity import ResolvedIdentity

# The built-in table. Runtime passes the catalogue's table instead, so a bot
# added as configuration is routable without this file changing; the default
# keeps `classify_issue("...")` working for callers and tests that want today's
# behaviour.
TOPIC_KEYWORDS: Dict[str, Sequence[str]] = {
    battery_support.TOPIC: battery_support.KEYWORDS,
    motor_support.TOPIC: motor_support.KEYWORDS,
}

DEFAULT_SUPPORTED_SUMMARY = "battery and motor problems"

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


def classify_issue(
    text: str, keywords: Optional[Mapping[str, Sequence[str]]] = None
) -> Optional[str]:
    """Topic from keywords, or None when the model needs to decide.

    Returning None is a real answer, not a failure: forcing a guess here is how a
    motor complaint ends up in a battery agent, which then troubleshoots the
    wrong component confidently and at length.
    """
    table = keywords if keywords is not None else TOPIC_KEYWORDS
    lowered = text.lower()
    matched = [
        topic for topic, words in table.items()
        if any(word in lowered for word in words)
    ]
    if len(matched) != 1:
        # Zero means we do not know. More than one means the message covers two
        # components — "the motor is noisy and the battery drains fast" is two
        # issues, and picking the one with more keyword hits silently drops the
        # other. Both cases end in asking, which is cheap and correct.
        return None
    return matched[0]


def topic_from_pill(
    pill: Optional[str], keywords: Optional[Mapping[str, Sequence[str]]] = None
) -> Optional[str]:
    """Canonical topic for a channel's own pill vocabulary.

    Every channel names its entry points differently — a WhatsApp template
    `battery_issue`, an Amiigo screen `battery_health`, an IVR keypress `1`. They
    all have to land on the same topic, so the mapping lives here rather than
    each adapter guessing what triage wants to be told.

    Unmapped values fall through to keyword matching on the pill itself, which
    covers most of them without a table entry; anything left returns None and is
    resolved from the message text instead of being force-fitted.
    """
    table = keywords if keywords is not None else TOPIC_KEYWORDS
    if not pill:
        return None
    if pill in table:
        return pill
    return classify_issue(pill.replace("_", " "), table)


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


def describe_bike(bike: Dict[str, Any]) -> str:
    parts = [bike.get("product_name") or "your bike"]
    if bike.get("product_color"):
        parts.append("(%s)" % bike["product_color"])
    frame = bike.get("frame_number")
    if frame:
        parts.append("ending %s" % frame[-4:])
    return " ".join(parts)


class TriageAgent:
    """Greets, narrows to one bike, captures the issue, hands off."""

    def __init__(
        self,
        topic_agents: Dict[str, str],
        keywords: Optional[Mapping[str, Sequence[str]]] = None,
        supported_summary: Optional[str] = None,
    ) -> None:
        # topic -> sub-agent name, e.g. {"battery": "battery_support"}.
        self.topic_agents = topic_agents
        self.keywords = keywords if keywords is not None else TOPIC_KEYWORDS
        # What the unsupported-topic reply says we *can* do. Derived from the
        # catalogue in production so a new bot is named without editing a string.
        self.supported_summary = supported_summary or DEFAULT_SUPPORTED_SUMMARY

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
        pill_topic = topic_from_pill(pill, self.keywords)
        topic = pill_topic or classify_issue(text, self.keywords)
        source = "pill:%s" % pill if pill_topic else "text"

        if state.selected_frame is None:
            bikes = resolved.bikes
            if len(bikes) > 1:
                state.move_to(AWAITING_BIKE_SELECTION, "%d bikes" % len(bikes))
                state.pending_topic = topic
                state.pending_topic_source = source
                return TriageOutcome(
                    reply=self._ask_which_bike(bikes),
                    reason="multiple_bikes:%d" % len(bikes),
                    metadata={"bikes": [b["frame_number"] for b in bikes]},
                )
            if len(bikes) == 1:
                state.select_bike(bikes[0]["frame_number"])

        return self._route_or_ask(topic, state, source)

    # -- phases --------------------------------------------------------------

    def _ask_which_bike(self, bikes: Sequence[Dict[str, Any]]) -> str:
        lines = ["You have %d bikes registered with us. Which one is this about?" % len(bikes)]
        for index, bike in enumerate(bikes, start=1):
            lines.append("%d. %s" % (index, describe_bike(bike)))
        return "\n".join(lines)

    def _resolve_selection(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> TriageOutcome:
        bike = match_bike(text, resolved.bikes)
        if bike is None:
            # Re-ask rather than guess. An unmatched reply usually means the
            # customer answered something else entirely, and picking a bike here
            # would silently attach the whole conversation to the wrong one.
            return TriageOutcome(
                reply=(
                    "Sorry, I did not catch which bike you meant. "
                    + self._ask_which_bike(resolved.bikes)
                ),
                reason="selection_unmatched",
            )

        state.select_bike(bike["frame_number"])
        state.move_to(AWAITING_ISSUE, "bike_selected")
        topic = state.pending_topic
        source = state.pending_topic_source or "text"
        state.pending_topic = None
        state.pending_topic_source = None
        return self._route_or_ask(topic, state, source)

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
                    "I can help with %s from here. For anything else, "
                    "let me put you through to the support team." % self.supported_summary
                ),
                reason="unsupported_topic:%s" % topic,
            )

        state.move_to(AWAITING_ISSUE, "need_issue")
        return TriageOutcome(
            reply="What is happening with the bike? A short description is enough.",
            reason="issue_unknown",
        )
