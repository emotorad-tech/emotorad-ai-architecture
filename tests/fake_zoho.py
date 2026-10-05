"""A fake Zoho for the suite: an `opener` double, so no test opens a socket.

This follows the pattern of tests/test_oms.py: the client under test takes an
opener, and this one records every request and answers from a script. Bodies
come from the shapes in docs/api-shapes/zoho-*.json, so the fake answers what
the shapes say Zoho answers. tests/test_zoho_desk.py ShapeTests holds the
client to the keys those shapes carry. Part 1's scripts replace the shapes
with captures, and tests/test_zoho_scripts.py CapturedShapeTests holds what
they write to the same checks.

It can answer with any status, a JSON or raw body and headers, including HTTP
200 throttling bodies from the token endpoint and 204 with no body. It can
also raise an exception, either before any answer (socket.timeout, URLError,
ConnectionResetError) or while the body is read.
"""

import http.client
import io
import json
import os
import threading
import urllib.error
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from emotorad_ai.zoho.settings import load_zoho_settings

SHAPES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "api-shapes")
TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
DESK_HOST = "desk.zoho.in"

# Fake values only. The three credentials are marked so that a leak is plain
# in any assertion message.
REFRESH = "1000.refresh-DO-NOT-LEAK"
CLIENT_ID = "1000.CLIENTID-DO-NOT-LEAK"
CLIENT_SECRET = "client-secret-DO-NOT-LEAK"
SECRETS = (REFRESH, CLIENT_ID, CLIENT_SECRET)
ORG_ID = "60000000001"
TEST_DEPARTMENT = "4000000000001"
TEST_CONTACT = "4000000000002"
LIVE_DEPARTMENT = "4000000000003"
UNVERIFIED_CONTACT = "4000000000004"

ENV = {
    "EMOTORAD_ZOHO_REFRESH_TOKEN": REFRESH,
    "EMOTORAD_ZOHO_CLIENT_ID": CLIENT_ID,
    "EMOTORAD_ZOHO_CLIENT_SECRET": CLIENT_SECRET,
    "EMOTORAD_ZOHO_ORG_ID": ORG_ID,
    "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID": TEST_DEPARTMENT,
    "EMOTORAD_ZOHO_TEST_CONTACT_ID": TEST_CONTACT,
    "EMOTORAD_AI_ENV": "stage",
}
LIVE_ENV = dict(ENV, EMOTORAD_ZOHO_DEPARTMENT_ID=LIVE_DEPARTMENT,
                EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID=UNVERIFIED_CONTACT, EMOTORAD_ZOHO_LIVE="yes")


def zoho_settings(live: bool = False, **env_changes: str):
    """Settings from the fake environment, test mode unless `live`."""
    env = dict(LIVE_ENV if live else ENV)
    env.update(env_changes)
    settings, status = load_zoho_settings(env)
    if settings is None:
        raise AssertionError("the fake settings did not load: %s" % status)
    return settings


def _without_notes(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_notes(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_without_notes(item) for item in value]
    return value


def shape(name: str, shapes_dir: str = SHAPES) -> Any:
    """A recorded shape without its `_source` note, read fresh each time.
    `shapes_dir` is another folder of them, such as a script's captures."""
    with open(os.path.join(shapes_dir, name), encoding="utf-8") as handle:
        return _without_notes(json.load(handle))


def token_answer(n: int) -> Dict[str, Any]:
    """The recorded token answer, with a token the tests can tell apart."""
    answer = shape("zoho-token.json")["refresh"]
    answer["access_token"] = "tok-%d" % n
    return answer


def headers_of(pairs: Dict[str, str]) -> http.client.HTTPMessage:
    message = http.client.HTTPMessage()
    for name, value in pairs.items():
        message[name] = str(value)
    return message


class Answer:
    """One scripted answer: a status, a JSON or raw body and headers. It can
    also carry an error raised while the body is read."""

    def __init__(self, status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None,
                 read_error: Optional[BaseException] = None) -> None:
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        self.read_error = read_error

    def raw(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            return self.body
        return json.dumps(self.body).encode("utf-8")

    def __repr__(self) -> str:
        return "Answer(%d)" % self.status


class _Response(io.BytesIO):
    def __init__(self, status: int, raw: bytes, headers: http.client.HTTPMessage,
                 read_error: Optional[BaseException] = None) -> None:
        super().__init__(raw)
        self.status = status
        self.headers = headers
        self._read_error = read_error

    def getcode(self) -> int:
        return self.status

    def read(self, *args: Any) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return super().read(*args)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


class FakeZoho:
    """Records every request and answers from a script, first in, first out.

    With `auto_token`, the default, a request to the token endpoint gets a
    fresh token (tok-1, tok-2 and so on) and never takes a scripted answer.
    When `token_gate` is set, it runs inside each such request before the
    answer, so a test can hold one refresh open.
    """

    def __init__(self, auto_token: bool = True) -> None:
        self.auto_token = auto_token
        self.token_gate: Optional[Callable[[], None]] = None
        self.requests: List[Dict[str, Any]] = []
        self.answers: List[Any] = []
        self.tokens_issued = 0
        self._lock = threading.Lock()

    def queue(self, *answers: Any) -> "FakeZoho":
        self.answers.extend(answers)
        return self

    def __call__(self, request: Any, timeout: Optional[float] = None) -> _Response:
        parts = urllib.parse.urlsplit(request.full_url)
        record = {
            "method": request.get_method(),
            "url": request.full_url,
            "host": parts.netloc,
            "path": parts.path,
            "query": urllib.parse.parse_qs(parts.query, keep_blank_values=True),
            "headers": {name.lower(): value for name, value in request.header_items()},
            "body": request.data or b"",
            "timeout": timeout,
        }
        with self._lock:
            self.requests.append(record)
        if self.auto_token and request.full_url == TOKEN_URL:
            if self.token_gate is not None:
                self.token_gate()
            with self._lock:
                self.tokens_issued += 1
                issued = self.tokens_issued
            return _Response(200, json.dumps(token_answer(issued)).encode("utf-8"), headers_of({}))
        with self._lock:
            if not self.answers:
                raise AssertionError("FakeZoho has no answer queued for %s %s" % (record["method"], parts.path))
            answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        raw = answer.raw()
        headers = headers_of(answer.headers)
        if answer.status >= 400:
            raise urllib.error.HTTPError(request.full_url, answer.status, "fake", headers, io.BytesIO(raw))
        return _Response(answer.status, raw, headers, answer.read_error)

    @property
    def desk_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["host"] == DESK_HOST]

    @property
    def token_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["url"] == TOKEN_URL]
