"""The tool registry (build plan §3.4).

Every tool is wrapped once, engineer-maintained, and answers with the same
envelope. Three guardrails are enforced here rather than prompted:

1. Identity arguments (customer_id) are injected from the resolved profile. They
   are not part of the schema the model sees, so the model cannot ask for one
   customer's data while answering another.
2. Write tools require an idempotency key; a retried call returns the first
   result instead of creating a second ticket/booking.
3. Tool failures come back as an error envelope, never an exception that kills
   the turn — the model is told the tool failed and can say so.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..conversation import StoreUnavailable

Envelope = Dict[str, Any]


def ok(data: Any, freshness_seconds: int = 0) -> Envelope:
    return {"data": data, "freshness_seconds": freshness_seconds}


def err(code: str, message: str, retryable: bool = False, remedy: Optional[str] = None) -> Envelope:
    """An error the model can act on.

    ``remedy`` names a recovery path the *platform* knows about — a machine-readable
    action, not prose. Some failures are not dead ends: a missing purchase date has
    a defined fix (ask for the invoice), and naming it in code keeps that routing
    out of the prompt, where it would be a suggestion rather than a rule.
    """
    error: Dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if remedy:
        error["remedy"] = remedy
    return {"error": error}


def is_error(envelope: Envelope) -> bool:
    return "error" in envelope


class ToolError(Exception):
    """Raise inside a tool to return a clean error envelope to the model."""

    def __init__(
        self, code: str, message: str, retryable: bool = False, remedy: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.remedy = remedy


@dataclass(frozen=True)
class ToolContext:
    """Trusted, code-resolved facts a tool may need. Never model-supplied."""

    conversation_id: str
    # Phone is the OMS lookup key and the identifier every channel supplies.
    # It is injected, never model-supplied — the model must not be able to ask
    # about a number the platform did not resolve.
    phone: Optional[str] = None
    cluster_id: Optional[str] = None
    customer_id: Optional[str] = None
    dealer_id: Optional[str] = None

    def value_for(self, name: str) -> Any:
        return getattr(self, name, None)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON-schema properties the model may supply
    required: Tuple[str, ...]
    fn: Callable[..., Any]
    injects: Tuple[str, ...] = ()  # ToolContext fields passed in behind the model's back
    write: bool = False  # writes require an idempotency key and are deduplicated

    def schema(self) -> Dict[str, Any]:
        """The tool definition as Claude sees it — injected fields are absent."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": dict(self.parameters),
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


# How long a claim holds a key. Far longer than any tool takes, and short
# enough that a server which died mid-call does not block the write (the
# safety ticket included) for the week a finished receipt is kept.
CLAIM_LEASE_SECONDS = 300


def write_in_progress() -> Envelope:
    return err("write_in_progress", "This action is already being carried out; try again shortly.", retryable=True)


class _Pending:
    __slots__ = ("claimed_at",)

    def __init__(self, claimed_at: float) -> None:
        self.claimed_at = claimed_at


class IdempotencyStore:
    """Maps an idempotency key to the envelope its first execution produced.

    Claim-before-execute: `claim` marks a key as in progress and only the
    caller that won the claim runs the tool, so a retry arriving while the
    first attempt is still running cannot execute it a second time. A claim
    older than the lease is taken over. In-memory, one process; the MongoDB
    store (stores/mongo.py) implements the same four methods for every server.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, lease_seconds: int = CLAIM_LEASE_SECONDS) -> None:
        self._seen: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._lease = lease_seconds

    def claim(self, key: str) -> Optional[Envelope]:
        with self._lock:
            existing = self._seen.get(key)
            now = self._clock()
            if existing is None or (isinstance(existing, _Pending) and now - existing.claimed_at >= self._lease):
                self._seen[key] = _Pending(now)
                return None
            if isinstance(existing, _Pending):
                return write_in_progress()
            return existing

    def get(self, key: str) -> Optional[Envelope]:
        with self._lock:
            existing = self._seen.get(key)
            return None if isinstance(existing, _Pending) else existing

    def put(self, key: str, envelope: Envelope) -> None:
        with self._lock:
            self._seen[key] = envelope

    def release(self, key: str) -> None:
        with self._lock:
            if isinstance(self._seen.get(key), _Pending):
                del self._seen[key]


@dataclass
class ToolRegistry:
    specs: Dict[str, ToolSpec] = field(default_factory=dict)
    idempotency: IdempotencyStore = field(default_factory=IdempotencyStore)

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        required: Iterable[str] = (),
        injects: Iterable[str] = (),
        write: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self.specs:
                raise ValueError("tool already registered: %s" % name)
            self.specs[name] = ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
                required=tuple(required),
                fn=fn,
                injects=tuple(injects),
                write=write,
            )
            return fn

        return decorator

    def schemas_for(self, names: Iterable[str]) -> List[Dict[str, Any]]:
        return [self.specs[n].schema() for n in names]

    def call(
        self, name: str, arguments: Dict[str, Any], context: ToolContext, run_without_idempotency: bool = False
    ) -> Envelope:
        """Run one tool and answer with its envelope; never raises.

        If the idempotency store cannot be reached, a write is refused with a
        retryable error rather than run unprotected, unless the caller passes
        `run_without_idempotency`: for a write that must happen now (the safety
        ticket), a possible duplicate is the lesser harm. Either way the
        envelope carries `idempotency_warning` for the caller to log.
        """
        spec = self.specs.get(name)
        if spec is None:
            return err("unknown_tool", "No tool named %r is registered." % name)

        arguments = dict(arguments or {})

        # Injected identity always wins over anything the model supplied.
        for field_name in spec.injects:
            value = context.value_for(field_name)
            if value is None:
                return err(
                    "missing_identity",
                    "%s is required for %s but was not resolved for this conversation."
                    % (field_name, name),
                )
            arguments[field_name] = value

        # The idempotency key is checked before the other arguments so a
        # missing key is reported as itself rather than as one more absent field.
        scoped_key = None
        if spec.write:
            key = arguments.get("idempotency_key")
            if not key:
                return err("missing_idempotency_key", "%s requires an idempotency_key." % name)
            scoped_key = "%s:%s:%s" % (context.conversation_id, name, key)

        # Validated before the claim, so a rejected call never holds one.
        missing = [key for key in spec.required if key not in arguments]
        if missing:
            return err("missing_arguments", "Missing required argument(s): %s" % ", ".join(missing))

        warning = None
        if scoped_key is not None:
            try:
                previous = self.idempotency.claim(scoped_key)
            except StoreUnavailable as exc:
                if not run_without_idempotency:
                    envelope = err("idempotency_unavailable",
                                   "This action cannot be carried out safely right now; try again shortly.", retryable=True)
                    envelope["idempotency_warning"] = "claim failed: %s" % exc
                    return envelope
                previous, scoped_key = None, None
                warning = "ran without a receipt: %s" % exc
            if previous is not None:
                return previous

        try:
            result = spec.fn(**arguments)
        except ToolError as exc:
            envelope = err(exc.code, exc.message, exc.retryable, exc.remedy)
        except Exception as exc:  # a broken tool must not kill the conversation
            envelope = err("tool_exception", "%s: %s" % (type(exc).__name__, exc), retryable=True)
        else:
            envelope = result if isinstance(result, dict) and ("data" in result or "error" in result) else ok(result)

        if scoped_key is not None:
            try:
                if is_error(envelope):
                    # A failed write did nothing; let the retry run it.
                    self.idempotency.release(scoped_key)
                else:
                    self.idempotency.put(scoped_key, envelope)
            except StoreUnavailable as exc:
                # The write happened (or failed) either way: the customer gets
                # the result. Unrecorded, a retry could repeat it; an unreleased
                # claim lapses with the lease.
                warning = "receipt not recorded: %s" % exc
        if warning:
            envelope = dict(envelope, idempotency_warning=warning)
        return envelope
