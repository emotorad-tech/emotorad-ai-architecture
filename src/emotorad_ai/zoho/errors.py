"""Zoho's answers, as the errors the worker acts on (spec 2026-10-05, section 4).

Each class is a row of the spec's answers table. `error` is short and safe to
log as `error=`. It is Zoho's own error code when Zoho sent one that looks
like a code, and otherwise a name of ours ("timeout", "http_503"). A message
never holds a request or response body, a token, a secret or a phone number:
the worker logs it, and the registry copies `str(exc)` to the model and the
log (registry.py:295-296).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

# Zoho's codes are words joined by underscores (INVALID_OAUTH, and
# invalid_client_secret from the token endpoint). Anything else, including
# free text in a body, is not passed on.
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,59}")


def safe_code(value: object, fallback: str) -> str:
    """`value` when it looks like an error code, else `fallback`."""
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return fallback


class ZohoError(Exception):
    """Base for every Zoho failure. `error` is what the log and /health show."""

    error = "zoho_error"

    def __init__(self, message: str, *, error: Optional[str] = None) -> None:
        super().__init__(message)
        if error is not None:
            self.error = safe_code(error, type(self).error)


class ZohoUnavailable(ZohoError):
    """Zoho or the network did not answer, and nothing was written. The retry schedule."""

    error = "unavailable"


class ZohoUnknownOutcome(ZohoError):
    """A write was sent and no usable answer came back, so it may have been
    made. The worker looks before it writes again."""

    error = "unknown_outcome"


class ZohoRejected(ZohoError):
    """Zoho refused what we sent (400 or 422). `fields` are the names Zoho gave."""

    error = "INVALID_DATA"

    def __init__(self, message: str, *, error: Optional[str] = None, fields: Iterable[str] = ()) -> None:
        super().__init__(message, error=error)
        self.fields: Tuple[str, ...] = tuple(fields)


class ZohoConfigError(ZohoError):
    """Ours to fix: scope, organisation, access, licence or a bad id. Retried hourly."""

    error = "config"


class ZohoAuthExpired(ZohoError):
    """401 INVALID_OAUTH: the access token died. Refresh once and retry the step once."""

    error = "INVALID_OAUTH"


class ZohoGone(ZohoError):
    """404: the ticket or contact was deleted or merged in Desk."""

    error = "not_found"


class ZohoTooLarge(ZohoError):
    """413: an attachment over Zoho's limit."""

    error = "RESOURCE_SIZE_EXCEEDED"


class ZohoBusy(ZohoError):
    """429 TOO_MANY_REQUESTS: too many calls at once. Retry in 30 seconds."""

    error = "TOO_MANY_REQUESTS"


class ZohoCreditsExhausted(ZohoError):
    """429 THRESHOLD_EXCEEDED: the organisation's API credits for the day are gone."""

    error = "THRESHOLD_EXCEEDED"

    def __init__(self, message: str, *, error: Optional[str] = None,
                 retry_after_seconds: Optional[float] = None) -> None:
        super().__init__(message, error=error)
        self.retry_after_seconds = retry_after_seconds


class ZohoTokenRefused(ZohoError):
    """The token endpoint refused the refresh token or the client."""

    error = "token_refused"


class ZohoTokenThrottled(ZohoError):
    """The token endpoint said "Access Denied": too many token requests."""

    error = "access_denied"
