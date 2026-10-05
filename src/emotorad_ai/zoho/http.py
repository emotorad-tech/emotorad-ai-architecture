"""One HTTP call to Zoho, with Zoho's answer classified (spec 2026-10-05, section 4).

This uses the standard library only, like tools/oms.py and openrouter.py. The
opener is the seam: the suite passes tests/fake_zoho.py, so no test opens a
socket.

Answers are classified by Zoho's error code, not the status alone
(https://desk.zoho.com/DeskAPIDocument#Errors). A write is handled more
carefully than a read. urllib wraps a failure while the request is being sent
in URLError (urllib.request.AbstractHTTPHandler.do_open), so Zoho never had
the whole request, and that is unavailable. Anything raised after that point,
while waiting for or reading the answer, may follow a write Zoho has made. So
a timeout, a dropped connection or a 5xx after a write is an unknown outcome,
and the caller looks before it writes again.

Nothing here logs or retries, or puts a body, a header value or a query
string in an exception. The caller logs `exc.error`.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import (
    ZohoAuthExpired,
    ZohoBusy,
    ZohoConfigError,
    ZohoCreditsExhausted,
    ZohoError,
    ZohoGone,
    ZohoRejected,
    ZohoTooLarge,
    ZohoUnavailable,
    ZohoUnknownOutcome,
    safe_code,
)

DEFAULT_TIMEOUT = 8.0
CREDITS_HEADER = "X-Rate-Limit-Remaining-v3"
# A field name Zoho gave, such as "/contactId". With the slash gone it must
# start with a letter, so a number can never pass as one.
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{0,79}")
_LINE_BREAK = re.compile(r"[\r\n\x00]")


def _header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    value = headers.get(name)
    if value is None and isinstance(headers, dict):
        wanted = name.lower()
        value = next((item for key, item in headers.items() if str(key).lower() == wanted), None)
    return value


def _parse(raw: bytes) -> Tuple[bool, Any]:
    """(readable, parsed). An empty body is readable, and None."""
    if not raw or not raw.strip():
        return True, None
    try:
        return True, json.loads(raw.decode("utf-8"))
    except ValueError:
        return False, None


def _fields(payload: Any) -> Tuple[str, ...]:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    names = []
    for item in errors if isinstance(errors, list) else ():
        name = item.get("fieldName") if isinstance(item, dict) else None
        if isinstance(name, str):
            name = name.strip().lstrip("/")
            if _FIELD.fullmatch(name) and name not in names:
                names.append(name)
    return tuple(names)


def _retry_after(headers: Any) -> Optional[float]:
    try:
        seconds = float(str(_header(headers, "Retry-After")).strip())
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _not_sent(reason: Any) -> ZohoError:
    error = "timeout" if isinstance(reason, TimeoutError) else "network"
    return ZohoUnavailable("Zoho could not be reached (%s); nothing was sent" % type(reason).__name__, error=error)


def _no_answer(exc: BaseException, write: bool) -> ZohoError:
    # socket.timeout is TimeoutError since Python 3.10.
    error = "timeout" if isinstance(exc, TimeoutError) else "network"
    name = type(exc).__name__
    if write:
        return ZohoUnknownOutcome("a write to Zoho had no answer (%s); it may have been made" % name, error=error)
    return ZohoUnavailable("Zoho did not answer (%s)" % name, error=error)


def _failure(status: int, payload: Any, headers: Any, write: bool) -> ZohoError:
    code = payload.get("errorCode") if isinstance(payload, dict) else None
    named = safe_code(code, "http_%d" % status)
    said = "Zoho answered %d %s" % (status, named)
    if status == 401 and code == "INVALID_OAUTH":
        return ZohoAuthExpired(said, error=named)
    if status in (401, 403):
        # SCOPE_MISMATCH, OAUTH_ORG_MISMATCH, FORBIDDEN, LICENSE_ACCESS_LIMITED
        # and any other refusal of access: ours to fix, retried hourly, and
        # shown on /health as "sending failing: <code>".
        return ZohoConfigError(said, error=named)
    if status in (400, 422):
        fields = _fields(payload)
        text = said + (" naming %s" % ", ".join(fields) if fields else "")
        return ZohoRejected(text, error=named, fields=fields)
    if status == 404:
        return ZohoGone(said, error=named)
    if status == 408:
        # The server gave up before it had the whole request.
        return ZohoUnavailable(said, error=named)
    if status == 413:
        return ZohoTooLarge(said, error=named)
    if status == 429:
        if code == "THRESHOLD_EXCEEDED":
            return ZohoCreditsExhausted(said, error=named, retry_after_seconds=_retry_after(headers))
        return ZohoBusy(said, error=named)
    if status >= 500:
        if write:
            return ZohoUnknownOutcome(said + " to a write; it may have been made", error=named)
        return ZohoUnavailable(said, error=named)
    return ZohoRejected(said, error=named)


class DeskHTTP:
    """Sends one request and classifies the answer. It holds no credential:
    the caller passes the headers, and this never repeats them."""

    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._opener = opener
        self.timeout = timeout
        # Zoho's X-Rate-Limit-Remaining-v3 from the last answer that had it:
        # the organisation's API credits left today, shared with the OMS.
        self.last_credits_remaining: Optional[int] = None

    def __repr__(self) -> str:
        return "DeskHTTP(timeout=%r)" % self.timeout

    def call(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None,
             *, write: bool, timeout: Optional[float] = None, classify: bool = True) -> Tuple[int, Any]:
        """(status, parsed JSON or None), or a ZohoError.

        With `classify=False`, any HTTP answer comes back as it came. This is
        for the token endpoint, whose body says what went wrong whatever the
        status. A 2xx body that is not JSON, and network failures, still raise.
        """
        for value in headers.values():
            if _LINE_BREAK.search(str(value)):
                # http.client would raise a ValueError that quotes the whole
                # header, token and all (the OpenRouter key, 2026-09-29). It is
                # refused here, without the value.
                raise ZohoConfigError("a request header holds a line break; nothing was sent", error="bad_header")
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener(request, timeout=timeout or self.timeout) as response:
                status = int(getattr(response, "status", None) or response.getcode())
                answer_headers = getattr(response, "headers", None)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status, answer_headers = exc.code, exc.headers
            try:
                raw = exc.read() or b""
            except (http.client.HTTPException, OSError):
                raw = b""
        except urllib.error.URLError as exc:
            raise _not_sent(exc.reason) from None
        except (http.client.HTTPException, OSError) as exc:
            raise _no_answer(exc, write) from None
        self._note_credits(answer_headers)
        readable, payload = _parse(raw)
        if status == 204:
            return 204, None
        if 200 <= status < 300:
            if readable:
                return status, payload
            if write:
                raise ZohoUnknownOutcome("Zoho answered %d to a write with a body that is not JSON" % status,
                                         error="unreadable")
            raise ZohoUnavailable("Zoho answered %d with a body that is not JSON" % status, error="unreadable")
        if not classify:
            return status, payload if readable else None
        raise _failure(status, payload if readable else None, answer_headers, write)

    def _note_credits(self, headers: Any) -> None:
        value = _header(headers, CREDITS_HEADER)
        if value is None:
            return
        text = str(value).strip()
        if text.isascii() and text.isdigit():
            self.last_credits_remaining = int(text)
