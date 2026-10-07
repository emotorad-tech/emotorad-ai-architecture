"""Confirming a serial read off the customer's photo (the person's rule, 7 October 2026).

serial_read.py reads the battery serial, the motor serial, the controller's
S/N and the frame number off the photos the serial ask asked for, and keeps each reading
unconfirmed. This puts each reading to the customer once: "We read your
frame number as X. Is that right?". Yes confirms it. No asks them to type it
exactly as printed, and what they type replaces the reading (the photo's
reading is kept beside it). A reading Gemini could not make is asked for by
typing straight away. The warranty seal is never asked about: it is not a
number.

All of it is code: the model never writes a question, never reads an answer
and never sees a reading it could repeat. The runtime adds the question to
the end of a fault agent's reply, and a graph node (`serial_confirm`, after
the safety, handoff, erasure and verify gates) takes the answer. A message
that is not an answer (a question, more photos) goes on to the agent as
usual, and the question is added once more to that reply; after that it is
dropped, and the readings stay unconfirmed.

This module is pure: it takes the confirmation under way (a dict kept on the
conversation, `ConversationState.serial_confirm`) and the customer's words,
and returns the next one, the reply and the store updates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .serial_read import looks_like_serial

# The parts confirmed with the customer, in the order they are asked.
PARTS = ("battery", "motor", "controller", "frame")

LABELS_EN = {"battery": "battery serial number", "motor": "motor serial number",
             "controller": "controller serial number (S/N)", "frame": "frame number"}
# DRAFT: for a Hindi speaker to check before real traffic.
LABELS_HI = {"battery": "बैटरी सीरियल नंबर", "motor": "मोटर सीरियल नंबर", "controller": "कंट्रोलर सीरियल नंबर (S/N)",
             "frame": "फ़्रेम नंबर"}

# Words, lowercased. Matched as whole tokens of the explicit class below,
# never with \b or \w alone (Devanagari vowel signs fall outside \w).
YES = frozenset({"yes", "y", "yeah", "yep", "yup", "ya", "haan", "han", "haa", "ha", "hn", "ji", "correct",
                 "right", "sahi", "sahee", "हाँ", "हां", "हा", "जी", "सही"})
# Not "ok" or "theek hai": customers say them to mean "I heard you", and a
# number confirmed by an acknowledgement is not confirmed.
NO = frozenset({"no", "n", "nope", "nah", "nahi", "nahin", "nai", "nhi", "galat", "wrong", "incorrect",
                "नहीं", "नही", "गलत", "ग़लत"})
SKIP = frozenset({"skip", "later", "pata", "dont", "don't", "cant", "can't", "unable", "पता"})
_TOKENS = re.compile(r"[\wऀ-ॿ']+")
# An answer is short: a longer message is the customer saying something else.
SHORT = 5
# Typing a serial: at most this many tokens ("EMIN 2407 1501 23").
TYPED_TOKENS = 4
# The question goes with the agent's reply at most this many times in all.
MAX_ASKS = 2
# Tries at typing one serial before it is left unconfirmed.
MAX_TYPING_TRIES = 2

CONFIRM, WHICH, TYPE = "confirm", "which", "type"


def _tokens(text: str) -> List[str]:
    return _TOKENS.findall((text or "").lower())


def _hindi(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


@dataclass
class Outcome:
    """What one answer did. `reply` None: the message was not an answer."""

    confirm: Optional[Dict[str, Any]]
    reply: Optional[str]
    # (reading id, the fields to set) for the store.
    updates: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    # For the log: what happened, never a value.
    event: str = ""


def start(readings: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """A confirmation for the readings not yet put to the customer, or None.
    Readable ones are confirmed together; unreadable ones are typed after."""
    wanted = sorted((r for r in readings if r.get("part") in PARTS),
                    key=lambda r: (PARTS.index(r["part"]), r.get("read_at") or "", r["_id"]))
    if not wanted:
        return None
    values = {r["_id"]: {"part": r["part"], "serial": r.get("serial")} for r in wanted}
    confirm = [r["_id"] for r in wanted if r.get("serial")]
    typing = [r["_id"] for r in wanted if not r.get("serial")]
    return {"step": CONFIRM if confirm else TYPE, "confirm": confirm, "typing": typing, "values": values,
            "asks": 0, "tries": 0}


def question(state: Mapping[str, Any], hindi: bool) -> str:
    """The question for the step the confirmation is at."""
    values = state["values"]
    labels = LABELS_HI if hindi else LABELS_EN
    if state["step"] == CONFIRM:
        ids = state["confirm"]
        if len(ids) == 1:
            one = values[ids[0]]
            if hindi:
                return ("आपकी फ़ोटो से हमने आपका %s %s पढ़ा है। क्या यह सही है? कृपया हाँ या नहीं में जवाब दें।"
                        % (labels[one["part"]], one["serial"]))
            return ("We read your %s from your photo as %s. Is that right? Please reply YES or NO."
                    % (labels[one["part"]], one["serial"]))
        listed = "\n".join("%d. %s: %s" % (n, labels[values[i]["part"]][:1].upper() + labels[values[i]["part"]][1:],
                                             values[i]["serial"]) for n, i in enumerate(ids, 1))
        if hindi:
            return ("आपकी फ़ोटो से हमने ये पढ़े हैं:\n%s\nक्या ये सही हैं? हाँ लिखें, या जो गलत है उसका नंबर लिखें।"
                    % listed)
        return "We read these from your photos:\n%s\nAre they right? Reply YES, or the number of any that is wrong." % listed
    if state["step"] == WHICH:
        if hindi:
            return "कौन सा गलत है? कृपया उसका नंबर लिखें, जैसे 1।"
        return "Which one is wrong? Please reply with its number, for example 1."
    current = values[state["typing"][0]]
    label = labels[current["part"]]
    if not current.get("serial"):
        if hindi:
            return "हम फ़ोटो से आपका %s नहीं पढ़ पाए। कृपया इसे ठीक वैसे ही टाइप करें जैसा लिखा है।" % label
        return "We couldn't read your %s from the photo. Please type it exactly as it is printed." % label
    if hindi:
        return "कृपया अपना %s ठीक वैसे ही टाइप करें जैसा लिखा है।" % label
    return "Please type your %s exactly as it is printed." % label


def _thanks(hindi: bool, several: bool) -> str:
    if hindi:
        return "धन्यवाद, मैंने इन्हें नोट कर लिया है।" if several else "धन्यवाद, मैंने इसे नोट कर लिया है।"
    return "Thank you, I've noted them." if several else "Thank you, I've noted it."


def _after_confirming(state: Dict[str, Any], wrong: Sequence[str], now: str, hindi: bool, event: str) -> Outcome:
    """The readings not named wrong are confirmed; the wrong ones are typed,
    before any that could not be read."""
    right = [i for i in state["confirm"] if i not in wrong]
    updates = [(i, {"confirmed": True, "confirmed_at": now, "confirmed_by": "customer"}) for i in right]
    typing = list(wrong) + [i for i in state["typing"] if i not in wrong]
    if not typing:
        return Outcome(None, _thanks(hindi, len(right) > 1), updates, event)
    nxt = dict(state, step=TYPE, confirm=[], typing=typing, tries=0)
    return Outcome(nxt, question(nxt, hindi), updates, event)


def _numbers(tokens: Sequence[str], count: int) -> Optional[List[int]]:
    found = [int(t) for t in tokens if t.isascii() and t.isdigit()]
    if not found or any(n < 1 or n > count for n in found):
        return None
    return sorted(set(found))


def answer(state: Mapping[str, Any], text: str, now: str) -> Outcome:
    """The confirmation after the customer's message `text`; `reply` None
    when the message is not an answer."""
    current = dict(state)
    hindi = _hindi(text)
    tokens = _tokens(text)
    yes = any(t in YES for t in tokens)
    no = any(t in NO for t in tokens)
    step = current["step"]

    if step in (CONFIRM, WHICH):
        ids = current["confirm"]
        if len(tokens) > SHORT or not tokens:
            return Outcome(current, None)
        numbers = _numbers(tokens, len(ids))
        if numbers is not None and not yes:
            return _after_confirming(current, [ids[n - 1] for n in numbers], now, hindi, "wrong_named")
        if yes and not no:
            return _after_confirming(current, [], now, hindi, "confirmed")
        if no and not yes and step == CONFIRM:
            if len(ids) == 1:
                return _after_confirming(current, ids, now, hindi, "wrong")
            nxt = dict(current, step=WHICH)
            return Outcome(nxt, question(nxt, hindi), [], "which")
        return Outcome(current, None)

    # Typing one serial.
    reading_id = current["typing"][0]
    rest = current["typing"][1:]
    if not tokens or len(tokens) > TYPED_TOKENS:
        return Outcome(current, None)
    if any(t in SKIP or t in NO for t in tokens):
        return _next_typing(current, rest, [], hindi, "typing_skipped")
    typed = re.sub(r"\s+", "", text.strip()).upper()
    if not looks_like_serial(typed):
        tries = current["tries"] + 1
        if tries >= MAX_TYPING_TRIES:
            return _next_typing(current, rest, [], hindi, "typing_given_up")
        label = (LABELS_HI if hindi else LABELS_EN)[current["values"][reading_id]["part"]]
        nxt = dict(current, tries=tries)
        if hindi:
            return Outcome(nxt, "यह %s जैसा नहीं लगता। कृपया केवल अक्षर और अंक, ठीक वैसे ही टाइप करें जैसे लिखे हैं।"
                           % label, [], "typing_retry")
        return Outcome(nxt, "That doesn't look like a %s. Please type only the letters and numbers, exactly as "
                            "printed." % label, [], "typing_retry")
    ocr = current["values"][reading_id].get("serial")
    update = (reading_id, {"serial": typed, "ocr_serial": ocr, "source": "typed", "confirmed": True,
                           "confirmed_at": now, "confirmed_by": "customer"})
    return _next_typing(current, rest, [update], hindi, "typed")


def _next_typing(state: Mapping[str, Any], rest: Sequence[str], updates: List[Tuple[str, Dict[str, Any]]],
                 hindi: bool, event: str) -> Outcome:
    if not rest:
        return Outcome(None, _thanks(hindi, False), updates, event)
    nxt = dict(state, typing=list(rest), tries=0)
    return Outcome(nxt, question(nxt, hindi), updates, event)
