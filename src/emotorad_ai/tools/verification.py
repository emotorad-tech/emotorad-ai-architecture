"""Turning an anonymous chat into a verified one, without letting the model do it.

A customer who arrives on website chat saying "hi" is `anonymous`, and
``Identity.may_disclose`` is False, so the agent may not name a bike or state
coverage. The only honest way to open that gate is to prove the person holds the
number they claim. This module is that proof step.

Two tools, and the split between them is the whole design:

- ``request_identity_verification(phone)`` — the one place a model-supplied
  phone number is allowed, because on its own it grants **nothing**. It sends a
  code and returns no customer data at all.
- ``verify_identity(code)`` — the model passes the code the customer typed; this
  module compares it. A match records the phone as verified for that
  conversation.

**The model never sees the code and never decides the outcome.** It cannot be
argued into "close enough", because it is not the thing doing the comparing —
`==` in `check()` is. That matters: an LLM asked to validate a secret can be
talked out of it, and the customer is exactly the party with an incentive to
try.

**Existence is never revealed before verification.** A request for a code answers
the same way whether or not the number is registered, so the flow cannot be used
to enumerate who owns an EMotorad. Whether the customer has bikes is answered
after they prove the number is theirs, by the warranty tool.

In this build the code is generated locally and read off the screen instead of
an SMS. Replace ``send`` with the real SMS provider; nothing else changes.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .oms import OMSConfigError, normalise_mobile
from .registry import ToolError, ToolRegistry, ok

REQUEST_IDENTITY_VERIFICATION = "request_identity_verification"
VERIFY_IDENTITY = "verify_identity"

# Enough attempts for a mistyped digit, few enough that a six-digit code cannot
# be guessed. The counter is per conversation and is never reset by asking for a
# new code, or requesting one would be a way to buy more guesses.
MAX_ATTEMPTS = 5


@dataclass
class _Pending:
    phone: str
    code: str
    attempts: int = 0
    verified: bool = False


@dataclass
class VerificationStore:
    """Conversation id -> the number being proved, and whether it has been.

    In-memory, like IdempotencyStore. Back it with the real session store before
    this is anything but a harness: a verified identity that evaporates on
    restart is an annoyance here and a security question in production, where
    "verified" has to mean the same thing on every node.
    """

    _pending: Dict[str, _Pending] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(self, conversation_id: str, phone: str, code: str) -> None:
        with self._lock:
            previous = self._pending.get(conversation_id)
            # Attempts survive a re-request on purpose; see MAX_ATTEMPTS.
            attempts = previous.attempts if previous else 0
            self._pending[conversation_id] = _Pending(phone=phone, code=code, attempts=attempts)

    def check(self, conversation_id: str, code: str) -> bool:
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is None or pending.attempts >= MAX_ATTEMPTS:
                return False
            pending.attempts += 1
            # The comparison is here, in code. Not in the prompt, and not in the
            # model's judgement.
            if (code or "").strip() != pending.code:
                return False
            pending.verified = True
            return True

    def verified_phone(self, conversation_id: str) -> Optional[str]:
        """The number this conversation has proved, or None."""
        with self._lock:
            pending = self._pending.get(conversation_id)
            return pending.phone if pending and pending.verified else None

    def pending_code(self, conversation_id: str) -> Optional[str]:
        """The outstanding code — for the harness to display. Never for the model."""
        with self._lock:
            pending = self._pending.get(conversation_id)
            return pending.code if pending and not pending.verified else None

    def attempts_left(self, conversation_id: str) -> int:
        with self._lock:
            pending = self._pending.get(conversation_id)
            return MAX_ATTEMPTS - (pending.attempts if pending else 0)

    def reset(self, conversation_id: str) -> None:
        with self._lock:
            self._pending.pop(conversation_id, None)


def _six_digits() -> str:
    return "%06d" % random.randint(0, 999999)


def register_verification_tools(
    registry: ToolRegistry,
    store: VerificationStore,
    code_factory: Callable[[], str] = _six_digits,
    send: Optional[Callable[[str, str], None]] = None,
) -> None:
    """Add the two identity tools to a registry."""

    @registry.register(
        REQUEST_IDENTITY_VERIFICATION,
        "Send a one-time code by SMS to a phone number the customer has given you, so their "
        "identity can be confirmed. Use this when you need their bike, warranty or order "
        "details and the customer context says identity is not available. Ask for the number "
        "in their own words first; do not guess it. This returns no customer information and "
        "does not say whether the number is registered — it only sends the code.",
        parameters={
            "phone": {
                "type": "string",
                "description": "The mobile number the customer gave you, digits as they said them.",
            }
        },
        required=("phone",),
        injects=("conversation_id",),
    )
    def request_identity_verification(conversation_id: str, phone: str) -> Dict[str, Any]:
        try:
            normalised = normalise_mobile(phone)
        except OMSConfigError:
            raise ToolError(
                "invalid_phone",
                "That does not look like a ten-digit Indian mobile number. Ask the customer to "
                "read it out again.",
            )
        code = code_factory()
        store.issue(conversation_id, "+91%s" % normalised, code)
        if send is not None:
            send(normalised, code)
        # Deliberately identical whether or not the number is registered.
        return ok({"sent": True, "phone_ending": normalised[-4:]})

    @registry.register(
        VERIFY_IDENTITY,
        "Check the one-time code the customer typed. Pass exactly what they sent, digits only. "
        "If it matches, their identity is confirmed and you may then look up their bikes and "
        "coverage. Never tell the customer what the code is, never guess it for them, and never "
        "treat a code as correct because they say it is — this tool decides, not you.",
        parameters={
            "code": {"type": "string", "description": "The code the customer typed, digits only."}
        },
        required=("code",),
        injects=("conversation_id",),
    )
    def verify_identity(conversation_id: str, code: str) -> Dict[str, Any]:
        if store.check(conversation_id, code):
            return ok({"verified": True})
        remaining = store.attempts_left(conversation_id)
        if remaining <= 0:
            raise ToolError(
                "verification_locked",
                "Too many incorrect codes on this conversation. Do not send another code. Hand "
                "over to a human agent to verify this customer another way.",
                remedy="human_handoff",
            )
        raise ToolError(
            "verification_failed",
            "That code is not correct. %d attempt(s) remain. Ask the customer to check the SMS "
            "and read the code again." % remaining,
        )
