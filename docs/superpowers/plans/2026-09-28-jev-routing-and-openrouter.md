# Jev routing, standard responses and OpenRouter models: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- Score each customer message with Jev on OpenRouter's Decisions API.
- Use the scores to send the turn down one of three paths:
  - a standard response
  - a narrow DeepSeek agent that sees one knowledge record
  - today's full agent on Haiku 4.5
- Orchestrate the turn with LangGraph.

**Architecture:**
- `Runtime.handle()` keeps its signature and becomes `graph.invoke()`. Each node is a thin wrapper over code that exists today.
- New pure modules do the thinking. `decisions.py` builds the questions and picks the path, `jev.py` parses answers, and `standard_responses.py` loads reviewed replies. That keeps the graph thin and every rule table-testable.
- OpenRouter is one standard-library transport shared by the chat client and the Jev client.

**Tech stack:** Python 3.12, `unittest`, `urllib`, PyYAML (already present), `langgraph>=1.2,<2` (new).

**Spec:** `docs/superpowers/specs/2026-09-28-jev-routing-and-openrouter-design.md`

## Global constraints

- **Tests.**
  - Run from the repo root with `python -m unittest discover -s tests -t .`.
  - A single module runs with `python -m unittest tests.<module> -v`. `tests/__init__.py` puts `src/` on the path.
  - Use `unittest` only; the repo has no pytest.
- **Baseline.** Before this plan, the suite ran 468 tests: 2 errors in `tests.test_video` (a missing `imageio_ffmpeg` on this machine) and 1 skip. Those two errors are reported, never "fixed" by editing tests.
- **Dependencies.** The only new one is `langgraph>=1.2,<2`. HTTP uses stdlib `urllib`, like `tools/oms.py`. There is no `openai` SDK.
- **Env vars, exactly:**
  - `EMOTORAD_AI_MODE` (`offline` | `bedrock` | `openrouter`)
  - `OPENROUTER_API_KEY`
  - `EMOTORAD_OPENROUTER_BASE_URL` (default `https://openrouter.ai/api`)
  - `EMOTORAD_JEV_MODEL` (default `typesafe/jev-1.13`)
  - `EMOTORAD_NARROW_MODEL` (default `deepseek/deepseek-v4-flash-0731`)
  - `EMOTORAD_FALLBACK_MODEL` (default `anthropic/claude-haiku-4.5`)
  - `EMOTORAD_JEV_TIMEOUT` (default `2.0`)
  - `EMOTORAD_OPENROUTER_TIMEOUT` (default `30`)
  - `EMOTORAD_OPENROUTER_ZDR` (default `1`)
- **Paths.** Chat goes to `/v1/chat/completions` and Jev to `/alpha/decisions`, both under the base URL.
- **Keys.** The OpenRouter key is read only by `OpenRouterTransport`. It never appears in `Settings`, a log event, an exception message or a `repr`.
- **Behaviour in other modes.** In `offline` and `bedrock` modes, Jev is never called and every existing test passes unchanged.
- **Order of controls.** Identity, the safety check and the handoff check run before Jev. The coverage post-check, the evidence post-check and the disclosure run after every path.
- **Prefetch.** Only read tools are prefetched: `lookup_warranty_record` and `lookup_error_code`. Write tools are never prefetched.
- **Style.**
  - Comments and customer-facing strings are in British English, with no em dashes in new text.
  - Match the repo's comment density and idiom: docstrings explain why.
- **Line endings.** Files use LF. On Windows, write files with the editor tools, not `Path.write_text`, which converts to CRLF.
- **Git.** Commit after each task on `feat/jev-routing`. Never push.

## Review focus

These are the five inputs most likely to bite, each with a test in the owning task:

1. **A photo sent with no text in the middle of a flow.** Expect no Jev call, and the conversation stays on the current record. With no current record, the full agent runs. (Task 6, rule for `EMPTY_MESSAGE`; Task 9 runtime test.)
2. **OpenRouter returns HTTP 200 with an `{"error": ...}` body**, which it does for upstream provider failures. Expect a typed error and a fallback, never a crash or an empty reply. (Task 1.)
3. **The narrow model returns tool-call `arguments` that are not valid JSON.** Expect `{}`, so the registry answers `missing_arguments` and the loop continues. (Task 2.)
4. **Jev confidently picks a standard response, but the message is Hindi and that response has no Hindi reply.** Expect the full agent, never an English canned reply. (Task 6.)
5. **The narrow model fails partway through a turn after the agent loop has already appended the user message.** Expect history to be rolled back before the full agent runs, so the model never sees the customer's message twice. (Task 9.)

---

### Task 1: Settings modes and the OpenRouter transport

**Files:**
- Modify: `src/emotorad_ai/config.py`
- Create: `src/emotorad_ai/openrouter.py`
- Create: `tests/fake_http.py`
- Test: `tests/test_openrouter_transport.py`

**Interfaces:**
- Produces:
  - `Settings.mode`, `.openrouter_base_url`, `.jev_model`, `.narrow_model`, `.fallback_model`, `.jev_timeout`, `.openrouter_timeout`, `.openrouter_zdr`
  - `MODES`
  - `openrouter.OpenRouterTransport(api_key=None, base_url=..., timeout=30.0, opener=urlopen)` with `.post(path: str, body: dict, timeout: float | None = None) -> dict`
  - errors `OpenRouterError` (attribute `.code`) and its subclasses `OpenRouterConfigError`, `OpenRouterAuthError`, `OpenRouterPaymentRequired`, `OpenRouterRateLimited`, `OpenRouterUnavailable`, `OpenRouterRequestError`, `OpenRouterBadResponse`
  - constants `API_KEY_ENV`, `CHAT_PATH`, `DECISIONS_PATH`
  - `tests/fake_http.FakeServer`, `tests/fake_http.FakeTransport`

- [ ] **Step 1: Write the test helpers and the failing tests**

`tests/fake_http.py`:

```python
"""Test doubles for anything that speaks HTTP.

`FakeServer` is a real server on 127.0.0.1 with an ephemeral port, so the
transport is exercised through urllib exactly as it runs in production.
`FakeTransport` stands in for the transport itself when a test only cares about
the body a client builds and how it reads the answer.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    def __init__(self):
        self.requests = []
        self._responses = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw or b"{}")}
                )
                if outer._responses:
                    status, body, delay = outer._responses.pop(0)
                else:
                    status, body, delay = 500, {"error": "no response queued"}, 0.0
                if delay:
                    time.sleep(delay)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def queue(self, status, body, delay=0.0):
        self._responses.append((status, body, delay))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


class FakeTransport:
    """Replays queued bodies (or raises queued exceptions) and records every post."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def post(self, path, body, timeout=None):
        self.calls.append({"path": path, "body": body, "timeout": timeout})
        if not self.responses:
            raise AssertionError("FakeTransport ran out of queued responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
```

`tests/test_openrouter_transport.py`:

```python
import os
import unittest
from unittest import mock

from emotorad_ai.config import MODES, Settings
from emotorad_ai.openrouter import (
    API_KEY_ENV,
    OpenRouterAuthError,
    OpenRouterBadResponse,
    OpenRouterConfigError,
    OpenRouterPaymentRequired,
    OpenRouterRateLimited,
    OpenRouterRequestError,
    OpenRouterTransport,
    OpenRouterUnavailable,
)
from tests.fake_http import FakeServer

KEY = "sk-or-test-DO-NOT-LEAK"


class SettingsModeTests(unittest.TestCase):
    def test_the_modes_are_the_three_the_spec_names(self):
        self.assertEqual(MODES, ("offline", "bedrock", "openrouter"))
        self.assertEqual(Settings(mode="openrouter").mode, "openrouter")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            Settings(mode="openai")

    def test_model_defaults_match_the_spec(self):
        settings = Settings()
        self.assertEqual(settings.jev_model, "typesafe/jev-1.13")
        self.assertEqual(settings.narrow_model, "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(settings.fallback_model, "anthropic/claude-haiku-4.5")
        self.assertEqual(settings.openrouter_base_url, "https://openrouter.ai/api")

    def test_the_key_is_not_a_setting(self):
        self.assertFalse(any("key" in name for name in Settings.__dataclass_fields__))


class TransportTests(unittest.TestCase):
    def transport(self, server, timeout=5.0):
        return OpenRouterTransport(api_key=KEY, base_url=server.url, timeout=timeout)

    def test_posts_json_with_the_bearer_key_and_returns_the_body(self):
        with FakeServer() as server:
            server.queue(200, {"choices": [{"message": {"content": "hi"}}]})
            body = self.transport(server).post("/v1/chat/completions", {"model": "m"})
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(request["body"], {"model": "m"})

    def test_status_codes_map_to_typed_errors(self):
        cases = [
            (401, OpenRouterAuthError), (403, OpenRouterAuthError), (402, OpenRouterPaymentRequired),
            (429, OpenRouterRateLimited), (500, OpenRouterUnavailable), (529, OpenRouterUnavailable),
            (408, OpenRouterUnavailable), (400, OpenRouterRequestError), (422, OpenRouterRequestError),
        ]
        for status, error in cases:
            with self.subTest(status=status), FakeServer() as server:
                server.queue(status, {"error": {"code": status, "message": "nope"}})
                with self.assertRaises(error):
                    self.transport(server).post("/x", {})

    def test_a_200_carrying_an_error_body_is_an_error_not_an_answer(self):
        # OpenRouter reports an upstream provider failure this way.
        with FakeServer() as server:
            server.queue(200, {"error": {"code": 502, "message": "provider returned error"}})
            with self.assertRaises(OpenRouterUnavailable):
                self.transport(server).post("/x", {})

    def test_a_body_that_is_not_json_is_a_bad_response(self):
        with FakeServer() as server:
            server.queue(200, b"<html>gateway</html>")
            with self.assertRaises(OpenRouterBadResponse):
                self.transport(server).post("/x", {})

    def test_a_slow_answer_is_unavailable(self):
        with FakeServer() as server:
            server.queue(200, {"choices": []}, delay=1.0)
            with self.assertRaises(OpenRouterUnavailable):
                self.transport(server, timeout=0.2).post("/x", {})

    def test_a_per_call_timeout_overrides_the_default(self):
        with FakeServer() as server:
            server.queue(200, {"answers": {}}, delay=1.0)
            with self.assertRaises(OpenRouterUnavailable):
                self.transport(server, timeout=10.0).post("/x", {}, timeout=0.2)

    def test_the_key_never_appears_in_an_error_or_the_repr(self):
        with FakeServer() as server:
            server.queue(401, {"error": {"code": 401, "message": "bad key " + KEY}})
            transport = self.transport(server)
            with self.assertRaises(OpenRouterAuthError) as caught:
                transport.post("/x", {})
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(transport))

    def test_a_missing_key_fails_when_the_transport_is_built(self):
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}):
            with self.assertRaises(OpenRouterConfigError):
                OpenRouterTransport()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_openrouter_transport -v`
Expected: ERROR, `ImportError: cannot import name 'MODES'` (and `No module named 'emotorad_ai.openrouter'`).

- [ ] **Step 3: Implement**

In `src/emotorad_ai/config.py`, replace the `Settings` class body's end and add `MODES`. The full file becomes:

```python
"""Runtime settings. Everything is env-overridable so the same code runs against
mocks locally and against real systems in ECS without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Which models answer. `offline` is a fixed planner, no network; `bedrock` is
# Claude in EMotorad's own AWS account; `openrouter` is Jev routing plus the
# OpenRouter reply models, and sends customer text outside AWS, so it needs
# sign-off before real customer traffic (spec §1.8).
MODES = ("offline", "bedrock", "openrouter")


@dataclass(frozen=True)
class Settings:
    # Claude on Bedrock keeps LLM traffic inside Emotorad's existing AWS boundary.
    # Bedrock model IDs carry the `anthropic.` prefix.
    model: str = os.environ.get("EMOTORAD_AI_MODEL", "anthropic.claude-opus-5")
    aws_region: str = os.environ.get("AWS_REGION", "ap-south-1")

    # Support chat is latency-sensitive (target: under 3s per turn), and battery
    # triage is a bounded problem — start low and raise per-route if evals say so.
    effort: str = os.environ.get("EMOTORAD_AI_EFFORT", "low")
    max_tokens: int = int(os.environ.get("EMOTORAD_AI_MAX_TOKENS", "16000"))

    # Hard ceiling on tool-calling round trips in a single turn.
    max_agent_iterations: int = int(os.environ.get("EMOTORAD_AI_MAX_ITERATIONS", "6"))

    log_path: str = os.environ.get("EMOTORAD_AI_LOG_PATH", "logs/conversations.jsonl")
    log_to_stdout: bool = os.environ.get("EMOTORAD_AI_LOG_STDOUT", "0") == "1"

    mode: str = os.environ.get("EMOTORAD_AI_MODE", "offline")

    # OpenRouter. The key is deliberately not here: Settings gets printed and
    # logged, a credential must not be. The transport reads it from the
    # environment itself.
    openrouter_base_url: str = os.environ.get("EMOTORAD_OPENROUTER_BASE_URL", "https://openrouter.ai/api")
    jev_model: str = os.environ.get("EMOTORAD_JEV_MODEL", "typesafe/jev-1.13")
    narrow_model: str = os.environ.get("EMOTORAD_NARROW_MODEL", "deepseek/deepseek-v4-flash-0731")
    fallback_model: str = os.environ.get("EMOTORAD_FALLBACK_MODEL", "anthropic/claude-haiku-4.5")
    # Jev sits in front of every turn, so it gets a tight budget: a slow answer
    # falls back to the full agent rather than holding the customer up.
    jev_timeout: float = float(os.environ.get("EMOTORAD_JEV_TIMEOUT", "2.0"))
    openrouter_timeout: float = float(os.environ.get("EMOTORAD_OPENROUTER_TIMEOUT", "30"))
    # Zero-data-retention providers only, unless someone deliberately turns it off.
    openrouter_zdr: bool = os.environ.get("EMOTORAD_OPENROUTER_ZDR", "1") == "1"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("EMOTORAD_AI_MODE must be one of %s, not %r" % (", ".join(MODES), self.mode))


def load_settings() -> Settings:
    return Settings()
```

`src/emotorad_ai/openrouter.py`:

```python
"""OpenRouter over HTTP: chat completions for the reply models, and the Decisions
API for Jev.

Standard library only, like tools/oms.py, so the transport is small enough to
read in one sitting and testable against a local server.

The key is a credential. It is read from the environment when the transport is
built, sent as a header, and nothing else: never logged, never part of an
exception message, never shown by repr.
"""

from __future__ import annotations

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
        key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        if not key:
            raise OpenRouterConfigError("%s is not set" % API_KEY_ENV)
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
            error = _for_status(exc.code, _error_message(exc.read() or b""))
            raise self._scrubbed(error) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
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
            status = code if isinstance(code, int) else 502
            raise self._scrubbed(_for_status(status, str(error.get("message") or "upstream error")[:200]))
        return payload

    def _scrubbed(self, error: OpenRouterError) -> OpenRouterError:
        """Belt and braces: a provider that echoes the key back must not leak it."""
        message = str(error).replace(self._api_key, "[key]")
        return type(error)(message)
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_openrouter_transport -v`
Expected: all pass.

Run: `python -m unittest discover -s tests -t .`
Expected: 468 + 12 new tests. Only the 2 known `test_video` errors.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/config.py src/emotorad_ai/openrouter.py tests/fake_http.py tests/test_openrouter_transport.py
git commit -m "Add model modes to Settings and an OpenRouter transport"
```

---

### Task 2: `OpenRouterChat`, the reply models behind the existing LLM interface

**Files:**
- Modify: `src/emotorad_ai/llm.py` (append after `BedrockClaude`)
- Test: `tests/test_openrouter_chat.py`

**Interfaces:**
- Consumes: `openrouter.CHAT_PATH`, and any transport with `.post(path, body, timeout=None) -> dict`
- Produces:
  - `llm.OpenRouterChat(model: str, transport, max_tokens: int = 4096, zdr: bool = True)`, with `.model` and `.create(system, messages, tools) -> LLMResponse`
  - `llm.to_openai_messages(system, messages, cache_system=False) -> list`
  - `llm.to_openai_tools(tools) -> list`
  - `llm.from_openai_response(body) -> LLMResponse`

- [ ] **Step 1: Write the failing tests**

`tests/test_openrouter_chat.py`:

```python
import json
import unittest

from emotorad_ai.llm import OpenRouterChat, from_openai_response, to_openai_messages, to_openai_tools
from emotorad_ai.openrouter import CHAT_PATH, OpenRouterBadResponse
from tests.fake_http import FakeTransport

HISTORY = [
    {"role": "user", "content": "my battery won't charge"},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "private", "signature": "sig"},
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_1", "name": "search_knowledge", "input": {"query": "won't charge"}},
            {"type": "tool_use", "id": "toolu_2", "name": "lookup_warranty_record", "input": {}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"data\": {}}", "is_error": False},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "{\"error\": {}}", "is_error": True},
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "Try another socket."}]},
]


def reply(content="", tool_calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "gen-1", "choices": [{"finish_reason": finish, "message": message}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.00001}}


class ToOpenAIMessagesTests(unittest.TestCase):
    def test_history_translates_block_for_block(self):
        messages = to_openai_messages("SYS", HISTORY)
        self.assertEqual(messages[0], {"role": "system", "content": "SYS"})
        self.assertEqual(messages[1], {"role": "user", "content": "my battery won't charge"})
        assistant = messages[2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"], "Let me check.")
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["toolu_1", "toolu_2"])
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"query": "won't charge"})
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "search_knowledge")
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "toolu_1", "content": "{\"data\": {}}"})
        self.assertEqual(messages[4], {"role": "tool", "tool_call_id": "toolu_2", "content": "{\"error\": {}}"})
        self.assertEqual(messages[5], {"role": "assistant", "content": "Try another socket."})

    def test_thinking_blocks_never_leave(self):
        self.assertNotIn("private", json.dumps(to_openai_messages("SYS", HISTORY)))

    def test_an_assistant_turn_with_only_tool_calls_has_null_content(self):
        history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}]
        self.assertIsNone(to_openai_messages("S", history)[1]["content"])

    def test_the_system_prompt_is_marked_for_caching_when_asked(self):
        system = to_openai_messages("SYS", [], cache_system=True)[0]
        self.assertEqual(system["content"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])


class ToOpenAIToolsTests(unittest.TestCase):
    def test_input_schema_becomes_function_parameters(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        tools = to_openai_tools([{"name": "search", "description": "d", "input_schema": schema}])
        self.assertEqual(tools, [{"type": "function", "function": {"name": "search", "description": "d", "parameters": schema}}])


class FromOpenAIResponseTests(unittest.TestCase):
    def test_parallel_tool_calls_become_tool_use_blocks(self):
        body = reply(content="", finish="tool_calls", tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "lookup_warranty_record", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "search_knowledge", "arguments": "{\"query\":\"x\"}"}},
        ])
        response = from_openai_response(body)
        self.assertEqual(response.stop_reason, "tool_use")
        self.assertTrue(response.wants_tools)
        self.assertEqual([t.name for t in response.tool_uses], ["lookup_warranty_record", "search_knowledge"])
        self.assertEqual(response.tool_uses[1].arguments, {"query": "x"})
        self.assertEqual([b["type"] for b in response.api_content], ["tool_use", "tool_use"])

    def test_tool_calls_decide_the_stop_reason_even_when_finish_says_stop(self):
        body = reply(finish="stop", tool_calls=[{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{}"}}])
        self.assertEqual(from_openai_response(body).stop_reason, "tool_use")

    def test_arguments_that_are_not_json_become_an_empty_dict(self):
        body = reply(finish="tool_calls", tool_calls=[{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{not json"}}])
        self.assertEqual(from_openai_response(body).tool_uses[0].arguments, {})

    def test_text_and_finish_reasons_map(self):
        self.assertEqual(from_openai_response(reply("Hi")).stop_reason, "end_turn")
        self.assertEqual(from_openai_response(reply("Hi")).text, "Hi")
        self.assertEqual(from_openai_response(reply("Hi", finish="length")).stop_reason, "max_tokens")
        self.assertEqual(from_openai_response(reply("Hi")).api_content, [{"type": "text", "text": "Hi"}])

    def test_usage_carries_tokens_and_cost(self):
        usage = from_openai_response(reply("Hi")).usage
        self.assertEqual(usage, {"input_tokens": 100, "output_tokens": 20, "cost": 0.00001})

    def test_no_choices_is_a_bad_response(self):
        with self.assertRaises(OpenRouterBadResponse):
            from_openai_response({"id": "x", "choices": []})


class OpenRouterChatTests(unittest.TestCase):
    def test_create_posts_the_translated_request(self):
        transport = FakeTransport([reply("Hello")])
        chat = OpenRouterChat("deepseek/deepseek-v4-flash-0731", transport, max_tokens=512)
        response = chat.create("SYS", [{"role": "user", "content": "hi"}], [
            {"name": "search", "description": "d", "input_schema": {"type": "object", "properties": {}}}
        ])
        self.assertEqual(response.text, "Hello")
        call = transport.calls[0]
        self.assertEqual(call["path"], CHAT_PATH)
        body = call["body"]
        self.assertEqual(body["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["usage"], {"include": True})
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["tools"][0]["function"]["name"], "search")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})

    def test_no_tools_key_when_there_are_no_tools(self):
        transport = FakeTransport([reply("Hello")])
        OpenRouterChat("m", transport).create("S", [], [])
        self.assertNotIn("tools", transport.calls[0]["body"])

    def test_anthropic_models_cache_the_system_prompt_and_others_do_not(self):
        transport = FakeTransport([reply("a"), reply("b")])
        OpenRouterChat("anthropic/claude-haiku-4.5", transport).create("S", [], [])
        OpenRouterChat("deepseek/deepseek-v4-flash-0731", transport).create("S", [], [])
        self.assertIsInstance(transport.calls[0]["body"]["messages"][0]["content"], list)
        self.assertEqual(transport.calls[1]["body"]["messages"][0]["content"], "S")

    def test_zdr_off_sends_no_provider_preferences(self):
        transport = FakeTransport([reply("a")])
        OpenRouterChat("m", transport, zdr=False).create("S", [], [])
        self.assertNotIn("provider", transport.calls[0]["body"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_openrouter_chat -v`
Expected: ERROR, `ImportError: cannot import name 'OpenRouterChat'`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/llm.py`, change the import block to add `CHAT_PATH` and `OpenRouterBadResponse`:

```python
from .config import Settings
from .openrouter import CHAT_PATH, OpenRouterBadResponse
```

Update the module docstring's first line to `"""Model access: Claude on AWS Bedrock, reply models on OpenRouter, and scripted stand-ins for tests.`. Then insert after the `BedrockClaude` class:

```python
class OpenRouterChat:
    """A reply model on OpenRouter, behind the same interface as BedrockClaude.

    Everything upstream keeps the Anthropic-shaped history it has always used:
    text, tool_use and tool_result blocks. Translation to OpenRouter's
    OpenAI-shaped messages happens here and only here, so the agent loop, the
    playground and ScriptedClaude do not know which provider answered.
    """

    def __init__(self, model: str, transport: Any, max_tokens: int = 4096, zdr: bool = True) -> None:
        self.model = model
        self._transport = transport
        self.max_tokens = max_tokens
        self.zdr = zdr
        # Prompt caching is an explicit breakpoint on Anthropic models and
        # automatic on the others, so only Anthropic gets the marker.
        self.cache_system = model.startswith("anthropic/")

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, messages, cache_system=self.cache_system),
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        return from_openai_response(self._transport.post(CHAT_PATH, body))


def to_openai_messages(
    system: str, messages: Sequence[Dict[str, Any]], cache_system: bool = False
) -> List[Dict[str, Any]]:
    """Anthropic-shaped history to OpenAI-shaped messages.

    Thinking blocks are dropped: they belong to the model that wrote them and
    another provider cannot verify their signatures.
    """
    if cache_system:
        system_message: Dict[str, Any] = {
            "role": "system",
            "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }
    else:
        system_message = {"role": "system", "content": system}
    out: List[Dict[str, Any]] = [system_message]

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = [b for b in (content or []) if isinstance(b, dict)]
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")]

        if role == "assistant":
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})},
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            entry: Dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
            if calls:
                entry["tool_calls"] = calls
            elif entry["content"] is None:
                entry["content"] = ""
            out.append(entry)
            continue

        # A user turn: tool results become `tool` messages, which must follow
        # the assistant turn that asked for them, and any text comes after.
        for block in blocks:
            if block.get("type") == "tool_result":
                result = block.get("content")
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": result if isinstance(result, str) else json.dumps(result, default=str),
                    }
                )
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def to_openai_tools(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


_FINISH_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def from_openai_response(body: Dict[str, Any]) -> LLMResponse:
    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        raise OpenRouterBadResponse("OpenRouter returned no choices")
    choice = choices[0] or {}
    message = choice.get("message") or {}

    content = message.get("content")
    if isinstance(content, list):
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    else:
        text = content or ""
    text = text.strip()

    tool_uses: List[ToolUse] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            # The registry answers missing_arguments and the loop carries on,
            # which beats failing the whole turn over one malformed call.
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        tool_uses.append(ToolUse(id=call.get("id") or "call_%d" % len(tool_uses), name=function.get("name", ""), arguments=arguments))

    api_content: List[Dict[str, Any]] = []
    if text:
        api_content.append({"type": "text", "text": text})
    for tool_use in tool_uses:
        api_content.append({"type": "tool_use", "id": tool_use.id, "name": tool_use.name, "input": tool_use.arguments})

    # Some providers report "stop" alongside tool calls; the calls are the truth.
    if tool_uses:
        stop_reason = "tool_use"
    else:
        stop_reason = _FINISH_REASONS.get(choice.get("finish_reason") or "stop", "end_turn")

    return LLMResponse(
        stop_reason=stop_reason,
        text=text,
        tool_uses=tool_uses,
        api_content=api_content,
        usage=_openrouter_usage(body.get("usage")),
    )


def _openrouter_usage(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    usage: Dict[str, Any] = {
        "input_tokens": int(raw.get("prompt_tokens") or 0),
        "output_tokens": int(raw.get("completion_tokens") or 0),
    }
    if raw.get("cost") is not None:
        usage["cost"] = float(raw["cost"])
    cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached:
        usage["cache_read_input_tokens"] = int(cached)
    return usage
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_openrouter_chat -v`, then the full suite.
Expected: all pass. Only the 2 known `test_video` errors.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/llm.py tests/test_openrouter_chat.py
git commit -m "Add OpenRouterChat behind the existing LLM interface"
```

---

### Task 3: The Jev client

**Files:**
- Create: `src/emotorad_ai/jev.py`
- Create: `tests/data/jev_decisions_documented.json`
- Test: `tests/test_jev.py`

**Interfaces:**
- Consumes: `openrouter.DECISIONS_PATH`, `OpenRouterError` (`.code`)
- Produces:
  - `Question(type, instructions, criteria)` with `.to_dict()`
  - `choice(instructions, criteria: Mapping[str, str]) -> Question`
  - `noul(instructions, true="", false="") -> Question`
  - `ChoiceAnswer(choice, probabilities, confidence)` with `.p`
  - `NoulAnswer(p)`
  - `JevDecision(answers, model="", cost=None, latency_ms=0)` with `.scores()`
  - `JevError(code, message)`
  - `JevClient(transport, model="typesafe/jev-1.13", timeout=2.0, clock=time.monotonic)` with `.decide(state, questions) -> JevDecision`
  - `parse_decision(payload, questions) -> JevDecision`
  - `ScriptedJev(decisions)` with `.calls`
  - test helpers `choose(choice, p)` and `yes(p)`

- [ ] **Step 1: Write the fixture and the failing tests**

`tests/data/jev_decisions_documented.json`. This is the response shape OpenRouter documents for the Decisions API, fetched 2026-09-28. `scripts/jev_probe.py` (Task 12) saves the live shape next to it in `docs/api-shapes/`.

```json
{
  "questions": {
    "team": {"type": "choice", "instructions": "Which team should handle this ticket?",
             "criteria": {"billing": "Payment or refund issues", "technical": "Bugs or outages", "sales": "Plans or upgrades"}},
    "refund_requested": {"type": "noul", "instructions": "The customer is explicitly asking for a refund"}
  },
  "response": {
    "id": "gen-documented-example",
    "model": "typesafe/jev-1.13-20260915",
    "provider": "TypeSafe",
    "answers": {
      "team": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.91, "technical": 0.06, "sales": 0.03}, "confidence": 0.84},
      "refund_requested": {"type": "noul", "noul": 0.97}
    },
    "usage": {"input_tokens": 357, "output_tokens": 38, "cost": 0.000014994}
  }
}
```

`tests/test_jev.py`:

```python
import json
import unittest
from pathlib import Path

from emotorad_ai.jev import (
    ChoiceAnswer,
    JevClient,
    JevDecision,
    JevError,
    NoulAnswer,
    Question,
    ScriptedJev,
    choice,
    choose,
    noul,
    parse_decision,
    yes,
)
from emotorad_ai.openrouter import (
    DECISIONS_PATH,
    OpenRouterAuthError,
    OpenRouterBadResponse,
    OpenRouterPaymentRequired,
    OpenRouterRateLimited,
    OpenRouterUnavailable,
)
from tests.fake_http import FakeTransport

DOCUMENTED = Path(__file__).parent / "data" / "jev_decisions_documented.json"
LIVE_SHAPE = Path(__file__).resolve().parents[1] / "docs" / "api-shapes" / "jev-decisions.json"

QUESTIONS = {
    "category": choice("Which area is this?", {"battery": "Battery", "motor": "Motor", "none_of_these": "Other"}),
    "needs_warranty_lookup": noul("Asks about warranty", true="Asks about cover", false="Does not"),
}
GOOD = {
    "model": "typesafe/jev-1.13-20260915",
    "answers": {
        "category": {"type": "choice", "choice": "battery",
                     "probabilities": {"battery": 0.9, "motor": 0.07, "none_of_these": 0.03}, "confidence": 0.8},
        "needs_warranty_lookup": {"type": "noul", "noul": 0.2},
    },
    "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0000126},
}


def questions_from_fixture(raw):
    return {qid: Question(q["type"], q["instructions"], q.get("criteria")) for qid, q in raw.items()}


class QuestionTests(unittest.TestCase):
    def test_choice_and_noul_serialise_to_the_decisions_api_shape(self):
        self.assertEqual(QUESTIONS["category"].to_dict()["type"], "choice")
        self.assertEqual(QUESTIONS["category"].to_dict()["criteria"]["battery"], "Battery")
        self.assertEqual(
            QUESTIONS["needs_warranty_lookup"].to_dict(),
            {"type": "noul", "instructions": "Asks about warranty", "criteria": {"true": "Asks about cover", "false": "Does not"}},
        )
        self.assertNotIn("criteria", noul("Plain").to_dict())

    def test_a_choice_needs_two_to_255_options(self):
        with self.assertRaises(ValueError):
            choice("x", {"only": "one"})
        with self.assertRaises(ValueError):
            choice("x", {str(i): "o" for i in range(256)})


class ParseTests(unittest.TestCase):
    def test_parses_choice_and_noul_answers(self):
        decision = parse_decision(GOOD, QUESTIONS)
        self.assertEqual(decision.answers["category"].choice, "battery")
        self.assertAlmostEqual(decision.answers["category"].p, 0.9)
        self.assertAlmostEqual(decision.answers["needs_warranty_lookup"].p, 0.2)
        self.assertEqual(decision.model, "typesafe/jev-1.13-20260915")
        self.assertAlmostEqual(decision.cost, 0.0000126)

    def test_the_documented_shape_parses(self):
        raw = json.loads(DOCUMENTED.read_text(encoding="utf-8"))
        decision = parse_decision(raw["response"], questions_from_fixture(raw["questions"]))
        self.assertEqual(decision.answers["team"].choice, "billing")
        self.assertAlmostEqual(decision.answers["refund_requested"].p, 0.97)

    def test_the_live_shape_parses_once_the_probe_has_saved_it(self):
        if not LIVE_SHAPE.exists():
            self.skipTest("run scripts/jev_probe.py once to capture the live Decisions API shape")
        raw = json.loads(LIVE_SHAPE.read_text(encoding="utf-8"))
        decision = parse_decision(raw["response"], questions_from_fixture(raw["questions"]))
        self.assertEqual(set(decision.answers), set(raw["questions"]))

    def test_malformed_answers_are_refused(self):
        def broken(mutate):
            payload = json.loads(json.dumps(GOOD))
            mutate(payload)
            return payload

        cases = {
            "no answers object": broken(lambda p: p.pop("answers")),
            "missing answer": broken(lambda p: p["answers"].pop("category")),
            "wrong type": broken(lambda p: p["answers"]["category"].update(type="noul")),
            "choice not in criteria": broken(lambda p: p["answers"]["category"].update(choice="brakes")),
            "probability key not in criteria": broken(lambda p: p["answers"]["category"]["probabilities"].update(brakes=0.1)),
            "probability out of range": broken(lambda p: p["answers"]["category"]["probabilities"].update(battery=1.4)),
            "noul out of range": broken(lambda p: p["answers"]["needs_warranty_lookup"].update(noul=-0.1)),
            "noul not a number": broken(lambda p: p["answers"]["needs_warranty_lookup"].update(noul="high")),
            "noul is a bool": broken(lambda p: p["answers"]["needs_warranty_lookup"].update(noul=True)),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(JevError) as caught:
                parse_decision(payload, QUESTIONS)
            self.assertEqual(caught.exception.code, "bad_response")


class ClientTests(unittest.TestCase):
    def test_decide_posts_state_and_questions_with_the_jev_timeout(self):
        transport = FakeTransport([GOOD])
        ticks = iter([10.0, 10.25])
        client = JevClient(transport, model="typesafe/jev-1.13", timeout=2.0, clock=lambda: next(ticks))
        decision = client.decide({"message": "battery not charging"}, QUESTIONS)
        call = transport.calls[0]
        self.assertEqual(call["path"], DECISIONS_PATH)
        self.assertEqual(call["timeout"], 2.0)
        self.assertEqual(call["body"]["model"], "typesafe/jev-1.13")
        self.assertEqual(call["body"]["state"], {"message": "battery not charging"})
        self.assertEqual(set(call["body"]["questions"]), {"category", "needs_warranty_lookup"})
        self.assertEqual(decision.latency_ms, 250)

    def test_transport_failures_become_jev_errors_with_stable_codes(self):
        cases = [
            (OpenRouterAuthError("x"), "auth"), (OpenRouterRateLimited("x"), "rate_limited"),
            (OpenRouterUnavailable("x"), "unavailable"), (OpenRouterBadResponse("x"), "bad_response"),
            (OpenRouterPaymentRequired("x"), "payment_required"),
        ]
        for error, code in cases:
            with self.subTest(code), self.assertRaises(JevError) as caught:
                JevClient(FakeTransport([error])).decide({}, QUESTIONS)
            self.assertEqual(caught.exception.code, code)


class ScriptedJevTests(unittest.TestCase):
    def test_replays_decisions_and_errors_and_records_calls(self):
        jev = ScriptedJev([JevDecision(answers={"category": choose("battery", 0.9)}), JevError("unavailable", "down")])
        self.assertEqual(jev.decide({"m": 1}, QUESTIONS).answers["category"].choice, "battery")
        with self.assertRaises(JevError):
            jev.decide({"m": 2}, QUESTIONS)
        self.assertEqual([c["state"] for c in jev.calls], [{"m": 1}, {"m": 2}])

    def test_helpers_build_answers(self):
        self.assertEqual(choose("battery", 0.9), ChoiceAnswer("battery", {"battery": 0.9}, 0.9))
        self.assertEqual(yes(0.7), NoulAnswer(0.7))

    def test_scores_are_compact_and_carry_no_text(self):
        decision = JevDecision(answers={"category": choose("battery", 0.912345), "w": yes(0.1)})
        self.assertEqual(decision.scores(), {"category": {"choice": "battery", "p": 0.9123}, "w": {"p": 0.1}})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_jev -v`
Expected: ERROR, `No module named 'emotorad_ai.jev'`.

- [ ] **Step 3: Implement `src/emotorad_ai/jev.py`**

```python
"""Jev, TypeSafe's System One decision model, through OpenRouter's Decisions API.

Jev does not write text. It takes a block of state and a set of typed
questions and returns, for each, a structured answer with probabilities:

* **choice**: one label from the criteria we define, the full distribution over
  every label, and a confidence (how concentrated that distribution is);
* **noul**: one probability that the statement is true.

Our code reads those numbers and decides what happens next (decisions.py). The
parser here is strict on purpose: the Decisions API is alpha, and an answer we
cannot fully trust is reported as an error so the turn falls back to the full
agent, rather than half-read and acted on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from .openrouter import DECISIONS_PATH, OpenRouterError


class JevError(Exception):
    """Anything that stops a turn using Jev. `code` is stable and loggable:
    auth, rate_limited, unavailable, bad_response, bad_request,
    payment_required, config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Question:
    type: str
    instructions: str
    criteria: Any = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria:
            payload["criteria"] = dict(self.criteria)
        return payload


def choice(instructions: str, criteria: Mapping[str, str]) -> Question:
    if not 2 <= len(criteria) <= 255:
        raise ValueError("a choice needs between 2 and 255 options, got %d" % len(criteria))
    return Question("choice", instructions, dict(criteria))


def noul(instructions: str, true: str = "", false: str = "") -> Question:
    criteria = {key: text for key, text in (("true", true), ("false", false)) if text}
    return Question("noul", instructions, criteria or None)


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float

    @property
    def p(self) -> float:
        """The probability of the option Jev chose, which is what thresholds compare."""
        return float(self.probabilities.get(self.choice, 0.0))


@dataclass(frozen=True)
class NoulAnswer:
    p: float


Answer = Union[ChoiceAnswer, NoulAnswer]


@dataclass(frozen=True)
class JevDecision:
    answers: Mapping[str, Answer]
    model: str = ""
    cost: Optional[float] = None
    latency_ms: int = 0

    def scores(self) -> Dict[str, Any]:
        """What gets logged: the numbers, never the state they were computed from."""
        out: Dict[str, Any] = {}
        for question_id, answer in self.answers.items():
            if isinstance(answer, ChoiceAnswer):
                out[question_id] = {"choice": answer.choice, "p": round(answer.p, 4)}
            else:
                out[question_id] = {"p": round(answer.p, 4)}
        return out


def _probability(value: Any, question_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError("bad_response", "%s: probability is not a number" % question_id)
    if not 0.0 <= float(value) <= 1.0:
        raise JevError("bad_response", "%s: probability %r is outside 0..1" % (question_id, value))
    return float(value)


def parse_decision(payload: Mapping[str, Any], questions: Mapping[str, Question]) -> JevDecision:
    raw_answers = payload.get("answers") if isinstance(payload, Mapping) else None
    if not isinstance(raw_answers, Mapping):
        raise JevError("bad_response", "the response has no answers object")

    answers: Dict[str, Answer] = {}
    for question_id, question in questions.items():
        raw = raw_answers.get(question_id)
        if not isinstance(raw, Mapping):
            raise JevError("bad_response", "no answer for %s" % question_id)
        if raw.get("type") != question.type:
            raise JevError("bad_response", "%s: expected a %s answer" % (question_id, question.type))

        if question.type == "choice":
            chosen = raw.get("choice")
            if chosen not in question.criteria:
                raise JevError("bad_response", "%s: %r is not one of the options" % (question_id, chosen))
            probabilities = raw.get("probabilities")
            if not isinstance(probabilities, Mapping) or not probabilities:
                raise JevError("bad_response", "%s: no probabilities" % question_id)
            clean: Dict[str, float] = {}
            for option, value in probabilities.items():
                if option not in question.criteria:
                    raise JevError("bad_response", "%s: probability for unknown option %r" % (question_id, option))
                clean[option] = _probability(value, question_id)
            answers[question_id] = ChoiceAnswer(
                choice=chosen,
                probabilities=clean,
                confidence=_probability(raw.get("confidence", 0.0), question_id),
            )
        elif question.type == "noul":
            answers[question_id] = NoulAnswer(p=_probability(raw.get("noul"), question_id))
        else:
            raise JevError("bad_request", "%s: unsupported question type %r" % (question_id, question.type))

    usage = payload.get("usage")
    cost = usage.get("cost") if isinstance(usage, Mapping) else None
    return JevDecision(
        answers=answers,
        model=str(payload.get("model") or ""),
        cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


class JevClient:
    def __init__(
        self,
        transport: Any,
        model: str = "typesafe/jev-1.13",
        timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self.model = model
        self.timeout = timeout
        self._clock = clock

    def decide(self, state: Any, questions: Mapping[str, Question]) -> JevDecision:
        body = {
            "model": self.model,
            "state": state,
            "questions": {question_id: q.to_dict() for question_id, q in questions.items()},
        }
        started = self._clock()
        try:
            payload = self._transport.post(DECISIONS_PATH, body, timeout=self.timeout)
        except OpenRouterError as exc:
            raise JevError(exc.code, str(exc)) from None
        decision = parse_decision(payload, questions)
        return replace(decision, latency_ms=int(round((self._clock() - started) * 1000)))


class ScriptedJev:
    """Returns queued decisions (or raises queued JevErrors) in order.

    Records every call, so a test can assert Jev was never consulted, which is
    how the safety and handoff paths prove they still run first.
    """

    def __init__(self, decisions: Sequence[Union[JevDecision, JevError]] = ()) -> None:
        self._queue = list(decisions)
        self.calls: List[Dict[str, Any]] = []

    def decide(self, state: Any, questions: Mapping[str, Question]) -> JevDecision:
        self.calls.append({"state": state, "questions": dict(questions)})
        if not self._queue:
            raise AssertionError("ScriptedJev ran out of queued decisions")
        item = self._queue.pop(0)
        if isinstance(item, JevError):
            raise item
        return item


def choose(chosen: str, p: float) -> ChoiceAnswer:
    """Scripted choice answer."""
    return ChoiceAnswer(choice=chosen, probabilities={chosen: p}, confidence=p)


def yes(p: float) -> NoulAnswer:
    """Scripted noul answer."""
    return NoulAnswer(p=p)
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_jev -v`, then the full suite.
Expected: all pass. The live-shape test is skipped until the probe runs.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/jev.py tests/test_jev.py tests/data/jev_decisions_documented.json
git commit -m "Add the Jev Decisions API client with a strict answer parser"
```

---

### Task 4: Standard responses

**Files:**
- Create: `src/emotorad_ai/standard_responses.py`
- Create: `knowledge/_standard/thanks-goodbye.yaml`, `knowledge/_standard/acknowledged.yaml`, `knowledge/_standard/are-you-a-bot.yaml`
- Test: `tests/test_standard_responses.py`

**Interfaces:**
- Consumes: `guardrails.check_coverage_claim`, `guardrails.check_evidence`, `knowledge.KNOWLEDGE_DIR`
- Produces:
  - `LANGUAGES = ("english", "hindi", "hinglish", "marathi", "tamil")`
  - `StandardResponse(id, status, approved_by, criteria, replies, examples, counter_examples)` with `.approved` and `.reply_for(language)`
  - `StandardResponseError`
  - `load_standard_responses(directory=None) -> list[StandardResponse]`, which returns drafts too

- [ ] **Step 1: Write the failing tests**

`tests/test_standard_responses.py`:

```python
import tempfile
import textwrap
import unittest
from pathlib import Path

from emotorad_ai.standard_responses import StandardResponseError, load_standard_responses

VALID = """\
id: std-thanks
status: approved
approved_by: Kushendra
criteria: The customer is only saying thank you.
replies:
  english: You're welcome.
  hinglish: Aapka swagat hai.
examples: [thanks]
counter_examples: [thanks but it is still broken]
"""


def write(directory, name, text):
    Path(directory, name).write_text(textwrap.dedent(text), encoding="utf-8")


class LoadTests(unittest.TestCase):
    def test_loads_an_approved_response(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, "a.yaml", VALID)
            [response] = load_standard_responses(Path(directory))
        self.assertTrue(response.approved)
        self.assertEqual(response.reply_for("english"), "You're welcome.")
        self.assertIsNone(response.reply_for("hindi"))
        self.assertEqual(response.examples, ("thanks",))

    def test_the_seed_drafts_load_and_none_is_approved(self):
        responses = load_standard_responses()
        self.assertEqual(
            sorted(r.id for r in responses),
            ["std-acknowledged", "std-are-you-a-bot", "std-thanks-goodbye"],
        )
        self.assertFalse(any(r.approved for r in responses))

    def test_invalid_responses_are_refused_at_load(self):
        bad = {
            "coverage claim": VALID.replace("You're welcome.", "Good news, it's covered under warranty."),
            "fault conclusion": VALID.replace("You're welcome.", "Your battery is dead, so we will arrange a replacement."),
            "placeholder": VALID.replace("You're welcome.", "You're welcome, {name}."),
            "approved without approver": VALID.replace("approved_by: Kushendra", "approved_by: ''"),
            "unknown status": VALID.replace("status: approved", "status: live"),
            "unknown language": VALID.replace("  hinglish:", "  french:"),
            "empty criteria": VALID.replace("criteria: The customer is only saying thank you.", "criteria: ''"),
            "no replies": VALID.split("replies:")[0] + "replies: {}\n",
        }
        for name, text in bad.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                write(directory, "a.yaml", text)
                with self.assertRaises(StandardResponseError):
                    load_standard_responses(Path(directory))

    def test_duplicate_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, "a.yaml", VALID)
            write(directory, "b.yaml", VALID)
            with self.assertRaises(StandardResponseError):
                load_standard_responses(Path(directory))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_standard_responses -v`
Expected: ERROR, `No module named 'emotorad_ai.standard_responses'`.

- [ ] **Step 3: Implement**

`src/emotorad_ai/standard_responses.py`:

```python
"""Standard responses: reviewed replies Jev can pick without any LLM call.

Authored as files under knowledge/_standard/, like the knowledge base, so Git
is the audit trail and a PR is the approval. Only `status: approved` records
ever reach Jev; drafts load (so calibration can measure them) and are
otherwise inert.

A standard reply is identical for every customer, so it must never be
personal and never assert anything the turn's tools did not establish. Those
two rules are checked here, at load time, with the same post-checks the
runtime applies to model replies: a canned "it's covered" is the Air Canada
failure with no model to blame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .guardrails import check_coverage_claim, check_evidence
from .knowledge import KNOWLEDGE_DIR

STANDARD_DIR = KNOWLEDGE_DIR / "_standard"
LANGUAGES = ("english", "hindi", "hinglish", "marathi", "tamil")
STATUSES = ("draft", "approved")
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


class StandardResponseError(Exception):
    """A standard response is malformed or unsafe. Raised at load, never at reply time."""


@dataclass(frozen=True)
class StandardResponse:
    id: str
    status: str
    approved_by: str
    criteria: str
    replies: Mapping[str, str]
    examples: Sequence[str] = field(default_factory=tuple)
    counter_examples: Sequence[str] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def reply_for(self, language: Optional[str]) -> Optional[str]:
        return self.replies.get(language or "")


def _validate(raw: Mapping[str, Any], where: str) -> None:
    for name in ("id", "status", "criteria", "replies"):
        if not raw.get(name):
            raise StandardResponseError("%s is missing %s" % (where, name))
    if raw["status"] not in STATUSES:
        raise StandardResponseError("%s: status must be one of %s" % (where, ", ".join(STATUSES)))
    if raw["status"] == "approved" and not str(raw.get("approved_by") or "").strip():
        raise StandardResponseError("%s: an approved response needs approved_by" % where)
    replies = raw["replies"]
    if not isinstance(replies, Mapping):
        raise StandardResponseError("%s: replies must map a language to text" % where)
    for language, text in replies.items():
        if language not in LANGUAGES:
            raise StandardResponseError("%s: unknown language %r" % (where, language))
        if not isinstance(text, str) or not text.strip():
            raise StandardResponseError("%s: the %s reply is empty" % (where, language))
        if _PLACEHOLDER.search(text):
            raise StandardResponseError("%s: the %s reply has a placeholder; standard replies are never personal" % (where, language))
        if check_coverage_claim(text, []).blocked:
            raise StandardResponseError("%s: the %s reply makes a coverage claim" % (where, language))
        if check_evidence(text, False).blocked:
            raise StandardResponseError("%s: the %s reply concludes a fault" % (where, language))


def load_standard_responses(directory: Optional[Path] = None) -> List[StandardResponse]:
    import yaml

    root = Path(directory) if directory else STANDARD_DIR
    if not root.exists():
        return []
    responses: List[StandardResponse] = []
    seen: Dict[str, Path] = {}
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _validate(raw, str(path))
        if raw["id"] in seen:
            raise StandardResponseError("duplicate standard response id %r in %s and %s" % (raw["id"], seen[raw["id"]], path))
        seen[raw["id"]] = path
        responses.append(
            StandardResponse(
                id=raw["id"],
                status=raw["status"],
                approved_by=str(raw.get("approved_by") or ""),
                criteria=str(raw["criteria"]).strip(),
                replies={language: text.strip() for language, text in raw["replies"].items()},
                examples=tuple(raw.get("examples") or ()),
                counter_examples=tuple(raw.get("counter_examples") or ()),
            )
        )
    return responses
```

`knowledge/_standard/thanks-goodbye.yaml`:

```yaml
# DRAFT. Needs an SME's approval (status: approved, approved_by: <name>) in a PR
# before Jev may send it. The wording states no business fact on purpose.
id: std-thanks-goodbye
status: draft
approved_by: ""
criteria: >
  The customer is only thanking us or saying goodbye, with no new question, problem or
  information. Not this if the message also asks something, or says a problem continues.
replies:
  english: "You're welcome. If anything else comes up with your bike, just message here and we'll help."
  hinglish: "Aapka swagat hai. Bike ke saath kuch aur ho toh yahin message kar dijiye, hum madad karenge."
  hindi: "आपका स्वागत है। बाइक से जुड़ी कोई और बात हो तो यहीं मैसेज कर दीजिए, हम मदद करेंगे।"
examples: ["thanks", "thank you so much", "ok bye", "dhanyavaad", "shukriya", "धन्यवाद"]
counter_examples: ["thanks but it still won't charge", "ok thanks, and is it under warranty?"]
```

`knowledge/_standard/acknowledged.yaml`:

```yaml
# DRAFT. Needs an SME's approval before Jev may send it.
id: std-acknowledged
status: draft
approved_by: ""
criteria: >
  The customer only says they will try what was suggested, or asks for a moment, and reports
  no result yet. Not this if they report what happened, ask a question, or describe a new problem.
replies:
  english: "Take your time. Tell me what happens once you've tried it, and we'll go from there."
  hinglish: "Koi jaldi nahi. Try karne ke baad batayiye kya hua, phir aage dekhte hain."
  hindi: "कोई जल्दी नहीं। आज़माने के बाद बताइए क्या हुआ, फिर आगे देखते हैं।"
examples: ["ok I will try", "let me check", "give me 5 minutes", "ruko check karta hoon", "try karke batata hoon"]
counter_examples: ["I tried it and nothing happened", "ok, the light is red now"]
```

`knowledge/_standard/are-you-a-bot.yaml`:

```yaml
# DRAFT. Needs an SME's approval before Jev may send it.
id: std-are-you-a-bot
status: draft
approved_by: ""
criteria: >
  The customer only asks whether they are talking to a bot, an AI or a real person.
  Not this if they ask to be transferred to a person, which the handoff check handles first.
replies:
  english: "Yes, I'm EMotorad's virtual assistant, an AI rather than a person. If you'd prefer someone from our support team, just say so and you'll be passed over."
  hinglish: "Haan, main EMotorad ka virtual assistant hoon, ek AI, insaan nahi. Agar aap support team se baat karna chahte hain, bas bataiye, aapko team se jod diya jayega."
  hindi: "हाँ, मैं EMotorad का वर्चुअल असिस्टेंट हूँ, एक AI, कोई इंसान नहीं। अगर आप सपोर्ट टीम से बात करना चाहते हैं, तो बस बताइए, आपको टीम से जोड़ दिया जाएगा।"
examples: ["are you a bot?", "am I talking to a real person", "kya aap robot ho", "क्या आप इंसान हैं"]
counter_examples: ["I want to talk to a real person"]
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_standard_responses -v`, then the full suite.
Expected: all pass. `load_records` already skips `_`-prefixed directories, so the retrieval evals are unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/standard_responses.py knowledge/_standard tests/test_standard_responses.py
git commit -m "Add reviewed standard responses with load-time safety checks"
```

---

### Task 5: Routing configuration: thresholds, catalogue, questions and state

**Files:**
- Create: `src/emotorad_ai/decisions.py` (first half)
- Create: `knowledge/_routing/thresholds.yaml`
- Modify: `src/emotorad_ai/knowledge.py`, adding the public `KnowledgeBase.applicable`
- Test: `tests/test_decisions_config.py`

**Interfaces:**
- Consumes:
  - `jev.choice`, `jev.noul`, `jev.Question`
  - `standard_responses.StandardResponse`, `standard_responses.LANGUAGES`
  - `knowledge.KnowledgeBase`, `knowledge.KnowledgeRecord`, `knowledge.KNOWLEDGE_DIR`
  - `errorcodes.ErrorCodeTable`, `errorcodes.ANY_CODE`
  - `observability.redact_pii`
- Produces:
  - question id constants `Q_STANDARD`, `Q_CATEGORY`, `Q_SUB_CATEGORY`, `Q_ERROR_CODE`, `Q_LANGUAGE`, `Q_WARRANTY`, plus `NONE`, `NONE_OF_THESE`, `EMPTY_MESSAGE`
  - `Thresholds(standard_response, language, category, sub_category, error_code, tools)`
  - `RoutingConfigError`
  - `load_thresholds(path=None) -> Thresholds`
  - `RoutingCatalogue(records, error_codes, standard, applicable)`
  - `build_catalogue(knowledge_base, standard=(), error_table=None, include_drafts=False) -> RoutingCatalogue`
  - `build_questions(catalogue) -> dict[str, Question]`
  - `build_state(message_text, history, channel, bike=None, current_sub_category=None, redact=()) -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/test_decisions_config.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from emotorad_ai.decisions import (
    NONE,
    NONE_OF_THESE,
    Q_CATEGORY,
    Q_ERROR_CODE,
    Q_LANGUAGE,
    Q_STANDARD,
    Q_SUB_CATEGORY,
    Q_WARRANTY,
    RoutingConfigError,
    Thresholds,
    build_catalogue,
    build_questions,
    build_state,
    load_thresholds,
)
from emotorad_ai.errorcodes import load_table
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import StandardResponse

APPROVED = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome."})
DRAFT = StandardResponse("std-draft", "draft", "", "Draft.", {"english": "Hi."})


class ThresholdTests(unittest.TestCase):
    def test_the_committed_file_loads_with_the_spec_starting_values(self):
        thresholds = load_thresholds()
        self.assertEqual(
            (thresholds.standard_response, thresholds.language, thresholds.category,
             thresholds.sub_category, thresholds.error_code),
            (0.90, 0.80, 0.85, 0.75, 0.85),
        )
        self.assertEqual(dict(thresholds.tools), {"lookup_warranty_record": 0.60})

    def test_bad_files_are_refused(self):
        for text in ("category: 1.5\n", "category: 0\n", "surprise: 0.5\n", "tools: {find_anything: 0.5}\n", "category: high\n"):
            with self.subTest(text), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "t.yaml")
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(RoutingConfigError):
                    load_thresholds(path)


class CatalogueAndQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.table = load_table()

    def test_only_approved_standard_responses_are_offered(self):
        catalogue = build_catalogue(self.kb, standard=[APPROVED, DRAFT])
        self.assertEqual(set(catalogue.standard), {"std-thanks"})
        self.assertEqual(set(build_catalogue(self.kb, [APPROVED, DRAFT], include_drafts=True).standard), {"std-thanks", "std-draft"})

    def test_questions_cover_every_live_record_code_and_language(self):
        questions = build_questions(build_catalogue(self.kb, [APPROVED], self.table))
        self.assertEqual(set(questions), {Q_STANDARD, Q_CATEGORY, Q_SUB_CATEGORY, Q_ERROR_CODE, Q_LANGUAGE, Q_WARRANTY})
        self.assertEqual(set(questions[Q_SUB_CATEGORY].criteria), {r.id for r in self.kb.records} | {NONE})
        self.assertEqual(set(questions[Q_CATEGORY].criteria), {"battery", "motor", NONE_OF_THESE})
        self.assertIn("E07", questions[Q_ERROR_CODE].criteria)
        self.assertNotIn("*", questions[Q_ERROR_CODE].criteria)
        self.assertIn("E-07", questions[Q_ERROR_CODE].criteria["E07"])
        self.assertEqual(set(questions[Q_LANGUAGE].criteria), {"english", "hindi", "hinglish", "marathi", "tamil", "other"})
        self.assertEqual(questions[Q_WARRANTY].type, "noul")
        self.assertEqual(set(questions[Q_STANDARD].criteria), {"std-thanks", NONE})

    def test_questions_without_standard_responses_or_codes_leave_those_out(self):
        questions = build_questions(build_catalogue(self.kb))
        self.assertNotIn(Q_STANDARD, questions)
        self.assertNotIn(Q_ERROR_CODE, questions)

    def test_every_question_serialises(self):
        for question in build_questions(build_catalogue(self.kb, [APPROVED], self.table)).values():
            json.dumps(question.to_dict())


class StateTests(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": "hi, my number is 9876543210"},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi Ananya Rao, your EMX Plus frame EMXP2025004417."}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "{}"}]},
        {"role": "user", "content": "it still won't charge"},
    ]

    def test_state_carries_the_message_recent_turns_and_bike_model_only(self):
        state = build_state(
            "charger light is off, email me at a@b.com",
            self.HISTORY,
            "whatsapp",
            bike={"product_name": "EMX Plus", "frame_number": "EMXP2025004417"},
            current_sub_category="battery-wont-charge",
            redact=("Ananya Rao", "Ananya", "EMXP2025004417"),
        )
        self.assertEqual(set(state), {"message", "recent_turns", "channel", "bike_model", "current_sub_category"})
        self.assertEqual(state["bike_model"], "EMX Plus")
        self.assertEqual(state["current_sub_category"], "battery-wont-charge")
        self.assertEqual(len(state["recent_turns"]), 3)
        dumped = json.dumps(state)
        for secret in ("9876543210", "a@b.com", "Ananya", "EMXP2025004417"):
            self.assertNotIn(secret, dumped)

    def test_no_bike_means_no_bike_model(self):
        self.assertIsNone(build_state("hi", [], "website_chat")["bike_model"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_decisions_config -v`
Expected: ERROR, `No module named 'emotorad_ai.decisions'`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/knowledge.py`, add this method to `KnowledgeBase` directly after `_applicable`:

```python
    def applicable(self, record: KnowledgeRecord, bike: Mapping[str, Any]) -> bool:
        """Public form of the hard filter, for routing that picks a record without searching."""
        return self._applicable(record, bike)
```

`knowledge/_routing/thresholds.yaml`:

```yaml
# Per-question thresholds for Jev routing (src/emotorad_ai/decisions.py).
#
# Strict starting values. They are replaced by scripts/calibrate_jev.py from the
# labelled set, reviewed in a PR, never tuned by feel. Jev's own docs warn that
# thresholds do not transfer between question formats, so each question has its own.
standard_response: 0.90
language: 0.80
category: 0.85
sub_category: 0.75
error_code: 0.85
tools:
  lookup_warranty_record: 0.60
calibrated_at: null
calibration_set_size: 0
```

`src/emotorad_ai/decisions.py` (first half; Task 6 appends `route`):

```python
"""Jev routing: the questions we ask, the state we send, and the path we pick.

Everything here is deterministic. Jev supplies probabilities; this module
turns them into one of three paths, and nothing else in the system makes that
call. Keeping it pure (no I/O, no model) is what lets every rule and every
threshold boundary be table-tested.

The questions are built from files (knowledge records, error codes, approved
standard responses), never hand-written, so adding a knowledge record makes
it routable without touching this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .errorcodes import ANY_CODE, ErrorCodeTable
from .jev import Question, choice, noul
from .knowledge import KNOWLEDGE_DIR, KnowledgeBase, KnowledgeRecord
from .observability import redact_pii
from .standard_responses import LANGUAGES, StandardResponse

THRESHOLDS_PATH = KNOWLEDGE_DIR / "_routing" / "thresholds.yaml"

Q_STANDARD = "standard_response"
Q_CATEGORY = "category"
Q_SUB_CATEGORY = "sub_category"
Q_ERROR_CODE = "error_code"
Q_LANGUAGE = "language"
Q_WARRANTY = "needs_warranty_lookup"

NONE = "none"
NONE_OF_THESE = "none_of_these"
# Not a Jev error: the reason a turn skipped Jev because there was no text to
# score (a photo on its own).
EMPTY_MESSAGE = "empty_message"

RECENT_TURNS = 3

CATEGORY_INSTRUCTIONS = "Which area of the bike is the customer's message about?"
CATEGORY_CRITERIA = {
    "battery": (
        "The battery or charging: will not charge, charges slowly, range or backup has dropped, "
        "the bike will not power on, the battery indicator or on/off switch, damage to the battery "
        "or its charging port, storing the battery, or a battery replacement."
    ),
    "motor": (
        "The drive while riding: motor noise, no pedal assist, the throttle does not respond, "
        "power cutting out while riding, or jerking."
    ),
    NONE_OF_THESE: (
        "Anything else: buying a new bike, prices, orders and delivery, refunds, accessories, "
        "general questions, or a message that describes no problem at all."
    ),
}
SUB_CATEGORY_INSTRUCTIONS = (
    "Which documented issue matches what the customer describes? Pick none unless one matches closely."
)
STANDARD_INSTRUCTIONS = (
    "Is the whole message fully answered by one of these standard replies? "
    "Pick none unless one clearly fits the entire message."
)
STANDARD_NONE = (
    "None fits: the message needs more than a standard reply, or asks about this customer's "
    "bike, order, warranty or a fault."
)
ERROR_CODE_INSTRUCTIONS = "Does the customer mention an error code shown on the bike's display?"
LANGUAGE_INSTRUCTIONS = "Which language is the customer's message written in?"
LANGUAGE_CRITERIA = {
    "english": "English.",
    "hindi": "Hindi written in Devanagari script.",
    "hinglish": "Hindi written in Latin letters, often mixed with English words.",
    "marathi": "Marathi, in any script.",
    "tamil": "Tamil, in any script.",
    "other": "Any other language, or too short to tell.",
}
WARRANTY_INSTRUCTIONS = (
    "The customer asks whether something is covered by warranty, or asks about warranty, "
    "a free replacement, or what a repair will cost."
)

_SCALAR_KEYS = ("standard_response", "language", "category", "sub_category", "error_code")
_TOOL_KEYS = ("lookup_warranty_record",)
_META_KEYS = ("calibrated_at", "calibration_set_size")


class RoutingConfigError(Exception):
    """thresholds.yaml is malformed. Raised at startup, never mid-conversation."""


@dataclass(frozen=True)
class Thresholds:
    standard_response: float = 0.90
    language: float = 0.80
    category: float = 0.85
    sub_category: float = 0.75
    error_code: float = 0.85
    tools: Mapping[str, float] = field(default_factory=lambda: {"lookup_warranty_record": 0.60})


def _threshold(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
        raise RoutingConfigError("%s must be a number in (0, 1], got %r" % (where, value))
    return float(value)


def load_thresholds(path: Optional[Path] = None) -> Thresholds:
    import yaml

    target = Path(path) if path else THRESHOLDS_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise RoutingConfigError("%s must be a mapping" % target)
    unknown = set(raw) - set(_SCALAR_KEYS) - {"tools"} - set(_META_KEYS)
    if unknown:
        raise RoutingConfigError("%s has unknown keys: %s" % (target, ", ".join(sorted(unknown))))
    defaults = Thresholds()
    values = {key: _threshold(raw[key], key) if key in raw else getattr(defaults, key) for key in _SCALAR_KEYS}
    tools_raw = raw.get("tools") or {}
    if not isinstance(tools_raw, Mapping):
        raise RoutingConfigError("tools must map a tool name to a threshold")
    unknown_tools = set(tools_raw) - set(_TOOL_KEYS)
    if unknown_tools:
        raise RoutingConfigError("unknown prefetch tools: %s" % ", ".join(sorted(unknown_tools)))
    tools = dict(defaults.tools)
    tools.update({name: _threshold(value, "tools." + name) for name, value in tools_raw.items()})
    return Thresholds(tools=tools, **values)


@dataclass(frozen=True)
class RoutingCatalogue:
    records: Mapping[str, KnowledgeRecord]
    error_codes: Sequence[str]
    standard: Mapping[str, StandardResponse]
    applicable: Callable[[KnowledgeRecord, Mapping[str, Any]], bool]


def build_catalogue(
    knowledge_base: KnowledgeBase,
    standard: Sequence[StandardResponse] = (),
    error_table: Optional[ErrorCodeTable] = None,
    include_drafts: bool = False,
) -> RoutingCatalogue:
    codes = sorted({entry.code for entry in error_table.entries if entry.code != ANY_CODE}) if error_table else []
    return RoutingCatalogue(
        records={record.id: record for record in knowledge_base.records},
        error_codes=tuple(codes),
        standard={s.id: s for s in standard if s.approved or include_drafts},
        applicable=knowledge_base.applicable,
    )


def _describe_record(record: KnowledgeRecord) -> str:
    symptoms = "; ".join(s.replace("_", " ") for s in list(record.symptoms)[:10])
    return "%s. Customers say things like: %s" % (record.title, symptoms)


def _spellings(code: str) -> str:
    number = int(code[1:]) if code[1:].isdigit() else None
    if number is None:
        return code
    return "E-%02d, E%d or E %02d" % (number, number, number)


def build_questions(catalogue: RoutingCatalogue) -> Dict[str, Question]:
    questions: Dict[str, Question] = {}
    if catalogue.standard:
        criteria = {s.id: s.criteria for s in catalogue.standard.values()}
        criteria[NONE] = STANDARD_NONE
        questions[Q_STANDARD] = choice(STANDARD_INSTRUCTIONS, criteria)
    questions[Q_CATEGORY] = choice(CATEGORY_INSTRUCTIONS, CATEGORY_CRITERIA)
    sub_categories = {record.id: _describe_record(record) for record in catalogue.records.values()}
    sub_categories[NONE] = "None of these issues matches what the customer describes."
    questions[Q_SUB_CATEGORY] = choice(SUB_CATEGORY_INSTRUCTIONS, sub_categories)
    if catalogue.error_codes:
        codes = {
            code: "The customer mentions display error %s (it may be written %s)." % (code, _spellings(code))
            for code in catalogue.error_codes
        }
        codes[NONE] = "The customer does not mention a display error code."
        questions[Q_ERROR_CODE] = choice(ERROR_CODE_INSTRUCTIONS, codes)
    assert set(LANGUAGE_CRITERIA) - {"other"} == set(LANGUAGES)
    questions[Q_LANGUAGE] = choice(LANGUAGE_INSTRUCTIONS, LANGUAGE_CRITERIA)
    questions[Q_WARRANTY] = noul(
        WARRANTY_INSTRUCTIONS,
        true="The customer asks about warranty, cover or the cost of a repair.",
        false="The customer does not ask about warranty, cover or cost.",
    )
    return questions


def _text_of(entry: Mapping[str, Any]) -> str:
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def _recent_texts(history: Sequence[Mapping[str, Any]], limit: int) -> List[str]:
    texts: List[str] = []
    for entry in reversed(history):
        text = _text_of(entry).strip()
        if text:
            texts.append("%s: %s" % (entry.get("role", "?"), text))
        if len(texts) >= limit:
            break
    return list(reversed(texts))


def build_state(
    message_text: str,
    history: Sequence[Mapping[str, Any]],
    channel: str,
    bike: Optional[Mapping[str, Any]] = None,
    current_sub_category: Optional[str] = None,
    redact: Sequence[str] = (),
) -> Dict[str, Any]:
    """The minimum Jev needs. No name, phone, frame number or warranty status.

    Jev's own docs: accuracy falls as the state grows with content unrelated to
    the decision. Sending less is also sending less customer data outside AWS.
    """
    terms = sorted({term for term in redact if term and len(term) > 2}, key=len, reverse=True)

    def clean(text: str) -> str:
        text = redact_pii(text or "")
        for term in terms:
            text = re.sub(re.escape(term), "[redacted]", text, flags=re.IGNORECASE)
        return text

    return {
        "message": clean(message_text),
        "recent_turns": [clean(text) for text in _recent_texts(history, RECENT_TURNS)],
        "channel": channel,
        "bike_model": (bike or {}).get("product_name") or None,
        "current_sub_category": current_sub_category,
    }
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_decisions_config -v`, then the full suite.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/decisions.py src/emotorad_ai/knowledge.py knowledge/_routing tests/test_decisions_config.py
git commit -m "Build Jev questions and state from the knowledge files, with thresholds"
```

---

### Task 6: `route()`, where the scores pick the path

**Files:**
- Modify: `src/emotorad_ai/decisions.py` (append)
- Test: `tests/test_route.py`

**Interfaces:**
- Consumes:
  - `jev.JevDecision`, `jev.ChoiceAnswer`, `jev.NoulAnswer`
  - everything from Task 5
  - `tools.mocks.LOOKUP_WARRANTY_RECORD`, `tools.mocks.LOOKUP_ERROR_CODE`
- Produces:
  - `PrefetchCall(tool, arguments)`
  - `Route(path, reasons, scores, standard_response_id, category, sub_category, error_code, language, prefetch)`
  - `route(decision, error, thresholds, catalogue, current_sub_category=None, bike=None) -> Route`

- [ ] **Step 1: Write the failing tests**

`tests/test_route.py`:

```python
import unittest

from emotorad_ai.decisions import (
    EMPTY_MESSAGE,
    NONE,
    NONE_OF_THESE,
    Q_CATEGORY,
    Q_ERROR_CODE,
    Q_LANGUAGE,
    Q_STANDARD,
    Q_SUB_CATEGORY,
    Q_WARRANTY,
    PrefetchCall,
    Thresholds,
    build_catalogue,
    route,
)
from emotorad_ai.jev import JevDecision, choose, yes
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import StandardResponse

T = Thresholds()
THANKS = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome.", "hinglish": "Swagat hai."})
EMX = {"product_name": "EMX Plus"}
DOODLE = {"product_name": "Doodle V3"}


def decide(**answers):
    return JevDecision(answers=answers)


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogue = build_catalogue(KnowledgeBase(), standard=[THANKS])

    def go(self, decision, error=None, current=None, bike=EMX):
        return route(decision, error, T, self.catalogue, current_sub_category=current, bike=bike)

    # rule 1 ------------------------------------------------------------------
    def test_no_decision_is_the_full_agent_with_the_reason(self):
        self.assertEqual(self.go(None).path, "full")
        self.assertEqual(self.go(None).reasons, ("jev_disabled",))
        self.assertEqual(self.go(None, error="unavailable").reasons, ("jev_error:unavailable",))

    def test_an_empty_message_mid_flow_stays_on_the_current_record(self):
        result = self.go(None, error=EMPTY_MESSAGE, current="battery-wont-charge")
        self.assertEqual((result.path, result.sub_category, result.category), ("narrow", "battery-wont-charge", "battery"))
        self.assertEqual(self.go(None, error=EMPTY_MESSAGE).path, "full")

    # rule 2 ------------------------------------------------------------------
    def test_a_confident_standard_response_in_a_language_we_have(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.95), Q_LANGUAGE: choose("english", 0.9)}))
        self.assertEqual((result.path, result.standard_response_id, result.language), ("standard", "std-thanks", "english"))

    def test_a_standard_response_without_a_reply_in_that_language_goes_to_the_full_agent(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.99), Q_LANGUAGE: choose("hindi", 0.99)}))
        self.assertEqual(result.path, "full")
        self.assertIn("standard_no_reply_for:hindi", result.reasons)

    def test_a_standard_response_needs_a_confident_language(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.99), Q_LANGUAGE: choose("english", 0.5)}))
        self.assertEqual(result.path, "full")
        self.assertIn("standard_language_unsure", result.reasons)

    def test_standard_threshold_boundary(self):
        at = self.go(decide(**{Q_STANDARD: choose("std-thanks", T.standard_response), Q_LANGUAGE: choose("english", 0.9)}))
        below = self.go(decide(**{Q_STANDARD: choose("std-thanks", T.standard_response - 0.0001), Q_LANGUAGE: choose("english", 0.9)}))
        self.assertEqual((at.path, below.path), ("standard", "full"))

    def test_none_as_the_standard_choice_is_not_a_standard_reply(self):
        result = self.go(decide(**{Q_STANDARD: choose(NONE, 0.99), Q_LANGUAGE: choose("english", 0.99)}))
        self.assertEqual(result.path, "full")

    def test_a_standard_id_we_do_not_know_is_ignored(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-gone", 0.99), Q_LANGUAGE: choose("english", 0.99)}))
        self.assertIn("standard_unknown:std-gone", result.reasons)

    # rule 3 ------------------------------------------------------------------
    def test_confident_category_and_sub_category_pick_the_narrow_agent(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.9), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.8)}))
        self.assertEqual((result.path, result.category, result.sub_category), ("narrow", "battery", "battery-wont-charge"))
        self.assertEqual(result.prefetch, ())

    def test_category_and_sub_category_boundaries(self):
        def at(category_p, sub_p):
            return self.go(decide(**{Q_CATEGORY: choose("battery", category_p), Q_SUB_CATEGORY: choose("battery-wont-charge", sub_p)})).path
        self.assertEqual(at(T.category, T.sub_category), "narrow")
        self.assertEqual(at(T.category - 0.0001, 0.99), "full")
        self.assertEqual(at(0.99, T.sub_category - 0.0001), "full")

    def test_a_sub_category_from_the_other_topic_is_a_mismatch(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("motor-noise", 0.95)}))
        self.assertEqual(result.path, "full")
        self.assertIn("topic_mismatch:battery/motor-noise", result.reasons)

    def test_a_record_that_does_not_apply_to_this_bike_is_refused(self):
        # battery-wont-power-on excludes the Doodle; its Doodle twin applies only to it.
        refused = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-power-on", 0.95)}), bike=DOODLE)
        self.assertEqual(refused.path, "full")
        self.assertIn("not_applicable:battery-wont-power-on", refused.reasons)
        allowed = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-doodle-wont-power-on", 0.95)}), bike=DOODLE)
        self.assertEqual(allowed.path, "narrow")

    def test_none_of_these_is_never_narrow(self):
        result = self.go(decide(**{Q_CATEGORY: choose(NONE_OF_THESE, 0.99), Q_SUB_CATEGORY: choose(NONE, 0.99)}))
        self.assertEqual(result.path, "full")
        self.assertIn("category_none_of_these", result.reasons)

    # rule 4 ------------------------------------------------------------------
    def test_an_unsure_follow_up_continues_the_current_record(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose(NONE, 0.6)}), current="battery-wont-charge")
        self.assertEqual((result.path, result.sub_category), ("narrow", "battery-wont-charge"))
        self.assertIn("narrow_continue:battery-wont-charge", result.reasons)

    def test_a_confident_switch_of_topic_does_not_continue(self):
        result = self.go(decide(**{Q_CATEGORY: choose("motor", 0.95), Q_SUB_CATEGORY: choose(NONE, 0.9)}), current="battery-wont-charge")
        self.assertEqual((result.path, result.category), ("full", "motor"))

    def test_a_confident_new_record_replaces_the_current_one(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-range-dropped", 0.9)}), current="battery-wont-charge")
        self.assertEqual(result.sub_category, "battery-range-dropped")

    # prefetch ----------------------------------------------------------------
    def test_prefetch_follows_the_warranty_noul_and_the_error_code(self):
        result = self.go(decide(**{
            Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9),
            Q_WARRANTY: yes(T.tools["lookup_warranty_record"]), Q_ERROR_CODE: choose("E07", 0.9),
        }))
        self.assertEqual(result.prefetch, (PrefetchCall("lookup_warranty_record", {}), PrefetchCall("lookup_error_code", {"code": "E07"})))
        quiet = self.go(decide(**{
            Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9),
            Q_WARRANTY: yes(T.tools["lookup_warranty_record"] - 0.0001), Q_ERROR_CODE: choose(NONE, 0.99),
        }))
        self.assertEqual(quiet.prefetch, ())

    def test_scores_travel_with_the_route_for_the_log(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.9), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.8)}))
        self.assertEqual(result.scores[Q_CATEGORY], {"choice": "battery", "p": 0.9})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_route -v`
Expected: ERROR, `ImportError: cannot import name 'PrefetchCall'`.

- [ ] **Step 3: Implement, appending to `src/emotorad_ai/decisions.py`**

Add to the imports at the top:

```python
from typing import Tuple

from .jev import ChoiceAnswer, JevDecision, NoulAnswer
from .tools.mocks import LOOKUP_ERROR_CODE, LOOKUP_WARRANTY_RECORD
```

Then append:

```python
@dataclass(frozen=True)
class PrefetchCall:
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Route:
    path: str  # "standard" | "narrow" | "full"
    reasons: Tuple[str, ...] = ()
    scores: Mapping[str, Any] = field(default_factory=dict)
    standard_response_id: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    error_code: Optional[str] = None
    language: Optional[str] = None
    prefetch: Tuple[PrefetchCall, ...] = ()


def _confident(decision: JevDecision, question_id: str, threshold: float) -> Optional[str]:
    answer = decision.answers.get(question_id)
    if isinstance(answer, ChoiceAnswer) and answer.p >= threshold:
        return answer.choice
    return None


def _noul(decision: JevDecision, question_id: str) -> float:
    answer = decision.answers.get(question_id)
    return answer.p if isinstance(answer, NoulAnswer) else 0.0


def route(
    decision: Optional[JevDecision],
    error: Optional[str],
    thresholds: Thresholds,
    catalogue: RoutingCatalogue,
    current_sub_category: Optional[str] = None,
    bike: Optional[Mapping[str, Any]] = None,
) -> Route:
    """Pick the path. Rules in order, first match wins (spec §3.3).

    The safe direction is always `full`: today's agent at today's cost. Every
    other path has to be earned by a score above its own threshold.
    """
    bike = bike or {}
    current = catalogue.records.get(current_sub_category) if current_sub_category else None

    # 1. No decision.
    if decision is None:
        if error == EMPTY_MESSAGE and current is not None:
            return Route(path="narrow", reasons=("empty_message_continue",), category=current.topic, sub_category=current.id)
        return Route(path="full", reasons=("jev_error:%s" % error if error else "jev_disabled",))

    scores = decision.scores()
    reasons: List[str] = []
    language = _confident(decision, Q_LANGUAGE, thresholds.language)

    # 2. Standard response.
    standard_id = _confident(decision, Q_STANDARD, thresholds.standard_response)
    if standard_id and standard_id != NONE:
        response = catalogue.standard.get(standard_id)
        if response is None:
            reasons.append("standard_unknown:%s" % standard_id)
        elif language is None:
            reasons.append("standard_language_unsure")
        elif response.reply_for(language) is None:
            reasons.append("standard_no_reply_for:%s" % language)
        else:
            return Route(
                path="standard", reasons=tuple(reasons + ["standard:%s" % standard_id]), scores=scores,
                standard_response_id=standard_id, language=language,
            )

    category = _confident(decision, Q_CATEGORY, thresholds.category)
    if category == NONE_OF_THESE:
        reasons.append("category_none_of_these")
        category = None
    sub_id = _confident(decision, Q_SUB_CATEGORY, thresholds.sub_category)
    record = catalogue.records.get(sub_id) if sub_id and sub_id != NONE else None
    error_code = _confident(decision, Q_ERROR_CODE, thresholds.error_code)
    if error_code == NONE:
        error_code = None

    # 3. A new record, earned by both scores and allowed for this bike.
    chosen: Optional[KnowledgeRecord] = None
    if category and record is not None:
        if record.topic != category:
            reasons.append("topic_mismatch:%s/%s" % (category, record.id))
        elif not catalogue.applicable(record, bike):
            reasons.append("not_applicable:%s" % record.id)
        else:
            chosen = record
            reasons.append("narrow_new:%s" % record.id)

    # 4. Mid-flow: "yes, it's red now" scores low on everything. Stay on the
    #    record unless Jev confidently names a different topic.
    if chosen is None and current is not None and (category is None or category == current.topic):
        chosen = current
        reasons.append("narrow_continue:%s" % current.id)

    # 5. Otherwise the full agent.
    if chosen is None:
        if category is None:
            reasons.append("category_unsure")
        elif record is None:
            reasons.append("sub_category_unsure")
        return Route(path="full", reasons=tuple(reasons), scores=scores, category=category, error_code=error_code, language=language)

    prefetch: List[PrefetchCall] = []
    if _noul(decision, Q_WARRANTY) >= thresholds.tools.get(LOOKUP_WARRANTY_RECORD, 1.01):
        prefetch.append(PrefetchCall(LOOKUP_WARRANTY_RECORD, {}))
    if error_code:
        prefetch.append(PrefetchCall(LOOKUP_ERROR_CODE, {"code": error_code}))
    return Route(
        path="narrow", reasons=tuple(reasons), scores=scores, category=chosen.topic, sub_category=chosen.id,
        error_code=error_code, language=language, prefetch=tuple(prefetch),
    )
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_route -v`, then the full suite.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/decisions.py tests/test_route.py
git commit -m "Pick the turn's path from Jev's scores with per-question thresholds"
```

---

### Task 7: The narrow agent, prefetched tool calls and `sub_category` state

**Files:**
- Create: `src/emotorad_ai/agents/narrow_support.py`
- Modify:
  - `src/emotorad_ai/agents/base.py` (`Agent.run` gains `prefetched`)
  - `src/emotorad_ai/conversation.py` (`sub_category`, cleared on bike change and handback)
- Test: `tests/test_narrow_support.py`

**Interfaces:**
- Consumes:
  - `battery_support._facts_block`, `_context_block`, `_entry_block` (the same import motor_support uses)
  - `knowledge.KnowledgeRecord`
  - tool name constants
- Produces:
  - `narrow_support.AGENT_NAME = "narrow_support"`
  - `narrow_support.TOOL_NAMES`
  - `build_narrow_definition(record, prefetched) -> AgentDefinition`
  - `Agent.run(message, resolved, history, context="", prefetched=())`
  - `ConversationState.sub_category`

- [ ] **Step 1: Write the failing tests**

`tests/test_narrow_support.py`:

```python
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import Agent
from emotorad_ai.agents.narrow_support import AGENT_NAME, TOOL_NAMES, build_narrow_definition
from emotorad_ai.config import Settings
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.tools.mocks import (
    BOOK_SERVICE_SLOT,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    LOOKUP_WARRANTY_RECORD,
    SEND_GUIDE_MEDIA,
    build_registry,
)
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 7, 28)


class NarrowDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.record = next(r for r in cls.kb.records if r.id == "battery-wont-charge")
        cls.registry = build_registry(today=TODAY)
        cls.resolver = IdentityResolver(cls.registry)
        message = WebsiteChatAdapter(cls.resolver).to_message({"session_token": "sess-ananya", "text": "won't charge"})
        cls.message = message
        cls.resolved = cls.resolver.hydrate(message)
        cls.warranty = {
            "tool": LOOKUP_WARRANTY_RECORD, "arguments": {}, "prefetched": True,
            "result": cls.registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c", phone="+919876543210")),
        }

    def prompt(self, prefetched=()):
        return build_narrow_definition(self.record, prefetched).build_system_prompt(self.message, self.resolved, "")

    def test_the_prompt_holds_exactly_one_record(self):
        prompt = self.prompt()
        self.assertIn(self.record.title, prompt)
        for step in self.record.steps:
            self.assertIn(step, prompt)
        for other in self.kb.records:
            if other.id != self.record.id:
                self.assertNotIn(other.title, prompt)

    def test_the_prompt_is_small(self):
        self.assertLess(len(self.prompt([self.warranty])) // 4, 1500)

    def test_prefetched_results_are_in_the_prompt(self):
        self.assertIn("lookup_warranty_record", self.prompt([self.warranty]))
        self.assertNotIn("Looked up for this turn", self.prompt())

    def test_the_customer_facts_block_is_the_shared_one(self):
        self.assertIn("EMX Plus", self.prompt())

    def test_the_tool_slice_has_no_lookups_and_the_writes_the_flow_needs(self):
        definition = build_narrow_definition(self.record, ())
        self.assertEqual(definition.name, AGENT_NAME)
        self.assertEqual(set(definition.tool_names), {SEND_GUIDE_MEDIA, CREATE_SUPPORT_TICKET, FIND_SERVICE_SLOTS, BOOK_SERVICE_SLOT})
        self.assertEqual(tuple(definition.tool_names), TOOL_NAMES)


class PrefetchedAgentRunTests(NarrowDefinitionTests):
    def test_prefetched_calls_lead_the_turns_tool_calls(self):
        llm = ScriptedClaude([say("Let's start with the socket.")])
        agent = Agent(build_narrow_definition(self.record, [self.warranty]), self.registry, llm, EventLog(path=None), Settings(log_path=""))
        turn = agent.run(self.message, self.resolved, [], "", prefetched=[self.warranty])
        self.assertEqual(turn.tool_calls[0]["tool"], LOOKUP_WARRANTY_RECORD)
        self.assertEqual(turn.text, "Let's start with the socket.")


class SubCategoryStateTests(unittest.TestCase):
    def test_changing_bike_or_handing_back_clears_the_sub_category(self):
        state = ConversationState(conversation_id="c")
        state.select_bike("A")
        state.sub_category = "battery-wont-charge"
        state.select_bike("B")
        self.assertIsNone(state.sub_category)
        state.sub_category = "battery-wont-charge"
        state.hand_back("resolved")
        self.assertIsNone(state.sub_category)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_narrow_support -v`
Expected: ERROR, `No module named 'emotorad_ai.agents.narrow_support'`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/conversation.py`, add this field after `evidence_seen`:

```python
    # The knowledge record the narrow agent is working through. Held across
    # turns so a follow-up like "yes, the light is red now", which scores low
    # on everything, stays on the same record (decisions.route rule 4).
    sub_category: Optional[str] = None
```

In `select_bike`, inside the `if self.selected_frame and self.selected_frame != frame_number:` block, add `self.sub_category = None` after `self.agent = None`. In `hand_back`, add `self.sub_category = None` after `self.agent = None`.

In `src/emotorad_ai/agents/base.py`, change the `run` signature and the `turn` construction:

```python
    def run(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        history: List[Dict[str, Any]],
        context: str = "",
        prefetched: Sequence[Dict[str, Any]] = (),
    ) -> AgentTurn:
```

```python
        # Calls the runtime made before the loop (decisions.route prefetch)
        # count as this turn's tool calls, so the coverage post-check sees the
        # warranty result the reply was written from.
        turn = AgentTurn(text="", agent=self.definition.name, tool_calls=list(prefetched))
```

`src/emotorad_ai/agents/narrow_support.py`:

```python
"""The narrow support agent: one known issue, one record, a short prompt.

Reached only when Jev has confidently named the issue (decisions.route). It
runs through the same Agent loop as every other sub-agent, with the same
iteration cap, stuck detection, idempotency and post-checks, but it is handed
the one knowledge record that applies instead of searching, and the lookups
the runtime already made instead of making them itself. That is where the
saving is: a prompt a fraction of the size, on a cheaper model, in fewer
round trips.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from ..knowledge import KnowledgeRecord
from ..tools.mocks import BOOK_SERVICE_SLOT, CREATE_SUPPORT_TICKET, FIND_SERVICE_SLOTS, SEND_GUIDE_MEDIA
from .base import AgentDefinition
from .battery_support import _context_block, _entry_block, _facts_block

AGENT_NAME = "narrow_support"

# No lookups: the record is given and the reads were prefetched. The writes a
# troubleshooting flow ends in stay, requested by the model and enforced by
# the registry exactly as in the full agents.
TOOL_NAMES = (SEND_GUIDE_MEDIA, CREATE_SUPPORT_TICKET, FIND_SERVICE_SLOTS, BOOK_SERVICE_SLOT)

_RULES = """\
You are EMotorad's support assistant. The customer's issue has already been identified, \
and the documented steps for it are below. Be warm, plain-spoken and brief.

Rules that always apply:
- Work through the documented steps, one or two at a time. Do not invent steps that are not listed.
- Ask at most one or two questions at a time, and only when the answer changes what you would suggest.
- Never say a repair or part is covered, free or chargeable unless the warranty result below says so for this bike.
- Do not conclude that a part is dead or faulty, and do not raise a ticket for a fault, until the customer \
has sent a photo or video of it.
- If the customer describes smoke, swelling, heat, sparks or a burning smell, tell them to stop using and \
stop charging the bike now.
- If the documented steps do not resolve it, say so plainly and offer to raise a support ticket.
- Reply in the language the customer writes in."""


def _record_block(record: KnowledgeRecord) -> str:
    lines = ["\n\nThe documented steps for this issue (%s):" % record.title]
    for number, step in enumerate(record.steps, start=1):
        lines.append("%d. %s" % (number, step))
    if record.escalate_when:
        lines.append("Escalate when: %s" % record.escalate_when)
    media = [item for item in record.media if item.get("id")]
    if media:
        lines.append("Guide media you can send with send_guide_media, by key:")
        for item in media:
            lines.append("- %s: %s" % (item["id"], item.get("caption", "")))
    return "\n".join(lines)


def _prefetched_block(prefetched: Sequence[Dict[str, Any]]) -> str:
    if not prefetched:
        return ""
    lines = ["\n\nLooked up for this turn (treat as fact, and do not look these up again):"]
    for call in prefetched:
        lines.append("- %s: %s" % (call["tool"], json.dumps(call["result"], default=str, ensure_ascii=False)))
    return "\n".join(lines)


def build_narrow_definition(record: KnowledgeRecord, prefetched: Sequence[Dict[str, Any]]) -> AgentDefinition:
    frozen: List[Dict[str, Any]] = list(prefetched)

    def build_system_prompt(message: InboundMessage, resolved: ResolvedIdentity, context: str = "") -> str:
        return (
            _RULES
            + _facts_block(resolved)
            + _context_block(context)
            + _entry_block(message)
            + _record_block(record)
            + _prefetched_block(frozen)
        )

    return AgentDefinition(name=AGENT_NAME, tool_names=TOOL_NAMES, build_system_prompt=build_system_prompt)
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_narrow_support -v`, then the full suite.
Expected: all pass. If `test_the_prompt_is_small` fails, shorten `_RULES`. Do not raise the limit, because 1,500 tokens is the spec's target.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/agents/narrow_support.py src/emotorad_ai/agents/base.py src/emotorad_ai/conversation.py tests/test_narrow_support.py
git commit -m "Add the narrow support agent and prefetched tool calls"
```

---

### Task 8: The turn as a LangGraph graph, with behaviour unchanged

**Files:**
- Modify: `requirements.txt`
- Create: `src/emotorad_ai/graph.py`
- Modify: `src/emotorad_ai/runtime.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: `decisions.Route`
- Produces:
  - `graph.TurnState` (a TypedDict with `message`, `conversation`, `resolved`, `route`, `reply`)
  - `graph.TurnNodes` (a dataclass of eight callables)
  - `graph.NODE_NAMES`
  - `graph.build_turn_graph(nodes)` returning a compiled graph
  - `Runtime.graph`
  - the `Runtime._node_*` methods
  - `Runtime._run(agent, message, resolved, state, prefetched=(), route_path=None) -> Reply`

- [ ] **Step 1: Install and pin LangGraph**

Add to `requirements.txt`, directly after the `anthropic` block:

```
# Turn orchestration (src/emotorad_ai/graph.py). The graph encodes the order the
# design requires; runtime.py holds what each step does.
langgraph>=1.2,<2
```

Run: `pip install "langgraph>=1.2,<2"`
Expected: `Successfully installed langgraph-1.2.x ...`.

- [ ] **Step 2: Write the failing test**

`tests/test_graph.py`:

```python
import unittest

from emotorad_ai.graph import NODE_NAMES
from tests.test_agent_and_runtime import make_runtime, send
from emotorad_ai.llm import say


class GraphShapeTests(unittest.TestCase):
    def test_the_runtime_turn_is_a_graph_with_every_step_as_a_node(self):
        runtime, _, _ = make_runtime([])
        nodes = set(runtime.graph.get_graph().nodes)
        self.assertTrue(set(NODE_NAMES) <= nodes, nodes)

    def test_a_turn_through_the_graph_returns_the_reply(self):
        runtime, adapter, llm = make_runtime([say("Check the socket first.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertIn("Check the socket first.", reply.text)
        self.assertEqual(reply.handled_by, "battery_support")


if __name__ == "__main__":
    unittest.main()
```

Run: `python -m unittest tests.test_graph -v`
Expected: ERROR, `No module named 'emotorad_ai.graph'`.

- [ ] **Step 3: Implement `src/emotorad_ai/graph.py`**

```python
"""The turn, as a LangGraph graph.

The graph is the order, and the order is the design (runtime.py's docstring):
identity and context, then the safety and handoff gates, then persona routing
and triage, then Jev's path, then an agent. Each node's work lives in
runtime.py; this file only says what may follow what, so the order is visible
in one place and cannot be rearranged by an edit to a step.

Conversation memory stays in ConversationStore. The graph state is one turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .contract import InboundMessage, Reply
from .conversation import ConversationState
from .decisions import Route
from .identity import ResolvedIdentity


class TurnState(TypedDict, total=False):
    message: InboundMessage
    conversation: ConversationState
    resolved: ResolvedIdentity
    route: Route
    reply: Reply


Node = Callable[[TurnState], Dict[str, Any]]


@dataclass(frozen=True)
class TurnNodes:
    prepare: Node
    safety_gate: Node
    handoff_gate: Node
    persona_route: Node
    jev_classify: Node
    standard_reply: Node
    narrow_agent: Node
    full_agent: Node


NODE_NAMES = (
    "prepare", "safety_gate", "handoff_gate", "persona_route",
    "jev_classify", "standard_reply", "narrow_agent", "full_agent",
)

_PATH_NODES = {"standard": "standard_reply", "narrow": "narrow_agent", "full": "full_agent"}


def _replied_or(next_node: str) -> Callable[[TurnState], str]:
    def decide(state: TurnState) -> str:
        return END if state.get("reply") is not None else next_node

    return decide


def _by_path(state: TurnState) -> str:
    route = state.get("route")
    return _PATH_NODES.get(route.path if route else "full", "full_agent")


def build_turn_graph(nodes: TurnNodes):
    graph = StateGraph(TurnState)
    for name in NODE_NAMES:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "safety_gate")
    graph.add_conditional_edges("safety_gate", _replied_or("handoff_gate"), ["handoff_gate", END])
    graph.add_conditional_edges("handoff_gate", _replied_or("persona_route"), ["persona_route", END])
    graph.add_conditional_edges("persona_route", _replied_or("jev_classify"), ["jev_classify", END])
    graph.add_conditional_edges("jev_classify", _by_path, list(_PATH_NODES.values()))
    # A standard reply that fails its backstop, or a narrow agent whose model
    # failed, falls through to the full agent rather than ending the turn.
    graph.add_conditional_edges("standard_reply", _replied_or("full_agent"), ["full_agent", END])
    graph.add_conditional_edges("narrow_agent", _replied_or("full_agent"), ["full_agent", END])
    graph.add_edge("full_agent", END)
    return graph.compile()
```

- [ ] **Step 4: Rewire `Runtime` in `src/emotorad_ai/runtime.py`**

Update the module docstring's flow line to:

```
    channel adapter -> message contract -> identity resolution -> enrichment
    -> guardrails -> triage -> Jev path -> sub-agent -> tools -> post-checks
```

Add these imports:

```python
from .decisions import Route
from .graph import TurnNodes, build_turn_graph
```

At the end of `__init__`, add:

```python
        self.graph = build_turn_graph(
            TurnNodes(
                prepare=self._node_prepare,
                safety_gate=self._node_safety,
                handoff_gate=self._node_handoff,
                persona_route=self._node_persona,
                jev_classify=self._node_classify,
                standard_reply=self._node_standard,
                narrow_agent=self._node_narrow,
                full_agent=self._node_full,
            )
        )
```

Replace the whole `handle` method with the graph entry point and the node methods below. Every line of the old `handle` body appears in exactly one node, in the same order.

```python
    def handle(self, message: InboundMessage) -> Reply:
        return self.graph.invoke({"message": message})["reply"]

    # -- graph nodes (graph.py says what follows what) -----------------------

    def _node_prepare(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message = turn["message"]
        self.log.inbound(message)
        state = self.conversations.get(message.conversation_id)
        state.turns += 1

        resolved = self.resolver.hydrate(message)
        self.log.identity_resolved(
            message.conversation_id,
            resolved.persona,
            resolved.method,
            # The audit trail for disclosure: when a customer asks why the bot
            # knew their name, this records who we thought they were and what
            # proved it.
            cluster_id=resolved.cluster_id,
            strength=resolved.identity.strength,
            error=resolved.error,
        )

        # Built once per conversation and cached on the state. Rebuilding it every
        # turn costs queries, moves the block in the prompt (defeating prefix
        # caching), and cannot change the answer — a customer's bikes and history
        # do not move mid-chat.
        if state.context_block is None:
            context = self.enricher.build(resolved)
            state.context_block = context.render()
            self.log.emit(
                "context_built", message.conversation_id,
                kept=list(context.sections), dropped=context.dropped, tokens=context.tokens,
            )

        if message.attachments:
            state.evidence_seen = True
        return {"conversation": state, "resolved": resolved}

    def _node_safety(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 1. Safety. A keyword gate ahead of the agent turn, not something the
        #    model has to notice. Allowed to over-trigger.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        safety = check_safety(message.message_text)
        if safety.triggered:
            return {"reply": self._handle_safety(message, resolved, state, safety.matched)}
        return {}

    def _node_handoff(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 2. Human handoff, reachable at any point, no friction.
        message, state = turn["message"], turn["conversation"]
        handoff = check_human_handoff(message.message_text)
        if handoff.triggered:
            self.log.guardrail(message.conversation_id, "human_handoff", handoff.matched)
            self.log.escalation(message.conversation_id, "customer_requested_human", None)
            return {"reply": self._finish(
                message, state, HANDOFF_MESSAGE, "guardrail:human_handoff",
                escalated=True, metadata={"matched": handoff.matched},
            )}
        return {}

    def _node_persona(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if resolved.persona == "dealer":
            # Dealers bypass customer triage entirely. There is no bike to
            # disambiguate and no customer record to enrich from — and routing
            # them through the customer path is precisely how a dealer would end
            # up holding someone else's warranty data.
            state.route_to(DEALER_ORDERS)
            self.log.routed(message.conversation_id, DEALER_ORDERS, "persona:dealer")
            return {"reply": self._run_agent(DEALER_ORDERS, message, resolved, state)}

        if resolved.persona != "customer":
            return {"reply": self._finish(
                message, state, UNSUPPORTED_MESSAGE, "router",
                escalated=True, metadata={"persona": resolved.persona},
            )}

        # 3. A customer with no bike on record goes straight to registration —
        #    triage has nothing to disambiguate and no issue it can act on.
        if resolved.method in ("no_warranty_record",) and LATE_WARRANTY in self.agents:
            state.route_to(LATE_WARRANTY)
            return {"reply": self._run_agent(LATE_WARRANTY, message, resolved, state)}

        # 4. Triage: which bike, what issue, which agent.
        if state.agent is None:
            outcome = self.triage.handle(message, resolved, state)
            self.log.routed(message.conversation_id, outcome.agent or "triage", outcome.reason)
            # Every classification, with the raw text that produced it. This is the
            # labelled set that makes a semantic router worth building later, and
            # it is worthless if collection starts late — so it starts now.
            self.log.emit(
                "classification", message.conversation_id,
                text=message.message_text, phase=state.phase,
                topic=state.pending_topic, agent=outcome.agent, reason=outcome.reason,
            )
            if not outcome.is_handoff:
                return {"reply": self._finish(
                    message, state, outcome.reply or UNSUPPORTED_MESSAGE, "triage",
                    metadata=dict(outcome.metadata, reason=outcome.reason),
                )}
        return {}

    def _node_classify(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        return {"route": Route(path="full", reasons=("jev_disabled",))}

    def _node_standard(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def _node_narrow(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def _node_full(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        return {"reply": self._run_agent(state.agent or BATTERY_SUPPORT, message, resolved, state)}
```

Split `_run_agent` so the narrow agent can share its post-checks. Replace the start of `_run_agent` up to and including the `turn = self.agents[agent_name].run(...)` call with:

```python
    def _run_agent(
        self,
        agent_name: str,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
    ) -> Reply:
        return self._run(self.agents[agent_name], message, resolved, state)

    def _run(
        self,
        agent: Agent,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        prefetched: Sequence[Dict[str, Any]] = (),
        route_path: Optional[str] = None,
    ) -> Reply:
        """One agent turn and every post-check, for whichever agent ran it.

        One place, so the narrow agent cannot skip a check the full agents get.
        """
        turn = agent.run(
            message, resolved, state.history, state.context_block or "", prefetched=prefetched
        )
```

Leave the rest of the old `_run_agent` body in place, now as `_run`'s body. Then change its final `metadata=` argument to:

```python
            metadata=dict(
                {"tool_calls": [c["tool"] for c in turn.tool_calls], "iterations": turn.iterations},
                **({"route": route_path} if route_path else {}),
            ),
```

Add `Sequence` to the `typing` import.

- [ ] **Step 5: Run everything**

Run: `python -m unittest tests.test_graph -v`
Expected: 2 pass.

Run: `python -m unittest discover -s tests -t .`
Expected: every pre-existing test passes unchanged, plus the new ones. Only the 2 known `test_video` errors. **This run is the parity proof.** Any other failure means a step moved or changed. Fix `runtime.py`, never the test.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt src/emotorad_ai/graph.py src/emotorad_ai/runtime.py tests/test_graph.py
git commit -m "Run the turn as a LangGraph graph with behaviour unchanged"
```

---

### Task 9: Jev paths in the runtime

**Files:**
- Modify:
  - `src/emotorad_ai/runtime.py`
  - `src/emotorad_ai/observability.py` (`EventLog.jev_decision`)
- Test: `tests/test_jev_runtime.py`

**Interfaces:**
- Consumes:
  - everything from Tasks 3 to 8
  - `openrouter.OpenRouterError`
  - `agents.base.HANDOVER_TEXT`
- Produces:
  - `Runtime(..., jev=None, narrow_llm=None, thresholds=None, standard_responses=None)`
  - `Runtime.catalogue`, `Runtime.jev_questions`
  - `EventLog.jev_decision(conversation_id, route, decision=None, error=None)`

- [ ] **Step 1: Write the failing tests**

`tests/test_jev_runtime.py`:

```python
import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.decisions import Q_CATEGORY, Q_LANGUAGE, Q_STANDARD, Q_SUB_CATEGORY, Q_WARRANTY
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, JevError, ScriptedJev, choose, yes
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import OpenRouterUnavailable
from emotorad_ai.runtime import Runtime
from emotorad_ai.standard_responses import StandardResponse
from emotorad_ai.tools.mocks import build_registry

TODAY = date(2026, 7, 28)
THANKS = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome, glad that helped."})

NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}
NARROW_WITH_WARRANTY = dict(NARROW, **{Q_WARRANTY: yes(0.9)})
UNSURE = {Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose("none", 0.5)}


class RaisingLLM:
    def __init__(self):
        self.requests = []

    def create(self, system, messages, tools):
        self.requests.append({"system": system, "messages": list(messages)})
        raise OpenRouterUnavailable("down")


def build(jev_decisions, fallback=(), narrow=(), narrow_llm=None, fallback_llm=None):
    registry = build_registry(today=TODAY)
    jev = ScriptedJev(jev_decisions)
    fallback_llm = fallback_llm or ScriptedClaude(list(fallback))
    narrow_llm = narrow_llm or ScriptedClaude(list(narrow))
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=fallback_llm,
        narrow_llm=narrow_llm,
        jev=jev,
        standard_responses=[THANKS],
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    return runtime, WebsiteChatAdapter(runtime.resolver), jev, narrow_llm, fallback_llm


def send(runtime, adapter, text, session="sess-ananya", attachments=()):
    return runtime.handle(adapter.to_message(
        {"conversation_id": "conv-1", "session_token": session, "text": text, "attachments": list(attachments)}
    ))


class GatesRunBeforeJevTests(unittest.TestCase):
    def test_a_safety_trigger_calls_neither_jev_nor_any_model(self):
        runtime, adapter, jev, narrow, fallback = build([])
        reply = send(runtime, adapter, "my battery is swollen")
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertEqual((jev.calls, narrow.requests, fallback.requests), ([], [], []))

    def test_a_request_for_a_human_calls_neither(self):
        runtime, adapter, jev, narrow, fallback = build([])
        send(runtime, adapter, "I want to talk to a human")
        self.assertEqual((jev.calls, narrow.requests, fallback.requests), ([], [], []))


class StandardPathTests(unittest.TestCase):
    def test_a_confident_standard_reply_uses_no_model_and_keeps_the_disclosure(self):
        runtime, adapter, jev, narrow, fallback = build(
            [JevDecision(answers={Q_STANDARD: choose("std-thanks", 0.97), Q_LANGUAGE: choose("english", 0.95)})]
        )
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertIn("You're welcome, glad that helped.", reply.text)
        self.assertTrue(has_disclosure(reply.text))
        self.assertEqual(reply.handled_by, "standard:std-thanks")
        self.assertEqual((narrow.requests, fallback.requests), ([], []))


class NarrowPathTests(unittest.TestCase):
    def test_a_confident_issue_goes_to_the_narrow_model_with_one_record(self):
        runtime, adapter, jev, narrow, fallback = build([JevDecision(answers=NARROW)], narrow=[say("Try another wall socket first.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertIn("Try another wall socket first.", reply.text)
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertEqual(reply.metadata.get("route"), "narrow")
        self.assertEqual(len(narrow.requests), 1)
        self.assertEqual(fallback.requests, [])
        self.assertIn("Battery will not charge", narrow.requests[0]["system"])
        self.assertNotIn("Range has dropped", narrow.requests[0]["system"])

    def test_the_prefetched_warranty_result_satisfies_the_coverage_check(self):
        runtime, adapter, *_ = build([JevDecision(answers=NARROW_WITH_WARRANTY)], narrow=[say("Good news, it's covered under warranty.")])
        reply = send(runtime, adapter, "my battery won't charge, is it under warranty")
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertIn("lookup_warranty_record", reply.metadata["tool_calls"])

    def test_a_narrow_coverage_claim_that_contradicts_the_prefetch_is_blocked(self):
        runtime, adapter, *_ = build([JevDecision(answers=NARROW_WITH_WARRANTY)], narrow=[say("Good news, it's covered under warranty.")])
        reply = send(runtime, adapter, "my battery won't charge, is it under warranty", session="sess-rohit")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")

    def test_an_unsure_follow_up_stays_on_the_same_record(self):
        runtime, adapter, jev, narrow, fallback = build(
            [JevDecision(answers=NARROW), JevDecision(answers=UNSURE)],
            narrow=[say("Is the charger light red?"), say("Red means it is charging. Leave it for four hours.")],
        )
        send(runtime, adapter, "my battery won't charge")
        reply = send(runtime, adapter, "yes it is red now")
        self.assertIn("Leave it for four hours", reply.text)
        self.assertEqual(len(narrow.requests), 2)
        self.assertIn("Battery will not charge", narrow.requests[1]["system"])
        self.assertEqual(fallback.requests, [])

    def test_a_photo_with_no_text_mid_flow_skips_jev_and_stays_on_the_record(self):
        runtime, adapter, jev, narrow, _ = build(
            [JevDecision(answers=NARROW)], narrow=[say("Please send a photo of the port."), say("Thanks, the port looks bent.")]
        )
        send(runtime, adapter, "my battery won't charge")
        reply = send(runtime, adapter, "", attachments=[{"kind": "image", "url": "https://example.test/port.jpg"}])
        self.assertEqual(len(jev.calls), 1)
        self.assertIn("the port looks bent", reply.text)


class FullPathTests(unittest.TestCase):
    def test_low_confidence_goes_to_the_fallback_model(self):
        runtime, adapter, jev, narrow, fallback = build([JevDecision(answers=UNSURE)], fallback=[say("Tell me more.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual((len(fallback.requests), narrow.requests), (1, []))

    def test_a_jev_failure_goes_to_the_fallback_model_and_is_logged(self):
        runtime, adapter, jev, narrow, fallback = build([JevError("unavailable", "down")], fallback=[say("Tell me more.")])
        send(runtime, adapter, "my battery won't charge")
        self.assertEqual(len(fallback.requests), 1)
        [event] = [e for e in runtime.log.events if e["event"] == "jev_decision"]
        self.assertEqual((event["path"], event["error"]), ("full", "unavailable"))

    def test_a_narrow_model_failure_rolls_history_back_before_the_full_agent(self):
        raising = RaisingLLM()
        runtime, adapter, jev, _, fallback = build([JevDecision(answers=NARROW)], fallback=[say("Tell me more.")], narrow_llm=raising)
        send(runtime, adapter, "my battery won't charge")
        self.assertEqual(len(raising.requests), 1)
        user_turns = [m for m in fallback.requests[0]["messages"] if m.get("content") == "my battery won't charge"]
        self.assertEqual(len(user_turns), 1)

    def test_a_full_agent_model_failure_hands_over(self):
        runtime, adapter, *_ = build([JevDecision(answers=UNSURE)], fallback_llm=RaisingLLM())
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "llm_error")

    def test_a_confident_motor_category_moves_the_conversation_to_the_motor_agent(self):
        runtime, adapter, *_ = build(
            [JevDecision(answers={Q_CATEGORY: choose("motor", 0.95), Q_SUB_CATEGORY: choose("none", 0.9)})],
            fallback=[say("Does the noise change with speed?")],
        )
        reply = send(runtime, adapter, "my battery won't charge and the motor is noisy")
        self.assertEqual(reply.handled_by, "motor_support")


class JevLogTests(unittest.TestCase):
    def test_the_decision_is_logged_with_scores_and_no_message_text(self):
        runtime, adapter, *_ = build([JevDecision(answers=NARROW, model="typesafe/jev-1.13-x", cost=0.00002)], narrow=[say("Ok.")])
        send(runtime, adapter, "my battery won't charge")
        [event] = [e for e in runtime.log.events if e["event"] == "jev_decision"]
        self.assertEqual(event["path"], "narrow")
        self.assertEqual(event["scores"][Q_CATEGORY]["choice"], "battery")
        self.assertEqual((event["model"], event["cost"]), ("typesafe/jev-1.13-x", 0.00002))
        self.assertNotIn("won't charge", json.dumps(event))

    def test_jev_receives_no_phone_or_name(self):
        runtime, adapter, jev, *_ = build([JevDecision(answers=UNSURE)], fallback=[say("Ok.")])
        # Not "call me": that phrase is a handoff request and would stop the turn before Jev.
        send(runtime, adapter, "hi, I'm Ananya, my battery won't charge, my number is 9876543210")
        dumped = json.dumps(jev.calls[0]["state"])
        self.assertNotIn("9876543210", dumped)
        self.assertNotIn("Ananya", dumped)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_jev_runtime -v`
Expected: ERROR, `TypeError: Runtime.__init__() got an unexpected keyword argument 'narrow_llm'`.

- [ ] **Step 3: Add `EventLog.jev_decision` to `src/emotorad_ai/observability.py`** (after `routed`)

```python
    def jev_decision(self, conversation_id: str, route: Any, decision: Any = None, error: Optional[str] = None) -> None:
        """The path Jev's scores chose, and why. Scores only: the state text is
        never logged here; `inbound` already holds the redacted message."""
        self.emit(
            "jev_decision",
            conversation_id,
            path=route.path,
            reasons=list(route.reasons),
            scores=dict(route.scores),
            sub_category=route.sub_category,
            standard_response=route.standard_response_id,
            prefetch=[call.tool for call in route.prefetch],
            error=error,
            model=getattr(decision, "model", None),
            cost=getattr(decision, "cost", None),
            latency_ms=getattr(decision, "latency_ms", None),
        )
```

- [ ] **Step 4: Implement the paths in `src/emotorad_ai/runtime.py`**

Add these imports:

```python
from dataclasses import replace

from .agents.base import HANDOVER_TEXT
from .agents.narrow_support import AGENT_NAME as NARROW_SUPPORT
from .agents.narrow_support import build_narrow_definition
from .decisions import (
    EMPTY_MESSAGE,
    Thresholds,
    build_catalogue,
    build_questions,
    build_state,
    load_thresholds,
    route,
)
from .errorcodes import load_table
from .jev import JevError
from .knowledge import KnowledgeBase
from .openrouter import OpenRouterError
from .standard_responses import StandardResponse, load_standard_responses
from .tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_ERROR_CODE, build_registry
```

The last import replaces the existing `from .tools.mocks import CREATE_SUPPORT_TICKET, build_registry`.

Extend `__init__`'s signature with `jev: Any = None`, `narrow_llm: Any = None`, `thresholds: Optional[Thresholds] = None` and `standard_responses: Optional[Sequence[StandardResponse]] = None`. Then add this before the `self.graph = ...` block:

```python
        # Jev routing. Absent in offline and bedrock modes, where the graph's
        # classify node always answers `full` and the turn is exactly today's.
        self.jev = jev
        self.narrow_llm = narrow_llm if narrow_llm is not None else self.llm
        self.catalogue = None
        self.jev_questions: Dict[str, Any] = {}
        self.thresholds = thresholds or Thresholds()
        if jev is not None:
            self.thresholds = thresholds or load_thresholds()
            self.catalogue = build_catalogue(
                KnowledgeBase(),
                standard=standard_responses if standard_responses is not None else load_standard_responses(),
                error_table=load_table() if LOOKUP_ERROR_CODE in self.registry.specs else None,
            )
            self.jev_questions = build_questions(self.catalogue)
```

Replace the four placeholder nodes from Task 8 with:

```python
    def _node_classify(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if self.jev is None:
            return {"route": Route(path="full", reasons=("jev_disabled",))}

        bike = self._selected_bike(resolved, state)
        decision, error = None, None
        if not message.message_text.strip():
            # A photo on its own: nothing to score. Stay on the record if there is one.
            error = EMPTY_MESSAGE
        else:
            jev_state = build_state(
                message.message_text, state.history, message.channel, bike=bike,
                current_sub_category=state.sub_category, redact=self._redaction_terms(resolved),
            )
            try:
                decision = self.jev.decide(jev_state, self.jev_questions)
            except JevError as exc:
                error = exc.code

        chosen = route(decision, error, self.thresholds, self.catalogue, state.sub_category, bike)
        self.log.jev_decision(message.conversation_id, chosen, decision, error)
        return {"route": chosen}

    def _node_standard(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, chosen = turn["message"], turn["conversation"], turn["route"]
        response = self.catalogue.standard[chosen.standard_response_id]
        text = response.reply_for(chosen.language)
        # Checked at load already; checked again here because a check that only
        # runs at load is a check an edit to the loader can remove.
        if check_coverage_claim(text, []).blocked or check_evidence(text, state.evidence_seen).blocked:
            self.log.guardrail(message.conversation_id, "standard_response_blocked", {"id": response.id})
            return {"route": replace(chosen, path="full", reasons=chosen.reasons + ("standard_blocked",))}
        return {"reply": self._finish(
            message, state, text, "standard:%s" % response.id, metadata={"route": "standard"},
        )}

    def _node_narrow(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved, chosen = turn["message"], turn["conversation"], turn["resolved"], turn["route"]
        record = self.catalogue.records[chosen.sub_category]
        state.sub_category = record.id

        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
        )
        prefetched: List[Dict[str, Any]] = []
        for call in chosen.prefetch:
            if call.tool not in self.registry.specs:
                continue
            arguments = dict(call.arguments)
            envelope = self.registry.call(call.tool, arguments, context)
            self.log.tool_call(message.conversation_id, call.tool, arguments, envelope)
            prefetched.append({"tool": call.tool, "arguments": arguments, "result": envelope, "prefetched": True})

        agent = Agent(build_narrow_definition(record, prefetched), self.registry, self.narrow_llm, self.log, self.settings)
        mark = len(state.history)
        try:
            reply = self._run(agent, message, resolved, state, prefetched=prefetched, route_path="narrow")
        except OpenRouterError as exc:
            # The loop appended the customer's message before failing. Undo it,
            # or the full agent would be shown the message twice.
            del state.history[mark:]
            self.log.emit("llm_error", message.conversation_id, agent=NARROW_SUPPORT, code=exc.code)
            return {"route": replace(chosen, path="full", reasons=chosen.reasons + ("narrow_llm_error:%s" % exc.code,))}
        return {"reply": reply}

    def _node_full(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        chosen = turn.get("route")

        # Jev may confidently place the message in a different topic from the
        # one triage picked; that is the category decision the spec gives it.
        topic_agent = TOPIC_AGENTS.get(chosen.category) if chosen and chosen.category else None
        if topic_agent and topic_agent != state.agent:
            state.route_to(topic_agent)
            self.log.routed(message.conversation_id, topic_agent, "jev:%s" % chosen.category)
        if chosen and chosen.category and state.sub_category:
            current = self.catalogue.records.get(state.sub_category) if self.catalogue else None
            if current is None or current.topic != chosen.category:
                state.sub_category = None

        name = state.agent or BATTERY_SUPPORT
        mark = len(state.history)
        try:
            return {"reply": self._run_agent(name, message, resolved, state)}
        except OpenRouterError as exc:
            # Only the OpenRouter models raise this; Bedrock behaviour is unchanged.
            del state.history[mark:]
            self.log.emit("llm_error", message.conversation_id, agent=name, code=exc.code)
            self.log.escalation(message.conversation_id, "llm_error", None)
            return {"reply": self._finish(
                message, state, HANDOVER_TEXT, "llm_error", escalated=True, metadata={"code": exc.code},
            )}

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _selected_bike(resolved: ResolvedIdentity, state: ConversationState) -> Optional[Dict[str, Any]]:
        for bike in resolved.bikes:
            if bike.get("frame_number") == state.selected_frame:
                return bike
        return resolved.single_bike

    @staticmethod
    def _redaction_terms(resolved: ResolvedIdentity) -> List[str]:
        """Names and frame numbers that must not reach Jev, even inside recent turns."""
        terms: List[str] = []
        name = (resolved.profile or {}).get("name") or ""
        if name:
            terms.append(name)
            terms.extend(part for part in name.split() if len(part) > 2)
        terms.extend(bike.get("frame_number") or "" for bike in resolved.bikes)
        return [term for term in terms if term]
```

- [ ] **Step 5: Run everything**

Run: `python -m unittest tests.test_jev_runtime -v`
Expected: all pass.

Run the full suite.
Expected: all pass. Only the 2 known `test_video` errors.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/runtime.py src/emotorad_ai/observability.py tests/test_jev_runtime.py
git commit -m "Route each customer turn by Jev's scores: standard, narrow or full"
```

---

### Task 10: Mode wiring for the CLI and the API

**Files:**
- Create: `src/emotorad_ai/wiring.py`
- Modify: `src/emotorad_ai/cli.py`, `src/emotorad_ai/api.py`
- Test: `tests/test_wiring.py`

**Interfaces:**
- Consumes: `Settings`, `OfflinePlanner`, `BedrockClaude`, `OpenRouterChat`, `OpenRouterTransport`, `JevClient`
- Produces:
  - `wiring.Models(llm, narrow_llm=None, jev=None)`
  - `wiring.build_models(settings, transport=None) -> Models`
  - `cli.resolve_mode(offline: bool, mode: str | None, environ=os.environ) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_wiring.py`:

```python
import os
import unittest
from unittest import mock

from emotorad_ai.cli import resolve_mode
from emotorad_ai.config import Settings
from emotorad_ai.jev import JevClient
from emotorad_ai.llm import OfflinePlanner, OpenRouterChat
from emotorad_ai.openrouter import API_KEY_ENV, OpenRouterConfigError
from emotorad_ai.wiring import build_models
from tests.fake_http import FakeTransport


class BuildModelsTests(unittest.TestCase):
    def test_offline_has_no_jev_and_no_narrow_model(self):
        models = build_models(Settings(mode="offline"))
        self.assertIsInstance(models.llm, OfflinePlanner)
        self.assertIsNone(models.jev)
        self.assertIsNone(models.narrow_llm)

    def test_openrouter_builds_jev_and_both_reply_models_on_one_transport(self):
        settings = Settings(mode="openrouter")
        models = build_models(settings, transport=FakeTransport())
        self.assertIsInstance(models.jev, JevClient)
        self.assertEqual(models.jev.model, settings.jev_model)
        self.assertEqual(models.jev.timeout, settings.jev_timeout)
        self.assertIsInstance(models.llm, OpenRouterChat)
        self.assertEqual(models.llm.model, settings.fallback_model)
        self.assertEqual(models.narrow_llm.model, settings.narrow_model)

    def test_openrouter_without_a_key_fails_loudly(self):
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}):
            with self.assertRaises(OpenRouterConfigError):
                build_models(Settings(mode="openrouter"))


class CliModeTests(unittest.TestCase):
    def test_offline_flag_wins(self):
        self.assertEqual(resolve_mode(True, "openrouter", {"EMOTORAD_AI_MODE": "bedrock"}), "offline")

    def test_mode_flag_beats_the_environment(self):
        self.assertEqual(resolve_mode(False, "openrouter", {"EMOTORAD_AI_MODE": "bedrock"}), "openrouter")

    def test_the_environment_is_used_when_no_flag_is_given(self):
        self.assertEqual(resolve_mode(False, None, {"EMOTORAD_AI_MODE": "openrouter"}), "openrouter")

    def test_with_nothing_set_the_cli_still_talks_to_bedrock_as_before(self):
        self.assertEqual(resolve_mode(False, None, {}), "bedrock")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_wiring -v`
Expected: ERROR, `No module named 'emotorad_ai.wiring'` (or `cannot import name 'resolve_mode'`).

- [ ] **Step 3: Implement**

`src/emotorad_ai/wiring.py`:

```python
"""Which models a process talks to, chosen once from Settings.mode.

One function so the CLI and the API cannot drift apart on what a mode means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import Settings
from .jev import JevClient
from .llm import BedrockClaude, OfflinePlanner, OpenRouterChat
from .openrouter import OpenRouterTransport


@dataclass
class Models:
    llm: Any
    narrow_llm: Any = None
    jev: Any = None


def build_models(settings: Settings, transport: Optional[Any] = None) -> Models:
    if settings.mode == "offline":
        return Models(llm=OfflinePlanner())
    if settings.mode == "bedrock":
        return Models(llm=BedrockClaude(settings))
    # openrouter: one transport, one key, three models.
    transport = transport or OpenRouterTransport(
        base_url=settings.openrouter_base_url, timeout=settings.openrouter_timeout
    )
    return Models(
        llm=OpenRouterChat(settings.fallback_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        narrow_llm=OpenRouterChat(settings.narrow_model, transport, max_tokens=settings.max_tokens, zdr=settings.openrouter_zdr),
        jev=JevClient(transport, model=settings.jev_model, timeout=settings.jev_timeout),
    )
```

In `src/emotorad_ai/cli.py`:
- Add `import os` and `from dataclasses import replace`.
- Replace `from .llm import OfflinePlanner` with `from .wiring import build_models`.
- Update the docstring examples. Add the line `python -m emotorad_ai.cli --mode openrouter --channel amiigo --session sess-amiigo-test` with the comment `# Jev routing + OpenRouter models (needs OPENROUTER_API_KEY)`.
- Add the function below, the new argument, and the new construction.

```python
def resolve_mode(offline: bool, mode: Optional[str], environ=os.environ) -> str:
    """--offline, then --mode, then EMOTORAD_AI_MODE. With none of them the CLI
    talks to Bedrock, exactly as it did before modes existed."""
    if offline:
        return "offline"
    if mode:
        return mode
    return environ.get("EMOTORAD_AI_MODE") or "bedrock"
```

```python
    parser.add_argument("--mode", choices=MODES, default=None, help="offline, bedrock or openrouter; overrides EMOTORAD_AI_MODE")
```

```python
    settings = replace(load_settings(), mode=resolve_mode(args.offline, args.mode))
    registry = build_registry(diagnostics_available=args.diagnostics)
    log = EventLog(path=settings.log_path, to_stdout=False)
    models = build_models(settings)
    runtime = Runtime(
        settings=settings,
        registry=registry,
        llm=models.llm,
        narrow_llm=models.narrow_llm,
        jev=models.jev,
        log=log,
        resolver=IdentityResolver(registry),
    )
```

Also:
- Add `Optional` to the typing import and `MODES` to the config import.
- Change the banner line to `print("Emotorad battery support (%s). Ctrl-C or an empty line to quit." % settings.mode)`.

In `src/emotorad_ai/api.py`:
- Replace `from .llm import OfflinePlanner` with `from .wiring import build_models`.
- Replace the `MODE = ...` line and the `runtime = Runtime(...)` block with:

```python
settings = load_settings()
registry = build_registry()
resolver = IdentityResolver(registry)
log = EventLog(path=settings.log_path, to_stdout=settings.log_to_stdout)
models = build_models(settings)
runtime = Runtime(
    settings=settings,
    registry=registry,
    llm=models.llm,
    narrow_llm=models.narrow_llm,
    jev=models.jev,
    log=log,
    resolver=resolver,
)
```

Then:
- Change `return {"status": "ok", "mode": MODE}` to `return {"status": "ok", "mode": settings.mode}`.
- Remove the now-duplicate `settings = load_settings()`, `registry = ...`, `resolver = ...` and `log = ...` lines if they appear twice.
- Keep the module docstring's mode examples and add `EMOTORAD_AI_MODE=openrouter uvicorn emotorad_ai.api:app`.

- [ ] **Step 4: Run everything**

Run: `python -m unittest tests.test_wiring -v`, then the full suite. That includes `tests.test_amiigo_test_session`, which drives `cli.main(["--offline", ...])`.
Expected: all pass. Only the 2 known `test_video` errors.

Run: `PYTHONPATH=src python -m emotorad_ai.cli --offline --channel amiigo --session sess-amiigo-test "hi"`
Expected: the two-bike greeting, as before.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/wiring.py src/emotorad_ai/cli.py src/emotorad_ai/api.py tests/test_wiring.py
git commit -m "Choose models from the mode in one place for the CLI and the API"
```

---

### Task 11: Model and cost in the log, and cost per resolved conversation

**Files:**
- Modify:
  - `src/emotorad_ai/observability.py` (`llm_turn` gains `model`)
  - `src/emotorad_ai/agents/base.py` (passes the model)
  - `src/emotorad_ai/metrics.py`
- Test: `tests/test_cost_metrics.py`

**Interfaces:**
- Produces:
  - `EventLog.llm_turn(..., usage=None, model=None)`
  - `ConversationSummary.cost`, `ConversationSummary.paths`
  - `Report.total_cost`, `Report.by_path`, `Report.cost_per_resolved()`

- [ ] **Step 1: Write the failing tests**

`tests/test_cost_metrics.py`:

```python
import json
import unittest

from emotorad_ai.config import Settings
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import OpenRouterChat
from emotorad_ai.metrics import build_report, render
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.openrouter import OpenRouterTransport
from tests.fake_http import FakeServer

KEY = "sk-or-test-NEVER-IN-LOGS"


def events(cid, handled_by, cost, path=None, escalated=False):
    rows = [
        {"event": "inbound", "conversation_id": cid, "channel": "whatsapp", "text": "battery not charging"},
        {"event": "llm_turn", "conversation_id": cid, "usage": {"input_tokens": 100, "output_tokens": 10, "cost": cost}},
        {"event": "outcome", "conversation_id": cid, "handled_by": handled_by, "escalated": escalated},
    ]
    if path:
        rows.insert(1, {"event": "jev_decision", "conversation_id": cid, "path": path, "cost": 0.00002})
    return rows


class CostReportTests(unittest.TestCase):
    def test_cost_per_resolved_counts_every_conversation_and_divides_by_the_resolved(self):
        log = events("a", "narrow_support", 0.0001, path="narrow") + events("b", "battery_support", 0.005, path="full", escalated=True)
        report = build_report(log)
        self.assertAlmostEqual(report.total_cost, 0.0001 + 0.005 + 0.00004)
        self.assertAlmostEqual(report.cost_per_resolved(), report.total_cost / 1)
        self.assertEqual(report.by_path, {"narrow": 1, "full": 1})
        text = render(report)
        self.assertIn("cost per resolved", text)
        self.assertIn("narrow", text)

    def test_no_resolved_conversation_is_infinite_cost(self):
        self.assertEqual(build_report(events("a", "llm_error", 0.001, escalated=True)).cost_per_resolved(), float("inf"))


class ModelInLogTests(unittest.TestCase):
    def test_llm_turn_records_the_model_and_the_key_never_reaches_the_log(self):
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": "Try another socket."}}],
                 "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.00001}}
        with FakeServer() as server:
            server.queue(200, reply)
            transport = OpenRouterTransport(api_key=KEY, base_url=server.url)
            registry = build_registry()
            runtime = Runtime(
                settings=Settings(log_path="", log_to_stdout=False),
                registry=registry,
                llm=OpenRouterChat("anthropic/claude-haiku-4.5", transport),
                narrow_llm=OpenRouterChat("deepseek/deepseek-v4-flash-0731", transport),
                jev=ScriptedJev([JevDecision(answers={Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)})]),
                standard_responses=[],
                log=EventLog(path=None),
                resolver=IdentityResolver(registry),
            )
            adapter = WebsiteChatAdapter(runtime.resolver)
            runtime.handle(adapter.to_message({"conversation_id": "c", "session_token": "sess-ananya", "text": "my battery won't charge"}))
        [turn] = [e for e in runtime.log.events if e["event"] == "llm_turn"]
        self.assertEqual(turn["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(turn["usage"]["cost"], 0.00001)
        self.assertNotIn(KEY, json.dumps(runtime.log.events, default=str))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_cost_metrics -v`
Expected: FAIL/ERROR. `Report` has no `total_cost`, and `llm_turn` has no `model`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/observability.py`, replace `llm_turn` with:

```python
    def llm_turn(
        self, conversation_id: str, agent: str, iteration: int, stop_reason: str,
        usage: Any = None, model: Optional[str] = None,
    ) -> None:
        fields: Dict[str, Any] = {"agent": agent, "iteration": iteration, "stop_reason": stop_reason, "usage": usage}
        if model:
            # Which model answered is what makes a cost per path comparable.
            fields["model"] = model
        self.emit("llm_turn", conversation_id, **fields)
```

In `src/emotorad_ai/agents/base.py`, change the `self.log.llm_turn(...)` call to pass `model=getattr(self.llm, "model", None)` as a final keyword argument.

In `src/emotorad_ai/metrics.py`:
- Add `cost: float = 0.0` and `paths: List[str] = field(default_factory=list)` to `ConversationSummary`.
- In `summarise`:
  - inside the `llm_turn` branch, add `summary.cost += float(usage.get("cost") or 0.0)`
  - add a branch:

```python
        elif kind == "jev_decision":
            summary.cost += float(event.get("cost") or 0.0)
            if event.get("path"):
                summary.paths.append(event["path"])
```

- Add to `Report`: `total_cost: float = 0.0` and `by_path: Dict[str, int] = field(default_factory=dict)`, plus this method:

```python
    def cost_per_resolved(self) -> float:
        """Money per *resolved* conversation, the number the Jev routing exists to lower."""
        return self.total_cost / self.resolved if self.resolved else float("inf")
```

- In `build_report`, after `report.total_tokens = ...`, add:

```python
    report.total_cost = sum(s.cost for s in summaries)
    report.by_path = dict(Counter(path for s in summaries for path in s.paths))
```

- In `render`, add after the `tokens per resolved` line:

```python
        "cost per resolved    $%.5f" % report.cost_per_resolved(),
```

  And before the guardrails block:

```python
    if report.by_path:
        lines.append("")
        lines.append("turns by path:")
        for name, count in sorted(report.by_path.items(), key=lambda kv: -kv[1]):
            lines.append("  %-28s %d" % (name, count))
```

- [ ] **Step 4: Run everything**

Run: `python -m unittest tests.test_cost_metrics -v`, then the full suite.
Expected: all pass. `render` with no resolved conversations prints `$inf`, which the existing render test accepts, since it only checks for a `str`.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/observability.py src/emotorad_ai/agents/base.py src/emotorad_ai/metrics.py tests/test_cost_metrics.py
git commit -m "Log the answering model and report cost per resolved conversation"
```

---

### Task 12: Labelled set, calibration, probe and docs

**Files:**
- Create:
  - `tests/data/jev_golden.yaml`
  - `tests/jev_golden.py`
  - `src/emotorad_ai/calibration.py`
  - `scripts/calibrate_jev.py`
  - `scripts/jev_probe.py`
- Modify:
  - `CLAUDE.md` (Commands)
  - `README.md` (Run it)
  - the spec (implementation notes)
- Test: `tests/test_calibration.py`

**Interfaces:**
- Consumes: `decisions.*`, `jev.*`, `standard_responses.load_standard_responses`, `tests.test_retrieval_evals.GOLDEN`, `metrics.detect_language`
- Produces:
  - `tests.jev_golden.GoldenCase`
  - `tests.jev_golden.load_golden() -> list[GoldenCase]`
  - `calibration.CANDIDATES`
  - `calibration.evaluate_choice(pairs, question_id, expected, candidates) -> dict`
  - `calibration.suggest_choice(rows, target) -> float | None`
  - `calibration.evaluate_noul(pairs, question_id, expected, candidates) -> dict`
  - `calibration.suggest_noul(rows, target_recall) -> float | None`

- [ ] **Step 1: Write the labelled extras**

`tests/data/jev_golden.yaml`:

```yaml
# Labels for Jev calibration beyond the retrieval goldens, which are imported from
# tests/test_retrieval_evals.py rather than copied. Standard-response examples and
# counter-examples are read from knowledge/_standard/. Omitted fields default to:
# sub_category none, standard_response none, needs_warranty_lookup false.
cases:
  - {text: "do you sell helmets", language: english, category: none_of_these}
  - {text: "what is the price of the new X2", language: english, category: none_of_these}
  - {text: "where is my order", language: english, category: none_of_these}
  - {text: "mera order kab aayega", language: hinglish, category: none_of_these}
  - {text: "can I get a refund", language: english, category: none_of_these}
  - {text: "is my battery still under warranty", language: english, category: battery, needs_warranty_lookup: true}
  - {text: "battery replace free mein hoga kya", language: hinglish, category: battery, needs_warranty_lookup: true}
  - {text: "क्या बैटरी वारंटी में है", language: hindi, category: battery, needs_warranty_lookup: true}
  - {text: "will I have to pay to fix the motor noise", language: english, category: motor, sub_category: motor-noise, needs_warranty_lookup: true}
  - {text: "my display shows E-07", language: english, category: battery, error_code: E07}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_calibration.py`:

```python
import unittest

from emotorad_ai.calibration import evaluate_choice, evaluate_noul, suggest_choice, suggest_noul
from emotorad_ai.decisions import NONE, NONE_OF_THESE, Q_CATEGORY
from emotorad_ai.jev import JevDecision, choose, yes
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import load_standard_responses
from tests.jev_golden import GoldenCase, load_golden


class GoldenSetTests(unittest.TestCase):
    def test_every_label_points_at_something_that_exists(self):
        records = {r.id for r in KnowledgeBase().records}
        standard = {s.id for s in load_standard_responses()}
        cases = load_golden()
        self.assertGreaterEqual(len(cases), 61 + 10)
        for case in cases:
            with self.subTest(case.text):
                self.assertIn(case.category, (None, "battery", "motor", NONE_OF_THESE))
                self.assertIn(case.sub_category, records | {NONE, None})
                self.assertIn(case.standard_response, standard | {NONE, None})

    def test_english_hinglish_and_hindi_are_all_represented(self):
        languages = {case.language for case in load_golden()}
        self.assertTrue({"english", "hinglish", "hindi"} <= languages)


def case(language, category):
    return GoldenCase(text="x", language=language, category=category)


class CalibrationMathTests(unittest.TestCase):
    PAIRS = [
        (case("english", "battery"), JevDecision(answers={Q_CATEGORY: choose("battery", 0.95)})),
        (case("english", "battery"), JevDecision(answers={Q_CATEGORY: choose("motor", 0.70)})),
        (case("hinglish", "battery"), JevDecision(answers={Q_CATEGORY: choose("battery", 0.88)})),
        (case("hinglish", "motor"), JevDecision(answers={Q_CATEGORY: choose("battery", 0.60)})),
    ]

    def test_choice_accuracy_and_coverage_per_language_never_averaged(self):
        rows = evaluate_choice(self.PAIRS, Q_CATEGORY, lambda c: c.category, [0.5, 0.8, 0.9])
        self.assertEqual(rows[0.5]["english"], {"n": 2, "covered": 2, "correct": 1})
        self.assertEqual(rows[0.8]["english"], {"n": 2, "covered": 1, "correct": 1})
        self.assertEqual(rows[0.8]["hinglish"], {"n": 2, "covered": 1, "correct": 1})
        self.assertEqual(rows[0.9]["hinglish"], {"n": 2, "covered": 0, "correct": 0})

    def test_the_suggestion_is_the_lowest_threshold_every_language_meets(self):
        rows = evaluate_choice(self.PAIRS, Q_CATEGORY, lambda c: c.category, [0.5, 0.8, 0.9])
        self.assertEqual(suggest_choice(rows, target=1.0), 0.8)
        self.assertIsNone(suggest_choice({0.5: rows[0.5]}, target=1.0))

    def test_noul_recall_drives_the_prefetch_suggestion(self):
        pairs = [
            (GoldenCase("x", "english", needs_warranty_lookup=True), JevDecision(answers={"w": yes(0.9)})),
            (GoldenCase("x", "english", needs_warranty_lookup=True), JevDecision(answers={"w": yes(0.65)})),
            (GoldenCase("x", "english", needs_warranty_lookup=False), JevDecision(answers={"w": yes(0.7)})),
        ]
        rows = evaluate_noul(pairs, "w", lambda c: c.needs_warranty_lookup, [0.6, 0.8])
        self.assertEqual(rows[0.6]["english"], {"tp": 2, "fp": 1, "fn": 0, "tn": 0})
        self.assertEqual(rows[0.8]["english"], {"tp": 1, "fp": 0, "fn": 1, "tn": 1})
        self.assertEqual(suggest_noul(rows, target_recall=1.0), 0.6)


if __name__ == "__main__":
    unittest.main()
```

Run: `python -m unittest tests.test_calibration -v`
Expected: ERROR, `No module named 'emotorad_ai.calibration'`.

- [ ] **Step 3: Implement**

`tests/jev_golden.py`:

```python
"""The labelled set Jev is calibrated against.

Three sources, none copied: the retrieval goldens (already labelled with topic
and record), the extras in tests/data/jev_golden.yaml, and the examples and
counter-examples authored on each standard response.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from emotorad_ai.decisions import NONE, NONE_OF_THESE
from emotorad_ai.metrics import detect_language
from emotorad_ai.standard_responses import load_standard_responses

EXTRAS = Path(__file__).parent / "data" / "jev_golden.yaml"
_LANGUAGE_NAMES = {"en": "english", "hinglish": "hinglish", "hi-deva": "hindi"}


@dataclass(frozen=True)
class GoldenCase:
    text: str
    language: str
    category: Optional[str] = None  # None: not scored on this question
    sub_category: Optional[str] = None
    standard_response: Optional[str] = None
    needs_warranty_lookup: Optional[bool] = None
    error_code: Optional[str] = None
    bike: Dict[str, Any] = field(default_factory=dict)


def _language(text: str) -> str:
    return _LANGUAGE_NAMES.get(detect_language(text), "english")


def load_golden() -> List[GoldenCase]:
    from tests.test_retrieval_evals import GOLDEN

    cases = [
        GoldenCase(text=query, language=_language(query), category=topic, sub_category=record_id,
                   standard_response=NONE, needs_warranty_lookup=False, bike=dict(bike))
        for query, record_id, topic, bike in GOLDEN
    ]
    raw = yaml.safe_load(EXTRAS.read_text(encoding="utf-8")) or {}
    for item in raw.get("cases", []):
        cases.append(GoldenCase(
            text=item["text"], language=item["language"], category=item.get("category"),
            sub_category=item.get("sub_category", NONE), standard_response=item.get("standard_response", NONE),
            needs_warranty_lookup=bool(item.get("needs_warranty_lookup", False)), error_code=item.get("error_code", NONE),
        ))
    for response in load_standard_responses():
        for example in response.examples:
            cases.append(GoldenCase(text=example, language=_language(example), category=NONE_OF_THESE, standard_response=response.id))
        for example in response.counter_examples:
            cases.append(GoldenCase(text=example, language=_language(example), standard_response=NONE))
    return cases
```

`src/emotorad_ai/calibration.py`:

```python
"""Thresholds from evidence: how Jev scores on the labelled set, per language.

Jev's probabilities are calibrated in aggregate, not per question format, so a
threshold is chosen per question by measuring, never by feel. Every number is
reported per language and never averaged; an average hides the language that
is worst, and Hinglish is the one most likely to be.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .jev import ChoiceAnswer, JevDecision, NoulAnswer

CANDIDATES = [round(0.50 + 0.05 * step, 2) for step in range(10)]  # 0.50 .. 0.95

Pair = Tuple[Any, JevDecision]


def evaluate_choice(
    pairs: Sequence[Pair], question_id: str, expected: Callable[[Any], Optional[str]], candidates: Iterable[float]
) -> Dict[float, Dict[str, Dict[str, int]]]:
    """For each threshold: per language, how many cases, how many Jev was
    confident on (covered), and how many of those it got right."""
    rows: Dict[float, Dict[str, Dict[str, int]]] = {}
    for threshold in candidates:
        by_language: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "covered": 0, "correct": 0})
        for case, decision in pairs:
            want = expected(case)
            answer = decision.answers.get(question_id)
            if want is None or not isinstance(answer, ChoiceAnswer):
                continue
            row = by_language[case.language]
            row["n"] += 1
            if answer.p >= threshold:
                row["covered"] += 1
                row["correct"] += int(answer.choice == want)
        rows[threshold] = dict(by_language)
    return rows


def suggest_choice(rows: Mapping[float, Mapping[str, Mapping[str, int]]], target: float) -> Optional[float]:
    """The lowest threshold at which every language is at least `target`
    accurate on what it covers. Lowest, because coverage is the saving."""
    for threshold in sorted(rows):
        languages = rows[threshold]
        if languages and all(
            (row["correct"] / row["covered"] if row["covered"] else 1.0) >= target for row in languages.values()
        ):
            return threshold
    return None


def evaluate_noul(
    pairs: Sequence[Pair], question_id: str, expected: Callable[[Any], Optional[bool]], candidates: Iterable[float]
) -> Dict[float, Dict[str, Dict[str, int]]]:
    rows: Dict[float, Dict[str, Dict[str, int]]] = {}
    for threshold in candidates:
        by_language: Dict[str, Dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
        for case, decision in pairs:
            want = expected(case)
            answer = decision.answers.get(question_id)
            if want is None or not isinstance(answer, NoulAnswer):
                continue
            predicted = answer.p >= threshold
            key = ("tp" if want else "fp") if predicted else ("fn" if want else "tn")
            by_language[case.language][key] += 1
        rows[threshold] = dict(by_language)
    return rows


def suggest_noul(rows: Mapping[float, Mapping[str, Mapping[str, int]]], target_recall: float) -> Optional[float]:
    """The highest threshold that still catches `target_recall` of the real
    cases in every language. A missed warranty lookup blocks a correct reply;
    an extra one costs a read, so recall is what matters."""
    for threshold in sorted(rows, reverse=True):
        languages = rows[threshold]
        recalls = [
            row["tp"] / (row["tp"] + row["fn"]) if (row["tp"] + row["fn"]) else 1.0 for row in languages.values()
        ]
        if languages and all(recall >= target_recall for recall in recalls):
            return threshold
    return None
```

`scripts/calibrate_jev.py`:

```python
"""Run the labelled set through Jev and propose thresholds (spec §9).

    OPENROUTER_API_KEY=... python scripts/calibrate_jev.py            # report only
    OPENROUTER_API_KEY=... python scripts/calibrate_jev.py --write    # also update thresholds.yaml

Costs well under a cent for the whole set. Run by a person, never in CI. The
proposal goes into a PR like any other reviewed change.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import yaml  # noqa: E402

from emotorad_ai.calibration import CANDIDATES, evaluate_choice, evaluate_noul, suggest_choice, suggest_noul  # noqa: E402
from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.decisions import (  # noqa: E402
    Q_CATEGORY, Q_ERROR_CODE, Q_LANGUAGE, Q_STANDARD, Q_SUB_CATEGORY, Q_WARRANTY, THRESHOLDS_PATH,
    build_catalogue, build_questions, build_state,
)
from emotorad_ai.errorcodes import load_table  # noqa: E402
from emotorad_ai.jev import JevClient, JevError  # noqa: E402
from emotorad_ai.knowledge import KnowledgeBase  # noqa: E402
from emotorad_ai.openrouter import OpenRouterTransport  # noqa: E402
from emotorad_ai.standard_responses import load_standard_responses  # noqa: E402
from tests.jev_golden import load_golden  # noqa: E402

TARGETS = {Q_STANDARD: 0.98, Q_CATEGORY: 0.95, Q_SUB_CATEGORY: 0.90, Q_LANGUAGE: 0.95, Q_ERROR_CODE: 0.95}
EXPECTED = {
    Q_STANDARD: lambda c: c.standard_response, Q_CATEGORY: lambda c: c.category, Q_SUB_CATEGORY: lambda c: c.sub_category,
    Q_LANGUAGE: lambda c: c.language, Q_ERROR_CODE: lambda c: c.error_code,
}
YAML_KEYS = {Q_STANDARD: "standard_response", Q_CATEGORY: "category", Q_SUB_CATEGORY: "sub_category", Q_LANGUAGE: "language", Q_ERROR_CODE: "error_code"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write the proposal to knowledge/_routing/thresholds.yaml")
    args = parser.parse_args()

    settings = load_settings()
    client = JevClient(OpenRouterTransport(base_url=settings.openrouter_base_url), model=settings.jev_model, timeout=30.0)
    catalogue = build_catalogue(KnowledgeBase(), load_standard_responses(), load_table(), include_drafts=True)
    questions = build_questions(catalogue)

    pairs, failures = [], 0
    cost = 0.0
    for case in load_golden():
        try:
            decision = client.decide(build_state(case.text, [], "whatsapp", bike=case.bike or None), questions)
        except JevError as exc:
            failures += 1
            print("jev failed on %r: %s" % (case.text, exc.code))
            continue
        cost += decision.cost or 0.0
        pairs.append((case, decision))
    print("scored %d cases, %d failures, cost $%.5f\n" % (len(pairs), failures, cost))

    proposal = {}
    for question_id, target in TARGETS.items():
        if question_id not in questions:
            continue
        rows = evaluate_choice(pairs, question_id, EXPECTED[question_id], CANDIDATES)
        print("%s (target accuracy %.0f%%)" % (question_id, target * 100))
        for threshold, languages in rows.items():
            cells = ["%s %d/%d/%d" % (lang, r["correct"], r["covered"], r["n"]) for lang, r in sorted(languages.items())]
            print("  >= %.2f  %s   (correct/covered/n)" % (threshold, "  ".join(cells)))
        suggestion = suggest_choice(rows, target)
        print("  suggested: %s\n" % suggestion)
        if suggestion is not None:
            proposal[YAML_KEYS[question_id]] = suggestion

    rows = evaluate_noul(pairs, Q_WARRANTY, lambda c: c.needs_warranty_lookup, CANDIDATES)
    warranty = suggest_noul(rows, target_recall=0.95)
    print("%s suggested: %s" % (Q_WARRANTY, warranty))
    if warranty is not None:
        proposal["tools"] = {"lookup_warranty_record": warranty}

    if args.write:
        current = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8")) or {}
        current.update(proposal)
        current["calibrated_at"] = date.today().isoformat()
        current["calibration_set_size"] = len(pairs)
        THRESHOLDS_PATH.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8", newline="\n")
        print("\nwrote %s; review it in a PR" % THRESHOLDS_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`scripts/jev_probe.py`:

```python
"""One real Decisions API call, to confirm the live response shape (spec §3.1, §11).

    OPENROUTER_API_KEY=... python scripts/jev_probe.py

Saves the questions and the raw response to docs/api-shapes/jev-decisions.json.
The state is a fixed, made-up sentence, so no customer data is sent or saved.
tests/test_jev.py then parses the saved file on every run.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.decisions import CATEGORY_CRITERIA, CATEGORY_INSTRUCTIONS, Q_CATEGORY, Q_WARRANTY, WARRANTY_INSTRUCTIONS  # noqa: E402
from emotorad_ai.jev import choice, noul, parse_decision  # noqa: E402
from emotorad_ai.openrouter import DECISIONS_PATH, OpenRouterTransport  # noqa: E402

OUT = ROOT / "docs" / "api-shapes" / "jev-decisions.json"


def main() -> int:
    settings = load_settings()
    questions = {
        Q_CATEGORY: choice(CATEGORY_INSTRUCTIONS, CATEGORY_CRITERIA),
        Q_WARRANTY: noul(WARRANTY_INSTRUCTIONS, true="Asks about warranty or cost.", false="Does not."),
    }
    body = {
        "model": settings.jev_model,
        "state": {"message": "my battery is not charging, is it under warranty?"},
        "questions": {qid: q.to_dict() for qid, q in questions.items()},
    }
    response = OpenRouterTransport(base_url=settings.openrouter_base_url).post(DECISIONS_PATH, body, timeout=30.0)
    decision = parse_decision(response, questions)
    OUT.write_text(
        json.dumps({"questions": body["questions"], "response": response}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    print("parsed OK: %s" % decision.scores())
    print("saved %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Update the docs**

In `CLAUDE.md` under `## Commands`, add:

```markdown
- Model modes: `EMOTORAD_AI_MODE=offline|bedrock|openrouter`. `openrouter` turns on Jev routing (`src/emotorad_ai/decisions.py`, `graph.py`) with DeepSeek Flash for the narrow path and Haiku 4.5 for the full agent, all through `OPENROUTER_API_KEY`. It sends customer text outside AWS: **Sachin signs off before real customer traffic**. CLI: `--mode openrouter`.
- Jev: `python scripts/jev_probe.py` saves the live Decisions API shape once; `python scripts/calibrate_jev.py [--write]` proposes thresholds from the labelled set (`tests/jev_golden.py`). Both need the key and are run by a person, never CI. Thresholds live in `knowledge/_routing/thresholds.yaml`, standard responses in `knowledge/_standard/` (drafts until an SME approves them in a PR).
```

In `README.md` under `## Run it`, add after the Bedrock block:

````markdown
Through OpenRouter, with Jev choosing each turn's path (standard reply, narrow DeepSeek agent, or the full Haiku agent). This needs `OPENROUTER_API_KEY`, and sends customer text outside AWS, so it is for testing until signed off:

```bash
PYTHONPATH=src python3 -m emotorad_ai.cli --mode openrouter --channel amiigo --session sess-amiigo-test
```
````

In the spec, add a new last section:

```markdown
## 15. Implementation notes (2026-09-28 plan)

- Shared prompt blocks stay in `battery_support.py` and are imported, as `motor_support.py` already does, rather than moving to a new `agents/blocks.py`.
- `needs_service_slots` is dropped. The warranty result carries no PIN code, so the slot lookup cannot be prefetched. The narrow agent keeps `find_service_slots` as a model-requested tool.
- The retrieval goldens are imported from `tests/test_retrieval_evals.py`, not moved.
- Post-checks and finishing run inside `Runtime._run` and `Runtime._finish`, shared by every agent node, instead of as separate graph nodes. The order guarantees are the same.
- Playground wiring is deferred. The playground has its own turn loop. OpenRouter mode reaches the CLI and the API in this change.
```

- [ ] **Step 5: Run everything**

Run: `python -m unittest tests.test_calibration -v`, then the full suite.
Expected: all pass. Only the 2 known `test_video` errors. The live-shape test is skipped.

- [ ] **Step 6: Commit**

```bash
git add tests/data/jev_golden.yaml tests/jev_golden.py tests/test_calibration.py src/emotorad_ai/calibration.py scripts/calibrate_jev.py scripts/jev_probe.py CLAUDE.md README.md docs/superpowers/specs/2026-09-28-jev-routing-and-openrouter-design.md
git commit -m "Add the Jev labelled set, calibration, the shape probe and docs"
```
