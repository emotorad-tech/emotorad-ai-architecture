# Photo Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A photo showing a hazard gets the safety reply and a priority ticket on the turn it arrives, and no reply can promise a ticket that was never raised.

**Architecture:** `photo_check.py` asks a small vision model (Gemini Flash through OpenRouter, no retention) for a hazard-focused description of each photo; the API puts it on the attachment's `summary`, which the runtime's existing safety gate already scans. In `Runtime._run`, a backstop after the existing post-checks raises the safety ticket itself when a reply gives unconditional stop instructions about a hazard with no ticket raised, and replaces any other unbacked ticket claim with the hand-over. The safety gate's ticket call moves into one method both use.

**Tech Stack:** Python 3.12, FastAPI, OpenRouter (Gemini Flash), unittest.

**Spec:** `docs/superpowers/specs/2026-10-01-photo-safety-design.md`

## Global Constraints

- Nothing in this feature may stop or delay a reply beyond the photo check's 15-second timeout: every failure is logged and the turn goes on.
- The photo check uses `google/gemini-3.8-flash` through OpenRouter with `"provider": {"zdr": True, "data_collection": "deny"}` unless `EMOTORAD_OPENROUTER_ZDR` is not `1`, as the video summariser does.
- A photo's description is customer content: it is logged (`photo_check`, with text) only when `DEV_CODES` is on, exactly as `video_summary` is. Production never writes it.
- Errors are logged by exception class or stable code only, never the response or the image.
- What the model sees is unchanged: an image is still sent as an image.
- The backstop's safety ticket is the safety gate's ticket: `battery_safety`, `critical`, idempotency key `safety:<conversation_id>:<started_at>`.
- An unbacked claim is only one made while the conversation has no ticket (`state.ticket_id` is None) and none was raised this turn.
- British English, no em dashes, in new comments, texts and docs.
- Test command (whole suite): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;." python <workspace>/suite.py` (the suite minus `tests.test_video` and `tests.test_start`, Windows-only failures that pass on Linux CI).

## Review Focus

1. A later reply that mentions a ticket raised earlier ("Your ticket EM-00001 is with the team"): left alone, never replaced by the hand-over.
2. Ordinary safety advice in a reply to a harmless photo ("If you ever see smoke, stop charging it"): no ticket.
3. A photo check that is slow or failing: the message is still answered, with the photo, and the failure is logged.
4. Three photos in one message, only one hazardous: the safety reply and ticket still come on that turn.
5. A website visitor who has not verified sends a hazardous photo first: the safety reply, and no ticket (no identity), as for typed hazards today.

---

### Task 1: The photo checker

**Files:**
- Create: `src/emotorad_ai/photo_check.py`
- Test: `tests/test_photo_check.py`

**Interfaces:**
- Produces: `OpenRouterPhotoChecker(transport, model=OPENROUTER_PHOTO_MODEL, zdr=True)` with `.provider == "openrouter"` and `.check(data: bytes, mime: str) -> str` (raises `PhotoCheckError`); `photo_checker_from_env(environ=None) -> Optional[OpenRouterPhotoChecker]`; constants `OPENROUTER_PHOTO_MODEL`, `TIMEOUT_SECONDS = 15`, `INLINE_LIMIT`, `PROMPT`.

- [ ] **Step 1: Write the failing tests**

```python
"""The safety look at every photo (photo_check.py, spec 2026-10-01)."""

import base64
import unittest

from emotorad_ai.openrouter import CHAT_PATH, OpenRouterError
from emotorad_ai.photo_check import (INLINE_LIMIT, OPENROUTER_PHOTO_MODEL, PROMPT, TIMEOUT_SECONDS,
                                     OpenRouterPhotoChecker, PhotoCheckError, photo_checker_from_env)


class FakeTransport:
    def __init__(self, text="A battery pack on a table. No smoke visible.", error=None):
        self.text, self.error, self.posts = text, error, []

    def post(self, path, body, timeout=None):
        self.posts.append({"path": path, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return {"model": OPENROUTER_PHOTO_MODEL,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": self.text}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 60}}


class CheckerTests(unittest.TestCase):
    def test_the_photo_goes_inline_once_with_the_prompt_and_no_retention(self):
        transport = FakeTransport()
        text = OpenRouterPhotoChecker(transport).check(b"\xff\xd8jpeg", "image/jpeg")
        self.assertEqual(text, "A battery pack on a table. No smoke visible.")
        [post] = transport.posts
        self.assertEqual((post["path"], post["timeout"]), (CHAT_PATH, TIMEOUT_SECONDS))
        body = post["body"]
        self.assertEqual(body["model"], "google/gemini-3.8-flash")
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": PROMPT})
        self.assertEqual(content[1]["image_url"]["url"],
                         "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode())

    def test_the_prompt_asks_about_every_hazard_and_how_to_say_none(self):
        for word in ("smoke", "flames", "scorch", "swelling", "melting", "leaking", "sparks", "no smoke visible"):
            self.assertIn(word, PROMPT)

    def test_a_photo_too_large_is_refused_before_any_request(self):
        transport = FakeTransport()
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(transport).check(b"x" * (INLINE_LIMIT + 1), "image/jpeg")
        self.assertEqual(str(raised.exception), "too_large")
        self.assertEqual(transport.posts, [])

    def test_a_provider_error_gives_its_code_only(self):
        error = OpenRouterError("rate_limited", "429 with the request echoed", retryable=True)
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(FakeTransport(error=error)).check(b"jpeg", "image/jpeg")
        self.assertEqual(str(raised.exception), "rate_limited")

    def test_an_empty_answer_is_an_error(self):
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(FakeTransport(text="  ")).check(b"jpeg", "image/jpeg")
        self.assertEqual(str(raised.exception), "empty")


class FromEnvTests(unittest.TestCase):
    def test_no_key_no_checker(self):
        self.assertIsNone(photo_checker_from_env({}))

    def test_a_key_gives_the_openrouter_checker(self):
        checker = photo_checker_from_env({"OPENROUTER_API_KEY": "sk-or-test"})
        self.assertEqual(checker.provider, "openrouter")
        self.assertTrue(checker.zdr)
```

Check `OpenRouterError`'s constructor in `src/emotorad_ai/openrouter.py` before running and match its arguments in the test.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_photo_check`
Expected: ERROR, `No module named 'emotorad_ai.photo_check'`

- [ ] **Step 3: Write the module**

```python
"""A safety look at every photo (spec 2026-10-01, photo safety).

The safety gate scans what a customer types and what a video's description
says. A photo was seen only by the model, so a photo of a burning pack was
handled by whatever the model chose to write (staging, 2026-10-01). This
describes each photo once, on arrival, in words the gate can scan: every
hazard stated, and every absent one written as "no <thing> visible" so the
gate's negation handling reads it as absent. The model still gets the photo.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Mapping, Optional

OPENROUTER_PHOTO_MODEL = "google/gemini-3.8-flash"
TIMEOUT_SECONDS = 15
# Photos are capped at 4 MB on the way in (attachments.py); this is a
# backstop for whatever else calls the checker.
INLINE_LIMIT = 8 * 1024 * 1024

PROMPT = (
    "You are checking a photo a customer sent to an electric cycle company's support chat, for "
    "safety only. In 2 to 4 plain sentences, say what is visible of the bike, battery, charger or "
    "wiring. Then say, for each of these, whether it is visible: smoke, flames, scorch or burn "
    "marks, swelling or bulging, melting, leaking fluid, sparks. Write each one that is not visible "
    "as \"no <thing> visible\", for example \"no smoke visible\". Describe only what is in the photo; "
    "do not guess at causes or give advice."
)


class PhotoCheckError(Exception):
    """A stable code only: a provider message can echo the request."""


class OpenRouterPhotoChecker:
    provider = "openrouter"

    def __init__(self, transport: Any, model: str = OPENROUTER_PHOTO_MODEL, zdr: bool = True) -> None:
        self.model = model
        self.zdr = zdr
        self._transport = transport

    def check(self, data: bytes, mime: str) -> str:
        from .llm import from_openai_response
        from .openrouter import CHAT_PATH, OpenRouterError

        if len(data) > INLINE_LIMIT:
            raise PhotoCheckError("too_large")
        url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            "usage": {"include": True},
        }
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        try:
            response = from_openai_response(self._transport.post(CHAT_PATH, body, timeout=TIMEOUT_SECONDS))
        except OpenRouterError as exc:
            raise PhotoCheckError(exc.code) from None
        text = (response.text or "").strip()
        if not text:
            raise PhotoCheckError("empty")
        return text


def photo_checker_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OpenRouterPhotoChecker]:
    """OpenRouter when its key is set, else None: offline and local runs keep
    today's behaviour."""
    from .openrouter import API_KEY_ENV, OpenRouterTransport

    env = os.environ if environ is None else environ
    key = (env.get(API_KEY_ENV) or "").strip()
    if not key:
        return None
    transport = OpenRouterTransport(
        api_key=key,
        base_url=env.get("EMOTORAD_OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
        timeout=TIMEOUT_SECONDS,
    )
    return OpenRouterPhotoChecker(transport, zdr=env.get("EMOTORAD_OPENROUTER_ZDR", "1") == "1")
```

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_photo_check`
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/photo_check.py tests/test_photo_check.py
git commit -m "Photo check: a hazard-focused description of each photo, through OpenRouter"
```

---

### Task 2: What counts as a safety reply and a ticket claim

**Files:**
- Modify: `src/emotorad_ai/guardrails.py`
- Test: `tests/test_reply_claims.py`

**Interfaces:**
- Produces: `gives_safety_stop(reply: str) -> List[str]` (the hazard labels, or `[]`); `claims_ticket(reply: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
"""What the backstop treats as a safety reply and as a ticket claim (spec 2026-10-01)."""

import unittest

from emotorad_ai.guardrails import claims_ticket, gives_safety_stop

STAGING_REPLY = (
    "Stop using and stop charging the bike now. The battery is swelling and smoking. This is a safety "
    "hazard.\n\nDo not try to charge it, ride it, or keep it indoors. Move it outside to a safe open "
    "space away from people and flammable things, and leave it there.\n\nI'm raising an urgent support "
    "ticket for you right now."
)


class SafetyReplyTests(unittest.TestCase):
    def test_the_staging_reply_is_one(self):
        self.assertIn("swelling", gives_safety_stop(STAGING_REPLY))

    def test_advice_is_not(self):
        for reply in ("If you ever see smoke, stop charging it.",
                      "In case the pack swells, stop using it and call us.",
                      "When it cools, stop charging at 80%. It is not swelling.",
                      "Stop using the throttle for a minute and try again.",
                      ""):
            self.assertEqual(gives_safety_stop(reply), [], reply)


class TicketClaimTests(unittest.TestCase):
    def test_claims(self):
        for reply in ("I'm raising an urgent support ticket for you right now.",
                      "Ticket EM-00001 has been raised.",
                      "I've opened a ticket for the team.",
                      "I have logged a support ticket.",
                      "I'll raise a ticket right away."):
            self.assertTrue(claims_ticket(reply), reply)

    def test_offers_are_not_claims(self):
        for reply in ("I can raise a ticket if you'd like.",
                      "Shall I raise a ticket?",
                      "Would you like me to raise a support ticket?",
                      "If the light stays off, I'll raise a ticket.",
                      ""):
            self.assertFalse(claims_ticket(reply), reply)
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_reply_claims`
Expected: ERROR, `cannot import name 'claims_ticket'`

- [ ] **Step 3: Write the two checks**

At the end of `src/emotorad_ai/guardrails.py`:

```python
# --- the backstop's two questions (spec 2026-10-01, photo safety) ------------
# A reply that gives safety stop instructions about a hazard must come with the
# safety ticket; a reply that says a ticket exists must have one. The model
# wrote "I'm raising an urgent support ticket for you right now" about a
# smoking battery and raised nothing (staging, 2026-10-01).

_STOP_INSTRUCTION = re.compile(
    r"\bstop\s+(?:using|charging|riding)\b|\b(?:do\s+not|don'?t)\s+charge\b"
    r"|\b(?:move|take|keep|put)\s+(?:it|the\s+(?:bike|battery|pack))\s+(?:outside|outdoors)\b"
    r"|\baway\s+from\s+(?:anything\s+)?(?:flammable|people)\b",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(r"\b(?:if|in\s+case|should|whenever|when|unless)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_TICKET_CLAIM = re.compile(
    r"\bI(?:'ve|\s+have)\s+(?:just\s+)?(?:raised|opened|created|logged)\b[^.!?\n]{0,40}\bticket\b"
    r"|\bI(?:'m|\s+am)\s+(?:now\s+)?(?:raising|opening|creating|logging)\b[^.!?\n]{0,40}\bticket\b"
    r"|\bticket\b[^.!?\n]{0,40}\b(?:has\s+been|is\s+now|was)\s+(?:raised|created|opened|logged)\b"
    r"|^(?![^.!?\n]*\bif\b)[^.!?\n]*\bI(?:'ll|\s+will)\s+(?:raise|open|create|log)\b[^.!?\n]{0,40}\bticket\b"
    r"[^.!?\n]{0,30}\b(?:now|right\s+away|immediately)\b",
    re.IGNORECASE | re.MULTILINE,
)


def gives_safety_stop(reply: str) -> List[str]:
    """The hazard labels when `reply` names a hazard and gives a stop
    instruction that is not conditional ("If you ever see smoke, stop
    charging it" is advice); [] otherwise."""
    hazards = check_safety(reply or "").matched
    if not hazards:
        return []
    for sentence in _SENTENCE.findall(reply or ""):
        found = _STOP_INSTRUCTION.search(sentence)
        if found and not _CONDITIONAL.search(sentence[:found.start()]):
            return hazards
    return []


def claims_ticket(reply: str) -> bool:
    """Whether `reply` says a ticket was or is being raised. An offer ("I can
    raise a ticket", "Shall I raise a ticket?") is not a claim."""
    return bool(_TICKET_CLAIM.search(reply or ""))
```

(`List` and `re` are already imported in `guardrails.py`; check, and add if not.)

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_reply_claims`
Expected: `Ran 4 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/guardrails.py tests/test_reply_claims.py
git commit -m "Guardrails: what counts as a safety reply and as a ticket claim"
```

---

### Task 3: Every photo checked on arrival (API)

**Files:**
- Modify: `src/emotorad_ai/api.py` (import, `PHOTO_CHECKER`, `_check_photo`, `_check_uploaded_photo`, the inline and uploaded attachment paths, `health`)
- Modify: `tests/test_api_health.py` (pinned dict gains `photo_check`)
- Test: `tests/test_api_photo_check.py`

**Interfaces:**
- Consumes: `photo_check.photo_checker_from_env`, `.check(data, mime)` (Task 1).
- Produces: `api.PHOTO_CHECKER`; each image attachment dict gains `"summary"` (the checker's text) when the check succeeds; `/health["photo_check"]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Every photo gets a safety look on arrival (spec 2026-10-01)."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_api_media_persistence import _Store, fresh_api, jpeg_data_url

HAZARD = "The battery casing is swollen and white smoke rises from it."
CLEAR = ("A battery pack on a table. No smoke visible, no flames visible, no scorch or burn marks visible, "
         "no swelling or bulging visible, no melting visible, no leaking fluid visible, no sparks visible.")


class FakeChecker:
    provider = "openrouter"

    def __init__(self, notes=(HAZARD,), error=None):
        self.notes, self.error, self.seen = list(notes), error, []

    def check(self, data, mime):
        self.seen.append((len(data), mime))
        if self.error is not None:
            raise self.error
        return self.notes[min(len(self.seen), len(self.notes)) - 1]


class PhotoCheckTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def post(self, checker, attachments, text="this is what i see", session="sess-ananya"):
        body = {"conversation_id": "c1", "text": text, "attachments": attachments}
        if session:
            body["session_token"] = session
        with mock.patch.object(self.api, "PHOTO_CHECKER", checker):
            r = self.client.post("/message", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_hazard_photo_gets_the_safety_reply_and_a_ticket_on_that_turn(self):
        reply = self.post(FakeChecker(), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertTrue(reply["ticket_id"])

    def test_a_clear_photo_goes_on_as_usual(self):
        reply = self.post(FakeChecker(notes=(CLEAR,)), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")

    def test_one_hazard_among_three_photos_is_enough(self):
        photo = {"kind": "image", "url": jpeg_data_url()}
        reply = self.post(FakeChecker(notes=(CLEAR, HAZARD, CLEAR)), [dict(photo), dict(photo), dict(photo)])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")

    def test_a_failing_check_leaves_the_photo_and_is_logged(self):
        reply = self.post(FakeChecker(error=TimeoutError("slow")), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")
        (event,) = [e for e in self.api.log.events if e["event"] == "photo_check_skipped"]
        self.assertEqual(event["error"], "TimeoutError")

    def test_an_uploaded_photo_is_checked_too(self):
        slot = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1",
                                                  "tree": "customers", "mime_type": "image/jpeg",
                                                  "size_bytes": 100}).json()
        self.store.objects[slot["key"]] = b"x" * 100
        self.store.meta[slot["key"]] = {"size": 100, "mime": "image/jpeg"}
        checker = FakeChecker()
        reply = self.post(checker, [{"upload_id": slot["upload_id"]}], text="")
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertEqual(checker.seen, [(100, "image/jpeg")])

    def test_an_unverified_visitor_gets_the_safety_reply_without_a_ticket(self):
        reply = self.post(FakeChecker(), [{"kind": "image", "url": jpeg_data_url()}], session=None)
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertFalse(reply["ticket_id"])

    def test_the_description_is_logged_only_with_dev_codes(self):
        with mock.patch.object(self.api, "DEV_CODES", False):
            self.post(FakeChecker(notes=(CLEAR,)), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertEqual([e for e in self.api.log.events if e["event"] == "photo_check"], [])


class HealthTests(unittest.TestCase):
    def test_health_says_whether_photos_are_checked(self):
        api = fresh_api(None)
        self.assertEqual(api.health()["photo_check"], "off")
        with mock.patch.object(api, "PHOTO_CHECKER", FakeChecker()):
            self.assertEqual(api.health()["photo_check"], "openrouter")
```

In `tests/test_api_health.py`, `test_offline_reports_no_secret`: add `"photo_check": "off"` to the expected dict (its environment already blanks `OPENROUTER_API_KEY`).

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_photo_check tests.test_api_health`
Expected: FAIL / ERROR: `api` has no `PHOTO_CHECKER`; no `photo_check` in `/health`.

- [ ] **Step 3: Wire it**

In `src/emotorad_ai/api.py`, import `from . import photo_check` beside the other local imports. After `VIDEO_SUMMARISER = summariser_from_env()`:

```python
# The safety look at every photo (photo_check.py), or None without the
# OpenRouter key: photos then go on as before.
PHOTO_CHECKER = photo_check.photo_checker_from_env()
```

After `_summarise_video`:

```python
def _check_photo(data: bytes, mime: str, conversation_id: str) -> Optional[str]:
    """A photo's safety description (photo_check.py), or None. Never stops the
    turn: a failure is logged by class and the photo goes on as before."""
    if PHOTO_CHECKER is None:
        return None
    try:
        note = PHOTO_CHECKER.check(data, mime)
    except Exception as exc:
        log.emit("photo_check_skipped", conversation_id, error=type(exc).__name__)
        return None
    # Customer content, as a video's description is: only on a staging box
    # with dev codes on.
    if DEV_CODES:
        log.emit("photo_check", conversation_id, chars=len(note), text=note)
    return note


def _check_uploaded_photo(key: str, mime: str, conversation_id: str) -> Optional[str]:
    if PHOTO_CHECKER is None:
        return None
    try:
        data = MEDIA_STORE.get_bytes(key)
    except StorageError as exc:
        log.emit("photo_check_skipped", conversation_id, error=type(exc).__name__)
        return None
    return _check_photo(data, mime, conversation_id)
```

In the attachment helper, before `stored: Dict[int, Dict[str, Any]] = {}`:

```python
    # Every inline photo gets its safety look first (photo_check.py), whether
    # or not it can be stored: the description becomes the attachment's
    # summary, which the safety gate scans.
    photo_notes: Dict[int, str] = {}
    for raw_item, item in zip(inline, validated):
        if item["media_type"].startswith("image/"):
            note = _check_photo(base64.b64decode(item["data"]), item["media_type"], conversation_id)
            if note:
                photo_notes[id(raw_item)] = note
```

In the uploads loop, after the `if claimed.kind == "videos":` block (same indentation):

```python
                if claimed.kind == "images":
                    note = _check_uploaded_photo(claimed.key, claimed.mime, conversation_id)
                    if note is not None:
                        claims[upload_id]["summary"] = note
```

Replace the helper's final `return [...]`:

```python
    attachments = []
    for item in items:
        if item.get("upload_id"):
            attachments.append(claims[item["upload_id"]])
            continue
        base = stored.get(id(item), item)
        note = photo_notes.get(id(item))
        # A copy: the customer's own item is never changed.
        attachments.append(dict(base, summary=note) if note else base)
    return attachments
```

In `health()`, after the `"video_summary"` entry:

```python
        "photo_check": PHOTO_CHECKER.provider if PHOTO_CHECKER is not None else "off",
```

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_photo_check tests.test_api_health tests.test_api_media_persistence`
Expected: OK.

- [ ] **Step 5: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/api.py tests/test_api_photo_check.py tests/test_api_health.py
git commit -m "API: every photo gets a safety look on arrival; a hazard is a safety turn"
```

---

### Task 4: The backstop (runtime)

**Files:**
- Modify: `src/emotorad_ai/runtime.py` (`_raise_safety_ticket` from `_handle_safety`; "photo or video"; `_safety_backstop`; the backstop in `_run`)
- Test: `tests/test_safety_backstop.py`

**Interfaces:**
- Consumes: `gives_safety_stop`, `claims_ticket` (Task 2).
- Produces: `Runtime._raise_safety_ticket(message, state, resolved, description) -> Optional[str]`; handled_by `guardrail:ticket_promise_unbacked`; events `safety_backstop`, `safety_backstop_failed`, `ticket_promise_unbacked`.

- [ ] **Step 1: Write the failing tests**

```python
"""The backstop after every reply (spec 2026-10-01, photo safety)."""

import unittest
from unittest import mock

from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.llm import call_tool, say
from emotorad_ai.runtime import HANDOVER_TEXT
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET
from tests.test_reply_claims import STAGING_REPLY
from tests.test_verify_first import ONE_BIKE, Chat

RIDER = Identity(strength=VERIFIED, phone=ONE_BIKE, em_aid="aid-1")
TICKET = {"category": "battery_safety", "severity": "critical", "description": "Smoke from the pack.",
          "idempotency_key": "model-1"}


def chat_saying(*replies):
    return Chat(replies=list(replies))


def ask(chat, text="my battery isn't charging"):
    return chat.say(text, identity=RIDER)


class SafetyBackstopTests(unittest.TestCase):
    def test_a_safety_reply_with_no_ticket_gets_one_on_that_turn(self):
        chat = chat_saying(say(STAGING_REPLY))
        reply = ask(chat)
        tickets = list(chat.registry.tickets.tickets.values())
        self.assertEqual(len(tickets), 1)
        self.assertEqual((tickets[0]["category"], tickets[0]["severity"]), ("battery_safety", "critical"))
        self.assertIn("I have raised this as a priority safety case, reference %s." % reply.ticket_id, reply.text)
        self.assertTrue(reply.escalated)
        self.assertEqual([e["event"] for e in chat.log.events].count("safety_backstop"), 0)  # logged as a guardrail
        self.assertTrue(any(e.get("guardrail") == "safety_backstop" or e.get("name") == "safety_backstop"
                            for e in chat.log.events))

    def test_a_ticket_raised_in_the_turn_is_not_doubled(self):
        chat = chat_saying(call_tool(CREATE_SUPPORT_TICKET, TICKET, "t1"), say(STAGING_REPLY))
        ask(chat)
        self.assertEqual(len(chat.registry.tickets.tickets), 1)

    def test_a_failing_backstop_leaves_the_reply_and_is_logged(self):
        chat = chat_saying(say(STAGING_REPLY))
        with mock.patch.object(chat.runtime, "_raise_safety_ticket", side_effect=RuntimeError("down")):
            reply = ask(chat)
        self.assertEqual(reply.text.count("priority safety case"), 0)
        (event,) = [e for e in chat.log.events if e["event"] == "safety_backstop_failed"]
        self.assertEqual(event["error"], "RuntimeError")


class TicketClaimBackstopTests(unittest.TestCase):
    def test_an_unbacked_claim_is_handed_to_a_person(self):
        chat = chat_saying(say("I've raised a support ticket for your charger; the team will call you."))
        reply = ask(chat)
        self.assertEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")
        self.assertTrue(reply.text.startswith(HANDOVER_TEXT))
        self.assertTrue(reply.escalated)

    def test_an_offer_is_left_alone(self):
        chat = chat_saying(say("Is the charger light on? I can raise a ticket if you'd like."))
        reply = ask(chat)
        self.assertNotEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")

    def test_a_later_mention_of_a_real_ticket_is_left_alone(self):
        chat = chat_saying(call_tool(CREATE_SUPPORT_TICKET, TICKET, "t1"), say("Ticket raised."),
                           say("Your ticket has been raised and the team will call you today."))
        first = ask(chat)
        self.assertTrue(first.ticket_id)
        later = ask(chat, "ok, when will they call?")
        self.assertNotEqual(later.handled_by, "guardrail:ticket_promise_unbacked")
```

Before running, read `EventLog.guardrail` in `src/emotorad_ai/observability.py` and change the two `safety_backstop` assertions in the first test to the exact shape it writes (event name and field holding the guardrail's name).

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_safety_backstop`
Expected: FAIL: no ticket raised; `handled_by` is the agent's.

- [ ] **Step 3: Move the safety ticket call into one method**

In `Runtime._handle_safety`, everything from `arguments: Dict[str, Any] = {` to `ticket_id = envelope["data"]["ticket_id"]` moves into:

```python
    def _raise_safety_ticket(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity, description: str
    ) -> Optional[str]:
        """The priority safety ticket, one per conversation run: the safety
        gate's, and the backstop's when a reply gave safety instructions
        without one (spec 2026-10-01). The ticket id, or None."""
        arguments: Dict[str, Any] = {
            "category": "battery_safety",
            "severity": "critical",
            "description": description,
            "idempotency_key": "safety:%s:%s" % (message.conversation_id, state.started_at or ""),
        }
        # (the existing comments, the chosen-bike handling, the registry call
        #  with run_without_idempotency=True, and the tool_request/tool_call
        #  logging, unchanged)
        ...
        return None if is_error(envelope) else envelope["data"]["ticket_id"]
```

and `_handle_safety` becomes, inside `if resolved.identity.phone:`, the description lines followed by `ticket_id = self._raise_safety_ticket(message, state, resolved, description)`. In the evidence line, "Seen in the customer's video: %s" becomes "Seen in the customer's photo or video: %s"; update any test that pins the old wording.

- [ ] **Step 4: The backstop**

New method after `_handle_safety`:

```python
    def _safety_backstop(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState, turn: Any,
        hazards: List[str],
    ) -> Any:
        """A reply gave safety stop instructions about a hazard and no ticket
        was raised: raise the safety ticket here and add its reference. The
        model wrote "I'm raising an urgent support ticket" about a smoking
        battery and raised nothing (staging, 2026-10-01). Never stops the
        reply: a failure is logged and the reply goes as written."""
        if not resolved.identity.phone:
            return turn
        description = (
            "Automatic safety escalation: the assistant gave safety instructions without raising a ticket. "
            "Customer wrote: %s. Assistant replied: %s. Matched safety indicators: %s."
            % (message.message_text or "(a photo or video, no text)", turn.text, ", ".join(hazards))
        )
        try:
            ticket_id = self._raise_safety_ticket(message, state, resolved, description)
        except Exception as exc:
            self.log.emit("safety_backstop_failed", message.conversation_id, error=type(exc).__name__)
            return turn
        if not ticket_id:
            self.log.emit("safety_backstop_failed", message.conversation_id, error="no_ticket")
            return turn
        self.log.guardrail(message.conversation_id, "safety_backstop", hazards)
        self.log.escalation(message.conversation_id, "safety_backstop", ticket_id)
        text = turn.text or ""
        if ticket_id not in text:
            text = text.rstrip() + "\n\nI have raised this as a priority safety case, reference %s." % ticket_id
            replace_turn_text(state.history, text)
        return replace(turn, text=text, ticket_id=ticket_id, escalate=True)
```

In `_run`, immediately before the final `return Reply(`:

```python
        # The backstop (spec 2026-10-01): a reply that gives safety stop
        # instructions must come with the safety ticket, and a reply that says
        # a ticket exists must have one.
        raised = any(call.get("tool") == CREATE_SUPPORT_TICKET and not is_error(call.get("result") or {})
                     for call in turn.tool_calls)
        if not raised:
            hazards = gives_safety_stop(turn.text)
            if hazards:
                turn = self._safety_backstop(message, resolved, state, turn, hazards)
            elif state.ticket_id is None and claims_ticket(turn.text):
                self.log.guardrail(message.conversation_id, "ticket_promise_unbacked",
                                   {"suppressed_text": turn.text})
                self.log.escalation(message.conversation_id, "ticket_promise_unbacked", turn.ticket_id)
                return self._finish(
                    message, state, HANDOVER_TEXT + self._already_done(turn), "guardrail:ticket_promise_unbacked",
                    escalated=True, ticket_id=turn.ticket_id,
                    metadata={"suppressed_text": turn.text}, already_in_history=True,
                )
```

Import `claims_ticket` and `gives_safety_stop` beside the other `guardrails` imports. If `_run` does not receive `resolved`, pass it through from its callers (it is in their scope) and say so in a ruling.

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_safety_backstop tests.test_reply_claims`
Expected: OK.

- [ ] **Step 6: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK. A test whose scripted reply happens to claim a ticket or give stop instructions without a ticket call now meets the backstop: read it, and update it only if the backstop's answer is the right one for that reply; record each such change as a ruling.

```bash
git add src/emotorad_ai/runtime.py tests/test_safety_backstop.py
git commit -m "Runtime: the backstop raises the safety ticket a safety reply promised, and hands over an unbacked claim"
```
