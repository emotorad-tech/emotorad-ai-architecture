"""The serial-photo ask (the person's rules, 7 October 2026).

When a customer reports an issue, the photos the bot asks for depend on the
part. The bot helps first; when it asks for the customer's own media
(evidence_asks.asks_for_media), code adds this request to that reply, once
per bike, with an example picture of each label from the reference library.
The model never writes it and cannot leave it out.

- Every issue: the bike's frame number sticker.
- A battery issue, besides: the battery's serial sticker, the controller's
  label and, on any bike but a Doodle, the battery's warranty seal (the
  example shows a downtube pack's).

A Doodle owner is shown the Doodle battery's sticker; anyone else, the
downtube battery's. On a bike the melt ask (melt_ask.py) was sent for, the
battery sticker and the controller label are not asked again: that ask
already asked for them.

Reading the serials and the seal off the customer's photos, and keeping them,
is serial_read.py; confirming a reading with the customer is
serial_confirm.py. Switched on by EMOTORAD_SERIAL_ASK, exactly "on", and even
then only when every catalogue picture exists and is code-only; api.py
decides once, at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import media

SWITCH_ENV = "EMOTORAD_SERIAL_ASK"

BATTERY_KEY = "battery_serial_label"
BATTERY_DOODLE_KEY = "battery_serial_label_doodle"
CONTROLLER_KEY = "controller_serial_label"
SEAL_KEY = "battery_warranty_seal_intact"
FRAME_KEY = "frame_number_sticker"
KEYS = (BATTERY_KEY, BATTERY_DOODLE_KEY, CONTROLLER_KEY, SEAL_KEY, FRAME_KEY)

# serials_asked_frames' entry while no bike is chosen.
NO_BIKE = "-"

# The parts, as serial_read.py names what a photo shows.
BATTERY = "battery"
CONTROLLER = "controller"
SEAL = "warranty_seal"
FRAME = "frame"

# What each part is called in the ask, in the order they are listed.
_ITEMS_EN = {
    BATTERY: "the serial number sticker on your battery",
    CONTROLLER: "the controller's label, on the frame where the battery slots in",
    SEAL: "the warranty seal sticker on your battery",
    FRAME: "your bike's frame number sticker, on the seat tube below the seat",
}
# DRAFT: for a Hindi speaker to check before real traffic. Used when the bot's
# reply is in Devanagari (the rule evidence_asks uses).
_ITEMS_HI = {
    BATTERY: "आपकी बैटरी पर लगा सीरियल नंबर स्टिकर",
    CONTROLLER: "फ़्रेम पर, जहाँ बैटरी लगती है, वहाँ कंट्रोलर का लेबल",
    SEAL: "आपकी बैटरी पर लगा वारंटी सील स्टिकर",
    FRAME: "सीट के नीचे सीट ट्यूब पर लगा आपकी बाइक का फ़्रेम नंबर स्टिकर",
}
_COUNT_EN = {1: "a photo", 2: "two photos", 3: "three photos", 4: "four photos"}
_COUNT_HI = {1: "एक फ़ोटो", 2: "दो फ़ोटो", 3: "तीन फ़ोटो", 4: "चार फ़ोटो"}


def switch_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(SWITCH_ENV) == "on"


def is_doodle(product_name: Optional[str]) -> bool:
    return "doodle" in (product_name or "").lower()


def parts_for(battery_issue: bool, product_name: Optional[str], melt_asked: bool = False) -> List[str]:
    """The parts to ask for, in order: the battery's for a battery issue (less
    what the melt ask already asked for, and the seal on a Doodle), then the
    frame number for every issue."""
    parts: List[str] = []
    if battery_issue:
        if not melt_asked:
            parts += [BATTERY, CONTROLLER]
        if not is_doodle(product_name):
            parts.append(SEAL)
    parts.append(FRAME)
    return parts


def text_for(parts: Sequence[str], hindi: bool) -> str:
    """The request, added below the bot's own ask so the customer sends all
    of it together: one part in a sentence, several as a numbered list."""
    items = [(_ITEMS_HI if hindi else _ITEMS_EN)[part] for part in parts]
    count = (_COUNT_HI if hindi else _COUNT_EN)[len(parts)]
    if len(items) == 1:
        if hindi:
            return "इसके साथ, कृपया %s की %s भी भेजें। नीचे दी गई तस्वीर दिखाती है कि यह कहाँ है।" % (items[0], count)
        return ("Along with that, please send a photo of %s. The picture below shows where it is." % items[0])
    listed = "\n".join("%d. %s" % (n, item) for n, item in enumerate(items, 1))
    if hindi:
        return "इसके साथ, कृपया %s भी भेजें:\n%s\nनीचे दी गई तस्वीरें दिखाती हैं कि हर एक कहाँ है।" % (count, listed)
    return ("Along with that, please send %s so we can identify your bike and its parts:\n%s\n"
            "The pictures below show where each one is." % (count, listed))


@dataclass(frozen=True)
class SerialAsk:
    """The ask, switched on: its catalogue pictures and the store that signs them."""

    items: Mapping[str, Mapping[str, Any]]
    store: Any

    @staticmethod
    def text(parts: Sequence[str], hindi: bool) -> str:
        return text_for(parts, hindi)

    @staticmethod
    def key_for(part: str, product_name: Optional[str]) -> str:
        if part == BATTERY:
            return BATTERY_DOODLE_KEY if is_doodle(product_name) else BATTERY_KEY
        return {CONTROLLER: CONTROLLER_KEY, SEAL: SEAL_KEY, FRAME: FRAME_KEY}[part]

    def pictures(self, parts: Sequence[str], product_name: Optional[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
        """One picture per part as a turn's attachments (the dicts
        send_guide_media returns), resolved now; and the keys that would not
        resolve, which are left out (never a URL)."""
        pictures: List[Dict[str, Any]] = []
        missing: List[str] = []
        for part in parts:
            key = self.key_for(part, product_name)
            try:
                found = media.resolve(self.items[key], self.store)
            except Exception:  # resolve reports rather than raises; belt and braces
                found = {"unresolved": True}
            if found.get("unresolved") or not found.get("url"):
                missing.append(key)
                continue
            pictures.append({field: found.get(field) for field in ("kind", "url", "mime_type", "caption", "poster")})
        return pictures, missing


def from_env(catalogue: Mapping[str, Mapping[str, Any]], store: Any,
             environ: Optional[Mapping[str, str]] = None) -> Tuple[Optional[SerialAsk], str]:
    """The ask, or None, and what /health says: "on", "off", or "off: " and
    why. Read once, at startup (api.py)."""
    if not switch_on(environ):
        return None, "off"
    missing = [key for key in KEYS if key not in catalogue]
    offered = [key for key in KEYS if key in catalogue and not media.is_code_only(catalogue[key])]
    reasons = []
    if missing:
        reasons.append("missing " + ", ".join(missing))
    if offered:
        reasons.append("not code_only " + ", ".join(offered))
    if reasons:
        return None, "off: " + "; ".join(reasons)
    return SerialAsk(items={key: dict(catalogue[key]) for key in KEYS}, store=store), "on"
