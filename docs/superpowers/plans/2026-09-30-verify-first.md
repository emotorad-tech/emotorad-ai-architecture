# Verify First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An anonymous customer on the web chat gives their mobile number, types a one-time code and picks their bike from a list with full frame numbers, all before triage or any model runs; and the guide-picture catalogue keeps only the two useful photos.

**Architecture:** A new fixed graph node, `verify_gate`, between `handoff_gate` and `persona_route`, driven by a `VerifyFirst` class in a new module `src/emotorad_ai/verify_first.py`. It calls the existing verification tools through the registry (so the comparison stays in code), keeps its progress in two new `ConversationState` fields, and on success re-hydrates the identity and hands the bike choice to triage. `Runtime(verify_first=True)` turns it on; only the web chat API sets it.

**Tech Stack:** Python 3.12, LangGraph, FastAPI, unittest, mongomock (not needed here).

**Spec:** `docs/superpowers/specs/2026-09-30-verify-first-design.md`

## Global Constraints

- British English in every customer-facing string, comment and doc. No em dashes (`—`) in anything added or edited.
- Never push, never open a PR. Commit locally after each task.
- Never run anything against the real MongoDB Atlas cluster or the real S3 bucket. Never make a paid OpenRouter call. Tests use `ScriptedClaude`, in-memory stores and fakes.
- The model never compares a code: `verify_identity`'s `==` does. The step calls the tools through `ToolRegistry.call`.
- No model call while the step is answering. Tests assert `llm.requests == []` for those turns.
- Every step outcome is logged as one `verify_first` event with `outcome` and `step`, never the number or the code.
- `Runtime(verify_first=...)` defaults to `False`; every existing caller and test keeps today's behaviour.
- The full suite command (run from the repo root, Git Bash):
  `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET PYTHONPATH="src;." python -m unittest discover -s tests -t .`
- One test module: `PYTHONPATH="src;." python -m unittest tests.test_verify_first -v`
- The Bash tool mangles backslashes inside heredocs. Edit files with the Edit and Write tools, not `cat <<EOF`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A number read out in groups (`+91 970 000 0010`, `97000-00010`) or inside Hindi text (`मेरा नंबर 9700000010 है`) is accepted as the number. Tested in Task 2 (parsing) and Task 5 (flow).
2. A six-digit code typed while the bot is still waiting for the number (a stale code) is not taken as a number or an order number: the bot asks for the number again. Tested in Task 5.
3. Every code sent or tried by the step is logged as a `tool_call` event, so a save conflict never runs the turn again and sends a second code (`Runtime._side_effects_since`). Tested in Task 5.
4. A verified session that expires mid-conversation (12 hours) sends the person back through the step from the top, even though the conversation is already routed with a bike selected. Tested in Task 5.
5. A conversation left in `awaiting_bike_selection` whose bike lookup then fails has no list to choose from: triage must not ask "which of 0 bikes" but carry on to the issue. Tested in Task 3.

---

### Task 1: Building blocks (redaction, resend, mock sender, test order numbers)

**Files:**
- Modify: `src/emotorad_ai/observability.py:24` (`_PHONE`)
- Modify: `src/emotorad_ai/tools/verification.py` (`pending_phone`, resend fallback, `MockOtpSender`, `apply_proven_phone`)
- Modify: `src/emotorad_ai/tools/mocks.py` (`build_registry(send_code=...)`)
- Modify: `src/emotorad_ai/tools/fixtures.py` (`ORDER_CODES`, `find_account_by_order_code`)
- Test: `tests/test_verify_first_parts.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `VerificationStore.pending_phone(conversation_id: str) -> Optional[str]`, the `+91…` number an unverified code was last issued to.
  - `request_identity_verification` with no `phone` argument sends to the order-code candidate, else to `pending_phone`.
  - `MockOtpSender()`: callable `(phone: str, code: str) -> None`, attribute `sent: List[str]` of masked numbers; logs `otp_sent to <masked> (mock: no SMS sent)` at INFO on logger `emotorad_ai.tools.verification`.
  - `apply_proven_phone(message: InboundMessage, phone: Optional[str]) -> InboundMessage`.
  - `build_registry(..., send_code: Optional[Callable[[str, str], None]] = None)`.
  - `fixtures.ORDER_CODES: Dict[str, str]`, `fixtures.find_account_by_order_code(code: str) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_first_parts.py`:

```python
"""The pieces the verify-first step is built from (spec 2026-09-30).

Each is small and has a use outside the step too: the transcript and the log
hide a number however it was typed, a code can be sent again to the number it
went to, the code sender is a stand-in that says what it did, and the order
number fallback can be tried without the OMS key.
"""

import itertools
import unittest

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.observability import redact_pii
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.tools.verification import (
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    MockOtpSender,
    VerificationStore,
    apply_proven_phone,
)


class RedactionTests(unittest.TestCase):
    def test_a_number_typed_in_groups_or_with_a_leading_zero_is_hidden(self):
        for typed in ("97000 00010", "97000-00010", "09700000010", "+91 97000-00010", "+91-9700000010"):
            self.assertNotIn("00010", redact_pii("call me on " + typed), typed)

    def test_the_forms_already_hidden_still_are(self):
        self.assertEqual(redact_pii("call me on 9876500000"), "call me on [phone]")
        self.assertEqual(redact_pii("+919876500000"), "[phone]")

    def test_other_numbers_are_left_alone(self):
        for text in ("1500 km and the range has dropped by half", "it shows E-06", "48V 14.4Ah removable",
                     "frame EMXP2026001234"):
            self.assertEqual(redact_pii(text), text)


def registry_with(store, codes=("111111", "222222", "333333")):
    # A bare registry (build_registry would register the same tools first, and
    # a second registration raises), with a fixed sequence of codes so a resend
    # can be told apart from the first.
    from emotorad_ai.tools.registry import ToolRegistry
    from emotorad_ai.tools.verification import register_verification_tools

    registry = ToolRegistry()
    register_verification_tools(registry, store, code_factory=itertools.cycle(codes).__next__)
    return registry


class ResendTests(unittest.TestCase):
    def setUp(self):
        self.store = VerificationStore()
        self.registry = registry_with(self.store)
        self.ctx = ToolContext(conversation_id="c1")

    def call(self, name, arguments):
        return self.registry.call(name, arguments, self.ctx)

    def test_a_code_can_be_sent_again_to_the_number_it_went_to(self):
        self.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "9700000010"})
        again = self.call(REQUEST_IDENTITY_VERIFICATION, {})
        self.assertEqual(again["data"]["phone_masked"], "•••••••010")
        self.assertEqual(self.store.pending_code("c1"), "222222")

    def test_with_nothing_pending_it_still_refuses(self):
        self.assertEqual(self.call(REQUEST_IDENTITY_VERIFICATION, {})["error"]["code"], "no_number_on_file")

    def test_a_proved_number_is_not_pending_any_more(self):
        self.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "9700000010"})
        self.call(VERIFY_IDENTITY, {"code": "111111"})
        self.assertIsNone(self.store.pending_phone("c1"))
        self.assertEqual(self.call(REQUEST_IDENTITY_VERIFICATION, {})["error"]["code"], "no_number_on_file")

    def test_the_pending_number_is_the_full_one_for_the_store_only(self):
        self.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "97000 00010"})
        self.assertEqual(self.store.pending_phone("c1"), "+919700000010")


class MockOtpSenderTests(unittest.TestCase):
    def test_it_logs_the_masked_number_and_never_the_code(self):
        sender = MockOtpSender()
        with self.assertLogs("emotorad_ai.tools.verification", level="INFO") as logs:
            sender("9700000010", "482913")
        said = "\n".join(logs.output)
        self.assertIn("•••••••010", said)
        self.assertNotIn("482913", said)
        self.assertNotIn("9700000010", said)
        self.assertEqual(sender.sent, ["•••••••010"])

    def test_the_registry_calls_it_when_a_code_is_sent(self):
        sender = MockOtpSender()
        registry = build_registry(verification=VerificationStore(), send_code=sender)
        with self.assertLogs("emotorad_ai.tools.verification", level="INFO"):
            registry.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "9700000010"}, ToolContext(conversation_id="c1"))
        self.assertEqual(sender.sent, ["•••••••010"])


def anonymous(phone=None):
    return InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="hi",
                          identity=Identity(strength=ANONYMOUS, em_aid="aid-1", phone=phone))


class ApplyProvenPhoneTests(unittest.TestCase):
    def test_it_opens_an_anonymous_identity(self):
        opened = apply_proven_phone(anonymous(), "+919700000010")
        self.assertEqual(opened.identity.strength, VERIFIED)
        self.assertEqual(opened.identity.phone, "+919700000010")

    def test_it_never_overwrites_a_phone_already_there(self):
        message = anonymous(phone="+919876543210")
        self.assertIs(apply_proven_phone(message, "+919700000010"), message)

    def test_no_proof_changes_nothing(self):
        message = anonymous()
        self.assertIs(apply_proven_phone(message, None), message)


class FixtureOrderCodesTests(unittest.TestCase):
    def test_an_order_and_an_invoice_number_find_the_test_rider(self):
        self.assertEqual(fixtures.find_account_by_order_code("EMO-100234"), fixtures.PHONE_AMIIGO_TEST_RIDER)
        self.assertEqual(fixtures.find_account_by_order_code(" inv-2026-0042 "), fixtures.PHONE_AMIIGO_TEST_RIDER)

    def test_an_unknown_number_finds_nobody(self):
        self.assertIsNone(fixtures.find_account_by_order_code("EMO-999999"))
        self.assertIsNone(fixtures.find_account_by_order_code(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first_parts -v`
Expected: ImportError for `MockOtpSender` / `apply_proven_phone`.

- [ ] **Step 3: Widen the phone pattern**

In `src/emotorad_ai/observability.py`, replace line 24:

```python
_PHONE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
```

with:

```python
# A leading 0 (the trunk prefix), and one space or dash in the middle, are how
# people type a number they are reading out: "97000 00010". The verify-first
# step accepts those forms, so the log and the transcript must hide them too.
_PHONE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
```

- [ ] **Step 4: Resend, the mock sender and `apply_proven_phone` in `tools/verification.py`**

Add `import logging` to the imports, `List` to the `typing` import, and after the imports:

```python
_logger = logging.getLogger(__name__)
```

Add to `VerificationStore`, after `pending_code`:

```python
    def pending_phone(self, conversation_id: str) -> Optional[str]:
        """The number an unproved code was last issued to, for a resend.

        Kept after the code expires (a resend is most needed then), until the
        store sweeps the entry on a later issue. Never for the model: the
        request tool uses it and returns only the masked form.
        """
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is None or pending.verified:
                return None
            return pending.phone
```

In `request_identity_verification`, replace:

```python
        if not phone:
            # Recovered from an order code and deliberately never shown to the
            # model; sending to it is the only thing the model may do with it.
            phone = store.candidate_phone(conversation_id)
```

with:

```python
        if not phone:
            # Recovered from an order code and deliberately never shown to the
            # model; sending to it is the only thing the model may do with it.
            # Otherwise a resend, to the number the last code went to.
            phone = store.candidate_phone(conversation_id) or store.pending_phone(conversation_id)
```

and in that tool's description change `"Omit the phone argument entirely to send the code to a number already recovered by find_account_by_code. "` to `"Omit the phone argument entirely to send the code to a number already recovered by find_account_by_code, or again to the number the last code went to. "`.

Add after `_six_digits`:

```python
class MockOtpSender:
    """Stands in for the OTP service until it is wired (the person, 2026-09-30).

    Sends nothing. Logs that a code went to the masked number, and never the
    code: on a test server the code is read from /dev/verification instead.
    The real service replaces this object and nothing else changes.
    """

    def __init__(self) -> None:
        self.sent: List[str] = []

    def __call__(self, phone: str, code: str) -> None:
        masked = mask_phone(phone)
        self.sent.append(masked)
        _logger.info("otp_sent to %s (mock: no SMS sent)", masked)
```

Replace the body of `apply_verified_identity` (keep its docstring) with:

```python
    return apply_proven_phone(message, store.verified_phone(message.conversation_id))
```

and add above it:

```python
def apply_proven_phone(message: InboundMessage, phone: Optional[str]) -> InboundMessage:
    """The inbound identity with a proved phone on it, or the message unchanged.

    An existing phone is never overwritten, so a stale proof on a reused
    conversation id can never redirect a lookup to somebody else's number.
    """
    if message.identity.phone or not phone:
        return message
    return replace(message, identity=replace(message.identity, strength=VERIFIED, phone=phone))
```

- [ ] **Step 5: `build_registry(send_code=...)`**

In `src/emotorad_ai/tools/mocks.py`, add the parameter to `build_registry` beside `account_finder`:

```python
    # Sends the one-time code (api.py passes MockOtpSender until the OTP
    # service is wired). None: the code is only stored, as before.
    send_code: Optional[Callable[[str, str], None]] = None,
```

and change line 582 to:

```python
        register_verification_tools(registry, verification, send=send_code, account_finder=account_finder)
```

- [ ] **Step 6: Test order numbers in `tools/fixtures.py`**

Change the typing import to `from typing import Any, Dict, List, Optional`, and add after `PHONE_WITH_NO_RECORD`:

```python
# Test order and invoice numbers for the order-number fallback, invented, so
# `find_account_by_code` can be tried without the OMS key (the person,
# 2026-09-30). With EMOTORAD_OMS_API_KEY set, the live lookup
# (tools/oms.live_account_finder) is used instead.
ORDER_CODES: Dict[str, str] = {
    "EMO-100234": PHONE_AMIIGO_TEST_RIDER,
    "INV-2026-0042": PHONE_AMIIGO_TEST_RIDER,
    "EMO-100117": "+919876543210",
}


def find_account_by_order_code(code: str) -> Optional[str]:
    """The phone an order or invoice number belongs to, or None. Case and
    spaces do not matter: customers read these off paper."""
    return ORDER_CODES.get("".join((code or "").split()).upper())
```

- [ ] **Step 7: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first_parts tests.test_verification tests.test_log_redaction tests.test_audit_edges -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/observability.py src/emotorad_ai/tools/verification.py src/emotorad_ai/tools/mocks.py src/emotorad_ai/tools/fixtures.py tests/test_verify_first_parts.py
git commit -m "Verify-first building blocks: resend, a mock code sender, test order numbers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Reading what the customer typed

**Files:**
- Create: `src/emotorad_ai/verify_first.py` (parsing and wording only in this task)
- Test: `tests/test_verify_first_parsing.py` (create)

**Interfaces:**
- Consumes: `tools.oms.normalise_mobile`, `tools.oms.OMSConfigError`.
- Produces (module `emotorad_ai.verify_first`):
  - `Found = Tuple[str, Tuple[int, int]]` (value, span in the text)
  - `find_phone(text: str) -> Optional[Found]` (value is the ten national digits)
  - `find_code(text: str) -> Optional[Found]` (value is six digits)
  - `find_order_code(text: str) -> Optional[Found]`
  - `looks_like_a_number(text: str) -> bool`
  - `asks_resend(text: str) -> bool`
  - `redact(text: str, span: Tuple[int, int], placeholder: str) -> str`
  - The reply texts as module constants (listed in Step 3).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_first_parsing.py`:

```python
"""What the verify-first step reads out of a message, on its own.

Deterministic on purpose (the person's decision, 2026-09-30): the step is the
same every time and costs nothing, so it has to understand the ways people
actually type a number, a code and an order number.
"""

import unittest

from emotorad_ai.verify_first import (
    asks_resend,
    find_code,
    find_order_code,
    find_phone,
    looks_like_a_number,
    redact,
)


class PhoneTests(unittest.TestCase):
    def test_the_forms_people_type(self):
        for typed in ("9700000010", "97000 00010", "97000-00010", "+91 97000-00010", "+919700000010",
                      "09700000010", "+91 970 000 0010", "my number is 9700000010, thanks",
                      "मेरा नंबर 9700000010 है"):
            found = find_phone(typed)
            self.assertIsNotNone(found, typed)
            self.assertEqual(found[0], "9700000010", typed)

    def test_the_span_covers_what_was_typed(self):
        text = "it's 97000 00010 ok"
        value, (start, end) = find_phone(text)
        self.assertEqual(text[start:end], "97000 00010")

    def test_a_number_then_a_code_finds_the_number(self):
        self.assertEqual(find_phone("9700000010 482913")[0], "9700000010")

    def test_not_a_mobile(self):
        for typed in ("1234567890", "482913", "12345", "EMXP2026001234", "INV-2026-0042", "hello"):
            self.assertIsNone(find_phone(typed), typed)


class CodeTests(unittest.TestCase):
    def test_the_forms_people_type(self):
        for typed in ("482913", "482 913", "482-913", "the code is 482913", " 482913 "):
            self.assertEqual(find_code(typed)[0], "482913", typed)

    def test_not_a_code(self):
        for typed in ("9700000010", "EMXP2026001234", "12345", "1234567", "what code?"):
            self.assertIsNone(find_code(typed), typed)


class OrderCodeTests(unittest.TestCase):
    def test_order_and_invoice_numbers(self):
        for typed, value in (("EMO-100234", "EMO-100234"), ("my order number is EMO-100234", "EMO-100234"),
                             ("INV-2026-0042", "INV-2026-0042"), ("order 2045678", "2045678"),
                             ("ORD/20456", "ORD/20456")):
            self.assertEqual(find_order_code(typed)[0], value, typed)

    def test_things_that_are_not_order_numbers(self):
        for typed in ("it shows E-06", "48V battery", "1234567890", "482913", "I don't remember it",
                      "Doodle V3", "9700000010"):
            self.assertIsNone(find_order_code(typed), typed)


class NumberAttemptTests(unittest.TestCase):
    def test_a_try_at_a_number(self):
        self.assertTrue(looks_like_a_number("1234567890"))
        self.assertTrue(looks_like_a_number("123 456 7890"))

    def test_not_a_try(self):
        self.assertFalse(looks_like_a_number("I don't remember"))
        self.assertFalse(looks_like_a_number("482913"))


class ResendTests(unittest.TestCase):
    def test_asking_for_another_code(self):
        for typed in ("resend", "Please resend", "send it again", "new code please", "I didn't get it",
                      "did not receive any code", "code nahi aaya"):
            self.assertTrue(asks_resend(typed), typed)

    def test_not_asking(self):
        for typed in ("482913", "yes", "my battery"):
            self.assertFalse(asks_resend(typed), typed)


class RedactTests(unittest.TestCase):
    def test_the_span_is_replaced(self):
        text = "my number is 97000 00010 thanks"
        _, span = find_phone(text)
        self.assertEqual(redact(text, span, "[phone]"), "my number is [phone] thanks")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first_parsing -v`
Expected: `ModuleNotFoundError: No module named 'emotorad_ai.verify_first'`.

- [ ] **Step 3: Write the parsing half of `verify_first.py`**

Create `src/emotorad_ai/verify_first.py`:

```python
"""Verify first: an anonymous customer proves their number before anything else.

The person's rule (2026-09-30): if a chat does not come from the Amiigo app or
anywhere else the person is already verified, the bot first asks for the
mobile number, sends a one-time code to it, checks the code, then lists every
bike on the number with its frame number and lets the person choose, and only
then carries on with the issue.

A fixed step, not the model: it answers the same way every time, it cannot be
talked past, and it costs nothing per message. It calls the existing tools in
tools/verification.py through the registry, so the comparison of the code is
still `==` in code. Safety and "talk to a person" run before it (graph.py).

This module reads the customer's words deterministically (below) and runs the
step (`VerifyFirst`).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .tools.oms import OMSConfigError, normalise_mobile

# What was found, and where in the text it was, so it can be replaced by a
# placeholder before the model or the transcript sees the message.
Found = Tuple[str, Tuple[int, int]]

# A mobile typed in one piece, with or without +91 or a leading 0.
_PHONE_TIGHT = re.compile(r"(?<![\d+])(?:\+?91|0)?[6-9]\d{9}(?!\d)")
# ...or read out in groups, with spaces or dashes.
_PHONE_LOOSE = re.compile(r"(?<![\d+])\+?\d[\d \-]{8,16}\d(?!\d)")
# Six digits, optionally split three and three.
_CODE = re.compile(r"(?<!\d)(\d{3})[ \-]?(\d{3})(?!\d)")
# A run of letters, digits, dashes and slashes, which an order number is.
_ORDER_TOKEN = re.compile(r"(?<![\w/-])[A-Za-z0-9][\w/-]{3,}(?![\w/-])")
_ORDER_WORDS = re.compile(r"\b(?:order|invoice|inv|bill)\b", re.IGNORECASE)
# Seven or more digits, however spaced: a try at a number, valid or not.
_NUMBER_ATTEMPT = re.compile(r"\+?\d[\d \-]{5,}\d")
_RESEND = re.compile(
    r"\b(?:resend|re-send|send (?:it |the code )?again|new code|another code|"
    r"didn'?t (?:get|receive)|did not (?:get|receive)|not received|no code)\b|nahi (?:aaya|mila)",
    re.IGNORECASE,
)


def find_phone(text: str) -> Optional[Found]:
    """The first valid Indian mobile in the text, as ten digits."""
    for pattern in (_PHONE_TIGHT, _PHONE_LOOSE):
        for match in pattern.finditer(text or ""):
            try:
                return normalise_mobile(match.group()), match.span()
            except OMSConfigError:
                continue
    return None


def find_code(text: str) -> Optional[Found]:
    """A six-digit code, spaces or a dash allowed in the middle."""
    match = _CODE.search(text or "")
    return (match.group(1) + match.group(2), match.span()) if match else None


def find_order_code(text: str) -> Optional[Found]:
    """An order or invoice number: at least five characters and three digits,
    with a letter or a slash in it, or anything of that length when the
    message says "order" or "invoice". Short codes such as E-06 and 48V, and
    phone numbers, are never taken for one."""
    text = text or ""
    mentions = bool(_ORDER_WORDS.search(text))
    for match in _ORDER_TOKEN.finditer(text):
        token = match.group()
        if len(token) < 5 or sum(ch.isdigit() for ch in token) < 3 or find_phone(token):
            continue
        if mentions or "/" in token or any(ch.isalpha() for ch in token):
            return token, match.span()
    return None


def looks_like_a_number(text: str) -> bool:
    """Seven or more digits: the customer tried to give a number."""
    return any(sum(ch.isdigit() for ch in m.group()) >= 7 for m in _NUMBER_ATTEMPT.finditer(text or ""))


def asks_resend(text: str) -> bool:
    return bool(_RESEND.search(text or ""))


def redact(text: str, span: Tuple[int, int], placeholder: str) -> str:
    start, end = span
    return text[:start] + placeholder + text[end:]


# -- the step's replies (fixed English text, like triage's) ------------------

ASK_NUMBER = "Before I look into this, I need to confirm it's you. What's the mobile number your bike is registered on?"
# Added when the first message carries a photo or video: a hazard shown only
# in a picture is not caught by the keyword gate, and saying it is.
PHOTO_SAFETY = "If you can see smoke, heat or swelling, stop using the bike and tell me now."
ASK_NUMBER_AGAIN = "I need the 10-digit mobile number your bike is registered on."
FALLBACK_ORDER = "If you can't recall it, send your order or invoice number instead."
FALLBACK_PERSON = "If you can't recall it, say 'talk to a person'."
INVALID_NUMBER = "That doesn't look like a 10-digit mobile number. Please send it again."
CODE_SENT = "I've sent a 6-digit code by SMS to {masked}. Please type it here."
CODE_RESENT = "I've sent a new code to {masked}. Please type it here."
ORDER_CODE_SENT = "I found that order. I've sent a 6-digit code to the number on it, {masked}. Please type it here."
ORDER_NOT_FOUND = "I couldn't find an order with that number. Please check it, or send your mobile number instead."
ASK_CODE = "Please type the 6-digit code I sent to {masked}. Say 'resend' for a new code, or send a different number."
WRONG_CODE = "That code isn't right. You have {left} left. Please check the SMS and type it again."
CODE_EXPIRED = "That code has expired. Say 'resend' and I'll send you a new one."
LOCKED = (
    "That's too many wrong codes, so I can't confirm it's you here. I'm passing you to our support "
    "team, who can verify you another way."
)
CONFIRMED = "Thanks, that's confirmed."
NO_BIKES = "I couldn't find a bike registered on this number. Would you like to register it now?"
LOOKUP_FAILED = "I can't load your bikes just now. What's happening with the bike?"


def tries(left: int) -> str:
    return "1 try" if left == 1 else "%d tries" % left
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first_parsing -v`
Expected: all pass. If a phone form fails, fix the pattern, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/verify_first.py tests/test_verify_first_parsing.py
git commit -m "Verify first: read a number, a code and an order number from what was typed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Triage lists full frame numbers and confirms a single bike

**Files:**
- Modify: `src/emotorad_ai/triage.py` (`describe_bike`, new `which_bike_text`, `says_yes`, `says_no`, `TriageAgent(unlisted_agent=...)`, `_resolve_selection`)
- Test: `tests/test_triage.py` (add a class)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `describe_bike(bike) -> str`: `"EMX Plus (Grey), frame EMXP2026001234"`.
  - `which_bike_text(bikes: Sequence[Dict]) -> str`: the list, with the one-bike or several-bike question.
  - `says_yes(text) -> bool`, `says_no(text) -> bool`.
  - `TriageAgent(topic_agents, unlisted_agent: Optional[str] = None)`. With one bike in selection, a "no" routes to `unlisted_agent` with `reason="bike_not_listed"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_triage.py` (before the `if __name__` block, if any; `message`, `resolved`, `BIKES` and `TOPIC_AGENTS` are the file's existing helpers):

```python
from emotorad_ai.triage import describe_bike, says_no, says_yes, which_bike_text


class FullFrameNumbersTests(unittest.TestCase):
    """The person's rule (2026-09-30): the list shows each bike's frame number
    so the customer can match it to the sticker on the frame."""

    def test_the_whole_frame_number_is_shown(self):
        self.assertEqual(
            describe_bike({"product_name": "EMX Plus", "product_color": "Grey", "frame_number": "EMXP2026001234"}),
            "EMX Plus (Grey), frame EMXP2026001234",
        )
        self.assertEqual(describe_bike({"product_name": "EMX Plus", "product_color": "", "frame_number": "F1"}),
                         "EMX Plus, frame F1")

    def test_several_bikes_ask_which(self):
        text = which_bike_text(BIKES)
        self.assertIn("I found %d bikes on this number:" % len(BIKES), text)
        for bike in BIKES:
            self.assertIn(bike["frame_number"], text)
        self.assertIn("Which one needs help?", text)

    def test_one_bike_asks_to_confirm(self):
        text = which_bike_text(BIKES[:1])
        self.assertIn("I found 1 bike on this number:", text)
        self.assertIn("Is this the bike that needs help? Reply yes", text)


class YesNoTests(unittest.TestCase):
    def test_yes(self):
        for typed in ("yes", "Yes, that's the one", "haan", "ji", "correct", "ok", "हाँ"):
            self.assertTrue(says_yes(typed), typed)
        for typed in ("yesterday it stopped", "no", "okayish"):
            self.assertFalse(says_yes(typed), typed)

    def test_no(self):
        for typed in ("no", "No.", "nope", "nahi", "नहीं", "not this one", "a different one"):
            self.assertTrue(says_no(typed), typed)
        for typed in ("no power at all", "yes", "nothing happens"):
            self.assertFalse(says_no(typed), typed)


class OneBikeSelectionTests(unittest.TestCase):
    """Only the verify-first step puts one bike into selection; triage on its
    own still picks a lone bike without asking."""

    def setUp(self):
        self.triage = TriageAgent(TOPIC_AGENTS, unlisted_agent="late_warranty_registration")
        self.state = ConversationState("c1")
        self.state.move_to(AWAITING_BIKE_SELECTION, "verified")
        self.state.pending_topic = "battery"

    def test_yes_selects_the_bike_and_routes_by_the_kept_topic(self):
        outcome = self.triage.handle(message("yes"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(self.state.selected_frame, BIKES[0]["frame_number"])

    def test_no_goes_to_the_unlisted_agent(self):
        outcome = self.triage.handle(message("no"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.agent, "late_warranty_registration")
        self.assertEqual(outcome.reason, "bike_not_listed")
        self.assertIsNone(self.state.selected_frame)

    def test_anything_else_asks_again(self):
        outcome = self.triage.handle(message("what?"), resolved(BIKES[:1]), self.state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("did not catch", outcome.reply)
        self.assertIn(BIKES[0]["frame_number"], outcome.reply)

    def test_no_bikes_to_choose_from_carries_on_to_the_issue(self):
        # A lookup that failed after verification: no list to ask about.
        outcome = self.triage.handle(message("2"), resolved([]), self.state)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertNotIn("0 bikes", outcome.reply or "")
```

Check the imports at the top of `tests/test_triage.py` include `AWAITING_BIKE_SELECTION`, `ConversationState` and `TriageAgent`; add any that are missing.

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_triage -v`
Expected: ImportError for `says_no` / `which_bike_text`.

- [ ] **Step 3: Implement in `triage.py`**

Replace `describe_bike` with:

```python
def describe_bike(bike: Dict[str, Any]) -> str:
    """One line of the list. The whole frame number, so the customer can match
    it to the sticker on the frame (the person's rule, 2026-09-30)."""
    name = bike.get("product_name") or "Your bike"
    if bike.get("product_color"):
        name += " (%s)" % bike["product_color"]
    frame = bike.get("frame_number")
    return "%s, frame %s" % (name, frame) if frame else name


def which_bike_text(bikes: Sequence[Dict[str, Any]]) -> str:
    """The list and the question, for one bike or several. Shared by triage and
    the verify-first step, so the two never word it differently."""
    count = len(bikes)
    lines = ["I found %d bike%s on this number:" % (count, "" if count == 1 else "s")]
    lines += ["%d. %s" % (index, describe_bike(bike)) for index, bike in enumerate(bikes, start=1)]
    if count == 1:
        lines.append("Is this the bike that needs help? Reply yes, or send the frame number of the bike you mean.")
    else:
        lines.append("Which one needs help? Reply with its number in the list or its frame number.")
    return "\n".join(lines)


# A yes or a no to "is this the bike?", in English and Hindi. A no must be the
# whole reply: "no power at all" is an issue, not an answer.
_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|ya|haan|haa|han|ha|ji|correct|right|sure|ok|okay|that'?s it|this one|same one|हाँ|हां|जी)(?!\w)",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(?:no|nope|nah|nahi|nahin|नहीं|not this(?: one)?|not that(?: one)?|wrong(?: bike)?|"
    r"a different one|different bike|another one|another bike|other bike)\s*[.!]*\s*$",
    re.IGNORECASE,
)


def says_yes(text: str) -> bool:
    return bool(_YES.match(text or ""))


def says_no(text: str) -> bool:
    return bool(_NO.match(text or ""))
```

Change `TriageAgent.__init__` to:

```python
    def __init__(self, topic_agents: Dict[str, str], unlisted_agent: Optional[str] = None) -> None:
        # topic -> sub-agent name, e.g. {"battery": "battery_support"}.
        self.topic_agents = topic_agents
        # Where a customer goes who says the one bike listed is not theirs:
        # the bike they mean is not registered on this number.
        self.unlisted_agent = unlisted_agent
```

Replace `_ask_which_bike` with a one-line wrapper so nothing else changes:

```python
    def _ask_which_bike(self, bikes: Sequence[Dict[str, Any]]) -> str:
        return which_bike_text(bikes)
```

Replace `_resolve_selection` with:

```python
    def _resolve_selection(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> TriageOutcome:
        bikes = resolved.bikes
        if not bikes:
            # Nothing to choose from (the lookup failed after verification):
            # carry on to the issue rather than asking about an empty list.
            state.move_to(AWAITING_ISSUE, "no_bikes_to_choose")
            return self._route_or_ask(self._take_pending(state)[0], state, "text")

        bike = bikes[0] if len(bikes) == 1 and says_yes(text) else match_bike(text, bikes)
        if bike is None:
            if len(bikes) == 1 and self.unlisted_agent and says_no(text):
                self._take_pending(state)
                state.route_to(self.unlisted_agent)
                return TriageOutcome(agent=self.unlisted_agent, reason="bike_not_listed")
            # Re-ask rather than guess. An unmatched reply usually means the
            # customer answered something else entirely, and picking a bike here
            # would silently attach the whole conversation to the wrong one.
            return TriageOutcome(
                reply="Sorry, I did not catch which bike you meant. " + which_bike_text(bikes),
                reason="selection_unmatched",
            )

        state.select_bike(bike["frame_number"])
        state.move_to(AWAITING_ISSUE, "bike_selected")
        topic, source = self._take_pending(state)
        return self._route_or_ask(topic, state, source)

    @staticmethod
    def _take_pending(state: ConversationState) -> Tuple[Optional[str], str]:
        topic, source = state.pending_topic, state.pending_topic_source or "text"
        state.pending_topic = None
        state.pending_topic_source = None
        return topic, source
```

Add `Tuple` to the `typing` import.

- [ ] **Step 4: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_triage tests.test_conversation_state -v`
Expected: all pass, the existing triage tests included.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/triage.py tests/test_triage.py
git commit -m "Triage: full frame numbers in the bike list, and yes or no for a single bike

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The verify-first step

**Files:**
- Modify: `src/emotorad_ai/verify_first.py` (add `GateReply`, `VerifyFirst`)
- Modify: `src/emotorad_ai/conversation.py` (`verify_step`, `verify_masked` on `ConversationState`)
- Test: `tests/test_verify_first_step.py` (create)

**Interfaces:**
- Consumes: Task 1 (`apply_proven_phone`, resend, `registry.verification`), Task 2 (parsing, texts), Task 3 (`which_bike_text`, `classify_issue`, `topic_from_pill`).
- Produces:
  - `ConversationState.verify_step: Optional[str]` (`None`, `"number"`, `"code"`), `ConversationState.verify_masked: Optional[str]`.
  - `GateReply(text: str, outcome: str, model_text: str, escalated: bool = False, resolved: Optional[ResolvedIdentity] = None)`.
  - `VerifyFirst(registry: ToolRegistry, resolver: IdentityResolver, log: EventLog)` with `applies(resolved: ResolvedIdentity) -> bool` and `handle(message: InboundMessage, state: ConversationState) -> GateReply`.
  - Outcomes: `ask_number`, `ask_number_again`, `invalid_number`, `code_sent`, `code_resent`, `order_code_sent`, `order_not_found`, `ask_code`, `wrong_code`, `code_expired`, `locked`, `verified`, `verified_no_bikes`, `verified_lookup_failed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_first_step.py`:

```python
"""The verify-first step on its own, without the runtime around it."""

import unittest
from datetime import date

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, ConversationState
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.observability import EventLog
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.verify_first import ASK_NUMBER, PHOTO_SAFETY, VerifyFirst

TODAY = date(2026, 9, 30)


def message(text, photo=False):
    attachments = [Attachment(kind="image", url="data:image/jpeg;base64,AAAA")] if photo else []
    return InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
                          identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), attachments=attachments)


class StepTests(unittest.TestCase):
    def setUp(self):
        self.store = VerificationStore()
        self.registry = build_registry(verification=self.store, today=TODAY,
                                       account_finder=fixtures.find_account_by_order_code)
        self.log = EventLog(path=None)
        self.step = VerifyFirst(self.registry, IdentityResolver(self.registry), self.log)
        self.state = ConversationState("c1")

    def test_first_contact_asks_for_the_number_and_keeps_the_topic(self):
        reply = self.step.handle(message("my battery isn't charging"), self.state)
        self.assertEqual(reply.outcome, "ask_number")
        self.assertEqual(reply.text, ASK_NUMBER)
        self.assertEqual(self.state.verify_step, "number")
        self.assertEqual(self.state.pending_topic, "battery")

    def test_a_photo_adds_the_safety_line(self):
        reply = self.step.handle(message("is this normal?", photo=True), self.state)
        self.assertEqual(reply.text, ASK_NUMBER + " " + PHOTO_SAFETY)

    def test_a_number_sends_a_code_and_the_model_text_hides_it(self):
        self.step.handle(message("hi"), self.state)
        reply = self.step.handle(message("it's 97000 00010"), self.state)
        self.assertEqual(reply.outcome, "code_sent")
        self.assertIn("•••••••010", reply.text)
        self.assertEqual(reply.model_text, "it's [phone]")
        self.assertEqual(self.state.verify_step, "code")
        self.assertIsNotNone(self.store.pending_code("c1"))

    def test_the_right_code_returns_the_bikes(self):
        self.step.handle(message("hi"), self.state)
        self.step.handle(message("9700000010"), self.state)
        code = self.store.pending_code("c1")
        reply = self.step.handle(message(code), self.state)
        self.assertEqual(reply.outcome, "verified")
        self.assertEqual(reply.model_text, "[code]")
        self.assertEqual([b["frame_number"] for b in reply.resolved.bikes], ["EMXP2026001234", "DDL32023045678"])
        self.assertTrue(reply.text.startswith("Thanks, that's confirmed. I found 2 bikes on this number:"))
        self.assertEqual(self.state.phase, AWAITING_BIKE_SELECTION)
        self.assertIsNone(self.state.verify_step)
        self.assertIsNone(self.state.context_block)

    def test_each_outcome_is_logged_without_the_number_or_the_code(self):
        self.step.handle(message("hi"), self.state)
        self.step.handle(message("9700000010"), self.state)
        code = self.store.pending_code("c1")
        self.step.handle(message(code), self.state)
        events = [e for e in self.log.events if e["event"] == "verify_first"]
        self.assertEqual([e["outcome"] for e in events], ["ask_number", "code_sent", "verified"])
        self.assertNotIn("9700000010", repr(events))
        self.assertNotIn(code, repr(events))

    def test_the_tools_it_calls_are_logged_as_tool_calls(self):
        # So a save conflict sees the side effect and never runs the turn again.
        self.step.handle(message("hi"), self.state)
        self.step.handle(message("9700000010"), self.state)
        tools = [e["tool"] for e in self.log.events if e["event"] == "tool_call"]
        self.assertIn("request_identity_verification", tools)


class AppliesTests(unittest.TestCase):
    def setUp(self):
        registry = build_registry(verification=VerificationStore())
        self.step = VerifyFirst(registry, IdentityResolver(registry), EventLog(path=None))

    def test_an_anonymous_customer(self):
        self.assertTrue(self.step.applies(ResolvedIdentity(persona="customer", method="unverified",
                                                           identity=Identity(strength=ANONYMOUS, em_aid="a"))))

    def test_not_a_verified_customer_or_a_dealer(self):
        self.assertFalse(self.step.applies(ResolvedIdentity(persona="customer", method="verified",
                                                            identity=Identity(strength=VERIFIED, phone="+919700000010"))))
        self.assertFalse(self.step.applies(ResolvedIdentity(persona="dealer", method="verified",
                                                            identity=Identity(strength=VERIFIED, phone="+919800000001"))))

    def test_not_without_the_verification_tools(self):
        registry = build_registry()
        step = VerifyFirst(registry, IdentityResolver(registry), EventLog(path=None))
        self.assertFalse(step.applies(ResolvedIdentity(persona="customer", method="unverified",
                                                       identity=Identity(strength=ANONYMOUS, em_aid="a"))))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first_step -v`
Expected: ImportError for `VerifyFirst`.

- [ ] **Step 3: Add the two fields to `ConversationState`**

In `src/emotorad_ai/conversation.py`, after `pending_topic_source`:

```python
    # The verify-first step (verify_first.py): which answer it is waiting for,
    # "number" or "code", and the masked number the code went to, for its
    # replies. Both None when the step is not running.
    verify_step: Optional[str] = None
    verify_masked: Optional[str] = None
```

- [ ] **Step 4: Add `GateReply` and `VerifyFirst` to `verify_first.py`**

Extend the imports (keep `import re` and `from __future__ import annotations`):

```python
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .contract import InboundMessage
from .conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ConversationState
from .identity import IdentityResolver, ResolvedIdentity
from .tools.oms import OMSConfigError, normalise_mobile
from .tools.registry import ToolContext, ToolRegistry, is_error
from .tools.verification import (
    FIND_ACCOUNT_BY_CODE,
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    apply_proven_phone,
)
from .triage import classify_issue, topic_from_pill, which_bike_text
```

Append:

```python
NUMBER = "number"
CODE = "code"


@dataclass
class GateReply:
    """The step's answer. `model_text` is the customer's message as the model's
    history and the transcript get it: the number, code or order number
    replaced by a placeholder. `resolved` is set once the person is verified,
    with their bikes."""

    text: str
    outcome: str
    model_text: str
    escalated: bool = False
    resolved: Optional[ResolvedIdentity] = None


class VerifyFirst:
    def __init__(self, registry: ToolRegistry, resolver: IdentityResolver, log: Any) -> None:
        self.registry = registry
        self.resolver = resolver
        self.log = log
        self.store = getattr(registry, "verification", None)

    def applies(self, resolved: ResolvedIdentity) -> bool:
        """An anonymous customer, on a registry that can verify them."""
        return (
            self.store is not None
            and REQUEST_IDENTITY_VERIFICATION in self.registry.specs
            and VERIFY_IDENTITY in self.registry.specs
            and resolved.persona == "customer"
            and not resolved.may_disclose
        )

    def handle(self, message: InboundMessage, state: ConversationState) -> GateReply:
        text = message.message_text or ""
        self._keep_topic(message, state)
        phone = find_phone(text)

        if state.verify_step == CODE:
            if phone:
                return self._send_code(message, state, phone)
            if asks_resend(text):
                return self._resend(message, state, text)
            code = find_code(text)
            if code:
                return self._check_code(message, state, code)
            return self._reply(message, state, ASK_CODE.format(masked=state.verify_masked), "ask_code", text)

        if phone:
            return self._send_code(message, state, phone)
        if state.verify_step is None:
            # First contact: the number, and only the number (phone first).
            state.verify_step = NUMBER
            ask = ASK_NUMBER
            if any(a.kind in ("image", "video") for a in message.attachments):
                ask += " " + PHOTO_SAFETY
            return self._reply(message, state, ask, "ask_number", text)

        order = find_order_code(text) if FIND_ACCOUNT_BY_CODE in self.registry.specs else None
        if order:
            return self._find_order(message, state, order)
        if looks_like_a_number(text):
            return self._reply(message, state, INVALID_NUMBER, "invalid_number", text)
        return self._ask_number_again(message, state, text)

    # -- steps ---------------------------------------------------------------

    def _send_code(self, message: InboundMessage, state: ConversationState, phone: Found) -> GateReply:
        number, span = phone
        model_text = redact(message.message_text, span, "[phone]")
        envelope = self._call(message, REQUEST_IDENTITY_VERIFICATION, {"phone": number})
        if is_error(envelope):
            return self._reply(message, state, INVALID_NUMBER, "invalid_number", model_text)
        state.verify_step = CODE
        state.verify_masked = envelope["data"]["phone_masked"]
        return self._reply(message, state, CODE_SENT.format(masked=state.verify_masked), "code_sent", model_text)

    def _resend(self, message: InboundMessage, state: ConversationState, text: str) -> GateReply:
        envelope = self._call(message, REQUEST_IDENTITY_VERIFICATION, {})
        if is_error(envelope):
            # Nothing pending any more (swept): start again from the number.
            return self._ask_number_again(message, state, text)
        state.verify_masked = envelope["data"]["phone_masked"]
        return self._reply(message, state, CODE_RESENT.format(masked=state.verify_masked), "code_resent", text)

    def _find_order(self, message: InboundMessage, state: ConversationState, order: Found) -> GateReply:
        value, span = order
        model_text = redact(message.message_text, span, "[order number]")
        if is_error(self._call(message, FIND_ACCOUNT_BY_CODE, {"code": value})):
            return self._reply(message, state, ORDER_NOT_FOUND, "order_not_found", model_text)
        sent = self._call(message, REQUEST_IDENTITY_VERIFICATION, {})
        if is_error(sent):
            return self._reply(message, state, ORDER_NOT_FOUND, "order_not_found", model_text)
        state.verify_step = CODE
        state.verify_masked = sent["data"]["phone_masked"]
        return self._reply(message, state, ORDER_CODE_SENT.format(masked=state.verify_masked),
                           "order_code_sent", model_text)

    def _check_code(self, message: InboundMessage, state: ConversationState, code: Found) -> GateReply:
        value, span = code
        model_text = redact(message.message_text, span, "[code]")
        cid = message.conversation_id
        expired = self.store.pending_code(cid) is None
        envelope = self._call(message, VERIFY_IDENTITY, {"code": value})
        if is_error(envelope):
            if envelope["error"]["code"] == "verification_locked":
                return self._reply(message, state, LOCKED, "locked", model_text, escalated=True)
            if expired:
                return self._reply(message, state, CODE_EXPIRED, "code_expired", model_text)
            left = tries(self.store.attempts_left(cid))
            return self._reply(message, state, WRONG_CODE.format(left=left), "wrong_code", model_text)
        return self._verified(message, state, model_text)

    def _verified(self, message: InboundMessage, state: ConversationState, model_text: str) -> GateReply:
        proved = apply_proven_phone(message, self.store.verified_phone(message.conversation_id))
        resolved = self.resolver.hydrate(proved)
        state.verify_step = None
        state.verify_masked = None
        # Rebuilt next turn with the bikes and past conversations; and the
        # agent cleared so the bike choice runs through triage, pin or not.
        state.context_block = None
        state.agent = None
        state.selected_frame = None
        if resolved.bikes:
            state.move_to(AWAITING_BIKE_SELECTION, "verified")
            text, outcome = CONFIRMED + " " + which_bike_text(resolved.bikes), "verified"
        elif resolved.method == "no_warranty_record":
            state.move_to(AWAITING_ISSUE, "verified_no_bikes")
            text, outcome = CONFIRMED + " " + NO_BIKES, "verified_no_bikes"
        else:
            state.move_to(AWAITING_ISSUE, "verified_lookup_failed")
            text, outcome = CONFIRMED + " " + LOOKUP_FAILED, "verified_lookup_failed"
        reply = self._reply(message, state, text, outcome, model_text)
        reply.resolved = resolved
        return reply

    def _ask_number_again(self, message: InboundMessage, state: ConversationState, text: str) -> GateReply:
        state.verify_step = NUMBER
        state.verify_masked = None
        fallback = FALLBACK_ORDER if FIND_ACCOUNT_BY_CODE in self.registry.specs else FALLBACK_PERSON
        return self._reply(message, state, ASK_NUMBER_AGAIN + " " + fallback, "ask_number_again", text)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _keep_topic(message: InboundMessage, state: ConversationState) -> None:
        """What the problem is, from the first message that says, so it is not
        asked for again once the bike is chosen."""
        if state.pending_topic is not None:
            return
        pill = message.pill_clicked
        topic = topic_from_pill(pill) or classify_issue(message.message_text or "")
        if topic:
            state.pending_topic = topic
            state.pending_topic_source = "pill:%s" % pill if pill and topic_from_pill(pill) else "text"

    def _call(self, message: InboundMessage, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        envelope = self.registry.call(name, arguments, ToolContext(conversation_id=message.conversation_id))
        # Logged like an agent's tool call, so Runtime._side_effects_since sees
        # a code sent or tried and a save conflict never runs the turn again.
        self.log.tool_call(message.conversation_id, name, arguments, envelope)
        return envelope

    def _reply(self, message: InboundMessage, state: ConversationState, text: str, outcome: str,
               model_text: str, escalated: bool = False) -> GateReply:
        self.log.emit("verify_first", message.conversation_id, outcome=outcome, step=state.verify_step)
        return GateReply(text=text, outcome=outcome, model_text=model_text, escalated=escalated)
```

Check that `from .triage import ...` does not create an import cycle: `triage` imports `conversation`, `contract` and `identity`, none of which import `verify_first`. Keep the parsing functions above this code.

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first_step tests.test_verify_first_parsing tests.test_conversation_state -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/verify_first.py src/emotorad_ai/conversation.py tests/test_verify_first_step.py
git commit -m "The verify-first step: number, code, then the bikes, with no model

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Wire the step into the turn

**Files:**
- Modify: `src/emotorad_ai/graph.py` (node `verify_gate`)
- Modify: `src/emotorad_ai/runtime.py` (`verify_first` parameter, `_node_verify`, `_node_prepare`, `_node_persona`, `_handle` transcript text, triage's `unlisted_agent`)
- Test: `tests/test_verify_first.py` (create)

**Interfaces:**
- Consumes: Task 4 (`VerifyFirst`, `GateReply`), Task 3 (`TriageAgent(unlisted_agent=...)`), Task 1 (`apply_proven_phone`).
- Produces: `Runtime(..., verify_first: bool = False)`; replies with `handled_by == "verify_first:<outcome>"`; `Reply.metadata["transcript_text"]` on the step's replies.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_first.py`:

```python
"""Verify first, through the whole turn (spec 2026-09-30).

The model in these tests fails the test if it is called before the bike is
chosen: ScriptedClaude raises when it runs out of replies, and only the turns
after the choice are given one.
"""

import base64
import io
import unittest
from datetime import date

from PIL import Image

from emotorad_ai.agents import battery_support, late_warranty
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.verify_first import PHOTO_SAFETY

TODAY = date(2026, 9, 30)
RIDER = fixtures.PHONE_AMIIGO_TEST_RIDER  # two bikes
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus
NO_BIKE = fixtures.PHONE_WITH_NO_RECORD


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (60, 60, 60)).save(out, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


class Chat:
    """One website visitor, turn by turn."""

    def __init__(self, replies=(), verify_first=True, account_finder=fixtures.find_account_by_order_code, clock=None):
        self.store = VerificationStore(clock=clock) if clock else VerificationStore()
        self.registry = build_registry(verification=self.store, today=TODAY, account_finder=account_finder)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            self_service_identity=True, phone_resolver=self.store.verified_phone, verify_first=verify_first,
        )

    def say(self, text, photo=False, identity=None, **metadata):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=identity or Identity(strength=ANONYMOUS, em_aid="aid-1"),
            attachments=[Attachment(kind="image", url=jpeg())] if photo else [], entry_metadata=metadata,
        ))

    def code(self):
        return self.store.pending_code("c1")

    def state(self):
        return self.conversations.peek("c1")

    def verify(self, phone=RIDER, first="my battery isn't charging"):
        self.say(first)
        self.say(phone[3:])
        return self.say(self.code())


class WholeFlowTests(unittest.TestCase):
    def test_number_code_bike_then_the_issue(self):
        chat = Chat(replies=[say("Let's check the charger first. Is its light on?")])
        first = chat.say("my battery isn't charging")
        self.assertEqual(first.handled_by, "verify_first:ask_number")
        sent = chat.say("97000 00010")
        self.assertEqual(sent.handled_by, "verify_first:code_sent")
        self.assertIn("•••••••010", sent.text)
        listed = chat.say(chat.code())
        self.assertEqual(listed.handled_by, "verify_first:verified")
        self.assertIn("EMXP2026001234", listed.text)
        self.assertIn("DDL32023045678", listed.text)
        self.assertEqual(chat.state().phase, AWAITING_BIKE_SELECTION)
        self.assertEqual(chat.llm.requests, [], "no model before the bike is chosen")

        answer = chat.say("2")
        self.assertEqual(chat.state().selected_frame, "DDL32023045678")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertIn("charger", answer.text)

    def test_the_model_never_sees_the_number_or_the_code(self):
        chat = Chat(replies=[say("Let's check the charger first.")])
        chat.say("my battery isn't charging")
        chat.say("97000 00010")
        code = chat.code()
        chat.say(code)
        chat.say("2")
        seen = repr(chat.llm.requests[0]["messages"])
        for secret in ("97000 00010", "9700000010", code):
            self.assertNotIn(secret, seen)
        self.assertIn("[phone]", seen)
        self.assertIn("[code]", seen)

    def test_nor_does_the_transcript(self):
        chat = Chat()
        chat.say("hi")
        chat.say("97000 00010")
        code = chat.code()
        chat.say("the code is " + code)
        # The turns' text only: a timestamp's microseconds could hold six digits.
        said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
        self.assertNotIn("97000 00010", said)
        self.assertNotIn(code, said)

    def test_the_conversation_is_the_riders_once_verified(self):
        chat = Chat()
        chat.verify()
        self.assertEqual(chat.state().user_key, "PHONE#" + RIDER)

    def test_a_number_and_an_issue_in_one_message(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        reply = chat.say("battery dead, my number is 9700000010")
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        chat.say(chat.code())
        chat.say("the EMX Plus")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)

    def test_a_number_inside_hindi_text(self):
        chat = Chat()
        chat.say("namaste")
        self.assertEqual(chat.say("मेरा नंबर 9700000010 है").handled_by, "verify_first:code_sent")

    def test_the_code_tools_are_logged_so_a_conflict_retry_cannot_resend(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say(chat.code())
        tools = [e["tool"] for e in chat.log.events if e["event"] == "tool_call"]
        self.assertIn("request_identity_verification", tools)
        self.assertIn("verify_identity", tools)


class OneBikeTests(unittest.TestCase):
    def test_yes_selects_it(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        listed = chat.verify(phone=ONE_BIKE)
        self.assertIn("Is this the bike that needs help?", listed.text)
        self.assertIn("EMXP2025004417", listed.text)
        chat.say("yes")
        self.assertEqual(chat.state().selected_frame, "EMXP2025004417")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)

    def test_no_goes_to_warranty_registration(self):
        chat = Chat(replies=[say("Let's register it. What's the frame number?")])
        chat.verify(phone=ONE_BIKE)
        chat.say("no")
        self.assertEqual(chat.state().agent, late_warranty.AGENT_NAME)
        self.assertEqual(len(chat.llm.requests), 1)


class NoBikeTests(unittest.TestCase):
    def test_registration_is_offered_then_the_agent_takes_over(self):
        chat = Chat(replies=[say("Great, what's the frame number?")])
        listed = chat.verify(phone=NO_BIKE)
        self.assertEqual(listed.handled_by, "verify_first:verified_no_bikes")
        self.assertIn("register it now", listed.text)
        chat.say("yes please")
        self.assertEqual(chat.state().agent, late_warranty.AGENT_NAME)


class CodeTests(unittest.TestCase):
    def wrong(self, chat):
        return "000000" if chat.code() != "000000" else "111111"

    def test_a_wrong_code_says_how_many_tries_are_left(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say(self.wrong(chat))
        self.assertEqual(reply.handled_by, "verify_first:wrong_code")
        self.assertIn("4 tries left", reply.text)

    def test_five_wrong_codes_hand_over(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        for left in ("4 tries", "3 tries", "2 tries", "1 try"):
            self.assertIn(left + " left", chat.say(self.wrong(chat)).text)
        locked = chat.say(self.wrong(chat))
        self.assertEqual(locked.handled_by, "verify_first:locked")
        self.assertTrue(locked.escalated)

    def test_resend(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say("I didn't get it, please resend")
        self.assertEqual(reply.handled_by, "verify_first:code_resent")
        self.assertIn("•••••••010", reply.text)
        self.assertEqual(chat.say(chat.code()).handled_by, "verify_first:verified")

    def test_a_different_number_while_waiting_for_the_code(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say("sorry, wrong number, it's 9876543210")
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        self.assertIn("•••••••210", reply.text)
        self.assertIn("EMXP2025004417", chat.say(chat.code()).text)

    def test_anything_else_asks_for_the_code_again(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say("what?")
        self.assertEqual(reply.handled_by, "verify_first:ask_code")
        self.assertIn("•••••••010", reply.text)

    def test_an_expired_code_says_so(self):
        now = [0.0]
        chat = Chat(clock=lambda: now[0])
        chat.say("hi")
        chat.say("9700000010")
        code = chat.code()
        now[0] += 601
        self.assertEqual(chat.say(code).handled_by, "verify_first:code_expired")


class OrderNumberTests(unittest.TestCase):
    def test_an_unhelpful_reply_offers_the_order_number(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        reply = chat.say("I don't remember it")
        self.assertEqual(reply.handled_by, "verify_first:ask_number_again")
        self.assertIn("order or invoice number", reply.text)

    def test_an_order_number_sends_the_code_to_the_number_on_it(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        chat.say("I don't remember it")
        reply = chat.say("my order number is EMO-100234")
        self.assertEqual(reply.handled_by, "verify_first:order_code_sent")
        self.assertIn("•••••••010", reply.text)
        self.assertIn("DDL32023045678", chat.say(chat.code()).text)

    def test_an_unknown_order_number_says_so(self):
        chat = Chat()
        chat.say("hi")
        chat.say("I can't recall")
        self.assertEqual(chat.say("EMO-999999").handled_by, "verify_first:order_not_found")

    def test_without_the_order_lookup_it_offers_a_person(self):
        chat = Chat(account_finder=None)
        chat.say("hi")
        self.assertIn("talk to a person", chat.say("I don't remember it").text)

    def test_a_number_that_is_not_a_mobile_is_refused(self):
        chat = Chat()
        chat.say("hi")
        self.assertEqual(chat.say("1234567890").handled_by, "verify_first:invalid_number")

    def test_a_stale_code_while_waiting_for_the_number_asks_for_the_number(self):
        chat = Chat()
        chat.say("hi")
        self.assertEqual(chat.say("482913").handled_by, "verify_first:ask_number_again")


class StillFirstTests(unittest.TestCase):
    def test_a_safety_report_comes_before_verification(self):
        chat = Chat()
        self.assertEqual(chat.say("there is smoke coming out of my battery").handled_by, "guardrail:battery_safety")

    def test_asking_for_a_person_comes_before_verification(self):
        chat = Chat()
        self.assertEqual(chat.say("I want to talk to a person").handled_by, "guardrail:human_handoff")

    def test_a_photo_on_the_first_message_adds_the_safety_line(self):
        chat = Chat()
        reply = chat.say("is this normal?", photo=True)
        self.assertEqual(reply.handled_by, "verify_first:ask_number")
        self.assertIn(PHOTO_SAFETY, reply.text)

    def test_no_photo_no_safety_line(self):
        self.assertNotIn(PHOTO_SAFETY, Chat().say("hi").text)


class SkippedTests(unittest.TestCase):
    def test_a_signed_in_rider_is_not_asked(self):
        chat = Chat()
        reply = chat.say("my battery isn't charging", identity=Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1"))
        self.assertEqual(reply.handled_by, "triage")
        self.assertIn("EMXP2026001234", reply.text)

    def test_without_verify_first_nothing_changes(self):
        chat = Chat(verify_first=False)
        reply = chat.say("hi")
        self.assertFalse(reply.handled_by.startswith("verify_first"), reply.handled_by)


class PinnedAgentTests(unittest.TestCase):
    def test_a_pinned_agent_still_gets_the_bike_choice(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        pin = {"pinned_agent": battery_support.AGENT_NAME}
        chat.say("my battery isn't charging", **pin)
        chat.say("9700000010", **pin)
        self.assertEqual(chat.say(chat.code(), **pin).handled_by, "verify_first:verified")
        chat.say("2", **pin)
        self.assertEqual(chat.state().selected_frame, "DDL32023045678")


class ExpiredSessionTests(unittest.TestCase):
    def test_an_expired_session_starts_the_step_again(self):
        now = [0.0]
        chat = Chat(replies=[say("Is the charger light on?")], clock=lambda: now[0])
        chat.verify()
        chat.say("2")
        now[0] += 12 * 60 * 60 + 1
        self.assertEqual(chat.say("still not charging").handled_by, "verify_first:ask_number")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first -v`
Expected: `TypeError: ... unexpected keyword argument 'verify_first'`.

- [ ] **Step 3: The graph node**

In `src/emotorad_ai/graph.py`:
- the module docstring's order line becomes: "identity and context, then the safety and handoff gates, then verification for an anonymous customer, then persona routing and triage, then Jev's path, then an agent."
- add `verify_gate: Node` to `TurnNodes` between `handoff_gate` and `persona_route`;
- `NODE_NAMES` gets `"verify_gate"` between `"handoff_gate"` and `"persona_route"`;
- replace the `handoff_gate` edge with:

```python
    graph.add_conditional_edges("handoff_gate", _replied_or("verify_gate"), ["verify_gate", END])
    graph.add_conditional_edges("verify_gate", _replied_or("persona_route"), ["persona_route", END])
```

- [ ] **Step 4: The runtime**

In `src/emotorad_ai/runtime.py`:

Imports: add `AWAITING_BIKE_SELECTION` to the `.conversation` import, `apply_proven_phone` to the `.tools.verification` import, and `from .verify_first import VerifyFirst`.

Constructor: add the parameter after `media_store`:

```python
        verify_first: bool = False,
```

Replace `self.triage = TriageAgent(TOPIC_AGENTS)` with:

```python
        # A customer who says the one bike listed is not theirs goes to
        # registration: the bike they mean is not on this number.
        self.triage = TriageAgent(TOPIC_AGENTS, unlisted_agent=LATE_WARRANTY)
        # Verify first (the person's decision, 2026-09-30): an anonymous
        # customer proves their number and picks a bike before triage or any
        # model. Off unless asked for; the web chat API turns it on.
        self.verify_gate = VerifyFirst(self.registry, self.resolver, self.log) if verify_first else None
```

In `TurnNodes(...)` add `verify_gate=self._node_verify,` after `handoff_gate=...`.

In `_node_prepare`, at the start after `state.turns += 1`:

```python
        if self.verify_gate is not None and self.phone_resolver is not None:
            # A number this conversation has proved opens the identity here,
            # so the runtime does not depend on the API having done it.
            message = apply_proven_phone(message, self.phone_resolver(message.conversation_id))
```

replace `if meta.get("pinned_agent"):` with:

```python
        choosing = self.verify_gate is not None and state.phase == AWAITING_BIKE_SELECTION
        if meta.get("pinned_agent") and not choosing:
            # A pin waits while the bike is being chosen (verify first).
```

and change its `return` to `return {"message": message, "conversation": state, "resolved": resolved}`.

Add the node after `_node_handoff`:

```python
    def _node_verify(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 3. Verify first: an anonymous customer's number, code and bike, by
        #    fixed replies (verify_first.py). No model is called here.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if self.verify_gate is None or not self.verify_gate.applies(resolved):
            return {}
        gate = self.verify_gate.handle(message, state)
        if gate.escalated:
            self.log.escalation(message.conversation_id, "verification_locked", None)
        # The history gets the message with the number or code replaced, so
        # no model and no Jev call sees them; the transcript gets the same.
        shown = replace(message, message_text=gate.model_text)
        update: Dict[str, Any] = {"reply": self._finish(
            shown, state, gate.text, "verify_first:" + gate.outcome, escalated=gate.escalated,
            metadata={"transcript_text": gate.model_text},
        )}
        if gate.resolved is not None:
            update["resolved"] = gate.resolved
        return update
```

In `_node_persona`, after the `if not outcome.is_handoff:` block inside the triage branch:

```python
            if outcome.agent == LATE_WARRANTY and LATE_WARRANTY in self.agents:
                # The one bike listed is not theirs: straight to registration,
                # with no Jev category to route around it.
                return {"reply": self._run_agent_or_handover(LATE_WARRANTY, message, resolved, state)}
```

In `_handle`, replace:

```python
            self.conversations.record_turn(state, message, reply, self._summary_for(state, resolved))
```

with:

```python
            recorded = message
            if "transcript_text" in reply.metadata:
                # The verify-first step's turns: the number or code replaced.
                recorded = replace(message, message_text=reply.metadata["transcript_text"])
            self.conversations.record_turn(state, recorded, reply, self._summary_for(state, resolved))
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_verify_first tests.test_graph tests.test_triage tests.test_self_service_identity tests.test_verified_mid_turn_owner -v`
Expected: all pass. If a flow test fails, find the cause with superpowers:systematic-debugging before changing anything; do not loosen a test.

- [ ] **Step 6: Run the whole suite**

Run the full suite command from Global Constraints.
Expected: `OK`. Note the count.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/graph.py src/emotorad_ai/runtime.py tests/test_verify_first.py
git commit -m "Run verify first in the turn, after safety and handoff, before triage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Turn it on for the web chat, with the test console and docs

**Files:**
- Modify: `src/emotorad_ai/api.py` (`verify_first=True`, `MockOtpSender`, fixture order numbers)
- Modify: `web/e2e-console.html` (`SCENARIOS`)
- Modify: `tests/test_e2e_console.py` (a scenario check)
- Modify: `CLAUDE.md` (one bullet), `docs/runbooks/media.md` (section 9, last paragraph)
- Test: `tests/test_api_verify_first.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces: `api.OTP_SENDER` (a `MockOtpSender`), `api.runtime.verify_gate` set.

- [ ] **Step 1: Write the failing API test**

Create `tests/test_api_verify_first.py`:

```python
"""Verify first through POST /message, as the web chat uses it."""

import base64
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:dev").decode()}


def fresh_api(dev_codes=True):
    env = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory", "EMOTORAD_OMS_API_KEY": "",
           "EMOTORAD_AI_PLAYGROUND_USER": "dev", "EMOTORAD_AI_PLAYGROUND_PASSWORD": "dev",
           "EMOTORAD_AI_DEV_CODES": "1" if dev_codes else "0", "EMOTORAD_AI_MEDIA_BUCKET": ""}
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
        import emotorad_ai.api as api
        return importlib.reload(api)


class ApiVerifyFirstTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api()
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(dev_codes=False))

    def post(self, text, **extra):
        body = dict({"conversation_id": "c-v", "em_aid": "aid-v", "text": text}, **extra)
        response = self.client.post("/message", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def code(self):
        return self.client.get("/dev/verification/c-v", headers=AUTH).json()["pending_code"]

    def test_an_anonymous_visitor_gives_the_number_the_code_then_sees_the_bikes(self):
        self.assertEqual(self.post("my battery isn't charging")["handled_by"], "verify_first:ask_number")
        with self.assertLogs("emotorad_ai.tools.verification", level="INFO") as logs:
            self.assertEqual(self.post("9700000010")["handled_by"], "verify_first:code_sent")
        code = self.code()
        said = "\n".join(logs.output)
        self.assertIn("•••••••010", said)
        self.assertNotIn(code, said)
        listed = self.post(code)
        self.assertEqual(listed["handled_by"], "verify_first:verified")
        self.assertIn("DDL32023045678", listed["text"])

    def test_the_test_order_number_works_without_the_oms_key(self):
        self.post("hi")
        self.post("I can't remember my number")
        self.assertEqual(self.post("EMO-100234")["handled_by"], "verify_first:order_code_sent")

    def test_a_signed_in_rider_is_not_asked(self):
        reply = self.client.post("/message", json={"conversation_id": "c-s", "session_token": "sess-amiigo-test",
                                                   "text": "my battery isn't charging"}).json()
        self.assertEqual(reply["handled_by"], "triage")
        self.assertIn("EMXP2026001234", reply["text"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_verify_first -v`
Expected: the first test fails, `handled_by` is `triage`, not `verify_first:ask_number`.

- [ ] **Step 3: Wire the API**

In `src/emotorad_ai/api.py`:
- add `from .tools import fixtures` near the other `.tools` imports, and add `MockOtpSender` to the `.tools.verification` import;
- after `verification_store = VerificationStore()`:

```python
# Sends the one-time code. A stand-in until the OTP service is wired (the
# person, 2026-09-30): it sends nothing and logs the masked number; the code
# itself is read from /dev/verification on a test server.
OTP_SENDER = MockOtpSender()
```

- in `_build_registry`, both `build_registry(...)` calls get `send_code=OTP_SENDER,`; the one without the OMS key also gets `account_finder=fixtures.find_account_by_order_code,` with a comment: `# Test order numbers (fixtures.ORDER_CODES), so the fallback can be tried without the OMS key.`
- in `runtime = Runtime(...)`, add after `self_service_identity=True,`:

```python
    # Verify first: an anonymous visitor gives their number, types the code and
    # picks a bike before triage or any model (the person's decision,
    # 2026-09-30). Signed-in visitors and the Amiigo session skip it.
    verify_first=True,
```

- [ ] **Step 4: Run the API tests**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_verify_first tests.test_api_uploads tests.test_audit_edges tests.test_chat_surface tests.test_location_sharing tests.test_e2e_console -v`
Expected: all pass. The anonymous posts in the older API tests only check status codes and logged text; if one asserts on the reply of an anonymous turn, give that test a session (`session_token`) rather than weakening its assertion, and say so in the task report.

- [ ] **Step 5: Update the test console scenarios**

In `web/e2e-console.html`, replace the `smoke-neutral-words`, `smoke-photo-only` and `verify-then-warranty` entries of `SCENARIOS` with the following, and add `verify-by-order-number` after `verify-then-warranty`:

```javascript
  { id: "smoke-neutral-words", title: "Smoke photo, no alarming words",
    why: "An anonymous visitor: the bot asks for the number first. With a photo it also says to stop using the bike if there is smoke, heat or swelling, because the safety gate reads only typed text.",
    who: { anonymous: true },
    steps: [ { text: "Hi, is this normal? It started after I charged it.", photo: true,
      expect: { handledBy: ["verify_first:ask_number"], mentionsAny: ["stop using"], mentionsLabel: "asks for the number and says to stop using the bike",
                media: true, oneStep: true } } ] },
```

```javascript
  { id: "smoke-photo-only", title: "Smoke photo with no words",
    why: "A photo on its own, from an anonymous visitor: the number is asked for, with the stop-using line.",
    who: { anonymous: true },
    steps: [ { text: "", photo: true,
      expect: { handledBy: ["verify_first:ask_number"], mentionsAny: ["stop using"], mentionsLabel: "says to stop using the bike", media: true, oneStep: true } } ] },
```

```javascript
  { id: "verify-then-warranty", title: "Anonymous visitor: number, code, bike, then warranty",
    why: "Verify first: the number, a code by (stand-in) SMS, the bike with its full frame number, then the warranty answer.",
    who: { anonymous: true },
    steps: [
      { text: "My battery won't charge.", expect: { handledBy: ["verify_first:ask_number"], oneStep: true } },
      { text: "9876543210", expect: { handledBy: ["verify_first:code_sent"], codeSent: true, oneStep: true } },
      { code: true, expect: { handledBy: ["verify_first:verified"], mentionsAny: ["emxp2025004417"], mentionsLabel: "lists the bike with its full frame number" } },
      { text: "yes", expect: { notHandledBy: ["triage", "verify_first:ask_number"], oneStep: true } },
      { text: "Is my battery covered under warranty?", expect: { mentionsAny: ["warranty", "covered"], oneStep: true } } ] },
  { id: "verify-by-order-number", title: "Anonymous visitor who can't recall the number",
    why: "The fallback: an order number (a test one), the code to the number on that order, then the choice of two bikes.",
    who: { anonymous: true },
    steps: [
      { text: "My battery won't charge.", expect: { handledBy: ["verify_first:ask_number"] } },
      { text: "I don't remember my number", expect: { handledBy: ["verify_first:ask_number_again"], mentionsAny: ["order or invoice"] } },
      { text: "EMO-100234", expect: { handledBy: ["verify_first:order_code_sent"], codeSent: true } },
      { code: true, expect: { handledBy: ["verify_first:verified"], mentionsAny: ["ddl32023045678"], mentionsLabel: "lists both bikes with full frame numbers" } },
      { text: "2", expect: { notHandledBy: ["triage"], oneStep: true } } ] }
```

(`mentionsAny` compares against the lower-cased reply, so the frame numbers are written in lower case.)

In the paragraph above `var SCENARIOS`, add: `A step that sends the number waits for the verify-first step, which every anonymous scenario now passes through first.`

- [ ] **Step 6: Check the console test**

Add to `tests/test_e2e_console.py`, in the class with `test_it_runs_the_smoke_scenarios`:

```python
    def test_it_runs_the_verify_first_scenarios(self):
        for scenario in ("verify-then-warranty", "verify-by-order-number"):
            self.assertIn('"%s"' % scenario, self.html)
        self.assertIn("verify_first:order_code_sent", self.html)
```

Run: `PYTHONPATH="src;." python -m unittest tests.test_e2e_console -v`
Expected: pass.

- [ ] **Step 7: Docs**

`CLAUDE.md`, after the "One step per reply" bullet:

```markdown
- **Verify first** (2026-09-30): with `Runtime(verify_first=True)` (the web chat API), an anonymous customer gives their mobile number, types the one-time code and picks a bike from a list with full frame numbers before triage or any model runs (`verify_first.py`, graph node `verify_gate`, after the safety and handoff gates). The step calls `request_identity_verification` and `verify_identity` itself; the model never compares a code. The order or invoice number is the fallback (`fixtures.ORDER_CODES` without the OMS key). `MockOtpSender` stands in for the OTP service. The model's history and the transcript get `[phone]`, `[code]` and `[order number]` in their place.
```

`docs/runbooks/media.md` section 9, replace the last paragraph with:

```markdown
The scenarios live in `web/e2e-console.html` (`SCENARIOS`). Every anonymous
scenario now starts with the verify-first step (2026-09-30): the bot asks for the
number, the console reads the code from `/dev/verification`, and the bike is
chosen before any model runs. `verify-by-order-number` uses the test order number
`EMO-100234`. Some checks state what a customer should get and does not yet
(2026-09-29): a signed-in customer whose photo alone shows a hazard is handed to a
person. They fail until that behaviour is fixed.
```

- [ ] **Step 8: Run the whole suite**

Run the full suite command. Expected: `OK`.

- [ ] **Step 9: Commit**

```bash
git add src/emotorad_ai/api.py web/e2e-console.html tests/test_api_verify_first.py tests/test_e2e_console.py CLAUDE.md docs/runbooks/media.md
git commit -m "Web chat: verify first, with a mock code sender and test order numbers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Only the two useful guide pictures

**Files:**
- Modify: `knowledge/_media/catalogue.yaml`
- Modify: `knowledge/battery/wont-charge.yaml`, `knowledge/battery/wont-power-on.yaml`, `knowledge/battery/melted-terminal-or-connector.yaml`, `knowledge/battery/doodle-wont-power-on.yaml`
- Modify: `tests/test_knowledge_migration.py`, `tests/test_plan_conformance.py`
- Test: `tests/test_guide_media_catalogue.py` (create)

**Interfaces:**
- Consumes: nothing from the other tasks.
- Produces: a catalogue of exactly `soc_button` and `battery_onoff_switch`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_guide_media_catalogue.py`:

```python
"""The guide pictures the bot may send: only the two the person chose
(2026-09-30), both in emotorad-ai-stage-media under assets/afs/battery/photos/."""

import unittest

from emotorad_ai.knowledge import load_records
from emotorad_ai.media import load_catalogue

KEPT = {
    "soc_button": "afs/battery/photos/soc-button-non-doodle.png",
    "battery_onoff_switch": "afs/battery/photos/battery-onoff-switch.png",
}


class CatalogueTests(unittest.TestCase):
    def test_exactly_the_two_pictures(self):
        catalogue = load_catalogue()
        self.assertEqual({key: item["id"] for key, item in catalogue.items()}, KEPT)

    def test_no_caption_has_an_em_dash(self):
        for key, item in load_catalogue().items():
            self.assertNotIn("—", item["caption"], key)

    def test_no_knowledge_record_names_another_picture(self):
        for record in load_records():
            for item in record.media:
                self.assertIn(item.get("id"), set(KEPT.values()), "%s: %r" % (record.id, item))

    def test_no_record_tells_the_bot_to_send_a_picture_it_no_longer_has(self):
        for record in load_records():
            body = " ".join(record.steps).lower()
            for promise in ("comparison picture", "comparison photograph", "melted-versus-normal picture",
                            "revival clip"):
                self.assertNotIn(promise, body, record.id)


if __name__ == "__main__":
    unittest.main()
```

(`emotorad_ai.knowledge.load_records()` returns `KnowledgeRecord`s with `steps: Sequence[str]` and `media: Sequence[Mapping]`.)

- [ ] **Step 2: Run it to see it fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_guide_media_catalogue -v`
Expected: failures on the catalogue keys, the em dash, and the melted record's media.

- [ ] **Step 3: The catalogue**

In `knowledge/_media/catalogue.yaml`, delete the `battery_revival`, `melted_battery_terminal` and `melted_controller_connector` entries, and change the switch caption to:

```yaml
  caption: The battery On/Off switch, set it to ON before charging
```

Add one line to the header comment: `# Only these two are sent (the person's choice, 2026-09-30). Both live in emotorad-ai-stage-media under assets/afs/battery/photos/, each with a .w900.webp copy.`

- [ ] **Step 4: The knowledge records**

- `wont-charge.yaml`: delete the `battery-revival-steps.mp4` item and the `https://cdn.emotorad.test/kb/charger-seating.png` item from `media`.
- `wont-power-on.yaml`: delete the `battery-revival-steps.mp4` item from `media`. In the step at line 66, replace `and carries the melted-versus-normal comparison photographs you send with each request.` with `and says what melted looks like at each end.` In the step at line 144, replace `with the melted-versus-normal picture sent alongside so they are comparing rather than guessing.` with `and tell them what melted looks like (fused metal pins, blackened plastic) so they are comparing rather than guessing.`
- `doodle-wont-power-on.yaml` line 45: replace `it sets the order and carries the comparison photographs.` with `it sets the order and says what melted looks like at each end.`
- `melted-terminal-or-connector.yaml`: set `media: []` with the comment `# No comparison pictures are available to send (2026-09-30); the steps describe what to look for instead.` In the second step replace `Send the melted-versus-normal terminal picture with the request, so the customer is comparing rather than guessing. Fused metal pins` with `Tell them what to look for, so they are comparing rather than guessing: fused metal pins`. In the third step replace `Send the controller comparison picture with this request too.` with `Tell them what a melted one looks like: fused pins, warped plastic and heat marks around the housing.`

Do not add em dashes. Existing em dashes on lines you do not edit stay as they are.

- [ ] **Step 5: Update the tests that named the removed pictures**

`tests/test_knowledge_migration.py`, in `test_the_target_still_carries_what_the_handoff_promises`: replace the media assertion with

```python
        media = [item for record in load_records() if record.id == self.POINTS_AT for item in record.media]
        self.assertEqual(media, [], "no comparison pictures to send (2026-09-30)")
        self.assertIn("fused metal pins", body)
```

and in the class docstring change `and carries the melted-versus-normal pictures` to `and says what melted looks like at each end`.

`tests/test_plan_conformance.py`: give `make_runtime` a `guide_media=None` parameter (`guide_media=guide_media if guide_media is not None else load_catalogue()`), and in `test_a_clip_is_not_announced_to_the_channel_as_a_photo` build the runtime with a test clip, since the catalogue no longer has one:

```python
        clip = {"test_clip": {"url": "https://cdn.emotorad.test/kb/revival.mp4", "kind": "video",
                              "caption": "A test clip"}}
        runtime, _ = make_runtime([
            call_tool(SEND_GUIDE_MEDIA, {"key": "test_clip"}, "t1"),
            say("This clip shows the revival process."),
        ], guide_media=dict(load_catalogue(), **clip))
```

- [ ] **Step 6: Run the tests**

Run: `PYTHONPATH="src;." python -m unittest tests.test_guide_media_catalogue tests.test_knowledge_migration tests.test_plan_conformance tests.test_media tests.test_guide_media_offers tests.test_guide_media_honesty -v`
Expected: all pass. A failure that names a removed key or id: move that test to a kept key (`soc_button` or `battery_onoff_switch`), keeping what it checks.

- [ ] **Step 7: Run the whole suite**

Run the full suite command. Expected: `OK`.

- [ ] **Step 8: Commit**

```bash
git add knowledge tests/test_guide_media_catalogue.py tests/test_knowledge_migration.py tests/test_plan_conformance.py
git commit -m "Guide pictures: only the SOC button and the On/Off switch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
