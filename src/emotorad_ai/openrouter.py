"""OpenRouter over HTTP: chat completions for the reply models, and the Decisions
API for Jev.

Standard library only, like tools/oms.py, so the transport is small enough to
read in one sitting and testable against a local server.

The key is a credential. It is read from the environment when the transport is
built, sent as a header, and nothing else: never logged, never part of an
exception message, never shown by repr.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

API_KEY_ENV = "OPENROUTER_API_KEY"
CHAT_PATH = "/v1/chat/completions"
DECISIONS_PATH = "/alpha/decisions"


class OpenRouterError(Exception):
    """Base for every failure reaching OpenRouter. `code` is stable and loggable."""

    code = "openrouter_error"


class OpenRouterConfigError(OpenRouterError):
    code = "config"


class OpenRouterAuthError(OpenRouterError):
    code = "auth"


class OpenRouterPaymentRequired(OpenRouterError):
    code = "payment_required"


class OpenRouterRateLimited(OpenRouterError):
    code = "rate_limited"


class OpenRouterUnavailable(OpenRouterError):
    """Ours or the network's, and retryable: timeouts, 5xx, 529 overloaded."""

    code = "unavailable"


class OpenRouterRequestError(OpenRouterError):
    """A 4xx other than auth, payment or rate limit: the request we built was wrong."""

    code = "bad_request"


class OpenRouterBadResponse(OpenRouterError):
    code = "bad_response"


def _for_status(status: int, message: str) -> OpenRouterError:
    text = "OpenRouter %d: %s" % (status, message)
    if status in (401, 403):
        return OpenRouterAuthError(text)
    if status == 402:
        return OpenRouterPaymentRequired(text)
    if status == 429:
        return OpenRouterRateLimited(text)
    if status == 408 or status >= 500:
        return OpenRouterUnavailable(text)
    return OpenRouterRequestError(text)


def _error_message(raw: bytes) -> str:
    """The provider's own message, trimmed. Never the request, which holds the key."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "no readable error body"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "error")[:200]
    return str(error or "error")[:200]


class OpenRouterTransport:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api",
        timeout: float = 30.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        # Stripped, as select_llm strips the Anthropic key: a value stored with
        # a trailing newline made http.client raise a ValueError that quoted
        # the whole Authorization header, key and all, and crashed the turn.
        key = (api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")).strip()
        if not key:
            raise OpenRouterConfigError("%s is not set" % API_KEY_ENV)
        if any(ch.isspace() or not ch.isprintable() for ch in key):
            # Damaged, not merely padded. Refused here, where it fails the
            # deploy's health check, and without the value in the message.
            raise OpenRouterConfigError(
                "%s contains a space or control character inside it; set it again" % API_KEY_ENV
            )
        self._api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener

    def __repr__(self) -> str:
        return "OpenRouterTransport(base_url=%r)" % self.base_url

    def post(self, path: str, body: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read() or b""
            except (http.client.HTTPException, OSError):
                body = b""
            raise self._scrubbed(_for_status(exc.code, _error_message(body))) from None
        except (http.client.HTTPException, OSError) as exc:
            # OSError covers URLError, timeouts and resets; HTTPException covers a
            # body cut off mid-read (IncompleteRead). All are ours to retry.
            raise OpenRouterUnavailable("OpenRouter could not be reached (%s)" % type(exc).__name__) from None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenRouterBadResponse("OpenRouter returned a body that is not JSON") from None
        if not isinstance(payload, dict):
            raise OpenRouterBadResponse("OpenRouter returned JSON that is not an object")

        # Upstream provider failures arrive as HTTP 200 with an error body.
        # Treating that as an answer would hand the agent an empty reply.
        if "error" in payload and not payload.get("choices") and "answers" not in payload:
            error = payload["error"] if isinstance(payload["error"], dict) else {}
            code = error.get("code")
            status = code if isinstance(code, int) and not isinstance(code, bool) else 502
            raise self._scrubbed(_for_status(status, str(error.get("message") or "upstream error")[:200]))
        return payload

    def _scrubbed(self, error: OpenRouterError) -> OpenRouterError:
        """Belt and braces: a provider that echoes the key back must not leak it."""
        message = str(error).replace(self._api_key, "[key]")
        return type(error)(message)
