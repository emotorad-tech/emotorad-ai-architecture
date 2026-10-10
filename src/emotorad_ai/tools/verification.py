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

import logging
import random
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

from ..contract import VERIFIED, InboundMessage
from .oms import OMSConfigError, normalise_mobile
from .registry import ToolError, ToolRegistry, ok

_logger = logging.getLogger(__name__)

REQUEST_IDENTITY_VERIFICATION = "request_identity_verification"
VERIFY_IDENTITY = "verify_identity"
FIND_ACCOUNT_BY_CODE = "find_account_by_code"

# Enough attempts for a mistyped digit, few enough that a six-digit code cannot
# be guessed. The counter is per conversation and is never reset by asking for a
# new code, or requesting one would be a way to buy more guesses.
MAX_ATTEMPTS = 5


def mask_phone(phone: str) -> str:
    """A number the owner will recognise and a stranger cannot reconstruct.

    Three digits. Someone holding an invoice they did not pay for learns almost
    nothing; the person whose number it is knows immediately whether to expect
    the code.
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    # Normalise to the ten national digits first. Masking "+919876543210" and
    # "9876543210" differently would show two dot counts for one number, which
    # reads as two different numbers to the person being asked to confirm it.
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    return ("•" * max(len(digits) - 3, 0)) + digits[-3:] if digits else ""


# How long a code is worth typing, and how long proving a number keeps someone
# signed in. Two lifetimes because they answer different questions: a code is a
# secret in transit and should be short, while a verified session is how long a
# customer may keep talking without proving themselves again. Ten minutes covers
# an SMS arriving and being typed; twelve hours covers someone who puts the
# phone down mid-diagnosis and comes back after work.
CODE_TTL_SECONDS = 600
VERIFIED_TTL_SECONDS = 12 * 60 * 60


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _session_expiry(verified_on: str) -> datetime:
    """When a session proved at `verified_on` (the store's wall-clock ISO
    string) stops being proof: VERIFIED_TTL_SECONDS later."""
    try:
        at = datetime.fromisoformat(verified_on)
    except (TypeError, ValueError):
        at = _utc_now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at + timedelta(seconds=VERIFIED_TTL_SECONDS)


def _seconds_since(verified_on: str, now_iso: str) -> Optional[float]:
    """How long ago, by the wall clock, a session was proved, or None when
    either time cannot be read (then nothing is taken back)."""
    try:
        then, now = datetime.fromisoformat(verified_on), datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # A server whose clock runs behind the one that saved it counts no time.
    return max(0.0, (now - then).total_seconds())


def proved_owner(state: Any) -> Optional[str]:
    """Who the saved conversation says has proved its number: its `user_key`,
    once verification has finished (`verify_step` None), else None.

    The one thing a saved session is checked against before it is taken back
    after a restart (VerificationStore.restore). A conversation saved while a
    number was being changed or a code was awaited has no owner here, so a
    saved session that this process ended but could not delete never comes
    back (the review of the fix, 2026-10-06).
    """
    if state is None or getattr(state, "verify_step", None) is not None:
        return None
    return getattr(state, "user_key", None)


class VerifiedSessions(Protocol):
    """Where a proved number is kept, so it outlives the process that proved it.

    A deploy restarts the container; with the proof in process memory only,
    every chat in progress became anonymous and was asked for its number, then
    its bike, again (staging, 2026-10-06). `get` answers None for a session
    past `expires_at`, by the wall clock, whatever the backend's own expiry has
    or has not removed yet. Implementations: InMemoryVerifiedSessions below,
    MongoVerifiedSessions in stores/mongo.py (`verification_sessions`).

    This carries one process (one container, one worker) across a restart. It
    does not make several servers safe: codes stay in each process's memory,
    and memory answers first (VerificationStore). Several servers need codes
    and proofs in the shared store, with the store, not memory, the authority.
    """

    def save(self, conversation_id: str, phone: str, verified_on: str, expires_at: datetime) -> None: ...

    def get(self, conversation_id: str) -> Optional[Tuple[str, str]]: ...

    def delete(self, conversation_id: str) -> None: ...


class InMemoryVerifiedSessions:
    """One process's saved sessions: what the memory store is built with, and
    what tests share between two VerificationStores to stand in for a restart."""

    def __init__(self, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now
        self._sessions: Dict[str, Tuple[str, str, datetime]] = {}
        self._lock = threading.Lock()

    def save(self, conversation_id: str, phone: str, verified_on: str, expires_at: datetime) -> None:
        with self._lock:
            now = self._now()
            # Expired sessions go on every save, the only moment this grows.
            for cid in [c for c, s in self._sessions.items() if s[2] <= now]:
                del self._sessions[cid]
            self._sessions[conversation_id] = (phone, verified_on, expires_at)

    def get(self, conversation_id: str) -> Optional[Tuple[str, str]]:
        with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None:
                return None
            if session[2] <= self._now():
                del self._sessions[conversation_id]
                return None
            return session[0], session[1]

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            self._sessions.pop(conversation_id, None)


@dataclass
class _Pending:
    phone: str
    code: str
    attempts: int = 0
    verified: bool = False
    # Monotonic, not wall clock: a laptop waking from sleep or an NTP correction
    # must not extend a code's life or cut a session short.
    issued_at: float = 0.0
    verified_at: float = 0.0
    # The time of day of the same moment, for a person to read (an erasure
    # request's proof). verified_at is monotonic and means nothing to them.
    verified_on: str = ""
    # The customer asked to use another number (cancel_code): the code can no
    # longer be used or resent, but its attempts still count.
    cancelled: bool = False

    def code_expired(self, now: float) -> bool:
        return now - self.issued_at > CODE_TTL_SECONDS

    def session_expired(self, now: float) -> bool:
        return now - self.verified_at > VERIFIED_TTL_SECONDS

    def dead(self, now: float) -> bool:
        """Nothing useful left: the session has lapsed, or an unverified code
        has expired and cannot be typed any more."""
        if self.verified:
            return self.session_expired(now)
        return self.code_expired(now)


@dataclass
class _Candidate:
    """A number recovered from an order code, held back from the model.

    The model asks for a code to be sent to "the number on file" without ever
    learning what it is — otherwise recovering an account from a printed invoice
    would hand out a phone number, which is the leak this whole flow exists to
    avoid.
    """

    phone: str


@dataclass
class VerificationStore:
    """Conversation id -> the number being proved, and whether it has been.

    Codes live in memory only: a restart while a code is outstanding means the
    customer asks for a new one, which costs them a message, not their proof.
    A proved number is also saved to `sessions` (VerifiedSessions) when one is
    given, for VERIFIED_TTL_SECONDS, so a deploy does not make a verified chat
    anonymous (staging, 2026-10-06). Without `sessions` it is memory only, as
    before. This is for one process (one container, one worker), restarted:
    see VerifiedSessions for what several servers would need.

    Memory is the only thing `verified_phone` and `verified_on` read. A saved
    session comes back into memory through `restore` alone, which the turn
    calls with the saved conversation's owner (proved_owner): it is taken
    back only when this process holds nothing for the conversation, has not
    ended a proof there that it could not delete, and the saved conversation
    says the same person finished verifying. Every place memory ends a proof
    (a new code, reset, the sweep of a lapsed session) deletes the saved one
    too, so a session can never outlive what memory would allow. Cancelling
    a code leaves a proved number alone, here as in memory.

    A backend failure never fails a turn: the conversation counts as not
    verified, and `verification_session_unavailable` is logged with the
    operation and the error's class, never the phone or the message.
    """

    _pending: Dict[str, _Pending] = field(default_factory=dict)
    _candidates: Dict[str, _Candidate] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Injected so expiry can be tested by elapsing time rather than sleeping.
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], str] = _utc_now_iso
    # Where proved numbers are saved (VerifiedSessions), or None.
    sessions: Optional[Any] = None
    # The EventLog, for verification_session_unavailable. Optional.
    log: Optional[Any] = None
    # Held while memory changes and the saved session follows, and while a
    # saved session is taken back, so two requests on one conversation leave
    # both in the same order and never see one without the other.
    _write_lock: threading.Lock = field(default_factory=threading.Lock)
    # Conversations whose saved session could not be deleted: never taken
    # back by this process until a new proof is saved or a delete succeeds.
    _unended: Set[str] = field(default_factory=set)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    def _sweep(self, now: float) -> List[str]:
        """Drop entries nothing can use again. Called on issue, which is the
        only moment the store grows, so expiry doubles as the eviction this
        store never had. Returns the conversations whose proof it dropped, for
        their saved sessions to go too (an unproved code has none: its issue
        already deleted it)."""
        proofs = []
        for conversation_id in [c for c, p in self._pending.items() if p.dead(now)]:
            if self._pending.pop(conversation_id).verified:
                proofs.append(conversation_id)
            self._candidates.pop(conversation_id, None)
        return proofs

    def remember_candidate(self, conversation_id: str, phone: str) -> None:
        with self._lock:
            self._candidates[conversation_id] = _Candidate(phone=phone)

    def candidate_phone(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            candidate = self._candidates.get(conversation_id)
            return candidate.phone if candidate else None

    def issue(self, conversation_id: str, phone: str, code: str) -> None:
        with self._write_lock:
            with self._lock:
                now = self.clock()
                swept = self._sweep(now)
                previous = self._pending.get(conversation_id)
                # Attempts survive a re-request on purpose; see MAX_ATTEMPTS. They do
                # not survive the code expiring: attempts spent against a code that
                # could no longer have worked would lock out a customer who simply
                # came back late.
                attempts = 0
                if previous and not previous.code_expired(now):
                    attempts = previous.attempts
                # A new code replaces any proof this conversation held.
                self._pending[conversation_id] = _Pending(
                    phone=phone, code=code, attempts=attempts, issued_at=now
                )
            self._end_sessions([conversation_id] + [c for c in swept if c != conversation_id])

    def check(self, conversation_id: str, code: str) -> bool:
        with self._write_lock:
            with self._lock:
                pending = self._pending.get(conversation_id)
                if pending is None or pending.cancelled or pending.attempts >= MAX_ATTEMPTS:
                    return False
                if not pending.code:
                    # A proof taken back from a saved session (restore) has
                    # no code: nothing typed, the empty string included, is it.
                    return False
                # An expired code fails without costing an attempt: the customer has
                # done nothing wrong, and they need a fresh code, not a lockout.
                if pending.code_expired(self.clock()):
                    return False
                pending.attempts += 1
                # The comparison is here, in code. Not in the prompt, and not in the
                # model's judgement.
                if (code or "").strip() != pending.code:
                    return False
                pending.verified = True
                pending.verified_at = self.clock()
                pending.verified_on = self.wall_clock()
                phone, verified_on = pending.phone, pending.verified_on
            self._save_session(conversation_id, phone, verified_on)
            return True

    def verified_phone(self, conversation_id: str) -> Optional[str]:
        """The number this conversation has proved, or None."""
        session = self._session(conversation_id)
        return session[0] if session else None

    def verified_on(self, conversation_id: str) -> Optional[str]:
        """When this conversation proved its number, as an ISO time of day, or
        None under the same conditions as verified_phone."""
        session = self._session(conversation_id)
        return (session[1] or None) if session else None

    def _session(self, conversation_id: str) -> Optional[Tuple[str, str]]:
        """(phone, verified_on) from memory. A saved session counts only once
        `restore` has taken it back."""
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is None or not pending.verified or pending.session_expired(self.clock()):
                return None
            return pending.phone, pending.verified_on

    def restore(self, conversation_id: str, owner: Optional[str]) -> Optional[str]:
        """After a restart: the saved session of this conversation, taken back
        into memory when the saved conversation agrees with it. Returns the
        number the conversation has proved afterwards (verified_phone).

        `owner` is proved_owner(the saved state): the user key of the person
        who finished verifying, or None. The saved session is read only when
        memory holds nothing for the conversation (a proof, live or lapsed,
        or a code: memory's verdict stands) and this process has not ended a
        proof there (`_unended`). It is taken back only when its number is
        the owner's, for what is left of its twelve hours, with no code that
        can be typed. Held under the write lock, so a reset or a new code
        deleting the saved session is either wholly before it or after it.
        """
        if self.sessions is None or not owner or not owner.startswith("PHONE#"):
            return self.verified_phone(conversation_id)
        with self._write_lock:
            with self._lock:
                held = conversation_id in self._pending or conversation_id in self._unended
            if held:
                return self.verified_phone(conversation_id)
            try:
                session = self.sessions.get(conversation_id)
            except Exception as exc:
                self._unavailable("get", conversation_id, exc)
                return None
            if session is None or "PHONE#" + session[0] != owner:
                return None
            phone, verified_on = session
            elapsed = _seconds_since(verified_on, self.wall_clock())
            if elapsed is None or elapsed >= VERIFIED_TTL_SECONDS:
                return None
            with self._lock:
                now = self.clock()
                self._pending[conversation_id] = _Pending(
                    phone=phone, code="", verified=True, issued_at=now - elapsed,
                    verified_at=now - elapsed, verified_on=verified_on,
                )
        return phone

    def pending_code(self, conversation_id: str) -> Optional[str]:
        """The outstanding code — for the harness to display. Never for the model."""
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is None or pending.verified or pending.cancelled:
                return None
            if pending.code_expired(self.clock()):
                return None
            return pending.code

    def pending_phone(self, conversation_id: str) -> Optional[str]:
        """The number an unproved code was last issued to, for a resend.

        Kept after the code expires (a resend is most needed then), until the
        store sweeps the entry on a later issue. Never for the model: the
        request tool uses it and returns only the masked form.
        """
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is None or pending.verified or pending.cancelled:
                return None
            return pending.phone

    def attempts_left(self, conversation_id: str) -> int:
        with self._lock:
            pending = self._pending.get(conversation_id)
            return MAX_ATTEMPTS - (pending.attempts if pending else 0)

    def cancel_code(self, conversation_id: str) -> None:
        """The outstanding code, voided: it can no longer be used or resent.
        The attempts stay, as they do on a re-request (MAX_ATTEMPTS), so asking
        for another number is not a way to buy more guesses (the final review,
        2026-10-02). A proved number is left alone: reset() forgets that, and
        the saved session is left alone with it."""
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is not None and not pending.verified:
                pending.cancelled = True

    def reset(self, conversation_id: str) -> None:
        with self._write_lock:
            with self._lock:
                self._pending.pop(conversation_id, None)
            self._end_sessions([conversation_id])

    # -- the saved sessions ---------------------------------------------------

    def _save_session(self, conversation_id: str, phone: str, verified_on: str) -> None:
        if self.sessions is None:
            return
        try:
            self.sessions.save(conversation_id, phone, verified_on, _session_expiry(verified_on))
        except Exception as exc:
            # Verified in this process all the same; a restart forgets it, as
            # before the sessions were saved.
            self._unavailable("save", conversation_id, exc)
            return
        with self._lock:
            self._unended.discard(conversation_id)

    def _end_sessions(self, conversation_ids: List[str]) -> None:
        if self.sessions is None:
            return
        for conversation_id in conversation_ids:
            try:
                self.sessions.delete(conversation_id)
            except Exception as exc:
                self._unavailable("delete", conversation_id, exc)
                with self._lock:
                    self._unended.add(conversation_id)
            else:
                with self._lock:
                    self._unended.discard(conversation_id)

    def _unavailable(self, operation: str, conversation_id: str, exc: Exception) -> None:
        # The class only: a driver's message can carry a host or a value.
        error = type(exc).__name__
        _logger.warning("verification_session_unavailable: %s failed (%s)", operation, error)
        if self.log is not None:
            self.log.emit("verification_session_unavailable", conversation_id, operation=operation, error=error)


def _six_digits() -> str:
    return "%06d" % random.randint(0, 999999)


class MockOtpSender:
    """Stands in for the OTP service until it is wired (the person, 2026-09-30).

    Sends nothing. Logs that a code went to the masked number, and never the
    code: on a test server the code is read from /dev/verification instead.
    The real service replaces this object and nothing else changes.
    """

    def __init__(self) -> None:
        self.sent: List[str] = []

    def __call__(self, phone: str, code: str) -> None:
        masked = mask_phone(phone)
        self.sent.append(masked)
        _logger.info("otp_sent to %s (mock: no SMS sent)", masked)


def register_verification_tools(
    registry: ToolRegistry,
    store: VerificationStore,
    code_factory: Callable[[], str] = _six_digits,
    send: Optional[Callable[[str, str], None]] = None,
    account_finder: Optional[Callable[[str], Optional[str]]] = None,
) -> None:
    """Add the identity tools to a registry.

    ``account_finder`` takes an order or invoice code and returns the registered
    phone, or None. Supplied only when there is something to look it up in; the
    recovery tool is absent otherwise, so an agent that cannot recover an account
    is never told that it can.
    """

    @registry.register(
        REQUEST_IDENTITY_VERIFICATION,
        "START HERE when the customer context says identity is not available. Ask the customer "
        "for the mobile number their warranty is registered against — that is the first and "
        "best way to find anyone — and pass it here to send them a one-time code. Never guess "
        "the number, and do not offer an order number as an equal alternative; it is only for "
        "customers who cannot recall their mobile. Omit the phone argument entirely to send the "
        "code to a number already recovered by find_account_by_code, or again to the number the "
        "last code went to. This returns no customer "
        "information and does not say whether the number is registered — it only sends a code.",
        parameters={
            "phone": {
                "type": "string",
                "description": (
                    "The mobile number the customer gave you, digits as they said them. Omit "
                    "this entirely to send the code to the number already found from their "
                    "order or invoice code — you are not told that number, and you do not "
                    "need it."
                ),
            }
        },
        required=(),
        injects=("conversation_id",),
    )
    def request_identity_verification(conversation_id: str, phone: Optional[str] = None) -> Dict[str, Any]:
        if not phone:
            # Recovered from an order code and deliberately never shown to the
            # model; sending to it is the only thing the model may do with it.
            # Otherwise a resend, to the number the last code went to.
            phone = store.candidate_phone(conversation_id) or store.pending_phone(conversation_id)
            if not phone:
                raise ToolError(
                    "no_number_on_file",
                    "No number has been established for this conversation. Ask the customer for "
                    "their registered mobile number, or for their order or invoice number.",
                )
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
        # Masked the same way everywhere. Returning four digits here while
        # find_account_by_code returns three would hand back, on the recovery
        # path, a digit more of a number the model was never given.
        return ok({"sent": True, "phone_masked": mask_phone(normalised)})

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

    if account_finder is None:
        return

    @registry.register(
        FIND_ACCOUNT_BY_CODE,
        "FALLBACK ONLY — ask for the registered mobile number first and use "
        "request_identity_verification with it. Reach for this tool solely when the customer "
        "has said they cannot recall that number. It takes the order number or invoice number "
        "printed on their invoice and finds which mobile the warranty sits against, returned "
        "MASKED: you are not told the full number and do not need it — call "
        "request_identity_verification with no phone argument to send the code there. A number "
        "printed on an invoice proves nothing about who is holding it, so this confirms no "
        "identity on its own; the customer still has to enter the one-time code before you may "
        "name their bike or state any coverage. Marketplace order numbers (Amazon, Flipkart) "
        "are not in our system and will not match.",
        parameters={
            "code": {
                "type": "string",
                "description": "The order number or invoice number, exactly as the customer read it out.",
            }
        },
        required=("code",),
        injects=("conversation_id",),
    )
    def find_account_by_code(conversation_id: str, code: str) -> Dict[str, Any]:
        phone = account_finder(code)
        if not phone:
            raise ToolError(
                "order_not_found",
                "No order matches that number. Ask the customer to read it out again — the "
                "order number and the invoice number are both printed on the invoice, and "
                "either will do.",
            )
        store.remember_candidate(conversation_id, phone)
        # Masked, and nothing else. No name, no address, no bike: the person
        # holding the invoice has proved nothing yet.
        return ok({"found": True, "phone_masked": mask_phone(phone)})


def apply_verified_identity(
    message: InboundMessage, store: VerificationStore
) -> InboundMessage:
    """Carry a phone this conversation has *proved* onto the inbound identity.

    `verify_identity` records the proof against the conversation, and until this
    function existed nothing read it back. The resolver hydrates from
    `message.identity`, which for an anonymous website visitor carries no phone,
    so `lookup_warranty_record` was refused for want of one on every turn after
    a correct code exactly as it was before. The customer typed a code and
    nothing changed.

    A surface whose channel already resolved identity upstream is left alone: an
    existing phone is never overwritten, so a stale entry on a reused
    conversation id can never redirect a lookup to somebody else's number.

    This and `apply_proven_phone` (which the runtime's verify-first step also
    uses) are where the disclosure gate opens, and it opens on the store's
    verdict rather than the model's. `Identity.may_disclose` follows from the
    strength set there, so no prompt wording can reach it.

    It reads memory only. After a restart a saved session is stamped by the
    turn itself (Runtime._node_prepare), once `restore` has checked it
    against the saved conversation; never here, before that check.
    """
    return apply_proven_phone(message, store.verified_phone(message.conversation_id))


def apply_proven_phone(message: InboundMessage, phone: Optional[str]) -> InboundMessage:
    """The inbound identity with a proved phone on it, or the message unchanged.

    An existing phone is never overwritten, so a stale proof on a reused
    conversation id can never redirect a lookup to somebody else's number.
    """
    if message.identity.phone or not phone:
        return message
    return replace(message, identity=replace(message.identity, strength=VERIFIED, phone=phone))
