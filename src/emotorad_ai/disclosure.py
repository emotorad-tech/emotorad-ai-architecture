"""The bot says it is a bot (risk register §16 — EU AI Act Article 50).

Applicable in the EU from 2 August 2026, with penalties up to €15M or 3% of
global turnover. We serve EU customers on the same bots as Indian ones, so the
choice is between two disclosure behaviours by region or one everywhere — and one
everywhere is both cheaper to build and impossible to get wrong.

Enforced in code rather than in the prompt for the ordinary reason: a prompt
edit six months from now, made by someone optimising tone, must not be able to
silently remove a legal obligation. The model is never asked to remember this.
"""

from __future__ import annotations

import re
from typing import Optional

from .conversation import ConversationState

# Text channels get it prepended once, on the first thing we ever say.
DISCLOSURE_TEXT = "Hi, I'm EMotorad's virtual assistant — an AI, not a person."

# Voice needs its own wording: read aloud, the text version scans badly and the
# em dash becomes a pause in the wrong place.
DISCLOSURE_VOICE = (
    "Hello, you are speaking to EMotorad's automated assistant. "
    "I am an A I, not a person."
)

VOICE_CHANNELS = ("voice",)

# Deliberately loose. This is used to *verify* a disclosure is present, including
# in evals over model-written text, so it must recognise reasonable rephrasings
# rather than only the exact string above.
_DISCLOSURE_MARKER = re.compile(
    r"\b(?:a[in]?\s*i|artificial intelligence|virtual assistant|automated assistant|"
    r"chatbot|bot|not a (?:real )?person|not a human)\b",
    re.IGNORECASE,
)


def disclosure_for(channel: str) -> str:
    return DISCLOSURE_VOICE if channel in VOICE_CHANNELS else DISCLOSURE_TEXT


def has_disclosure(text: str) -> bool:
    """Whether a reply identifies itself as a machine."""
    return bool(_DISCLOSURE_MARKER.search(text or ""))


def apply_disclosure(reply: str, state: ConversationState, channel: str) -> str:
    """Prepend the disclosure to the first outbound message of a conversation.

    Idempotent by conversation, not by message: a customer must be told once, not
    on every turn. If the reply already discloses — because the model happened to
    say so, or a guardrail message includes it — the state is marked and nothing
    is prepended, so we never say it twice in one breath.
    """
    if state.disclosed:
        return reply

    state.disclosed = True
    if has_disclosure(reply):
        return reply

    separator = " " if channel in VOICE_CHANNELS else "\n\n"
    return disclosure_for(channel) + separator + reply
