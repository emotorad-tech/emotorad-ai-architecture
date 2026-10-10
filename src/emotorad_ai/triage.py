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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .conversation import (
    AWAITING_BIKE_CONFIRMATION,
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    AWAITING_UNLISTED_BIKE,
    GREETING,
    ROUTED,
    ConversationState,
)
from .contract import InboundMessage
from .identity import ResolvedIdentity
from .navigation import GREETING_TEXT, is_greeting_only, names_the_mobile, wants_change_number
from .tools import fixtures
from .tools.amigo import MODEL_NAMES

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
        # AFS's motor cases (8 October 2026). Never bare "disc": it is inside
        # "discharge", a battery word, and two topics mean asking.
        "e-07", "e07", "e 07", "e-24", "e24", "freewheel", "disc rotor", "brake disc", "disc bolt",
        "loose disc", "disc loose", "rotor", "threading", "motor jam", "jammed motor", "wheel not",
        "not moving", "speedometer",
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
# A frame number: one token of 7 to 24 letters and digits that starts with a
# letter and has at least two letters and five digits in a row
# (EMXP2026001234, DDL32023045678, the staging bikes' TESTEMXP0000001). Two to
# five letters then digits was too narrow: no staging frame number was read
# (staging, 2026-10-01). Ticket references (EM-00001), upload ids
# (upl_...), phone numbers and pincodes are not tokens of this shape.
_FRAME = re.compile(
    r"(?<![A-Za-z0-9_])(?=(?:[0-9]*[A-Za-z]){2})(?=[A-Za-z0-9]*\d{5})[A-Za-z][A-Za-z0-9]{6,23}(?![A-Za-z0-9_])"
)


def says_no(text: str) -> bool:
    return bool(_NO.match(text or ""))


def says_yes(text: str) -> bool:
    return not says_no(text) and bool(_YES.match(text or ""))


def names_a_frame(text: str) -> bool:
    return bool(_FRAME.search(text or ""))


# --- a bike that is not in the list (spec 2026-10-01, unlisted bike) ---------
# "its not one of these 2" chose bike 2 on staging: the ordinal matched and
# nothing checked for the "not". This is checked before any bike is matched.
_NOT_LISTED = re.compile(
    r"\b(?:not|none)\s+(?:(?:one|any)\s+)?(?:of|from|in|among)\s+"
    r"(?:these|them|those|both|list|the\s+(?:two|three|2|3|above|list)|this\s+list|your\s+list)\b"
    r"|\bneither\b|^\W*none\W*$|\bnot\s+these\b"
    r"|\bnot\s+(?:mine|listed|here|there|in\s+(?:the|this|your)\s+list|on\s+(?:the|this|your)\s+list)\b"
    r"|\b(?:isn'?t|isnt|is\s+not|aren'?t|arent|are\s+not)\s+(?:listed|there|here|shown|showing|mine|my\s+bikes?"
    r"|in\s+(?:the\s+)?list|on\s+(?:the\s+)?list)\b"
    r"|\bnot\s+(?:the\s+)?(?:1|2|3|first|second|third)(?:\s+one)?\s+(?:or|nor)\s+(?:the\s+)?(?:1|2|3|first|second|third)\b"
    r"|\b(?:a\s+)?different\s+(?:one|bike|cycle)\b|\banother\s+(?:bike|cycle)\b"
    r"|\b(?:have|got|bought|own)\s+(?:a\s+)?(?:new|another|different|other)\s+(?:one|bike|cycle)\b"
    r"|\ba\s+third\s+(?:one|bike|cycle)\b"
    r"|\bnot\s+(?:this|that)\s+(?:one|bike)\b"
    r"|\bkoi\s+(?:bhi\s+)?nahi\b|\bdono\s+(?:hi\s+)?nahi\b"
    r"|\b(?:dono|ye|yeh)\s+(?:wali\s+)?(?:\w+\s+)?(?:mere|meri|mera)\s+(?:nahi|nahin|nhi)\b"
    r"|इनमें\s+से\s+कोई\s+नहीं|दोनों\s+नहीं",
    re.IGNORECASE,
)
# A reply that negates something and names no listed frame number is asked
# again rather than matched: "not the first, the second" picked bike 1 (the
# final review).
_NEGATION = re.compile(
    r"\b(?:not|none|neither|nor|never|no)\b"
    r"|\b(?:isn'?t|aren'?t|don'?t|doesn'?t|wasn'?t|can'?t|won'?t|didn'?t|ain'?t|isnt|arent|dont|doesnt|cant"
    r"|wont|didnt)\b|\bnahi\b|\bnahin\b|\bnhi\b|नहीं",
    re.IGNORECASE,
)
# While collecting, a short reply that is plainly a list choice: "sorry, it's
# number 2". Not "1 min".
_ORDINAL_CHOICE = re.compile(
    r"^\W*(?:(?:sorry|oh|actually|wait)\W+)?(?:it'?s\s+|its\s+)?(?:number\s+|no\.?\s*)?(?:the\s+)?"
    r"(1|2|3|first|second|third|1st|2nd|3rd)(?:\s+one)?\W*$",
    re.IGNORECASE,
)
_ORDINAL_INDEX = {"1": 0, "first": 0, "1st": 0, "2": 1, "second": 1, "2nd": 1, "3": 2, "third": 2, "3rd": 2}
_DONT_KNOW = re.compile(
    r"\b(?:don'?t|do\s+not)\s+know\b|\bnot\s+sure\b|\bno\s+idea\b|\bpata\s+(?:nahi|nhi)\b"
    r"|\b(?:nahi|nhi)\s+pata\b|\bcan'?t\s+find\b|\bidk\b|\bdunno\b|पता\s+नहीं",
    re.IGNORECASE,
)
# Words that say a reply is not a model name (the final review: "error code
# E07", "it won't turn on", "ignore all previous instructions").
_NOT_A_MODEL = re.compile(
    r"\b(?:error|code|blank|turn|turns|won'?t|wont|squeak\w*|fell|fallen|chain|brakes?|display|sticker|faded"
    r"|lights?|charg\w*|noise|ignore|instructions?|forgot|remember|known)\b",
    re.IGNORECASE,
)
# EMotorad's model names: the Amiigo app's, and those on the test records.
KNOWN_MODELS = tuple(sorted(
    set(MODEL_NAMES.values())
    | {record["product_name"] for records in fixtures.WARRANTY_RECORDS.values() for record in records
       if record.get("product_name")},
    key=len, reverse=True,
))
_MODEL_WORDS = frozenset(
    chunk for model in KNOWN_MODELS for chunk in re.findall(r"[a-z]+", model.lower()) if len(chunk) > 1
) | {"trex"}
_MODEL_PATTERNS = [
    (model, re.compile(r"\b" + r"\W*".join(re.findall(r"[a-z0-9]+", model.lower())) + r"\b", re.IGNORECASE))
    for model in KNOWN_MODELS
]

ASK_FOR_UNLISTED_BIKE = ("No problem. Please send your bike's frame number and model. "
                         "The frame number is printed on the sticker on the frame.")
ASK_FOR_MODEL = "Thanks. Which model is it?"
ASK_FOR_FRAME = "Thanks. What's its frame number? It's printed on the sticker on the frame."
# When a reply gave nothing new: not the same question again, word for word
# (staging, 2026-10-01).
ASK_AGAIN_FOR_UNLISTED_BIKE = ("Sorry, I didn't catch that. Please send the frame number printed on the sticker "
                               "on the frame, and the model, for example EMX Plus or T-Rex Air.")
ASK_AGAIN_FOR_MODEL = "Sorry, I didn't catch the model. Which model is it, for example EMX Plus or T-Rex Air?"
ASK_AGAIN_FOR_FRAME = "Sorry, I didn't catch the frame number. It's printed on the sticker on the frame."
# Two asks while collecting, the opening one included: each missing part is
# asked for at most twice, then the bot carries on with what it has.
UNLISTED_MAX_ASKS = 2
# The conversation's bike reference when the customer never gave a frame number.
UNLISTED_REF = "unlisted"

# --- confirming the bike once (spec 2026-10-02) ------------------------------
# After the customer said their bike is not in the list, the bike is confirmed
# once before it is the conversation's: their own details, or the listed bike
# whose frame number they gave.
CONFIRM_LISTED = "That frame number is the {name} in your list. Is that the bike?"
ASK_WHICH_WRONG = "Which is wrong, the frame number or the model?"
ASK_WHICH_WRONG_AGAIN = "Sorry, which part is wrong: the frame number, the model, or both?"
ASK_RIGHT_FRAME = "No problem. What's the right frame number? It's printed on the sticker on the frame."
ASK_RIGHT_MODEL = "No problem. Which model is it?"
CONFIRM_AGAIN = "Sorry, I didn't catch that. {question} Please reply yes or no."
# An answer that is neither yes nor no is asked again this many times, then
# taken as no.
CONFIRM_MAX_UNCLEAR = 1
# A no to "is that right?", in more words than a no to the bike list allows:
# here "no power at all" cannot be the answer.
_CONFIRM_NO = re.compile(
    r"^\W*(?:no|nope|nah|nahi|nahin|nhi|galat|wrong|incorrect|not\s+(?:right|correct|quite))\b"
    r"|^\W*(?:that'?s|it'?s|this\s+is)\s+(?:wrong|incorrect|not\s+(?:right|correct))\b|^\W*नहीं",
    re.IGNORECASE,
)
_WRONG_BOTH = re.compile(r"\b(?:both|dono|donon|everything)\b|दोनों", re.IGNORECASE)
_WRONG_FRAME = re.compile(r"\b(?:frame|chassis|sticker|number)\b|फ्रेम", re.IGNORECASE)
_WRONG_MODEL = re.compile(r"\b(?:model|name)\b|मॉडल", re.IGNORECASE)


def confirm_text(bike: Optional[Dict[str, Optional[str]]]) -> str:
    """The question for the customer's own details, saying what is missing."""
    bike = bike or {}
    model, frame = bike.get("model"), bike.get("frame_number")
    if model and frame:
        details = "your bike is the %s, frame %s" % (model, frame)
    elif model:
        details = "your bike is the %s, frame number not given" % model
    else:
        details = "your bike's frame number is %s, model not given" % frame
    return "Just to confirm: %s. Is that right?" % details


def not_listed(text: str) -> bool:
    return bool(_NOT_LISTED.search(text or ""))


# Short words that come before a number in ordinary text: "call me on
# 9876543210", "invoice no 123456". Never joined into a frame number.
_JOIN_STOP = frozenset((
    "on", "is", "no", "to", "at", "in", "of", "me", "my", "by", "or", "it", "as", "be", "we", "us", "so", "do",
    "go", "up", "an", "am", "if", "the", "and", "for", "was", "are", "not", "its", "nos", "num", "call", "date",
    "from", "dated", "hai", "ka", "ki", "ko", "se", "mera", "meri", "phone", "mob",
    # Words before a number that is not a frame number, now that a frame's
    # letters can be up to ten long (staging, 2026-10-01).
    "pincode", "pin", "zip", "code", "otp", "mobile", "number", "order", "invoice", "bill", "ticket", "ref",
    "reference", "id", "whatsapp", "contact", "amount", "rs", "inr", "price", "year", "km", "kms",
))


def _joined(text: str) -> str:
    """A frame number typed with a space or hyphen before its digits, joined:
    "emxp 2026009999" is EMXP2026009999. Only a word of two to five letters
    followed by six or more digits, and never an ordinary short word."""
    def join(match: "re.Match[str]") -> str:
        word = match.group(1)
        return match.group(0) if word.lower() in _JOIN_STOP else word + match.group(2)

    return re.sub(r"\b([A-Za-z]{2,10})[\s-]+(\d{6,})\b", join, text or "")


def _find_frame(text: str) -> Optional[str]:
    match = _FRAME.search(_joined(text))
    return match.group(0).upper() if match else None


def known_model(text: str) -> Optional[str]:
    """An EMotorad model named in the text, the longest when several match
    ("T-Rex Plus V2" over "T-Rex Plus"). Frame numbers are left out first:
    TREX2024881201 is not a T-Rex, and it holds "X2"."""
    words = _FRAME.sub(" ", _joined(text))
    for model, pattern in _MODEL_PATTERNS:
        if pattern.search(words):
            return model
    return None


def unlisted_label(bike: Optional[Dict[str, Optional[str]]]) -> str:
    bike = bike or {}
    parts = [bike.get("model") or "", "frame %s" % bike["frame_number"] if bike.get("frame_number") else ""]
    return ", ".join(part for part in parts if part) or "your bike"


def unlisted_context(bike: Optional[Dict[str, Optional[str]]]) -> str:
    """What the agent is told about the conversation's unlisted bike, or ""."""
    if not bike:
        return ""
    text = ("The customer's bike for this conversation is not registered on their number: %s, as they read it. "
            "There is no warranty record for it: never say it is or is not covered." % unlisted_label(bike))
    if bike.get("frame_number"):
        text += " Use this frame number on any ticket."
    return "\n\n" + text


def unlisted_as_bike(bike: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """The unlisted bike in the shape a listed one has, for the knowledge
    filter and Jev."""
    return {"product_name": bike.get("model"), "frame_number": bike.get("frame_number"), "on_record": False,
            # The coverage line the agent sees (battery_support._coverage_line).
            "coverage_status": "not_on_this_number"}


def _listed_frame(text: str, bikes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A listed bike whose own frame number the text gives, as a whole token,
    in any case or spacing. Against the list itself, not a frame-number shape:
    a listed frame the shape missed was taken for a new bike (staging,
    2026-10-01)."""
    tokens = {token.upper() for token in re.findall(r"[A-Za-z0-9]+", _joined(text))}
    for bike in bikes:
        frame = re.sub(r"[\s-]+", "", bike.get("frame_number") or "").upper()
        if frame and frame in tokens:
            return bike
    return None


def _model_as_typed(text: str, bike: Dict[str, Optional[str]], frame_in_reply: Optional[str]) -> bool:
    """Whether a reply is the model, as typed, when only the model is missing:
    a short answer that is not a question, not the issue, not "I don't know"."""
    words = (text or "").split()
    if not (bike.get("frame_number") and not bike.get("model") and not frame_in_reply and words and len(words) <= 4):
        return False
    if (text.strip().endswith("?") or classify_issue(text) is not None or _DONT_KNOW.search(text)
            or not_listed(text) or _NOT_A_MODEL.search(text)):
        return False
    # It must look like a model (the final review: "ok", "wait" and "2" were
    # kept): a word from EMotorad's model names, or a token of letters and
    # digits ("X5", "V2").
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    return any(token.lower() in _MODEL_WORDS for token in tokens) or any(
        re.search(r"[A-Za-z]", token) and re.search(r"\d", token) for token in tokens)


def _ordinal_choice(text: str, bikes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    match = _ORDINAL_CHOICE.match(text or "")
    if not match:
        return None
    index = _ORDINAL_INDEX[match.group(1).lower()]
    return bikes[index] if index < len(bikes) else None


class TriageAgent:
    """Greets, narrows to one bike, captures the issue, hands off."""

    def __init__(self, topic_agents: Dict[str, str], battery_melt: Optional[Callable[[str], bool]] = None) -> None:
        # topic -> sub-agent name, e.g. {"battery": "battery_support"}.
        self.topic_agents = topic_agents
        # The melt ask, when it is on (melt_ask.MeltAsk.battery_melt; the
        # controller's ruling 2 of 6 October 2026): a battery melt is the
        # battery topic, so "connector melted" or "E-06" is routed rather than
        # asked "What is happening?". None, the ask off: exactly classify_issue.
        self.battery_melt = battery_melt

    def classify(self, text: str) -> Optional[str]:
        """The topic of free text: the battery for a battery melt while the
        melt ask is on, otherwise classify_issue."""
        if self.battery_melt is not None and self.battery_melt(text):
            return "battery"
        return classify_issue(text)

    def handle(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
    ) -> TriageOutcome:
        text = message.message_text.strip()

        if state.phase == AWAITING_BIKE_CONFIRMATION:
            return self._resolve_confirmation(text, resolved, state)

        if state.phase == AWAITING_UNLISTED_BIKE:
            return self._collect_unlisted(text, resolved, state)

        if state.phase == AWAITING_BIKE_SELECTION:
            return self._resolve_selection(text, resolved, state)

        if (state.phase == GREETING and state.turns <= 1 and state.selected_frame is None
                and not message.pill_clicked and not message.attachments and is_greeting_only(text)):
            # A signed-in rider who only greets (spec 2026-10-02): greeted back,
            # with the AI line the first reply has to carry (disclosure.py).
            return TriageOutcome(reply=GREETING_TEXT, reason="greeting")

        # A tapped pill is the intent, already stated. It still has to pass
        # through bike selection — knowing they tapped "Battery issue" does not
        # say which of three bikes it is about.
        pill = message.pill_clicked
        topic = topic_from_pill(pill) or self.classify(text)
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
        # Before any ordinal, frame number or model is matched (spec
        # 2026-10-01, unlisted bike): "its not one of these 2" chose bike 2.
        if not_listed(text) or (single and says_no(text)):
            return self._start_unlisted(text, resolved, state)
        # A frame number that is not in the list, before any model or ordinal
        # is matched ("EMX Plus, frame EMXP2026009999" picked the listed EMX
        # Plus). Only when every listed bike has its frame number on record: an
        # app bike's sticker frame is that bike, read out (the final review).
        if (_find_frame(text) and all(b.get("frame_number") for b in bikes)
                and _listed_frame(text, bikes) is None):
            return self._start_unlisted(text, resolved, state)
        if _NEGATION.search(text) and _listed_frame(text, bikes) is None:
            return TriageOutcome(
                reply="Sorry, I did not catch which bike you meant. " + which_bike_text(bikes),
                reason="selection_negated",
            )
        bike = bikes[0] if single and says_yes(text) else match_bike(text, bikes)
        if bike is None and single and not bikes[0].get("frame_number") and names_a_frame(text):
            # The only bike has no frame number on record, so a frame number
            # typed now is the rider reading theirs, not a different bike.
            bike = bikes[0]
        if bike is None:
            # Re-ask rather than guess. An unmatched reply usually means the
            # customer answered something else entirely, and picking a bike here
            # would silently attach the whole conversation to the wrong one.
            return TriageOutcome(
                reply="Sorry, I did not catch which bike you meant. " + which_bike_text(bikes),
                reason="selection_unmatched",
            )

        state.select_bike(bike_ref(bike), bike_name(bike))
        # A listed bike chosen: any bike given earlier as unlisted is dropped.
        state.unlisted_bike, state.unlisted_asks, state.bike_confirmation = None, 0, None
        state.move_to(AWAITING_ISSUE, "bike_selected")
        topic, source = self._take_pending(state)
        return self._route_or_ask(topic, state, source)

    def _start_unlisted(self, text: str, resolved: ResolvedIdentity, state: ConversationState) -> TriageOutcome:
        state.move_to(AWAITING_UNLISTED_BIKE, "bike_not_listed")
        state.unlisted_bike = {"frame_number": None, "model": None}
        state.unlisted_asks = 0
        return self._collect_unlisted(text, resolved, state, opening=True)

    def _collect_unlisted(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState, opening: bool = False
    ) -> TriageOutcome:
        """The frame number and model of a bike that is not in the list,
        confirmed once (spec 2026-10-02), then on with the issue (spec
        2026-10-01, unlisted bike)."""
        if (wants_change_number(text) and not names_the_mobile(text)
                and (state.unlisted_bike or {}).get("frame_number")):
            # "sorry wrong number": the frame number just given, typed wrong
            # (the final review, 2026-10-02).
            state.unlisted_bike = dict(state.unlisted_bike, frame_number=None)
            return TriageOutcome(reply=ASK_RIGHT_FRAME, reason="unlisted_bike:correct")
        listed = _listed_frame(text, resolved.bikes)
        if listed is not None:
            # A listed bike's own frame number: confirmed once before it is
            # chosen (spec 2026-10-02).
            return self._confirm_listed(listed, state)
        if not (state.unlisted_bike or {}).get("frame_number"):
            chosen = _ordinal_choice(text, resolved.bikes)
            if chosen is not None:
                # "sorry, it's number 2": a choice from the list the bot showed,
                # which needs no confirming.
                return self._choose_listed(chosen, state)
        before = dict(state.unlisted_bike or {"frame_number": None, "model": None})
        bike = dict(before)
        frame = _find_frame(text)
        if frame and not bike.get("frame_number"):
            bike["frame_number"] = frame
        model = known_model(text)
        if model is None and _model_as_typed(text, bike, frame):
            model = " ".join(text.split())[:60]
        if model and not bike.get("model"):
            bike["model"] = model
        # The issue, if they gave it here, is kept for when the bike is known.
        topic = self.classify(text)
        if topic and not state.pending_topic:
            state.pending_topic, state.pending_topic_source = topic, "text"
        state.unlisted_bike = bike
        # Something new this reply: a part of the bike, or the issue.
        learnt = bike != before or bool(topic)
        missing = [part for part in ("frame_number", "model") if not bike.get(part)]
        if missing and state.unlisted_asks < UNLISTED_MAX_ASKS:
            state.unlisted_asks += 1
            again = not opening and not learnt
            if len(missing) == 2:
                reply = ASK_AGAIN_FOR_UNLISTED_BIKE if again else ASK_FOR_UNLISTED_BIKE
            elif missing == ["model"]:
                reply = ASK_AGAIN_FOR_MODEL if again else ASK_FOR_MODEL
            else:
                reply = ASK_AGAIN_FOR_FRAME if again else ASK_FOR_FRAME
            return TriageOutcome(reply=reply, reason="unlisted_bike:ask:%s" % "+".join(missing))
        if missing == ["frame_number", "model"]:
            # Nothing was given to confirm: on with the issue, as before.
            return self._unlisted_done(state)
        return self._confirm_unlisted(state)

    def _choose_listed(self, listed: Dict[str, Any], state: ConversationState) -> TriageOutcome:
        """A listed bike after all: chosen, and said, so the customer knows
        which bike the rest of the chat is about."""
        state.unlisted_bike, state.unlisted_asks, state.bike_confirmation = None, 0, None
        state.select_bike(bike_ref(listed), bike_name(listed))
        state.move_to(AWAITING_ISSUE, "bike_selected")
        topic, source = self._take_pending(state)
        outcome = self._route_or_ask(topic, state, source)
        if outcome.reason == "issue_unknown":
            frame = " frame %s," % listed["frame_number"] if listed.get("frame_number") else ""
            return TriageOutcome(reply="Thanks: that's the %s in the list,%s. %s" % (
                bike_name(listed), frame.rstrip(","), outcome.reply), reason=outcome.reason)
        return outcome

    def _confirm_listed(self, listed: Dict[str, Any], state: ConversationState) -> TriageOutcome:
        state.bike_confirmation = {"kind": "listed", "ref": bike_ref(listed), "step": "confirm", "unclear": 0}
        state.move_to(AWAITING_BIKE_CONFIRMATION, "confirm_listed")
        return TriageOutcome(reply=CONFIRM_LISTED.format(name=bike_name(listed)), reason="confirm_bike:listed")

    def _confirm_unlisted(self, state: ConversationState) -> TriageOutcome:
        state.bike_confirmation = {"kind": "unlisted", "ref": None, "step": "confirm", "unclear": 0}
        state.move_to(AWAITING_BIKE_CONFIRMATION, "confirm_unlisted")
        return TriageOutcome(reply=confirm_text(state.unlisted_bike), reason="confirm_bike:unlisted")

    def _resolve_confirmation(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> TriageOutcome:
        """The answer to "is that right?" (spec 2026-10-02): a yes makes the
        bike the conversation's; a correction is taken and confirmed; a no asks
        what is wrong, or, for a listed bike, goes back to asking for the
        details; anything else is asked again once, then taken as a no."""
        topic = self.classify(text)
        if topic and not state.pending_topic:
            state.pending_topic, state.pending_topic_source = topic, "text"
        asked = dict(state.bike_confirmation or {"kind": "unlisted", "ref": None, "step": "confirm", "unclear": 0})
        if asked.get("step") == "which_wrong":
            return self._which_wrong(text, resolved, state, asked)
        listed = None
        if asked.get("kind") == "listed":
            listed = next((bike for bike in resolved.bikes if bike_ref(bike) == asked.get("ref")), None)
            if listed is None:
                # That bike has left the list since: ask for the details again.
                state.bike_confirmation = None
                return self._start_unlisted("", resolved, state)
        if says_yes(text):
            return self._choose_listed(listed, state) if listed is not None else self._unlisted_done(state)
        corrected = self._correction(text, resolved, state)
        if corrected is not None:
            return corrected
        refused = says_no(text) or bool(_CONFIRM_NO.match(text or ""))
        if not refused and asked.get("unclear", 0) < CONFIRM_MAX_UNCLEAR:
            state.bike_confirmation = dict(asked, unclear=asked.get("unclear", 0) + 1)
            question = (CONFIRM_LISTED.format(name=bike_name(listed)) if listed is not None
                        else confirm_text(state.unlisted_bike))
            return TriageOutcome(reply=CONFIRM_AGAIN.format(question=question), reason="confirm_bike:unclear")
        if listed is not None:
            # Not that bike: its details again, from the start.
            state.bike_confirmation = None
            return self._start_unlisted("", resolved, state)
        state.bike_confirmation = dict(asked, step="which_wrong", unclear=0)
        return TriageOutcome(reply=ASK_WHICH_WRONG, reason="confirm_bike:no")

    def _correction(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> Optional[TriageOutcome]:
        """A frame number or a model in the answer itself ("no, it's a Doodle
        V3"): taken, and the bike confirmed again. A listed bike's frame number
        is that bike, confirmed as such. None when the answer gives neither."""
        listed = _listed_frame(text, resolved.bikes)
        if listed is not None and (state.bike_confirmation or {}).get("ref") != bike_ref(listed):
            return self._confirm_listed(listed, state)
        frame, model = _find_frame(text), known_model(text)
        if not (frame or model) or listed is not None:
            return None
        bike = dict(state.unlisted_bike or {"frame_number": None, "model": None})
        if frame:
            bike["frame_number"] = frame
        if model:
            bike["model"] = model
        state.unlisted_bike = bike
        return self._confirm_unlisted(state)

    def _which_wrong(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState, asked: Dict[str, Any]
    ) -> TriageOutcome:
        """The answer to "which is wrong?": that part is asked for again."""
        corrected = self._correction(text, resolved, state)
        if corrected is not None:
            return corrected
        both = bool(_WRONG_BOTH.search(text or ""))
        frame_wrong = both or bool(_WRONG_FRAME.search(text or ""))
        model_wrong = both or bool(_WRONG_MODEL.search(text or ""))
        if not (frame_wrong or model_wrong):
            if asked.get("unclear", 0) < CONFIRM_MAX_UNCLEAR:
                state.bike_confirmation = dict(asked, unclear=asked.get("unclear", 0) + 1)
                return TriageOutcome(reply=ASK_WHICH_WRONG_AGAIN, reason="confirm_bike:which_unclear")
            frame_wrong = model_wrong = True
        bike = dict(state.unlisted_bike or {"frame_number": None, "model": None})
        if frame_wrong:
            bike["frame_number"] = None
        if model_wrong:
            bike["model"] = None
        state.unlisted_bike, state.bike_confirmation = bike, None
        # This ask counts: one more is allowed before carrying on with what it has.
        state.unlisted_asks = 1
        state.move_to(AWAITING_UNLISTED_BIKE, "correcting")
        if frame_wrong and model_wrong:
            reply = ASK_FOR_UNLISTED_BIKE
        elif frame_wrong:
            reply = ASK_RIGHT_FRAME
        else:
            reply = ASK_RIGHT_MODEL
        return TriageOutcome(reply=reply, reason="unlisted_bike:correct")

    def _unlisted_done(self, state: ConversationState) -> TriageOutcome:
        state.bike_confirmation = None
        bike = state.unlisted_bike or {}
        state.select_bike(bike.get("frame_number") or UNLISTED_REF, unlisted_label(bike))
        state.move_to(AWAITING_ISSUE, "unlisted_bike")
        topic, source = self._take_pending(state)
        outcome = self._route_or_ask(topic, state, source)
        if outcome.reason == "issue_unknown":
            return TriageOutcome(reply="Thanks: %s. %s" % (unlisted_label(bike), outcome.reply), reason=outcome.reason)
        return outcome

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
