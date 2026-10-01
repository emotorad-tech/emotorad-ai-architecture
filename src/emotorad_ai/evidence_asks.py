"""Video first (spec 2026-10-01-video-first-evidence-design.md).

When the bot needs to see a fault it asks for a short video first, and a photo
only when the customer cannot take one. The model decides when to ask and what
to show. This module tells the runtime what a reply asked for and when a
customer said a video is not possible, and holds the lines the runtime adds.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Three asks with nothing back are sent; the fourth becomes a hand-over.
MAX_EVIDENCE_ASKS = 3

VIDEO_FIRST_LINE = "If you can, a short video is even better: it shows me more than a photo."
PHOTO_FALLBACK_LINE = "A photo is fine."
# For a reply the model wrote in Devanagari.
VIDEO_FIRST_LINE_HI = "हो सके तो एक छोटा वीडियो भेजें: उसमें फ़ोटो से ज़्यादा दिखता है।"
PHOTO_FALLBACK_LINE_HI = "फ़ोटो भी चलेगी।"

# Sentences end at . ! ? ; a dash, a new line, or the Devanagari full stop.
_SENTENCE = re.compile(r"[^.!?;\n\u2014\u0964]+[.!?\u0964]?")
_VIDEO = re.compile(r"\b(?:videos?|clips?|recordings?)\b|वीडियो", re.IGNORECASE)
_PHOTO = re.compile(
    r"\b(?:photos?|photographs?|pictures?|pics?|images?|snaps?|snapshots?|screenshots?)\b|फ़ोटो|फोटो|तस्वीर",
    re.IGNORECASE,
)
_VERB = r"(?:send|share|upload|attach|record|take|film|shoot|snap|show)"
# A request addressed to the customer. The imperative forms only: "bheja"
# (sent) and "dikhai" (is visible) are not requests.
_ASK = re.compile(
    r"\b(?:could|can|would|will)\s+you\s+(?:please\s+)?(?:also\s+)?(?:quickly\s+)?" + _VERB + r"\b"
    r"|\bplease\s+(?:also\s+)?" + _VERB + r"\b"
    r"|^\W*(?:also\s+)?" + _VERB + r"\b"
    r"|\bbhej(?:o|iye|ein|en|na|do|dein|de)?\b|\bdikha(?:o|iye|ein|en|na|do|dein|de)?\b"
    r"|भेजें|भेजिए|भेजो|भेज\s+दें|भेज\s+दीजिए|दिखाएं|दिखाइए|दिखाओ",
    re.IGNORECASE,
)
# The bot itself sending, in Hinglish: "main aapko photo bhej raha hoon".
_BOT_SENDS = re.compile(r"\b(?:main|mai|maine|hum)\b", re.IGNORECASE)
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

_DECLINES_VIDEO = re.compile(
    r"\b(?:can'?t|cannot|can\s+not|unable\s+to|not\s+able\s+to|won'?t\s+be\s+able\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:take|send|record|shoot|make|upload|do|film|share)\s+(?:\w+\s+){0,3}?videos?\b"
    r"|\b(?:don'?t|do\s+not)\s+have\s+(?:a\s+)?videos?\b"
    r"|\bno\s+videos?\b(?!\s+yet)"
    r"|\bvideos?\s+(?:is\s+|isn'?t\s+|not\s+)(?:not\s+)?possible\b"
    r"|\bcamera\s+(?:doesn'?t|does\s+not|can'?t|cannot|won'?t)\s+record\b"
    r"|\bonly\s+(?:a\s+)?(?:photos?|pictures?|pics?)\b"
    r"|\b(?:photos?|pictures?|pics?)\s+instead\b"
    r"|\bvideo\s+(?:nahi|nahin|nhi)\b|वीडियो\s+नहीं",
    re.IGNORECASE,
)


def asks_for_media(reply: Optional[str]) -> Optional[str]:
    """"video" when a sentence of the reply asks the customer for a video (or
    a video and a photo), "photo" when the sentences that ask name only a
    photo, None when nothing is asked."""
    asked: Optional[str] = None
    for sentence in _SENTENCE.findall(reply or ""):
        if not _ASK.search(sentence) or _BOT_SENDS.search(sentence):
            continue
        if _VIDEO.search(sentence):
            return "video"
        if _PHOTO.search(sentence):
            asked = "photo"
    return asked


def declines_video(text: Optional[str]) -> bool:
    """Whether the customer says a video is not possible."""
    return bool(_DECLINES_VIDEO.search(text or ""))


def added_line(reply: Optional[str], asked: Optional[str], video_declined: bool) -> Optional[Tuple[str, str]]:
    """The line the runtime adds to a reply that asked for evidence, and the
    event it logs, or None.

    A photo-only ask gets the video line, unless the customer said a video is
    not possible, or the reply already speaks of a video (one just arrived and
    a still of a detail is what is needed). A video ask after the customer said
    a video is not possible gets the photo line."""
    text = reply or ""
    hindi = bool(_DEVANAGARI.search(text))
    if asked == "photo" and not video_declined and not _VIDEO.search(text):
        return "video_first_added", VIDEO_FIRST_LINE_HI if hindi else VIDEO_FIRST_LINE
    if asked == "video" and video_declined:
        return "photo_fallback_added", PHOTO_FALLBACK_LINE_HI if hindi else PHOTO_FALLBACK_LINE
    return None
