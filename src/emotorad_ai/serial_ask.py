"""The serial-photo ask (the person's rule, 7 October 2026), for the battery.

When a customer reports an issue, the photos the bot asks for first depend on
the part: a battery issue needs the battery's serial sticker and the
controller's serial label. The bot helps first; when it asks for the
customer's own media (evidence_asks.asks_for_media), code adds this request
to that reply, once per bike, with an example picture of each label from the
reference library. The model never writes it and cannot leave it out.

A Doodle owner is shown the Doodle battery's sticker; anyone else, the
downtube battery's. A bike the melt ask (melt_ask.py) was sent for is not
asked again: that ask already asked for both photos.

Reading the serials off the customer's photos and keeping them is
serial_read.py. Switched on by EMOTORAD_SERIAL_ASK, exactly "on", and even
then only when the three catalogue pictures exist and are code-only; api.py
decides once, at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import media

SWITCH_ENV = "EMOTORAD_SERIAL_ASK"

BATTERY_KEY = "battery_serial_label"
BATTERY_DOODLE_KEY = "battery_serial_label_doodle"
CONTROLLER_KEY = "controller_serial_label"
KEYS = (BATTERY_KEY, BATTERY_DOODLE_KEY, CONTROLLER_KEY)

# Added below the bot's own ask, so the customer sends all of it together.
TEXT_EN = (
    "Along with that, please send two photos so we can identify the parts: the serial number sticker on "
    "your battery, and the controller's label, on the frame where the battery slots in. The pictures below "
    "show where each one is."
)

# DRAFT: for a Hindi speaker to check before real traffic. Used when the bot's
# reply is in Devanagari (the rule evidence_asks uses).
TEXT_HI = (
    "इसके साथ, पार्ट्स की पहचान के लिए कृपया दो फ़ोटो भी भेजें: आपकी बैटरी पर लगे सीरियल नंबर स्टिकर की, "
    "और फ़्रेम पर, जहाँ बैटरी लगती है, वहाँ कंट्रोलर के लेबल की। नीचे दी गई तस्वीरें दिखाती हैं कि दोनों कहाँ हैं।"
)


def switch_on(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(SWITCH_ENV) == "on"


def is_doodle(product_name: Optional[str]) -> bool:
    return "doodle" in (product_name or "").lower()


@dataclass(frozen=True)
class SerialAsk:
    """The ask, switched on: its catalogue pictures and the store that signs them."""

    items: Mapping[str, Mapping[str, Any]]
    store: Any

    @staticmethod
    def text(hindi: bool) -> str:
        return TEXT_HI if hindi else TEXT_EN

    def keys_for(self, product_name: Optional[str]) -> Tuple[str, str]:
        return (BATTERY_DOODLE_KEY if is_doodle(product_name) else BATTERY_KEY), CONTROLLER_KEY

    def pictures(self, product_name: Optional[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
        """The two pictures as a turn's attachments (the dicts send_guide_media
        returns), resolved now; and the keys that would not resolve, which are
        left out (never a URL)."""
        pictures: List[Dict[str, Any]] = []
        missing: List[str] = []
        for key in self.keys_for(product_name):
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
