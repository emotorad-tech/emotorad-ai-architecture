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
# An ask is a request whose object is the photo or video (the final review,
# 2026-10-01): "take a look at the picture", "a clearer picture" and "I will
# send you a picture" have a request and a media word, and ask for nothing.
_MEDIA = (r"(?:videos?|clips?|recordings?|photos?|photographs?|pictures?|pics?|images?|snaps?|snapshots?"
          r"|screenshots?)")
_REQUEST = (
    r"(?:\b(?:could|can|would|will)\s+you\s+(?:(?:please|also|quickly|kindly|just)\s+)*"
    r"|\b(?:would|are)\s+you\s+(?:be\s+)?able\s+to\s+(?:(?:please|also)\s+)*"
    r"|\bif\s+(?:possible|you\s+can|you\s+could)\W+(?:(?:please|also)\s+)*"
    r"|\b(?:please|kindly)\s+(?:(?:also|just|quickly)\s+)*"
    r"|^\W*(?:(?:also|just|now)\s+)*)"
)
_VERB = r"(?:send|share|upload|attach|record|take|film|shoot|snap|show|grab|capture)"
# What may stand between the verb and the media word: "a short", "two
# clear", "a few more", "one continuous".
_BETWEEN = (r"(?:(?:a|an|the|one|two|three|some|few|more|another|short|quick|clear|close-?up|small|brief"
            r"|little|your|its|both|each|single|continuous)\s+){0,4}")
_ASK_EN = re.compile(
    _REQUEST + _VERB + r"\b(?:\s+(?:me|us|over|across))?[\s:,\-]*" + _BETWEEN + _MEDIA + r"\b"
    r"|" + _REQUEST + r"(?:show|record|film|capture)\b[^.?!]{0,60}?\b(?:in|on|as)\s+(?:an?\s+)?"
    r"(?:short\s+|quick\s+)?" + _MEDIA + r"\b",
    re.IGNORECASE,
)
# Hinglish and Hindi put the media word first and the request after it, and
# only request forms count: "bhej diya" (sent) and "dikha raha" (is showing)
# do not.
_ASK_HINGLISH = re.compile(
    r"\b(?:video|photo|foto|pic|picture|clip|tasveer)\b(?:\s+\S+){0,3}?\s+"
    r"(?:bhej(?:iye|ein|en|o|do|dijiye|dein)|bhej\s+(?:sakte|sakti|denge|dijiye|do|dein|dena)"
    r"|dikha(?:iye|ein|o|do|dijiye)|dikha\s+(?:sakte|sakti|dijiye|do|dena)"
    r"|le\s+(?:sakte|sakti|lijiye|lo)|kheench(?:iye|o|ein)|bana(?:iye|o|ein)|bana\s+(?:sakte|sakti|dijiye))\b",
    re.IGNORECASE,
)
_ASK_HINDI = re.compile(
    r"(?:वीडियो|फ़ोटो|फोटो|तस्वीर)(?:\s+\S+){0,3}?\s+"
    r"(?:भेजें|भेजिए|भेजो|भेज\s+दें|भेज\s+दीजिए|भेज\s+सकते|भेज\s+सकती|दिखाएं|दिखाइए|दिखाओ|दिखा\s+सकते"
    r"|दिखा\s+सकती|लें|लीजिए|ले\s+सकते|ले\s+सकती)"
)
# A document or an identifier is not fault evidence: no video line, no count.
_DOCUMENT = re.compile(
    r"\b(?:invoices?|receipts?|bills?|proof\s+of\s+purchase|warranty\s+card|screenshots?|frame\s+number"
    r"|serial\s+number)\b|बिल|रसीद",
    re.IGNORECASE,
)
# A list after "please send:" carries the request into its items.
_LIST_ITEM = re.compile(r"\n\s*(?:[-*•]|\d+[.)])\s+")
_INSTEAD = re.compile(r"\binstead\b|के\s+बजाय", re.IGNORECASE)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_DECLINES_VIDEO = re.compile(
    r"\b(?:can'?t|cannot|can\s+not|unable\s+to|not\s+able\s+to|won'?t\s+be\s+able\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:take|send|record|shoot|make|upload|do|film|share)\s+(?:\w+\s+){0,3}?videos?\b"
    r"|\b(?:don'?t|do\s+not)\s+have\s+(?:a\s+)?videos?\b"
    r"|\b(?:don'?t|do\s+not)\s+know\s+how\s+to\s+(?:send|take|record|upload|make)\s+(?:a\s+)?videos?\b"
    r"|\bno\s+videos?\b(?!\s+yet)"
    r"|\bvideos?\s+(?:is\s+|isn'?t\s+|not\s+)(?:not\s+)?possible\b"
    r"|\bvideos?\b[^.?!]{0,30}?\b(?:won'?t|can'?t|cannot|isn'?t|is\s+not|not|doesn'?t|does\s+not)\s+"
    r"(?:upload|uploading|send|sending|go|going)\b"
    r"|\bvideos?\s+(?:is\s+|file\s+is\s+)?too\s+(?:big|large|long|heavy)\b"
    r"|\bvideo\s+upload\s+(?:failed|fails|failing|isn'?t\s+working|is\s+not\s+working)\b"
    r"|\bcamera\s+(?:doesn'?t|does\s+not|can'?t|cannot|won'?t)\s+record\b"
    r"|\bonly\s+(?:a\s+)?(?:photos?|pictures?|pics?)\b"
    r"|\b(?:photos?|pictures?|pics?)\s+instead\b"
    r"|\bcan\s+I\s+(?:just\s+|only\s+)?send\s+(?:a\s+)?(?:photos?|pictures?|pics?)\b"
    r"|\bvideo\s+(?:(?:bhejna|banana|lena|bana|record|upload)\s+)?(?:possible\s+)?(?:nahi|nahin|nhi)\b"
    r"|वीडियो\s+(?:भेजना\s+|बनाना\s+)?(?:संभव\s+)?नहीं",
    re.IGNORECASE,
)


def asks_for_media(reply: Optional[str]) -> Optional[str]:
    """"video" when a sentence of the reply asks the customer for a video (or
    a video and a photo), "photo" when the sentences that ask name only a
    photo, None when nothing is asked."""
    asked: Optional[str] = None
    for sentence in _SENTENCE.findall(_LIST_ITEM.sub(" ", reply or "")):
        if not (_ASK_EN.search(sentence) or _ASK_HINGLISH.search(sentence) or _ASK_HINDI.search(sentence)):
            continue
        if _DOCUMENT.search(sentence):
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
    a still of a detail is what is needed), or asks for a photo instead (the
    video would not go through). A video ask after the customer said
    a video is not possible gets the photo line."""
    text = reply or ""
    hindi = bool(_DEVANAGARI.search(text))
    if asked == "photo" and not video_declined and not _VIDEO.search(text) and not _INSTEAD.search(text):
        return "video_first_added", VIDEO_FIRST_LINE_HI if hindi else VIDEO_FIRST_LINE
    if asked == "video" and video_declined:
        return "photo_fallback_added", PHOTO_FALLBACK_LINE_HI if hindi else PHOTO_FALLBACK_LINE
    return None
