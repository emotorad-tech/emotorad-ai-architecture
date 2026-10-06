"""What every Amiigo route under /amiigo/v1/ shares (docs/contracts/amiigo-support-chat.md,
"Authentication", "Errors on the HTTP endpoints", "Caching and privacy").

* The rider, from the `Authorization` header (`require_rider`), or the 401
  with the contract's code. While the token check is off (no Amiigo public
  key), the answer is 503 before the token is read: a missing key is our
  outage, not a reason to send the rider to sign in again (the plan's Ruling 1).
* Errors as `{"detail": "<code>"}` (`amiigo_error`); a 422 keeps FastAPI's
  field list.
* `Cache-Control: no-store` on every HTTP answer under /amiigo/v1/, success
  or error (`NoStoreMiddleware`).
* Limits per rider, by the rider's hash (`RiderLimiter`).

What the routes need from the process (the stores, the token check, the
media bucket, the log, the clock and the limiters) is one `AmiigoContext` on
`app.state.amiigo`, set by api.py, so each app the tests build has its own.
"""

from __future__ import annotations

import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders

from ..ratelimit import RateLimiter
from .auth import Rider, TokenCheck, rider_from_header, utc_now
from .receipts import InMemoryAmiigoReceipts
from .sockets import SocketRegistry

PREFIX = "/amiigo/v1"
NO_STORE = "no-store"

# The contract's codes ("Errors on the HTTP endpoints").
CURSOR_INVALID = "cursor_invalid"
CONVERSATION_NOT_FOUND = "conversation_not_found"
RATE_LIMITED = "rate_limited"
HISTORY_UNAVAILABLE = "history_unavailable"
STORAGE_UNAVAILABLE = "storage_unavailable"
FILE_TOO_LARGE = "file_too_large"
FILE_TYPE_NOT_ACCEPTED = "file_type_not_accepted"
NOTHING_PENDING = "nothing_pending"
# A deletion request without `"confirm": true` ("Deleting conversation data":
# 400). The contract gives that 400 no code; this is it.
CONFIRM_REQUIRED = "confirm_required"

# "More than 60 history requests ... a minute for one rider", both history
# endpoints together.
HISTORY_PER_MINUTE = 60
# "Upload slots: 20 a minute per rider".
UPLOADS_PER_MINUTE = 20
# "More than 20 messages a minute from this rider" (the socket's rate_limited).
MESSAGES_PER_MINUTE = 20
# "The server closes a socket that sends nothing for 10 minutes."
SOCKET_IDLE_SECONDS = 600.0
# How often a socket waiting on the same message's turn elsewhere looks again.
SOCKET_POLL_SECONDS = 0.25
# The chat socket's own worker threads, apart from anyio's default pool that
# the sync HTTP routes (POST /message among them) share: turns, which can
# take a minute or two, and the short reads and writes of the receipts and
# the conversation's records, so long turns never hold up an admission.
SOCKET_TURN_THREADS = 16
SOCKET_STORE_THREADS = 8

# A chat's id: a UUID the app makes ("Conversation lifecycle"), in either case
# (iOS writes capitals), used exactly as sent. An explicit ASCII class, so no
# other script's digits pass, and anchored: pydantic searches a pattern.
CONVERSATION_ID_PATTERN = r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"


def amiigo_error(status: int, code: str) -> HTTPException:
    """The contract's error: the status, `{"detail": code}`, never cached. A
    401 also says which scheme it wants, as HTTP requires."""
    headers = {"Cache-Control": NO_STORE}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return HTTPException(status_code=status, detail=code, headers=headers)


class RiderLimiter:
    """`per_minute` calls a minute for each rider, told apart by `rider_hash`.
    In memory and per process, like the website chat's limits (ratelimit.py)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._limiter = RateLimiter(limit=per_minute, window_seconds=60.0, clock=clock)

    def allow(self, rider: Rider) -> bool:
        return self._limiter.allow(rider.rider_hash)


@dataclass
class AmiigoContext:
    """What the Amiigo routes read from the process. `stores` has
    `.conversations` and `.tickets` (wiring.Stores); `media_store` is the S3
    store or None without a bucket; `uploads` is the process's one upload
    registry (storage/uploads.py, the website's too), None without a bucket;
    `cluster_for_phone` gives a verified phone's identity-graph cluster, the
    one an upload's key is made under; `clock` gives the time history answers
    at and a deletion request is recorded at, and the chat socket's `ready`
    and token expiry are reckoned on.

    The chat socket (socket.py) also reads: `receipts`, the record of each
    message a rider sent (receipts.py); `sockets`, the open sockets by rider
    (sockets.py); `prepare_turn` and `handle_turn`, api.prepare_turn and the
    runtime's handle, set by api.py (the socket closes 1011 without them);
    and its limit, its thread allowances and timings. `thread_limiters`
    holds the socket's anyio CapacityLimiters, one pair per event loop
    (socket.py makes them).

    The Zoho Desk webhook (webhooks.py) reads `zoho_webhook_secret`, and
    pushes a closed ticket through `sockets`."""

    stores: Any
    tokens: TokenCheck
    log: Any
    media_store: Any = None
    uploads: Any = None
    cluster_for_phone: Optional[Callable[[str], str]] = None
    clock: Callable[[], datetime] = utc_now
    history_limiter: RiderLimiter = field(default_factory=lambda: RiderLimiter(HISTORY_PER_MINUTE))
    upload_limiter: RiderLimiter = field(default_factory=lambda: RiderLimiter(UPLOADS_PER_MINUTE))
    receipts: Any = field(default_factory=InMemoryAmiigoReceipts)
    sockets: SocketRegistry = field(default_factory=SocketRegistry)
    prepare_turn: Optional[Callable[..., Any]] = None
    handle_turn: Optional[Callable[[Any], Any]] = None
    message_limiter: RiderLimiter = field(default_factory=lambda: RiderLimiter(MESSAGES_PER_MINUTE))
    socket_idle_seconds: float = SOCKET_IDLE_SECONDS
    socket_poll_seconds: float = SOCKET_POLL_SECONDS
    socket_turn_threads: int = SOCKET_TURN_THREADS
    socket_store_threads: int = SOCKET_STORE_THREADS
    thread_limiters: Any = field(default_factory=weakref.WeakKeyDictionary, repr=False)
    # The Zoho Desk webhook's secret (webhooks.py), or None: the webhook then
    # answers 503. Never in the repr.
    zoho_webhook_secret: Optional[str] = field(default=None, repr=False)


def context_of(request: Request) -> AmiigoContext:
    return request.app.state.amiigo


class RequireRider:
    """A FastAPI dependency: the rider the request's token proves.

    `unavailable` is the 503 code while the token check is off:
    `history_unavailable` on the history routes (`require_rider`),
    `storage_unavailable` on uploads and erasure (`require_storage_rider`).
    A refused token is logged by its code only, never the token."""

    def __init__(self, unavailable: str) -> None:
        self.unavailable = unavailable

    def __call__(self, request: Request) -> Rider:
        context = context_of(request)
        if not context.tokens.enabled:
            raise amiigo_error(503, self.unavailable)
        found = rider_from_header(request.headers.get("authorization"), context.tokens)
        if isinstance(found, str):
            context.log.emit("amiigo_token_refused", "amiigo", error=found)
            raise amiigo_error(401, found)
        return found


require_rider = RequireRider(HISTORY_UNAVAILABLE)
require_storage_rider = RequireRider(STORAGE_UNAVAILABLE)


class NoStoreMiddleware:
    """`Cache-Control: no-store` on every HTTP answer whose path is under
    /amiigo/v1/: a route's answer, its errors and 422s, and the 404 or 405 for
    a path or method that is not there. Every other request, and every
    WebSocket, passes through untouched."""

    def __init__(self, app: Any, prefix: str = PREFIX) -> None:
        self.app = app
        self.prefix = prefix

    def _covers(self, path: Optional[str]) -> bool:
        return bool(path) and (path == self.prefix or path.startswith(self.prefix + "/"))

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self._covers(scope.get("path")):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = NO_STORE
            await send(message)

        await self.app(scope, receive, send_no_store)
