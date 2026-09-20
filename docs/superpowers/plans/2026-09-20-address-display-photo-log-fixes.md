# Address collection, display photo, log readability: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what the first real replacement order on a phone exposed: an address assembled across two customer messages was refused, so the model dropped the pincode and shipped without one; the bot never asked for a photo of the error code because the instruction was bundled with a tool the chat does not have; and the log now redacts every error code, so a refusal cannot be read.

**Architecture:** Three independent changes. The address backstop in `place_replacement_order` becomes token-based (every word of the confirmed address must have been typed by the customer or be on the record) and requires a six-digit pincode; the prompt collects the address pincode-first. The prompt asks for a display photo whenever a code is mentioned, independent of `lookup_error_code`. Redaction becomes parent-aware so an error envelope's `code` survives, and a bare six-digit message is logged as `[6 digits]` rather than `[code]`.

**Tech Stack:** Python 3.9, `unittest`, the existing `ToolRegistry`, `observability.redact_fields`, `prompts/battery_support.md`.

**Spec:** the transcript of conversation `b186a5dd` on 2026-09-20 and the owner's answers the same day: the OMS address is null for this customer; the display photo should always be asked for; pincode-first is the collection method; location sharing waits for WhatsApp.

## Global Constraints

- Every write stays a mock. Nothing network-bound.
- The model may only pass back an address the customer gave or the record holds; code decides whether it did.
- British English, no em dashes in any string the customer reads.
- Run tests with `../.venv/bin/python -m unittest <module> -v` from the repo root (in a worktree, the interpreter is `/Users/a646/Documents/Code/AFS_AI_testing/.venv/bin/python`).
- Commit after every task with the trailer exactly `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Never push.
- `tests.test_knowledge_migration`, `tests.test_retrieval_evals` and `tests.test_active_prompt` must pass after any prompt or record change.

---

## File structure

| File | Responsibility |
|---|---|
| `src/emotorad_ai/tools/mocks.py` | The address backstop inside `place_replacement_order`: pincode required, token provenance. |
| `src/emotorad_ai/conversation.py` | `address_tokens(text) -> Set[str]` helper beside `customer_texts`. |
| `prompts/battery_support.md` | Section 6a step 2 (pincode-first); section 5b0 (display photo on any code); section 5a0 (no narrating a picture's arrival). |
| `src/emotorad_ai/observability.py` | Parent-aware `redact_fields`; `[6 digits]` placeholder. |
| `tests/test_replacement_order_tool.py` | Address provenance and pincode tests. |
| `tests/test_history_window.py` | `address_tokens` tests beside `customer_texts`. |
| `tests/test_prompt_rules.py` | New: structural assertions on the prompt sentences these tasks add. |
| `tests/test_log_redaction.py` | Error-code and six-digit tests. |

---

### Task 1: Address provenance accepts what the customer actually typed, and requires a pincode

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (add `address_tokens` after `customer_texts`)
- Modify: `src/emotorad_ai/tools/mocks.py` (the block between `frame = bike["frame_number"]` and the coverage loop in `place_replacement_order`)
- Test: `tests/test_history_window.py`, `tests/test_replacement_order_tool.py`

**Interfaces:**
- Produces: `conversation.address_tokens(text: str) -> Set[str]`: lowercase alphanumeric tokens, punctuation stripped. New tool error `pincode_required`. `address_unconfirmed` keeps its code; its message changes to name the words that were not the customer's.

- [ ] **Step 1: Write the failing tests for the helper**

Append to `tests/test_history_window.py` before `if __name__`:

```python
from emotorad_ai.conversation import address_tokens


class AddressTokensTests(unittest.TestCase):
    """The unit the address backstop compares on. Punctuation and case are
    the model's; the words are the customer's."""

    def test_words_and_numbers_survive_punctuation(self):
        self.assertEqual(
            address_tokens("A1102, Park View City 1 - 122018."),
            {"a1102", "park", "view", "city", "1", "122018"},
        )

    def test_empty_is_empty(self):
        self.assertEqual(address_tokens(""), set())
        self.assertEqual(address_tokens(" , . "), set())
```

- [ ] **Step 2: Write the failing tests for the tool**

In `tests/test_replacement_order_tool.py`, the `_context` helper already accepts `customer_messages`. Replace the class `AddressProvenanceTests` if one exists, or add it, and update the two existing address tests as shown:

```python
class AddressProvenanceTests(unittest.TestCase):
    """What went wrong live on 2026-09-20, conversation b186a5dd.

    The customer gave the address in two messages: "It's A1102 Park view
    city 1", then "122018". The model combined them. The check required the
    combined string to appear verbatim in ONE customer message, refused it
    twice, and the model dropped the pincode to get past the check. The order
    shipped with no pincode and the bot told the customer logistics would
    "confirm the pincode when they call". A refusal a model can route around
    by degrading its input is worse than no check.

    The rule now: every word of the confirmed address must have been typed by
    the customer somewhere in this conversation or be on the record, and an
    Indian delivery address carries a six-digit pincode.
    """

    STREET = "It's A1102 Park view city 1"
    PIN = "122018"

    def test_an_address_given_across_two_messages_is_accepted(self):
        result = _place(
            _registry(), _context(customer_messages=(self.STREET, self.PIN)),
            confirmed_address="A1102 Park view city 1, 122018",
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["data"]["delivery_address"], "A1102 Park view city 1, 122018")

    def test_order_of_the_two_messages_does_not_matter(self):
        result = _place(
            _registry(), _context(customer_messages=(self.PIN, self.STREET)),
            confirmed_address="A1102 Park view city 1, 122018",
        )
        self.assertNotIn("error", result, result)

    def test_a_read_back_the_customer_agreed_to_is_accepted(self):
        """The bot restates the address and the customer says yes. That is
        exactly the confirmation the spec asks for and the old check ignored."""
        result = _place(
            _registry(), _context(customer_messages=(self.STREET, self.PIN, "Yes")),
            confirmed_address="A1102 Park view city 1, 122018",
        )
        self.assertNotIn("error", result, result)

    def test_a_word_the_customer_never_typed_is_refused(self):
        result = _place(
            _registry(), _context(customer_messages=(self.STREET, self.PIN)),
            confirmed_address="A1102 Park view city 1, Gurgaon, 122018",
        )
        self.assertEqual(result["error"]["code"], "address_unconfirmed")
        self.assertIn("gurgaon", result["error"]["message"].lower())

    def test_an_address_with_no_pincode_is_refused(self):
        """The refusal the model cannot route around by dropping something."""
        result = _place(
            _registry(), _context(customer_messages=(self.STREET,)),
            confirmed_address="A1102 Park view city 1",
        )
        self.assertEqual(result["error"]["code"], "pincode_required")

    def test_the_records_own_address_needs_no_customer_message(self):
        result = _place(_registry(), _context(customer_messages=()))
        self.assertNotIn("error", result, result)
```

Then update the two existing tests: `test_an_address_the_customer_typed_is_accepted` must use an address with a pincode (`"9 New Road, Mumbai 400001"` already has one; keep it) and `test_a_new_address_the_customer_gave_is_used` likewise. Delete `test_an_address_the_customer_never_gave_is_refused` if it duplicates the new invented-word test, or keep it if its address contains a pincode; if it does not, add one so it fails for the right reason (`address_unconfirmed`, not `pincode_required`).

- [ ] **Step 3: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_history_window tests.test_replacement_order_tool 2>&1 | tail -5`
Expected: `ImportError: cannot import name 'address_tokens'`, and once that import is stubbed, the two-message and pincode tests FAIL.

- [ ] **Step 4: Implement the helper**

Append to `src/emotorad_ai/conversation.py` after `customer_texts`:

```python
_ADDRESS_TOKEN = re.compile(r"[a-z0-9]+")


def address_tokens(text: str) -> Set[str]:
    """The comparable words of an address: lowercase, alphanumeric only.

    Punctuation, spacing and case are the model's formatting. The words and
    numbers are the customer's, and those are what the backstop checks. A
    set, because the customer may give the street in one message and the
    pincode in another, in either order, and the model may reorder them into
    a postal shape.
    """
    return set(_ADDRESS_TOKEN.findall((text or "").lower()))
```

Add `import re` and `Set` to the imports at the top of `conversation.py` if absent.

- [ ] **Step 5: Implement the check**

In `src/emotorad_ai/tools/mocks.py`, replace the block from the comment `# The address backstop:` through the `raise ToolError("address_unconfirmed", ...)` with:

```python
            # The address backstop, in two parts.
            #
            # First, a pincode. An Indian delivery address without one is
            # undeliverable, and on 2026-09-20 the model shipped an order
            # without one because dropping the pincode was the only way past
            # the old check. A refusal a model can route around by degrading
            # its input is worse than no check; this one cannot be.
            if not _PINCODE.search(confirmed_address):
                raise ToolError(
                    "pincode_required",
                    "The delivery address needs a six-digit pincode. Ask for it and pass the "
                    "full address with the pincode included.",
                )

            # Second, provenance. Every word of the address must have been
            # typed by the customer somewhere in this conversation, or be on
            # the record. Compared as a set of words, because the customer
            # gives the street in one message and the pincode in another and
            # the model reorders them into a postal shape. Without this a model
            # could invent an address and the tool would ship to it on the
            # model's word alone.
            wanted = address_tokens(confirmed_address)
            known = address_tokens(_clean(bike.get("full_address")) or "")
            for message in customer_messages:
                known |= address_tokens(message)
            unknown = sorted(wanted - known)
            if unknown:
                raise ToolError(
                    "address_unconfirmed",
                    "These parts of the address were not typed by the customer and are not on "
                    "the record: %s. Read the address back and pass what they confirmed."
                    % ", ".join(unknown),
                )
```

Add near the top of `mocks.py`, beside the other module constants:

```python
# An Indian pincode. Six digits, first digit 1 to 9.
_PINCODE = re.compile(r"\b[1-9]\d{5}\b")
```

and `from ..conversation import address_tokens` to the imports (check `re` is imported). Remove the now-unused `norm` lambda and `record_norm`.

- [ ] **Step 6: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_history_window tests.test_replacement_order_tool 2>&1 | tail -3`
Expected: OK

- [ ] **Step 7: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK. `tests/test_replacement_end_to_end.py` uses the fixture address, which has a pincode (411006).

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/tools/mocks.py tests/test_history_window.py tests/test_replacement_order_tool.py
git commit -m "Accept an address the customer typed across messages, and require a pincode

Conversation b186a5dd, 2026-09-20: the customer gave the street in one
message and the pincode in the next, the model combined them, and the check
refused the combination twice because it wanted the whole string in one
message. The model dropped the pincode to get past the check and shipped
without one. A refusal a model can route around by degrading its input is
worse than no check. Provenance is now per word, and a pincode is required.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The prompt collects the address pincode-first

**Files:**
- Modify: `prompts/battery_support.md` (section 6a step 2; section 6a step 3's error list; section 5a0)
- Create: `tests/test_prompt_rules.py`

**Interfaces:**
- Produces: nothing in code. The prompt is the deliverable; the test pins its sentences.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompt_rules.py`:

```python
"""Sentences in the live prompt that a code change depends on.

The prompt is the owner's, and it is tuned in the playground. These tests do
not judge its wording; they stop a retune from silently dropping a rule the
tools or the guardrails rely on. Each test names the code that relies on it.
"""

import pathlib
import unittest

PROMPT = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "battery_support.md"


class PromptRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = PROMPT.read_text(encoding="utf-8")

    def test_the_address_is_collected_pincode_first(self):
        """place_replacement_order refuses an address without a pincode."""
        self.assertIn("pincode first", self.text)

    def test_the_address_is_read_back_once_before_ordering(self):
        """The provenance check accepts a read-back the customer agreed to."""
        self.assertIn("read the whole address back once", self.text)

    def test_pincode_required_is_handled(self):
        self.assertIn("`pincode_required`", self.text)

    def test_a_picture_is_not_narrated_as_arriving(self):
        """Seen live: 'That comparison photo is on its way to you now', twice."""
        self.assertIn("do not narrate its arrival", self.text)

    def test_the_idempotency_key_carries_no_name(self):
        """A model-chosen key put the customer's name in the order store."""
        self.assertIn("frame number and the part", self.text)
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_prompt_rules -v 2>&1 | tail -3`
Expected: 5 failures.

- [ ] **Step 3: Edit the prompt**

In `prompts/battery_support.md`, section `## 6a. Replacing a part`, replace step 2 (the line beginning `2. Read the delivery address back`) with:

```markdown
2. The delivery address. If `delivery_address` on the warranty record is filled, read it back word for word: "Is this still the right address: …?" If they give another, or the record has none, collect it pincode first: ask for the six-digit pincode on its own, then for the house or flat, building, street and area. Compose those into one line with the pincode at the end and read the whole address back once: "So that's …, is that right?" Do not place anything until they have said yes. Never fill in a word they did not give you; the tool checks every word against what they typed.
```

In step 3's result list, add after the `already_placed` line:

```markdown
   - `pincode_required` — the address you passed has no six-digit pincode. Ask for it; do not place the order with an address that lacks one, and do not tell the customer someone will confirm it later.
   - `address_unconfirmed` — the message names the words that were not the customer's. Drop them or ask; never resubmit a shorter address to get past it.
```

In step 3's opening line, after "with the part, the confirmed address, and the frame", add: "and an idempotency key made from the frame number and the part, never from a name".

In section `### 5a0. Sending a guide picture`, after the sentence ending "say what it shows.", add:

```markdown
And do not narrate its arrival: no "the picture is on its way", no "coming through now". It is already there.
```

- [ ] **Step 4: Run to verify it passes, then the prompt validators**

Run: `../.venv/bin/python -m unittest tests.test_prompt_rules tests.test_active_prompt -v 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add prompts/battery_support.md tests/test_prompt_rules.py
git commit -m "Collect the address pincode first, read it back once, and stop narrating pictures

The OMS row was null, so the bot asked for the whole address in one go and
got it in two messages. Pincode first guarantees the field the tool now
requires and shortens what the customer has to type. Two smaller things from
the same transcript: the bot narrated a picture's arrival twice, and chose an
idempotency key with the customer's name in it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: A photo of the display on any error code, whether or not the lookup exists

**Files:**
- Modify: `prompts/battery_support.md` (section 5b0)
- Modify: `tests/test_prompt_rules.py`

- [ ] **Step 1: Write the failing test**

Append to `PromptRuleTests` in `tests/test_prompt_rules.py`:

```python
    def test_a_display_photo_is_asked_for_on_any_code(self):
        """lookup_error_code is not in the chat slice. The photo request was
        bundled with the call, so when the model skipped the call it skipped
        the photo too. Conversation b186a5dd never saw the display."""
        self.assertIn("whether or not `lookup_error_code` is available", self.text)
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_prompt_rules -v 2>&1 | tail -3`
Expected: 1 failure.

- [ ] **Step 3: Edit section 5b0**

Replace the paragraph beginning `**If they have a code:** call \`lookup_error_code\`` (it runs to "believe the photo and look it up again.") with:

```markdown
**If they have a code:** ask for a photo of the display showing it, in the same message and whether or not `lookup_error_code` is available to you. The whole case turns on those two characters: E-01 and E-07 are one glance apart and lead to completely different places, and the photo is what makes the code safe to act on. It also goes on the ticket. If `lookup_error_code` is available, call it with exactly what they read out in the same message; the lookup costs nothing and gives you a head start. If the photo shows something different from what they typed, believe the photo and look it up again. If the lookup is not available or the code is not documented for their bike, carry on with the symptom flow using the code as the first clue, and say plainly that you do not have that code documented rather than guessing what it means.
```

Keep the two lines above it (`If the bike does not power on, this does not apply`) and everything after it unchanged.

- [ ] **Step 4: Run to verify it passes, then the validators and the suite**

Run: `../.venv/bin/python -m unittest tests.test_prompt_rules tests.test_active_prompt -v 2>&1 | tail -3`
Expected: OK
Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add prompts/battery_support.md tests/test_prompt_rules.py
git commit -m "Ask for a photo of the display on any error code

The instruction existed and was bundled with a call to lookup_error_code,
which the chat agent does not have. The model skipped the call and the photo
with it. The photo matters on its own: E-01 and E-07 are one glance apart.
Decoupled, and the fallback when the lookup is absent or the code is not
documented is written down.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Error codes readable in the log again

**Files:**
- Modify: `src/emotorad_ai/observability.py` (`redact_fields`)
- Test: `tests/test_log_redaction.py`

**Interfaces:**
- Produces: `redact_fields(value, key=None, parent=None)`. The sensitive-key rule for `code` applies except when the parent key is `error`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_log_redaction.py` before `if __name__`:

```python
class ErrorCodesStayReadableTests(unittest.TestCase):
    """Redacting `code` to hide one-time codes also hid every error code.

    On 2026-09-20 a refused order was logged as {"error": {"code":
    "[redacted]", ...}} and the refusal could only be read from its message
    text. A log that cannot say which error fired is not doing its one job.
    """

    def setUp(self):
        self.log = EventLog(path=None)

    def test_an_error_envelopes_code_is_kept(self):
        self.log.tool_call("c1", "place_replacement_order", {}, {"error": {"code": "address_unconfirmed", "message": "m"}})
        self.assertIn("address_unconfirmed", str(self.log.events[-1]))

    def test_a_one_time_code_argument_is_still_hidden(self):
        self.log.tool_call("c1", "verify_identity", {"code": "039760"}, {"data": {}})
        self.assertNotIn("039760", str(self.log.events[-1]))

    def test_a_code_nested_under_data_is_still_hidden(self):
        """Only the error envelope is exempt. A tool that echoed a code back
        in its data would still be redacted."""
        self.log.tool_call("c1", "some_tool", {}, {"data": {"code": "039760"}})
        self.assertNotIn("039760", str(self.log.events[-1]))
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_log_redaction -v 2>&1 | tail -3`
Expected: `test_an_error_envelopes_code_is_kept` FAILS.

- [ ] **Step 3: Implement**

Replace `redact_fields` in `src/emotorad_ai/observability.py`:

```python
def redact_fields(value: Any, key: Optional[str] = None, parent: Optional[str] = None) -> Any:
    """Walk a logged structure and redact what should not be written down.

    Applied to whole events rather than to the few call sites that looked risky.
    `redact_pii` was on the inbound text alone, so a number the customer typed
    was redacted on the way in and written out in full a few lines later as a
    tool argument. Redacting at the sink means a field added later is covered by
    default instead of being covered only if someone remembers.

    One exemption, by parent: the `code` of an error envelope is the name of
    the error, not a secret. Redacting it hid every refusal reason in the log
    and left only the message text to read.
    """
    sensitive = key is not None and key.lower() in _SENSITIVE_KEYS and isinstance(value, str)
    if sensitive and not (key.lower() == "code" and parent == "error"):
        return "[redacted]"
    if isinstance(value, str):
        if _DATA_URI.match(value):
            return "[attachment]"
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_fields(v, k, key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_fields(v, key, parent) for v in value]
    return value
```

- [ ] **Step 4: Run to verify it passes, and the whole suite**

Run: `../.venv/bin/python -m unittest tests.test_log_redaction -v 2>&1 | tail -3`
Expected: OK
Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/observability.py tests/test_log_redaction.py
git commit -m "Keep an error envelope's code readable in the log

Redacting the code field to hide one-time codes also hid every error code, so
a refused order was logged as code [redacted] and could only be read from
its message. The error envelope is the one parent where code is a name, not
a secret.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: A bare six-digit message is logged as `[6 digits]`, not `[code]`

**Files:**
- Modify: `src/emotorad_ai/observability.py` (`redact_pii`)
- Test: `tests/test_log_redaction.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_log_redaction.py` before `if __name__`:

```python
class BareDigitsTests(unittest.TestCase):
    """A pincode and a one-time code are both six digits typed alone. The log
    cannot tell them apart, so it must not claim to. Conversation b186a5dd
    logged the customer's pincode as [code]."""

    def test_six_bare_digits_are_hidden_without_being_called_a_code(self):
        out = redact_pii("122018")
        self.assertNotIn("122018", out)
        self.assertEqual(out, "[6 digits]")

    def test_the_count_is_honest(self):
        self.assertEqual(redact_pii("1234"), "[4 digits]")
```

Update the existing `test_a_code_typed_as_a_message_is_redacted` in `ToolArgumentsAreRedactedTests` to assert `assertNotIn("039760", ...)` only (it may already), not the `[code]` placeholder.

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_log_redaction -v 2>&1 | tail -3`
Expected: `BareDigitsTests` FAIL (`[code]` != `[6 digits]`).

- [ ] **Step 3: Implement**

In `src/emotorad_ai/observability.py`, change the `_OTP_ALONE` branch of `redact_pii`:

```python
    match = _OTP_ALONE.match(text)
    if match:
        # A one-time code or a pincode, typed alone. Both are hidden; the
        # placeholder says only what it can know. Calling every bare
        # six-digit message a code logged a customer's pincode as [code].
        return "[%d digits]" % len(text.strip())
```

Update the comment above `_OTP_ALONE` to say it covers both.

- [ ] **Step 4: Run to verify it passes, and the whole suite**

Run: `../.venv/bin/python -m unittest tests.test_log_redaction -v 2>&1 | tail -3`
Expected: OK
Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/observability.py tests/test_log_redaction.py
git commit -m "Log bare digits as a count, not as a code

A pincode and a one-time code are both six digits typed alone. The log
cannot tell them apart and should not claim to.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-review

**Coverage of the three changes.** Address across messages, read-back plus yes, pincode required: Task 1 (code), Task 2 (prompt). Display photo decoupled from the lookup: Task 3. Error codes readable: Task 4. Pincode logged as `[code]`: Task 5. Not in this plan, on purpose: redacting home addresses in the log (owner has not decided), `lookup_error_code` in the chat slice (needs per-conversation bike context), an X1 C error-code table (content, not code), location sharing (WhatsApp channel).

**Placeholders.** None. Task 1 step 2 tells the implementer what to do with the two pre-existing address tests rather than pretending to know their exact current text.

**Type consistency.** `address_tokens(text) -> Set[str]` in Tasks 1 (helper, tool). `redact_fields(value, key, parent)` in Task 4; the recursive calls pass `key` as the child's parent for dicts and the same `parent` for list items. `_PINCODE` module constant in Task 1. Error codes `pincode_required` and `address_unconfirmed` in Tasks 1 and 2.
