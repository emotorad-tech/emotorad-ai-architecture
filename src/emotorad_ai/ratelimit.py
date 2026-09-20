"""A sliding-window cap on how often one caller may reach an endpoint.

`/message` is unauthenticated and every call reaches a real model and the real
OMS, so an open loop against it spends money. This bounds that.

It is deliberately not a security control. Authentication for the chat surface
is still an open decision, and a rate limit is not a substitute for one: it
slows an anonymous caller down, it does not establish who they are. In-memory
and per-process, like every other store here, so it bounds one node rather than
a deployment.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional


class RateLimiter:
    """Allow `limit` calls per caller per `window_seconds`.

    Sliding rather than fixed: a fixed window resets on a boundary, so a caller
    who spends their whole allowance just before it and again just after gets
    through twice the limit in a moment. Timestamps age out individually
    instead.
    """

    def __init__(
        self,
        limit: int = 20,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._calls: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._calls)

    def allow(self, caller: Optional[str]) -> bool:
        """Record a call and say whether it may proceed.

        A caller with no address shares one bucket rather than bypassing the
        limit. Unknown must never mean unlimited: that is the caller a limit
        exists for.
        """
        key = caller or "unknown"
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            # Callers whose every call has aged out are dropped, so this does
            # not become one more dictionary that grows for the life of the
            # process.
            for other in [k for k, v in self._calls.items() if not v or v[-1] <= cutoff]:
                if other != key:
                    del self._calls[other]

            calls = self._calls.setdefault(key, deque())
            while calls and calls[0] <= cutoff:
                calls.popleft()
            if len(calls) >= self.limit:
                return False
            calls.append(now)
            return True
