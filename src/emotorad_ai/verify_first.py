"""Verify first: an anonymous customer proves their number before anything else.

The person's rule (2026-09-30): if a chat does not come from the Amiigo app or
anywhere else the person is already verified, the bot first asks for the
mobile number, sends a one-time code to it, checks the code, then lists every
bike on the number with its frame number and lets the person choose, and only
then carries on with the issue.

A fixed step, not the model: it answers the same way every time, it cannot be
talked past, and it costs nothing per message. It calls the existing tools in
tools/verification.py through the registry, so the comparison of the code is
still `==` in code. Safety and "talk to a person" run before it (graph.py).

This module reads the customer's words deterministically (below) and runs the
step (`VerifyFirst`).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .tools.oms import OMSConfigError, normalise_mobile

# What was found, and where in the text it was, so it can be replaced by a
# placeholder before the model or the transcript sees the message.
Found = Tuple[str, Tuple[int, int]]

# A mobile typed in one piece, with or without +91 or a leading 0.
_PHONE_TIGHT = re.compile(r"(?<![\d+])(?:\+?91|0)?[6-9]\d{9}(?!\d)")
# ...or read out in groups, with spaces or dashes.
_PHONE_LOOSE = re.compile(r"(?<![\d+])\+?\d[\d \-]{8,16}\d(?!\d)")
# Six digits, optionally split three and three.
_CODE = re.compile(r"(?<!\d)(\d{3})[ \-]?(\d{3})(?!\d)")
# A run of letters, digits, dashes and slashes, which an order number is.
_ORDER_TOKEN = re.compile(r"(?<![\w/-])[A-Za-z0-9][\w/-]{3,}(?![\w/-])")
_ORDER_WORDS = re.compile(r"\b(?:order|invoice|inv|bill)\b", re.IGNORECASE)
# Seven or more digits, however spaced: a try at a number, valid or not.
_NUMBER_ATTEMPT = re.compile(r"\+?\d[\d \-]{5,}\d")
_RESEND = re.compile(
    r"\b(?:resend|re-send|send (?:it |the code )?again|new code|another code|"
    r"didn'?t (?:get|receive)|did not (?:get|receive)|not received|no code)\b|nahi (?:aaya|mila)",
    re.IGNORECASE,
)


def find_phone(text: str) -> Optional[Found]:
    """The first valid Indian mobile in the text, as ten digits."""
    for pattern in (_PHONE_TIGHT, _PHONE_LOOSE):
        for match in pattern.finditer(text or ""):
            try:
                return normalise_mobile(match.group()), match.span()
            except OMSConfigError:
                continue
    return None


def find_code(text: str) -> Optional[Found]:
    """A six-digit code, spaces or a dash allowed in the middle."""
    match = _CODE.search(text or "")
    return (match.group(1) + match.group(2), match.span()) if match else None


def find_order_code(text: str) -> Optional[Found]:
    """An order or invoice number: at least five characters and three digits,
    with a letter or a slash in it, or anything of that length when the
    message says "order" or "invoice". Short codes such as E-06 and 48V, and
    phone numbers, are never taken for one."""
    text = text or ""
    mentions = bool(_ORDER_WORDS.search(text))
    for match in _ORDER_TOKEN.finditer(text):
        token = match.group()
        if len(token) < 5 or sum(ch.isdigit() for ch in token) < 3 or find_phone(token):
            continue
        if mentions or "/" in token or any(ch.isalpha() for ch in token):
            return token, match.span()
    return None


def looks_like_a_number(text: str) -> bool:
    """Seven or more digits: the customer tried to give a number."""
    return any(sum(ch.isdigit() for ch in m.group()) >= 7 for m in _NUMBER_ATTEMPT.finditer(text or ""))


def asks_resend(text: str) -> bool:
    return bool(_RESEND.search(text or ""))


def redact(text: str, span: Tuple[int, int], placeholder: str) -> str:
    start, end = span
    return text[:start] + placeholder + text[end:]


# -- the step's replies (fixed English text, like triage's) ------------------

ASK_NUMBER = "Before I look into this, I need to confirm it's you. What's the mobile number your bike is registered on?"
# Added when the first message carries a photo or video: a hazard shown only
# in a picture is not caught by the keyword gate, and saying it is.
PHOTO_SAFETY = "If you can see smoke, heat or swelling, stop using the bike and tell me now."
ASK_NUMBER_AGAIN = "I need the 10-digit mobile number your bike is registered on."
FALLBACK_ORDER = "If you can't recall it, send your order or invoice number instead."
FALLBACK_PERSON = "If you can't recall it, say 'talk to a person'."
INVALID_NUMBER = "That doesn't look like a 10-digit mobile number. Please send it again."
CODE_SENT = "I've sent a 6-digit code by SMS to {masked}. Please type it here."
CODE_RESENT = "I've sent a new code to {masked}. Please type it here."
ORDER_CODE_SENT = "I found that order. I've sent a 6-digit code to the number on it, {masked}. Please type it here."
ORDER_NOT_FOUND = "I couldn't find an order with that number. Please check it, or send your mobile number instead."
ASK_CODE = "Please type the 6-digit code I sent to {masked}. Say 'resend' for a new code, or send a different number."
WRONG_CODE = "That code isn't right. You have {left} left. Please check the SMS and type it again."
CODE_EXPIRED = "That code has expired. Say 'resend' and I'll send you a new one."
LOCKED = (
    "That's too many wrong codes, so I can't confirm it's you here. I'm passing you to our support "
    "team, who can verify you another way."
)
CONFIRMED = "Thanks, that's confirmed."
NO_BIKES = "I couldn't find a bike registered on this number. Would you like to register it now?"
LOOKUP_FAILED = "I can't load your bikes just now. What's happening with the bike?"


def tries(left: int) -> str:
    return "1 try" if left == 1 else "%d tries" % left
