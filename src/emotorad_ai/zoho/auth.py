"""The Desk access token, kept in memory behind a lock (spec 2026-10-05, section 1).

One token serves the API process, refreshed five minutes before its hour
ends. The token endpoint's answer is read by its body, whatever the HTTP
status:

- "Access Denied" is Zoho's throttle (10 requests in 10 minutes per refresh
  token), sent with HTTP 200. No token request is made for 10 minutes.
- Any other error the endpoint names with a status below 500 is a refusal:
  invalid_code, invalid_client, invalid_client_secret, invalid_grant,
  unauthorized_client and the rest. invalid_client_secret is what rotating
  the OMS's secret without updating ours gives. Retrying one would only trip
  the throttle. `state` says "token refused: <name>" for /health, and no
  token request is made for an hour.
- A named error sent with a 5xx is a server fault, not a refusal: it is
  unavailable, on the 30-second schedule, and `state` is left alone.

The refresh token and the client secret go in the form body, never the
address. They are never logged, stored or put in an exception message, and
nor is the access token.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

from .errors import ZohoTokenRefused, ZohoTokenThrottled, ZohoUnavailable, safe_code
from .http import DeskHTTP
from .settings import ZohoSettings

# The India data centre. The person's scripts import this too, so no host is
# written twice.
ACCOUNTS_URL = "https://accounts.zoho.in"
TOKEN_URL = ACCOUNTS_URL + "/oauth/v2/token"
REFRESH_EARLY_SECONDS = 300.0
THROTTLE_WAIT_SECONDS = 600.0
REFUSED_WAIT_SECONDS = 3600.0
DEFAULT_LIFETIME_SECONDS = 3600.0
THROTTLED = "Access Denied"


def _lifetime(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LIFETIME_SECONDS
    return seconds if math.isfinite(seconds) and seconds > 0 else DEFAULT_LIFETIME_SECONDS


class TokenSource:
    """The one access token, refreshed on demand, early, and by one caller at a time."""

    def __init__(self, settings: ZohoSettings, http: DeskHTTP, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._renew_at = 0.0
        self._quiet_until = 0.0
        self._refused: Optional[str] = None
        self.state = "ok"

    def __repr__(self) -> str:
        return "TokenSource(state=%r)" % self.state

    def token(self) -> str:
        """A live access token, from memory or refreshed. Callers at the same
        moment share one refresh, because the lock is held across it."""
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if now < self._quiet_until:
                if self._refused:
                    raise ZohoTokenRefused(
                        "Zoho refused the token request (%s); not asked again within the hour" % self._refused,
                        error=self._refused,
                    )
                raise ZohoTokenThrottled("Zoho is throttling token requests; not asked again within ten minutes")
            return self._refresh(now)

    def invalidate(self) -> None:
        """Drop the cached token. A Desk 401 INVALID_OAUTH means it died early:
        Zoho keeps at most ten per refresh token, and the person's scripts
        share ours."""
        with self._lock:
            self._token = None
            self._renew_at = 0.0

    def _refresh(self, now: float) -> str:
        form = urllib.parse.urlencode({
            "refresh_token": self._settings.refresh_token,
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            "grant_type": "refresh_token",
        }).encode("ascii")
        status, payload = self._http.call(
            "POST", TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            form, write=False, classify=False,
        )
        body = payload if isinstance(payload, dict) else {}
        error = body.get("error")
        if error == THROTTLED:
            self._token = None
            self._refused = None
            self._quiet_until = now + THROTTLE_WAIT_SECONDS
            self.state = "throttled"
            raise ZohoTokenThrottled("Zoho is throttling token requests (HTTP %d); none for ten minutes" % status)
        if isinstance(error, str) and error.strip() and status >= 500:
            # A server fault that names an error is transient: the 30-second
            # schedule, not an hour's pause on every ticket, safety ones too.
            raise ZohoUnavailable(
                "Zoho's token endpoint failed (HTTP %d, %s)" % (status, safe_code(error, "no code")),
                error="http_%d" % status,
            )
        if isinstance(error, str) and error.strip():
            # Named by its code. Text that is no code is not passed on.
            named = safe_code(error, ZohoTokenRefused.error)
            self._token = None
            self._refused = named
            self._quiet_until = now + REFUSED_WAIT_SECONDS
            self.state = "token refused: %s" % named
            raise ZohoTokenRefused("Zoho refused the token request: %s (HTTP %d)" % (named, status), error=named)
        token = body.get("access_token")
        if error is not None or not isinstance(token, str) or not token.strip():
            raise ZohoUnavailable(
                "Zoho's token answer held no token (HTTP %d, %s)" % (status, safe_code(error, "no error named")),
                error="token_unreadable",
            )
        lifetime = _lifetime(body.get("expires_in"))
        self._token = token.strip()
        self._renew_at = now + max(lifetime - REFRESH_EARLY_SECONDS, lifetime / 2)
        self._quiet_until = 0.0
        self._refused = None
        self.state = "ok"
        return self._token
