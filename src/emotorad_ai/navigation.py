"""Going back, and the greeting (spec 2026-10-02-greeting-going-back-confirm-design.md).

The person's rule, 1 October 2026: nothing is one-way. A customer can ask for
another number, another bike, the list again or a fresh start from any step,
and a greeting is greeted back. Each phrase is the whole message, give or take
politeness and punctuation: "back" or "another bike" inside a sentence about
the bike ("the back wheel is wobbly", "my other bike works fine with this
charger") is not a request to go anywhere.
"""

from __future__ import annotations

import re

# A greeting-only first message (verify_first.py, triage.py). The AI line is
# part of it, so the runtime's disclosure is not added a second time.
GREETING_TEXT = "Hi there! I'm EMotorad's virtual assistant, an AI. How can I help with your bike today?"
# Before the bike list again (runtime._back_to_list).
BACK_TO_LIST = "No problem."
START_AGAIN = "No problem, let's start again."
# A signed-in rider's number is the sign-in's: there is no code step to redo.
NUMBER_FIXED_APP = "You're signed in to the app with your number, so I can't change it here."
NUMBER_FIXED = "This chat is linked to your number, so I can't change it here."
# Where a signed-in identity comes from the Amiigo app's sign-in.
APP_SIGN_IN_CHANNELS = ("amiigo_app", "website_chat")

_GREETING = re.compile(
    r"^[\W_]*(?:hi+|hello+|helo+|hey+|hiya|heya|namaste|namaskar|namaskaram|hola|yo"
    r"|good\s+(?:morning|afternoon|evening)|नमस्ते|नमस्कार)"
    r"(?:\s+(?:there|team|everyone|emotorad|bot|sir|madam|ji))?[\W_]*$",
    re.IGNORECASE,
)
# What may come before the request ("ok, ", "sorry ", "I want to ") and after
# it (" please", punctuation).
_LEAD = (
    r"^[\W_]*(?:(?:ok|okay|sorry|actually|wait|please|pls|plz|hi|hey|no|nahi)[\W_]+)*"
    r"(?:(?:i\s+want\s+to|i\s+wanna|i'?d\s+like\s+to|i\s+would\s+like\s+to|i\s+need\s+to|can\s+i|could\s+i"
    r"|can\s+we|let\s+me|let'?s|please|pls|plz)\s+)?"
)
_TAIL = r"(?:[\W_]+(?:please|pls|plz))?[\W_]*$"
# A number typed straight after "change number": the verify step reads it.
_NUMBER_AFTER = r"(?:[\s,:-]*(?:to|is|it'?s)?[\s,:-]*\+?\d[\d\s-]{7,16}\d)?"


def _whole(body: str, after: str = "") -> "re.Pattern[str]":
    return re.compile(_LEAD + "(?:" + body + ")" + after + _TAIL, re.IGNORECASE)


_CHANGE_NUMBER = _whole(
    r"(?:(?:that'?s|it'?s|that\s+is|this\s+is)\s+)?(?:the\s+|a\s+)?wrong\s+(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|(?:change|update|edit|correct|fix)\s+(?:the\s+|my\s+)?(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|(?:use|try|give(?:\s+you)?|enter|send)\s+(?:a\s+|an\s+)?(?:another|different|other|new)\s+"
    r"(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|(?:(?:that'?s|it'?s|this\s+is)\s+)?not\s+my\s+(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|galat\s+(?:number|no|nambar)|(?:number|no)\s+galat(?:\s+(?:hai|h|he))?|number\s+badal(?:na|o|do)(?:\s+hai)?"
    r"|गलत\s+नंबर|नंबर\s+गलत(?:\s+है)?",
    after=_NUMBER_AFTER,
)
_CHANGE_BIKE = _whole(
    r"not\s+(?:this|that)\s+(?:one|bike|cycle)"
    r"|(?:(?:it'?s|that'?s)\s+)?(?:the\s+|a\s+)?wrong\s+(?:bike|cycle|one)"
    r"|(?:change|switch)\s+(?:the\s+|my\s+)?(?:bike|cycle)"
    r"|(?:(?:it'?s|i\s+mean|i\s+meant)\s+)?(?:about\s+)?(?:a\s+|the\s+|my\s+)?(?:different|other|another)\s+(?:bike|cycle)"
    r"|a\s+different\s+one"
    r"|(?:doosri|dusri|doosra|dusra)\s+(?:bike|cycle|wali|wala)|(?:ye|yeh)\s+(?:wali|wala)\s+(?:nahi|nahin|nhi)"
    r"|दूसरी\s+(?:बाइक|साइकिल)|(?:ये|यह)\s+वाली\s+नहीं"
)
_LIST = _whole(
    r"(?:go\s+)?back|go\s+to\s+the\s+list|previous(?:\s+step)?"
    r"|(?:show|see|give|send)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:list|options|bikes)(?:\s+again)?"
    r"|(?:the\s+)?(?:list|options)(?:\s+again)?"
    r"|wapas|vapas|wapis|peeche|पीछे|वापस"
)
_START_OVER = _whole(
    r"start\s+(?:over|again|afresh|from\s+(?:the\s+)?(?:beginning|start|scratch))"
    r"|restart(?:\s+(?:the\s+)?(?:chat|conversation))?"
    r"|(?:begin|go)\s+(?:again|from\s+(?:the\s+)?(?:beginning|start))"
    r"|from\s+the\s+(?:beginning|start)"
    r"|(?:phir\s+se\s+)?shuru\s+se(?:\s+(?:karo|karte\s+hai|karte\s+hain))?|phir\s+se\s+shuru(?:\s+karo)?|शुरू\s+से"
)
# "restart?" alone is the customer asking about a step ("restart the bike"),
# not asking to start over.
_RESTART_QUESTION = re.compile(r"^[\W_]*restart[\W_]*\?[\W_]*$", re.IGNORECASE)
_MOBILE = re.compile(r"\b(?:mobile|phone|cell)\b|मोबाइल|फ़ोन|फोन", re.IGNORECASE)


def is_greeting_only(text: str) -> bool:
    return bool(_GREETING.match(text or ""))


def wants_change_number(text: str) -> bool:
    return bool(_CHANGE_NUMBER.match(text or ""))


def wants_change_bike(text: str) -> bool:
    return bool(_CHANGE_BIKE.match(text or ""))


def wants_list(text: str) -> bool:
    return bool(_LIST.match(text or ""))


def wants_start_over(text: str) -> bool:
    return bool(_START_OVER.match(text or "")) and not _RESTART_QUESTION.match(text or "")


def names_the_mobile(text: str) -> bool:
    """Whether the text says it is the mobile number, not a frame number."""
    return bool(_MOBILE.search(text or ""))
