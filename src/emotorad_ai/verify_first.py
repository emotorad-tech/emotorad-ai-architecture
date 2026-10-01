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
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

from .contract import InboundMessage
from .conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ConversationState, utc_now_iso
from .identity import IdentityResolver, ResolvedIdentity
from .navigation import GREETING_TEXT, is_greeting_only, wants_change_number
from .observability import LOOSE_PHONE
from .tools.oms import OMSConfigError, normalise_mobile
from .tools.registry import ToolContext, ToolRegistry, is_error
from .tools.verification import (
    FIND_ACCOUNT_BY_CODE,
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    apply_proven_phone,
)
from .triage import classify_issue, topic_from_pill, which_bike_text

# What was found, and where in the text it was, so it can be replaced by a
# placeholder before the model or the transcript sees the message.
Found = Tuple[str, Tuple[int, int]]

# A mobile typed in one piece, with or without +91 or a leading 0.
_PHONE_TIGHT = re.compile(r"(?<![\d+])(?:\+?91|0)?[6-9]\d{9}(?!\d)")
# ...or read out in groups, with spaces or dashes: the log's own pattern, so
# every form read here is also hidden there (observability.redact_pii).
_PHONE_LOOSE = LOOSE_PHONE
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

# Warmer than "before I look into this" (the person, 2026-10-01): help first,
# then why the number is needed.
ASK_NUMBER = ("Happy to help with that. First I need to confirm it's you: what's the mobile number your bike is "
              "registered on?")
# Another number, at the number or the code step, or after verifying (spec
# 2026-10-02, going back).
CHANGE_NUMBER = "No problem. What's the right mobile number?"
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


TOO_MANY_CODES = (
    "I can't send any more codes in this chat. I'm passing you to our support team, who can verify you "
    "another way."
)
# Codes one conversation may send before the step hands over: the first, and
# two more (a resend, or another number). Each is an SMS once the OTP service
# is wired, so the step must not be a way to send them without end.
MAX_CODES = 3


def tries(left: int) -> str:
    return "1 try" if left == 1 else "%d tries" % left


def ascii_digits(text: str) -> str:
    """Digits of any script (Devanagari ९७००…) as ASCII, everything else as typed."""
    return "".join(
        str(unicodedata.decimal(ch)) if not ch.isascii() and unicodedata.decimal(ch, None) is not None else ch
        for ch in text
    )


# -- the step ----------------------------------------------------------------

NUMBER = "number"
CODE = "code"


@dataclass
class GateReply:
    """The step's answer. `model_text` is the customer's message as the model's
    history and the transcript get it: the number, code or order number
    replaced by a placeholder. `resolved` is set once the person is verified,
    with their bikes."""

    text: str
    outcome: str
    model_text: str
    escalated: bool = False
    resolved: Optional[ResolvedIdentity] = None


class VerifyFirst:
    def __init__(self, registry: ToolRegistry, resolver: IdentityResolver, log: Any) -> None:
        self.registry = registry
        self.resolver = resolver
        self.log = log
        self.store = getattr(registry, "verification", None)

    def applies(self, resolved: ResolvedIdentity) -> bool:
        """An anonymous customer, on a registry that can verify them."""
        return (
            self.store is not None
            and REQUEST_IDENTITY_VERIFICATION in self.registry.specs
            and VERIFY_IDENTITY in self.registry.specs
            and resolved.persona == "customer"
            and not resolved.may_disclose
        )

    def handle(self, message: InboundMessage, state: ConversationState) -> GateReply:
        # Digits typed in Devanagari (or any script) are the same digits.
        message = replace(message, message_text=ascii_digits(message.message_text or ""))
        text = message.message_text
        self._keep_topic(message, state)
        if state.verify_step == CODE and self.store.attempts_left(message.conversation_id) <= 0:
            # Locked: the handover stands. No code is sent or tried again,
            # whatever the message, until the store lets the entry go.
            return self._reply(message, state, LOCKED, "locked", text, escalated=True)
        phone = find_phone(text)

        if state.verify_step == CODE:
            if phone:
                return self._send_code(message, state, phone)
            if asks_resend(text):
                return self._resend(message, state, text)
            if wants_change_number(text):
                return self._change_number(message, state, text)
            code = find_code(text)
            if code:
                return self._check_code(message, state, code)
            return self._reply(message, state, ASK_CODE.format(masked=state.verify_masked), "ask_code", text)

        if phone:
            return self._send_code(message, state, phone)
        if state.verify_step is None:
            if is_greeting_only(text) and state.turns <= 1 and not message.attachments:
                # Greeted back; the number waits until they say what is wrong
                # (the person, 2026-10-01). The step has not started.
                return self._reply(message, state, GREETING_TEXT, "greeting", text)
            # First contact: the number, and only the number (phone first).
            state.verify_step = NUMBER
            return self._reply(message, state, ASK_NUMBER, "ask_number", text)
        if wants_change_number(text):
            return self._change_number(message, state, text)

        order = find_order_code(text) if FIND_ACCOUNT_BY_CODE in self.registry.specs else None
        if order:
            return self._find_order(message, state, order)
        if looks_like_a_number(text):
            return self._reply(message, state, INVALID_NUMBER, "invalid_number", text)
        return self._ask_number_again(message, state, text)

    # -- steps ---------------------------------------------------------------

    def _send_code(self, message: InboundMessage, state: ConversationState, phone: Found) -> GateReply:
        number, span = phone
        model_text = redact(message.message_text, span, "[phone]")
        if state.verify_sends >= MAX_CODES:
            return self._too_many(message, state, model_text)
        envelope = self._call(message, REQUEST_IDENTITY_VERIFICATION, {"phone": number})
        if is_error(envelope):
            return self._reply(message, state, INVALID_NUMBER, "invalid_number", model_text)
        state.verify_sends += 1
        state.verify_step = CODE
        state.verify_masked = envelope["data"]["phone_masked"]
        return self._reply(message, state, CODE_SENT.format(masked=state.verify_masked), "code_sent", model_text)

    def _resend(self, message: InboundMessage, state: ConversationState, text: str) -> GateReply:
        if state.verify_sends >= MAX_CODES:
            return self._too_many(message, state, text)
        envelope = self._call(message, REQUEST_IDENTITY_VERIFICATION, {})
        if is_error(envelope):
            # Nothing pending any more (swept): start again from the number.
            return self._ask_number_again(message, state, text)
        state.verify_sends += 1
        state.verify_masked = envelope["data"]["phone_masked"]
        return self._reply(message, state, CODE_RESENT.format(masked=state.verify_masked), "code_resent", text)

    def _find_order(self, message: InboundMessage, state: ConversationState, order: Found) -> GateReply:
        value, span = order
        model_text = redact(message.message_text, span, "[order number]")
        if state.verify_sends >= MAX_CODES:
            return self._too_many(message, state, model_text)
        if is_error(self._call(message, FIND_ACCOUNT_BY_CODE, {"code": value})):
            return self._reply(message, state, ORDER_NOT_FOUND, "order_not_found", model_text)
        sent = self._call(message, REQUEST_IDENTITY_VERIFICATION, {})
        if is_error(sent):
            return self._reply(message, state, ORDER_NOT_FOUND, "order_not_found", model_text)
        state.verify_sends += 1
        state.verify_step = CODE
        state.verify_masked = sent["data"]["phone_masked"]
        return self._reply(message, state, ORDER_CODE_SENT.format(masked=state.verify_masked),
                           "order_code_sent", model_text)

    def _check_code(self, message: InboundMessage, state: ConversationState, code: Found) -> GateReply:
        value, span = code
        model_text = redact(message.message_text, span, "[code]")
        cid = message.conversation_id
        expired = self.store.pending_code(cid) is None
        envelope = self._call(message, VERIFY_IDENTITY, {"code": value})
        if is_error(envelope):
            if envelope["error"]["code"] == "verification_locked":
                return self._reply(message, state, LOCKED, "locked", model_text, escalated=True)
            if expired:
                return self._reply(message, state, CODE_EXPIRED, "code_expired", model_text)
            left = tries(self.store.attempts_left(cid))
            return self._reply(message, state, WRONG_CODE.format(left=left), "wrong_code", model_text)
        return self._verified(message, state, model_text)

    def _verified(self, message: InboundMessage, state: ConversationState, model_text: str) -> GateReply:
        phone = self.store.verified_phone(message.conversation_id)
        owner = "PHONE#" + phone
        if state.user_key and state.user_key != owner:
            # Somebody else's conversation, whose session had expired: a run
            # of this person's own, so nothing of the first person's reaches
            # them (the final review, 2026-09-30).
            state.restart_for(owner, utc_now_iso())
            self.log.emit("verify_first_new_person", message.conversation_id)
        proved = apply_proven_phone(message, phone)
        resolved = self.resolver.hydrate(proved)
        state.verify_step = None
        state.verify_masked = None
        state.verify_sends = 0
        # Rebuilt next turn with the bikes and past conversations; and the
        # agent cleared so the bike choice runs through triage, pin or not.
        state.context_block = None
        state.agent = None
        state.selected_frame = None
        # A bike given earlier as not in the list goes too: the list is asked
        # again (the final review, 2026-10-01).
        state.unlisted_bike, state.unlisted_asks, state.bike_confirmation = None, 0, None
        if resolved.bikes:
            state.move_to(AWAITING_BIKE_SELECTION, "verified")
            text, outcome = CONFIRMED + " " + which_bike_text(resolved.bikes), "verified"
        elif resolved.method == "no_warranty_record":
            state.move_to(AWAITING_ISSUE, "verified_no_bikes")
            text, outcome = CONFIRMED + " " + NO_BIKES, "verified_no_bikes"
        else:
            state.move_to(AWAITING_ISSUE, "verified_lookup_failed")
            text, outcome = CONFIRMED + " " + LOOKUP_FAILED, "verified_lookup_failed"
        reply = self._reply(message, state, text, outcome, model_text)
        reply.resolved = resolved
        return reply

    def _too_many(self, message: InboundMessage, state: ConversationState, model_text: str) -> GateReply:
        return self._reply(message, state, TOO_MANY_CODES, "too_many_codes", model_text, escalated=True)

    def _ask_number_again(self, message: InboundMessage, state: ConversationState, text: str) -> GateReply:
        state.verify_step = NUMBER
        state.verify_masked = None
        fallback = FALLBACK_ORDER if FIND_ACCOUNT_BY_CODE in self.registry.specs else FALLBACK_PERSON
        return self._reply(message, state, ASK_NUMBER_AGAIN + " " + fallback, "ask_number_again", text)

    def _change_number(self, message: InboundMessage, state: ConversationState, text: str) -> GateReply:
        """Another number, at the number or the code step: a code waiting for
        the old one is cancelled (spec 2026-10-02). The lock is checked first
        in handle(), so a locked step stays locked."""
        self.store.reset(message.conversation_id)
        state.verify_step = NUMBER
        state.verify_masked = None
        return self._reply(message, state, CHANGE_NUMBER, "change_number", text)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _keep_topic(message: InboundMessage, state: ConversationState) -> None:
        """What the problem is, from the first message that says, so it is not
        asked for again once the bike is chosen."""
        if state.pending_topic is not None:
            return
        pill = message.pill_clicked
        topic = topic_from_pill(pill) or classify_issue(message.message_text or "")
        if topic:
            state.pending_topic = topic
            state.pending_topic_source = "pill:%s" % pill if pill and topic_from_pill(pill) else "text"

    def _call(self, message: InboundMessage, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        envelope = self.registry.call(name, arguments, ToolContext(conversation_id=message.conversation_id))
        # Logged like an agent's tool call, so Runtime._side_effects_since sees
        # a code sent or tried and a save conflict never runs the turn again.
        self.log.tool_call(message.conversation_id, name, arguments, envelope)
        return envelope

    def _reply(self, message: InboundMessage, state: ConversationState, text: str, outcome: str,
               model_text: str, escalated: bool = False) -> GateReply:
        if any(a.kind in ("image", "video") for a in message.attachments) and PHOTO_SAFETY not in text:
            # At every step, not only the first: the keyword gate cannot see
            # a hazard that is only in a picture.
            text += " " + PHOTO_SAFETY
        self.log.emit("verify_first", message.conversation_id, outcome=outcome, step=state.verify_step)
        return GateReply(text=text, outcome=outcome, model_text=model_text, escalated=escalated)
