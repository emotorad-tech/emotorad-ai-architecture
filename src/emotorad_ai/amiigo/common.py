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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders

from ..ratelimit import RateLimiter
from .auth import Rider, TokenCheck, rider_from_header, utc_now

PREFIX = "/amiigo/v1"
NO_STORE = "no-store"

# The contract's codes ("Errors on the HTTP endpoints").
CURSOR_INVALID = "cursor_invalid"
CONVERSATION_NOT_FOUND = "conversation_not_found"
RATE_LIMITED = "rate_limited"
HISTORY_UNAVAILABLE = "history_unavailable"
STORAGE_UNAVAILABLE = "storage_unavailable"

# "More than 60 history requests ... a minute for one rider", both history
# endpoints together.
HISTORY_PER_MINUTE = 60


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
    store or None without a bucket; `clock` gives the time history answers at."""

    stores: Any
    tokens: TokenCheck
    log: Any
    media_store: Any = None
    clock: Callable[[], datetime] = utc_now
    history_limiter: RiderLimiter = field(default_factory=lambda: RiderLimiter(HISTORY_PER_MINUTE))


def context_of(request: Request) -> AmiigoContext:
    return request.app.state.amiigo


class RequireRider:
    """A FastAPI dependency: the rider the request's token proves.

    `unavailable` is the 503 code while the token check is off:
    `history_unavailable` on the history routes (`require_rider`),
    `storage_unavailable` on uploads and erasure (`RequireRider(STORAGE_UNAVAILABLE)`).
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
