"""The melt ask (the person's brief, 6 October 2026).

When a customer says something melted, the bot answers with one fixed message
written by code, not the model: don't use or charge the bike, and send these
three together (a photo of the battery's serial sticker, a photo of the
controller's label, a short video of both ends), with a reference picture for
each in the same reply. The knowledge record `battery-melted-terminal` asks
for the two ends one at a time; the AFS PDF asks for all three in one message,
and the person chose the PDF's way and kept the record's "stop using it".

Switched on by EMOTORAD_MELT_ASK, exactly "on", and even then only when all
three catalogue pictures exist, are code-only and resolve. The battery serial
sticker has no photo yet, so it stays off until one is uploaded and its
catalogue entry added (docs/runbooks/media.md). api.py decides once, at
startup; the runtime is handed the result (Runtime(melt_ask=...)), so the CLI,
the playground and the tests are unchanged unless they pass one.

What triggers it is the record's own symptoms, read through the knowledge
loader and never edited, plus the Devanagari phrases below. Safety runs first
and wins: "burnt", "jal gaya" and "very hot" are safety stops, and the safety
reply goes out instead (guardrails.py, unchanged).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Pattern, Sequence, Tuple

from . import media
from .contract import Attachment

SWITCH_ENV = "EMOTORAD_MELT_ASK"

# The catalogue keys, in the order the pictures are attached.
KEYS = ("melt_battery_serial", "melt_controller_label", "melt_terminals")

# The knowledge record whose symptoms trigger the ask. Read only.
RECORD_ID = "battery-melted-terminal"

# Hindi, by the customer's own words: the record's symptoms are English and
# Hinglish only.
DEVANAGARI_PHRASES = ("पिघल गया", "पिघल गई", "पिघला", "पिघल गए", "मेल्ट")

# British English, approved by the person on 6 October 2026.
TEXT_EN = (
    "Thanks for telling me. Until we've checked it, please don't use or charge the bike.\n"
    "\n"
    "To check it, please send these three together:\n"
    "1. A photo of the serial number sticker on your battery.\n"
    "2. A photo of the controller's label, on the frame where the battery slots in.\n"
    "3. A short video of the battery's metal terminals and the connector on the frame. "
    "Hold the camera still on each for a second.\n"
    "\n"
    "The pictures below show what each one looks like."
)

# DRAFT: for a Hindi speaker to check before real traffic. Sent when the
# message that triggered the ask has a Devanagari character (the rule
# evidence_asks and evidence_check use); Hinglish gets the English.
TEXT_HI = (
    "बताने के लिए धन्यवाद। जब तक हम इसकी जाँच न कर लें, कृपया बाइक न चलाएँ और न ही चार्ज करें।\n"
    "\n"
    "जाँच के लिए कृपया ये तीनों एक साथ भेजें:\n"
    "1. आपकी बैटरी पर लगे सीरियल नंबर स्टिकर की फ़ोटो।\n"
    "2. फ़्रेम पर, जहाँ बैटरी लगती है, वहाँ कंट्रोलर के लेबल की फ़ोटो।\n"
    "3. बैटरी के धातु वाले टर्मिनल और फ़्रेम के कनेक्टर का एक छोटा वीडियो। "
    "हर एक पर कैमरा एक सेकंड स्थिर रखें।\n"
    "\n"
    "नीचे दी गई तस्वीरों में हर एक का उदाहरण है।"
)

# ASCII phrases stand alone: no letter or digit either side, so "unmelted"
# and "E-061" do not count. Explicit lookarounds, never \b or \w, which miss
# Devanagari (CLAUDE.md). Under IGNORECASE these cover capitals too.
_ASCII_BEFORE = r"(?<![a-z0-9])"
_ASCII_AFTER = r"(?![a-z0-9])"
_SPACES = r"\s+"


def trigger_phrases(records: Optional[Sequence[Any]] = None) -> Tuple[str, ...]:
    """The record's symptoms, then the Devanagari phrases. `records` defaults
    to the knowledge loader's. A missing record raises LookupError; from_env
    turns that into "off" with the reason on /health, so a renamed record is
    seen at startup rather than leaving a trigger with no phrases."""
    if records is None:
        from .knowledge import load_records

        records = load_records()
    found = [record for record in records if record.id == RECORD_ID]
    if not found:
        raise LookupError("knowledge record %r not found" % RECORD_ID)
    phrases: List[str] = []
    for phrase in tuple(found[0].symptoms) + DEVANAGARI_PHRASES:
        phrase = " ".join(str(phrase).split())
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return tuple(phrases)


def _phrase_pattern(phrase: str) -> str:
    body = _SPACES.join(re.escape(word) for word in phrase.split())
    if phrase.isascii():
        return _ASCII_BEFORE + body + _ASCII_AFTER
    # Non-ASCII matches as a substring.
    return body


def compile_trigger(phrases: Sequence[str]) -> Pattern[str]:
    """One case-insensitive pattern for every phrase, longest first."""
    ordered = sorted(set(phrases), key=lambda phrase: (-len(phrase), phrase))
    return re.compile("|".join(_phrase_pattern(phrase) for phrase in ordered), re.IGNORECASE)


def triggered(pattern: Pattern[str], text: Optional[str]) -> bool:
    return bool(text) and pattern.search(text) is not None


def switch_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    """On only when EMOTORAD_MELT_ASK is exactly "on"."""
    env = os.environ if environ is None else environ
    return env.get(SWITCH_ENV) == "on"


@dataclass(frozen=True)
class MeltAsk:
    """The ask, switched on: the three pictures in order, the store that signs
    their links, and the trigger."""

    items: Tuple[Tuple[str, Mapping[str, Any]], ...]
    store: Any
    pattern: Pattern[str]

    def triggered(self, text: Optional[str]) -> bool:
        return triggered(self.pattern, text)

    @staticmethod
    def text(hindi: bool) -> str:
        return TEXT_HI if hindi else TEXT_EN

    def pictures(self) -> Tuple[List[Attachment], List[str]]:
        """The pictures as the channel renders them, resolved now, the way
        send_guide_media resolves one; and the keys that would not resolve,
        which are left out (never a URL)."""
        pictures: List[Attachment] = []
        missing: List[str] = []
        for key, item in self.items:
            try:
                found = media.resolve(item, self.store)
            except Exception:  # resolve reports rather than raises; belt and braces
                found = {"unresolved": True}
            if found.get("unresolved") or not found.get("url"):
                missing.append(key)
                continue
            pictures.append(Attachment(
                kind=found.get("kind") or "image",
                url=found["url"],
                mime_type=found.get("mime_type"),
                caption=found.get("caption"),
                poster=found.get("poster"),
            ))
        return pictures, missing


def from_env(
    catalogue: Mapping[str, Mapping[str, Any]],
    store: Any,
    environ: Optional[Mapping[str, str]] = None,
    records: Optional[Sequence[Any]] = None,
) -> Tuple[Optional[MeltAsk], str]:
    """The ask, or None, and what /health says: "on", "off", or "off: " and
    the keys that are missing, not code-only or would not resolve. Read once,
    at startup (api.py)."""
    if not switch_on(environ):
        return None, "off"
    missing = [key for key in KEYS if key not in catalogue]
    present = [key for key in KEYS if key in catalogue]
    offered = [key for key in present if not media.is_code_only(catalogue[key])]
    unresolvable = [key for key in present
                    if media.resolve(catalogue[key], store).get("unresolved")]
    reasons = []
    if missing:
        reasons.append("missing " + ", ".join(missing))
    if offered:
        reasons.append("not code_only " + ", ".join(offered))
    if unresolvable:
        reasons.append("unresolvable " + ", ".join(unresolvable))
    if reasons:
        return None, "off: " + "; ".join(reasons)
    try:
        pattern = compile_trigger(trigger_phrases(records))
    except LookupError:
        return None, "off: no knowledge record %s" % RECORD_ID
    return MeltAsk(items=tuple((key, dict(catalogue[key])) for key in KEYS), store=store, pattern=pattern), "on"
