# Replacement orders in OMS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `place_replacement_order` orders a part only when the evidence check passed, the date is from `em_purchase` and the part's own cover is active; orders go into a durable no-repeat ledger, and a worker sends them to OMS's `afs_order_add` behind a switch that stays off.

**Architecture:** The tool (in `tools/mocks.py`) gains the gate and the rider's details, and records into an order ledger (`fulfilment.ReplacementOrders` in memory, `stores.mongo.MongoOrderLedger` in MongoDB) with one open order per bike and part. `oms_afs.py` is the OMS WebSocket client (login, one socket per call, search, place); `order_worker.py` sends queued orders, looking OMS up by our reference before every send. `api.py` wires it behind `EMOTORAD_OMS_AFS_ORDERS`.

**Tech Stack:** Python 3, stdlib `unittest`, `websockets` 16 (sync client), `httpx`, pymongo/mongomock.

**Spec:** `docs/superpowers/specs/2026-10-10-oms-replacement-orders-design.md`

## Global Constraints

- An order is accepted only when: a listed bike; evidence verified for the part's fault; `ownership_source == "oms_purchase"` with a purchase date; the part's own component `active`; a part with `technician: false` and `ask: false`; the rider's name, email and mobile typed by them; no open order for the bike and part.
- Refusal codes: `evidence_not_verified`, `warranty_not_from_registration`, `part_not_in_warranty`, `coverage_undetermined`, `customer_name_required`, `customer_name_unconfirmed`, `email_invalid`, `email_unconfirmed`, `mobile_invalid`.
- Reference: `RO-` and seven digits, first `RO-1000001`.
- Statuses: `recorded`, `queued`, `sent`, `failed`, `cancelled`. Only `cancelled` clears `open_key`.
- OMS order: `order_type: "CO"`, `sale_type: "Warranty"`, `rate: 0`, `amount: 0`, `product_qty: 1`, `is_demo: false`, `idx: 1`, our reference in `ticket_number`, remark `Warranty replacement placed by the EMotorad support chatbot.`
- Retries after 1, 5, 15 and 60 minutes, then hourly; `failed` after 8 passes or 24 hours.
- The switch is `EMOTORAD_OMS_AFS_ORDERS`, exactly `on`; settings `EMOTORAD_OMS_WS_URL`, `EMOTORAD_OMS_LOGIN_URL`, `EMOTORAD_OMS_ADMIN_EMAIL`, `EMOTORAD_OMS_ADMIN_PASSWORD` required, `EMOTORAD_OMS_ADMIN_TOKEN` optional. No setting value is ever logged or put in an error.
- Never write a regex through a shell heredoc (CLAUDE.md): files with regexes are written with the editor tools.
- Tests: `python3 -m unittest discover -s tests -t .`; no network: the WebSocket and the login are injected fakes.

## Review Focus

1. OMS answers an error after creating the order: exactly one order, recorded as `sent`. Pinned in Task 5 (`test_an_error_after_the_order_was_made_ends_sent_with_one_order`).
2. A crash between sending and saving: the next pass finds the order and never sends again. Pinned in Task 5 (`test_a_pass_after_a_crash_finds_the_order_and_never_sends`).
3. A charger past 6 months on a bike whose battery is inside 12: refused. Pinned in Task 1 (`test_a_charger_past_its_own_term_is_refused_though_the_battery_is_covered`).
4. Two chats for the same bike and part, on two servers: one order. Pinned in Task 3 (`test_two_ledgers_on_one_database_make_one_order`).
5. The admin token refused mid-day: one login, one send, and no password in any log. Pinned in Task 4 (`test_a_refused_token_logs_in_once_and_retries`, `test_no_setting_value_reaches_an_error_or_a_log`).

---

### Task 1: The gate

**Files:**
- Modify: `src/emotorad_ai/fulfilment.py` (add `PART_COMPONENTS`, `PART_FAULT`)
- Modify: `src/emotorad_ai/tools/mocks.py` (`_coverage` bike dict; `place_replacement_order` injects and checks)
- Modify: `src/emotorad_ai/runtime.py` (fact `evidence_verified`, method `_verified_fault`)
- Create: `tests/test_replacement_gate.py`
- Modify: `tests/test_replacement_order_tool.py`, `tests/test_replacement_end_to_end.py` (fixtures, see Step 6)

**Interfaces:**
- Produces: `fulfilment.PART_COMPONENTS: Dict[str, str]`, `fulfilment.PART_FAULT: Dict[str, str]`; the lookup's bike carries `ownership_source`; the tool's optional inject `evidence_verified: Optional[str]` (`"battery"`, `"motor"`, `"any"` or None); `Runtime._verified_fault(state) -> Optional[str]`.

- [ ] **Step 1: Write the failing gate tests** (`tests/test_replacement_gate.py`)

```python
"""The order gate (spec 2026-10-10 replacement orders, section 1)."""

import unittest
from datetime import date

from emotorad_ai.fulfilment import ReplacementOrders
from emotorad_ai.tools import oms_db
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 10, 10)
PHONE = "+919876543210"
FRAME = "EMXP2025004417"
ADDRESS = "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006"
TYPED = ("I'm Test Rider, test.rider@example.com",)
RIDER = {"customer_name": "Test Rider", "email": "test.rider@example.com"}


def registration(bought=date(2026, 6, 1)):
    return oms_db.to_record({"frame_number": FRAME, "product_name": "EMX Plus", "purchase_date": bought,
                             "full_address": ADDRESS, "invoice_image": None, "status": None}, TODAY)


def order_bike(bought=date(2026, 6, 1)):
    return oms_db.order_to_record({"frame_number": FRAME, "product_name": "EMX Plus", "purchase_date": bought,
                                   "order_source": "End Customer"}, TODAY)


def coverage_for(record):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: [record])
    return registry.call("lookup_warranty_record", {}, ToolContext(conversation_id="c", phone=PHONE))


def place(record, verified="battery", part="battery", messages=TYPED, **extra):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: [record],
                              replacement_orders=ReplacementOrders())
    context = ToolContext(conversation_id="c1", phone=PHONE, late={
        "evidence_seen": lambda: True,
        "evidence_verified": lambda: verified,
        "coverage_result": lambda: coverage_for(record),
        "customer_messages": lambda: messages,
    })
    # Task 2 adds the rider's details here: dict(..., **RIDER).
    args = {"frame_number": FRAME, "part": part, "use_record_address": True, "idempotency_key": "k"}
    args.update(extra)
    return registry.call(PLACE_REPLACEMENT_ORDER, args, context)


def code_of(envelope):
    return (envelope.get("error") or {}).get("code")


class EvidenceTests(unittest.TestCase):
    def test_no_verified_fault_is_refused(self):
        self.assertEqual(code_of(place(registration(), verified=None)), "evidence_not_verified")

    def test_a_motor_verdict_does_not_order_a_battery(self):
        self.assertEqual(code_of(place(registration(), verified="motor")), "evidence_not_verified")

    def test_a_battery_verdict_orders_a_charger(self):
        self.assertTrue(place(registration(), verified="battery", part="charger")["ok"])

    def test_any_is_the_evidence_check_off(self):
        self.assertTrue(place(registration(), verified="any")["ok"])


class RegistrationTests(unittest.TestCase):
    def test_an_order_bike_is_refused(self):
        self.assertEqual(code_of(place(order_bike())), "warranty_not_from_registration")

    def test_an_undated_registration_is_refused(self):
        self.assertIn(code_of(place(registration(bought=None))),
                      ("warranty_not_from_registration", "coverage_undetermined"))

    def test_a_record_with_no_ownership_source_is_refused(self):
        record = dict(registration())
        del record["ownership_source"]
        self.assertEqual(code_of(place(record)), "warranty_not_from_registration")

    def test_the_lookup_carries_the_source_onto_each_bike(self):
        [bike] = coverage_for(registration())["data"]["bikes"]
        self.assertEqual(bike["ownership_source"], "oms_purchase")


class PartCoverTests(unittest.TestCase):
    def test_a_charger_past_its_own_term_is_refused_though_the_battery_is_covered(self):
        # Bought 2026-03-01: charger (6 months) ended 2026-08-31, battery (12) runs to 2027-02-28.
        record = registration(bought=date(2026, 3, 1))
        self.assertEqual(code_of(place(record, part="charger")), "part_not_in_warranty")
        self.assertTrue(place(record, part="battery")["ok"])

    def test_a_battery_past_its_term_is_refused(self):
        self.assertEqual(code_of(place(registration(bought=date(2025, 1, 1)))), "part_not_in_warranty")


class OcrTests(unittest.TestCase):
    def test_a_bike_dated_only_by_an_invoice_reading_is_never_ordered(self):
        # OCR writes invoice_readings and a ticket, never the record's date.
        self.assertIn(code_of(place(registration(bought=None))),
                      ("warranty_not_from_registration", "coverage_undetermined"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_replacement_gate -v`
Expected: FAIL/ERROR (`evidence_verified` unknown, no `ownership_source` on the bike, the charger ordered on the battery's cover).

- [ ] **Step 3: Implement**

In `src/emotorad_ai/fulfilment.py`, after `IN_FLIGHT_SECONDS`, add:

```python
# The warranty_terms component each orderable part is judged by (spec
# 2026-10-10 replacement orders, section 1, step 4): a charger by its own six
# months, never by the battery's twelve.
PART_COMPONENTS: Dict[str, str] = {"battery": "battery", "charger": "charger", "display": "display",
                                   "controller": "controller", "motor": "motor"}
# The evidence check's fault component each part belongs to (section 1, step 2).
PART_FAULT: Dict[str, str] = {"battery": "battery", "charger": "battery", "controller": "motor", "motor": "motor"}
```

In `src/emotorad_ai/tools/mocks.py`, `_coverage`'s first `bike = {...}` dict, add after `"frame_on_record": ...`:

```python
        # Where the purchase date came from (spec 2026-10-10): only
        # "oms_purchase" (em_purchase) may lead to a replacement order.
        "ownership_source": record.get("ownership_source"),
```

Change the import line `from ..fulfilment import ItemCodes, ReplacementOrders, decide, is_sure, load_parts_table` to also import `PART_COMPONENTS, PART_FAULT`.

In the `@registry.register(PLACE_REPLACEMENT_ORDER, ...)` call, change
`injects=("phone", "conversation_id", "evidence_seen", "coverage_result", "customer_messages"),` and `optional_injects=("unlisted_bike",),` to:

```python
            injects=("phone", "conversation_id", "evidence_seen", "coverage_result", "customer_messages"),
            optional_injects=("unlisted_bike", "evidence_verified"),
```

Add `evidence_verified: Optional[str] = None,` to the function's parameters (after `unlisted_bike`).

Replace the block from `# Coverage, from the lookup the runtime remembered.` down to and including the `if covered is False: raise ToolError("chargeable_not_supported", ...)` statement with:

```python
            # The gate (spec 2026-10-10 replacement orders, section 1), all in
            # code: the evidence, the registration's date, the part's own cover.
            if evidence_verified not in ("any", PART_FAULT.get(part)):
                raise ToolError(
                    "evidence_not_verified",
                    "The fault is not yet confirmed by the photos or video for this bike, so nothing can be "
                    "ordered. Ask for a short video that shows the fault, or raise a support ticket.",
                    remedy="collect_evidence",
                )
            entry = next((e for e in (coverage_result.get("data") or {}).get("bikes", [])
                          if e.get("frame_number") == frame), None)
            if entry is None:
                raise ToolError(
                    "coverage_undetermined",
                    "Coverage for this bike is not settled. Ask for the invoice before ordering.",
                    remedy="collect_purchase_proof",
                )
            if entry.get("ownership_source") != "oms_purchase" or not entry.get("purchase_date"):
                raise ToolError(
                    "warranty_not_from_registration",
                    "A replacement is ordered only for a bike whose purchase date is on its warranty "
                    "registration. Raise a support ticket with the evidence instead; the support team "
                    "will confirm the warranty.",
                    remedy="support_ticket",
                )
            component = PART_COMPONENTS.get(part)
            cover = next((c for c in entry.get("components") or [] if c.get("component") == component), None)
            if cover is None:
                raise ToolError(
                    "coverage_undetermined",
                    "The %s's cover is not on record. Raise a support ticket instead." % part,
                    remedy="support_ticket",
                )
            if cover.get("active") is not True:
                raise ToolError(
                    "part_not_in_warranty",
                    "The %s's warranty has ended, so this replacement is chargeable. Chargeable "
                    "replacements are handled by a person: raise a support ticket and hand over rather "
                    "than quote." % part,
                    remedy="human_handoff",
                )
            covered = True
```

(`covered` is still read by the existing `is_sure(...)` call until Task 3 removes it.)

In `src/emotorad_ai/runtime.py`, in the facts dict passed to `agent.run` (beside `"evidence_seen": lambda: state.evidence_seen,`), add:

```python
                # The fault the evidence proves, for the replacement order's
                # gate (spec 2026-10-10 replacement orders).
                "evidence_verified": lambda: self._verified_fault(state),
```

and after `_issue_verified`, add:

```python
    def _verified_fault(self, state: ConversationState) -> Optional[str]:
        """The fault the evidence proves, for the order gate (spec 2026-10-10
        replacement orders, section 1): with the evidence check on for this
        chat, the passing verdict's component ("battery" or "motor"); with it
        off, "any" once a photo or video reached the agent; otherwise None."""
        if self._evidence_gated(state):
            if verdict_passed(state.evidence_verdict, state):
                return (state.evidence_verdict or {}).get("component")
            return None
        return "any" if state.evidence_seen else None
```

- [ ] **Step 4: Run the gate tests**

Run: `python3 -m unittest tests.test_replacement_gate -v`
Expected: PASS.

- [ ] **Step 5: Run the order tests to see what the gate broke**

Run: `python3 -m unittest tests.test_replacement_order_tool tests.test_replacement_end_to_end tests.test_amigo_bikes_without_frame tests.test_unlisted_bike_runtime -v`
Expected: failures in the first two files only (no `evidence_verified`, no `ownership_source`, `chargeable_not_supported` renamed).

- [ ] **Step 6: Update the order tests to the gate**

In `tests/test_replacement_order_tool.py`:
- `COVERED`, `NOT_COVERED`, `UNDETERMINED`: add `"ownership_source": "oms_purchase", "purchase_date": "2026-03-01"` to each bike, and `"components"`: `[{"component": "battery", "active": True}, {"component": "charger", "active": True}]` to `COVERED`, `[{"component": "battery", "active": False}, {"component": "charger", "active": False}]` to `NOT_COVERED`; `UNDETERMINED` gets `"purchase_date": None` and no components.
- `_context(...)` gains `verified="any"` and passes `"evidence_verified": lambda: verified` in `late`.
- `test_out_of_warranty_is_refused_in_this_build` expects `part_not_in_warranty`.
- `test_undetermined_coverage_is_refused_with_the_invoice_remedy` expects `warranty_not_from_registration` (the undated bike now fails the registration check first); rename it `test_undetermined_coverage_is_refused`.
- `ApprovalModeTests.test_bot_mode_approves_even_without_a_photo` and `test_reasonable_mode_holds_a_case_with_no_photo` call `_context(evidence_seen=False)`: change them to `_context(verified=None)` and expect `evidence_not_verified` (an unverified fault is never ordered, whatever the mode).

In `tests/test_replacement_end_to_end.py`, `_runtime` passes `warranty_source=lambda phone: [REGISTERED]` where, at module level:

```python
from emotorad_ai.tools import oms_db

REGISTERED = oms_db.to_record({"frame_number": "EMXP2025004417", "product_name": "EMX Plus",
                               "purchase_date": date(2026, 3, 1), "full_address": ADDRESS,
                               "invoice_image": None, "status": None}, date(2026, 7, 28))
```

- [ ] **Step 7: Run them again**

Run: `python3 -m unittest tests.test_replacement_gate tests.test_replacement_order_tool tests.test_replacement_end_to_end tests.test_amigo_bikes_without_frame tests.test_unlisted_bike_runtime -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/fulfilment.py src/emotorad_ai/tools/mocks.py src/emotorad_ai/runtime.py tests/test_replacement_gate.py tests/test_replacement_order_tool.py tests/test_replacement_end_to_end.py
git commit -m "feat(replacement-orders): the gate: verified evidence, a registration's date, the part's own cover"
```

---

### Task 2: The rider's details

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py` (`place_replacement_order` parameters and checks)
- Modify: `prompts/battery_support.md` (section 6a)
- Modify: `tests/test_replacement_gate.py`, `tests/test_replacement_order_tool.py`, `tests/test_replacement_end_to_end.py`

**Interfaces:**
- Consumes: Task 1's tool.
- Produces: tool arguments `customer_name: str`, `email: str`, `mobile: Optional[str]`; the checked values `rider = {"name", "email", "mobile"}` (mobile ten digits) available to Task 3.

- [ ] **Step 1: Write the failing tests**

In `tests/test_replacement_gate.py`, change `place()`'s `args = {...}` line to
`args = dict({"frame_number": FRAME, "part": part, "use_record_address": True, "idempotency_key": "k"}, **RIDER)`
(and drop the comment above it), then append before `if __name__`:

```python
class DetailsTests(unittest.TestCase):
    def test_a_name_the_rider_never_typed_is_refused(self):
        self.assertEqual(code_of(place(registration(), customer_name="Someone Else")), "customer_name_unconfirmed")

    def test_no_name_is_refused(self):
        self.assertEqual(code_of(place(registration(), customer_name=" ")), "customer_name_required")

    def test_a_malformed_email_is_refused(self):
        for email in ("test.rider", "test rider@example.com", "a@b", "@example.com"):
            with self.subTest(email=email):
                self.assertEqual(code_of(place(registration(), email=email)), "email_invalid")

    def test_an_email_the_rider_never_typed_is_refused(self):
        self.assertEqual(code_of(place(registration(), email="other@example.com")), "email_unconfirmed")

    def test_a_mobile_must_be_indian_and_typed(self):
        self.assertEqual(code_of(place(registration(), mobile="+6591234567")), "mobile_invalid")
        self.assertEqual(code_of(place(registration(), mobile="9123456780")), "mobile_invalid")
        typed = TYPED + ("call me on 91234 56780",)
        self.assertTrue(place(registration(), messages=typed, mobile="9123456780")["ok"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_replacement_gate.DetailsTests -v`
Expected: FAIL/ERROR (`unexpected keyword argument 'customer_name'`).

- [ ] **Step 3: Implement** in `tools/mocks.py`

Add near the top-level helpers (with the editor, not a heredoc):

```python
# An email the rider typed: one @, a dot in the domain, no spaces (spec
# 2026-10-10 replacement orders, section 2). OMS requires one on a customer order.
_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")
```

Add to the tool's `parameters`:

```python
                "customer_name": {"type": "string",
                                  "description": "The customer's full name, as they typed it and confirmed."},
                "email": {"type": "string", "description": "The customer's email, as they typed it and confirmed."},
                "mobile": {"type": "string",
                           "description": "Only if they gave a number other than the one they are chatting from."},
```

Add to the function's parameters: `customer_name: Optional[str] = None, email: Optional[str] = None, mobile: Optional[str] = None,`.

Insert after the gate block of Task 1 (after `covered = True`):

```python
            # The rider's details (section 2): each typed by the rider in this chat.
            from .oms_db import last_ten

            name = " ".join((customer_name or "").split())
            if not name:
                raise ToolError("customer_name_required",
                                "Ask for the customer's full name, read it back, and pass it as customer_name.")
            typed = set()
            for message in customer_messages:
                typed |= address_tokens(message)
            stray = sorted(address_tokens(name) - typed)
            if stray:
                raise ToolError("customer_name_unconfirmed",
                                "These words of the name were not typed by the customer (%s). Ask for their "
                                "name and pass exactly what they confirmed." % ", ".join(stray))
            mail = (email or "").strip()
            if not _EMAIL.fullmatch(mail):
                raise ToolError("email_invalid", "Ask for the customer's email address and pass it as they typed it.")
            if not any(mail.lower() in message.lower() for message in customer_messages):
                raise ToolError("email_unconfirmed",
                                "That email was not typed by the customer. Ask for it and pass it as they typed it.")
            try:
                mobile_ten = last_ten(mobile) if mobile else last_ten(phone)
            except ValueError:
                raise ToolError("mobile_invalid", "Ask for an Indian mobile number the order can be delivered to.")
            if mobile and not any(mobile_ten in re.sub(r"[^0-9]", "", message) for message in customer_messages):
                raise ToolError("mobile_invalid",
                                "That mobile number was not typed by the customer. Ask for it and pass it as typed.")
            rider = {"name": name, "email": mail, "mobile": mobile_ten}
```

(`re` and `address_tokens` are already imported in `mocks.py`; if `re` is not, add `import re`.)

- [ ] **Step 4: Update the existing order tests for the new arguments**

- `tests/test_replacement_order_tool.py`: `_place` adds `"customer_name": "Test Rider", "email": "test.rider@example.com"` to `args` before `args.update(overrides)`; `_context` prepends `"I'm Test Rider, test.rider@example.com"` to `customer_messages`.
- `tests/test_replacement_end_to_end.py`: add `RIDER = {"customer_name": "Test Rider", "email": "test.rider@example.com"}` and `DETAILS = " I'm Test Rider, test.rider@example.com."`; every `call_tool(PLACE_REPLACEMENT_ORDER, {...})` gets `**RIDER` in its dict; `_send(...)` gains `details=False` and appends `DETAILS` to the text when true; each test passes `details=True` on the `_send` whose scripted responses contain the order call (in tests that place twice across conversations, each such `send`).

- [ ] **Step 5: The prompt** — in `prompts/battery_support.md` section 6a, insert a new step before step 2 ("The delivery address."):

```markdown
2. The customer's details. Ask for their full name and their email, and a mobile number only if the order should go to a number other than the one they are chatting from. Read them back with the address in one message and wait for their yes. Pass them as `customer_name`, `email` and (if given) `mobile`, exactly as they typed them; the tool checks every word against what they typed.
```

and renumber the following steps (2 → 3, 3 → 4, 4 → 5). In the tool signature line near the top (`place_replacement_order(part, confirmed_address, frame_number?)`), write `place_replacement_order(part, customer_name, email, mobile?, use_record_address | address, frame_number?)`. In the list of answers under the tool call, add after the `address_unconfirmed` bullet:

```markdown
   - `customer_name_required`, `customer_name_unconfirmed`, `email_invalid`, `email_unconfirmed`, `mobile_invalid` — ask for that detail again, read it back, and pass exactly what they typed.
   - `evidence_not_verified` — the fault is not confirmed by their photos or video yet. Ask for a short video that shows it; never order without it.
   - `warranty_not_from_registration` — the bike's purchase date is not on its warranty registration. Raise a support ticket with the evidence and tell them the support team will confirm the warranty; never order.
   - `part_not_in_warranty` — that part's own cover has ended. Say plainly it is chargeable, raise the ticket and hand over. Never quote a figure.
```

and delete the `chargeable_not_supported` bullet.

- [ ] **Step 6: Run the order tests and the prompt tests**

Run: `python3 -m unittest tests.test_replacement_gate tests.test_replacement_order_tool tests.test_replacement_end_to_end tests.test_prompt_rules tests.test_one_step_replies -v`
Expected: PASS. If `test_prompt_rules` pins a line you changed, update its expected text to the new wording.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/tools/mocks.py prompts/battery_support.md tests/test_replacement_gate.py tests/test_replacement_order_tool.py tests/test_replacement_end_to_end.py tests/test_prompt_rules.py
git commit -m "feat(replacement-orders): the rider's name, email and mobile, typed and confirmed"
```

---

### Task 3: The order ledger and the product ids

**Files:**
- Modify: `src/emotorad_ai/fulfilment.py` (replace `ItemCodes`, `ReplacementOrders`, `is_sure`, `decide`)
- Modify: `src/emotorad_ai/stores/mongo.py` (`REPLACEMENT_ORDERS`, index, `MongoOrderLedger`, erasure)
- Modify: `src/emotorad_ai/wiring.py` (`Stores.replacement_orders`, `replacement_orders_status`)
- Modify: `src/emotorad_ai/tools/mocks.py` (the tool records into the ledger; `build_registry(product_ids=..., orders_live=...)`)
- Modify: `src/emotorad_ai/guardrails.py` (`_ORDER_ID`)
- Modify: `scripts/mongo_setup.py` (`PERMANENT`)
- Create: `tests/test_order_ledger.py`
- Modify: `tests/test_fulfilment.py`, `tests/test_replacement_order_tool.py`, every test with an `RO-` literal

**Interfaces:**
- Consumes: Task 2's `rider`.
- Produces:
  - `fulfilment.order_reference(n: int) -> str`, `fulfilment.open_key(frame: str, part: str) -> str`, `fulfilment.FIRST_ORDER_NUMBER = 1000001`
  - `fulfilment.ProductIds(table=None)` with `.resolve(product_name, part) -> Optional[str]` and `.has_part(parts: Iterable[str]) -> bool`
  - Ledger interface (both classes): `next_reference() -> str`; `insert(order: dict) -> Tuple[dict, bool]` (created?); `open_order(frame, part) -> Optional[dict]`; `get(reference) -> Optional[dict]`; `due(now_iso: str, limit: int = 20) -> List[dict]`; `claim(reference, now_iso, lease_until_iso) -> Optional[dict]`; `save(order: dict) -> None`; `cancel(reference) -> None`
  - `fulfilment.ReplacementOrders` (memory) and `stores.mongo.MongoOrderLedger(db)` with `index_ready() -> bool`
  - `build_registry(..., replacement_orders=None, product_ids=None, orders_live=False)`; `item_codes` and `approval_mode` parameters removed
  - Order record keys: `_id`, `open_key`, `frame_number`, `part`, `product_id`, `product_name`, `customer` (`name`, `email`, `mobile`, `address`: `line1`, `line2`, `pincode`), `delivery_address`, `phone`, `conversation_id`, `ticket_reference`, `status`, `oms` (`intent_at`, `order_code`, `order_id`, `passes`, `last_error`), `next_attempt_at`, `lease_until`, `created_at`, `updated_at`

- [ ] **Step 1: Write the failing ledger tests** (`tests/test_order_ledger.py`)

```python
"""The replacement order ledger (spec 2026-10-10 replacement orders, section 3)."""

import unittest

import mongomock

from emotorad_ai.fulfilment import ProductIds, ReplacementOrders, open_key, order_reference
from emotorad_ai.stores.mongo import MongoOrderLedger, ensure_indexes


def order(ledger, frame="EMXP0001", part="battery", status="queued"):
    ref = ledger.next_reference()
    return {"_id": ref, "open_key": open_key(frame, part), "frame_number": frame, "part": part,
            "product_id": "p1", "product_name": "EMX Plus", "customer": {}, "delivery_address": "x",
            "phone": "+919876543210", "conversation_id": "c1", "ticket_reference": None, "status": status,
            "oms": {"intent_at": None, "order_code": None, "order_id": None, "passes": 0, "last_error": None},
            "next_attempt_at": "2026-10-10T00:00:00+00:00", "lease_until": None,
            "created_at": "2026-10-10T00:00:00+00:00", "updated_at": "2026-10-10T00:00:00+00:00"}


def ledgers():
    db = mongomock.MongoClient().db
    ensure_indexes(db)
    return {"memory": ReplacementOrders(), "mongodb": MongoOrderLedger(db)}


class ReferenceTests(unittest.TestCase):
    def test_references_are_ro_and_seven_digits_from_one_million_and_one(self):
        self.assertEqual(order_reference(1000001), "RO-1000001")
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                self.assertEqual([ledger.next_reference(), ledger.next_reference()], ["RO-1000001", "RO-1000002"])

    def test_the_open_key_ignores_case_and_spaces(self):
        self.assertEqual(open_key("emx p0001 ", "battery"), "EMXP0001|battery")


class OneOpenOrderTests(unittest.TestCase):
    def test_a_second_open_order_for_the_bike_and_part_returns_the_first(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, created = ledger.insert(order(ledger))
                self.assertTrue(created)
                again, created = ledger.insert(order(ledger))
                self.assertFalse(created)
                self.assertEqual(again["_id"], first["_id"])
                self.assertEqual(ledger.open_order("emxp0001", "battery")["_id"], first["_id"])

    def test_another_part_is_another_order(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                ledger.insert(order(ledger))
                self.assertTrue(ledger.insert(order(ledger, part="charger"))[1])

    def test_after_a_cancellation_a_new_order_is_allowed(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger))
                ledger.cancel(first["_id"])
                self.assertIsNone(ledger.open_order("EMXP0001", "battery"))
                self.assertTrue(ledger.insert(order(ledger))[1])

    def test_a_failed_order_still_blocks_a_new_one(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger, status="failed"))
                self.assertFalse(ledger.insert(order(ledger))[1])

    def test_two_ledgers_on_one_database_make_one_order(self):
        db = mongomock.MongoClient().db
        ensure_indexes(db)
        one, two = MongoOrderLedger(db), MongoOrderLedger(db)
        first, _ = one.insert(order(one))
        again, created = two.insert(order(two))
        self.assertFalse(created)
        self.assertEqual(again["_id"], first["_id"])


class WorkQueueTests(unittest.TestCase):
    def test_due_and_claim_take_a_queued_order_once(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger))
                ledger.insert(order(ledger, frame="EMXP0002", status="recorded"))
                due = ledger.due("2026-10-10T00:01:00+00:00")
                self.assertEqual([o["_id"] for o in due], [first["_id"]])
                self.assertIsNotNone(ledger.claim(first["_id"], "2026-10-10T00:01:00+00:00",
                                                  "2026-10-10T00:06:00+00:00"))
                self.assertIsNone(ledger.claim(first["_id"], "2026-10-10T00:02:00+00:00",
                                               "2026-10-10T00:07:00+00:00"))
                self.assertIsNotNone(ledger.claim(first["_id"], "2026-10-10T00:06:01+00:00",
                                                  "2026-10-10T00:11:01+00:00"))

    def test_save_keeps_what_the_worker_wrote(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger))
                first["status"], first["oms"]["order_code"] = "sent", "AFS/26-27/EC/1"
                ledger.save(first)
                self.assertEqual(ledger.get(first["_id"])["oms"]["order_code"], "AFS/26-27/EC/1")


class ProductIdTests(unittest.TestCase):
    def test_empty_until_agreed(self):
        self.assertIsNone(ProductIds().resolve("EMX Plus", "battery"))
        self.assertFalse(ProductIds().has_part(["motor", "controller"]))

    def test_resolves_by_model_and_part(self):
        ids = ProductIds({"EMX Plus": {"battery": "uuid-1"}})
        self.assertEqual(ids.resolve("EMX Plus Black-XX01/EM02", "battery"), "uuid-1")
        self.assertTrue(ids.has_part(["battery"]))


class IndexTests(unittest.TestCase):
    def test_the_index_is_reported(self):
        db = mongomock.MongoClient().db
        self.assertFalse(MongoOrderLedger(db).index_ready())
        ensure_indexes(db)
        self.assertTrue(MongoOrderLedger(db).index_ready())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_order_ledger -v`
Expected: ERROR: `cannot import name 'ProductIds'`.

- [ ] **Step 3: Implement the in-memory ledger and the product ids** in `src/emotorad_ai/fulfilment.py`

Delete `class ItemCodes`, `class ReplacementOrders` (the old one), `def is_sure` and `def decide`, and the now unused imports (`itertools`, `time`, `field` if unused). Add (with the editor; it has a regex):

```python
# Replacement orders (spec 2026-10-10 replacement orders, section 3).
FIRST_ORDER_NUMBER = 1000001
OPEN_STATUSES = ("recorded", "queued", "sent", "failed")


def order_reference(number: int) -> str:
    return "RO-%07d" % number


def open_key(frame_number: str, part: str) -> str:
    """One open order per bike and part: the frame upper-cased, no spaces."""
    return "%s|%s" % (re.sub(r"\s+", "", frame_number or "").upper(), part)


class ProductIds:
    """(bike model, part) -> OMS product id (em_product.id) for afs_order_add.
    Empty until the product ids are agreed (spec section 6); an order with no
    product id is recorded and never sent."""

    def __init__(self, table: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._table = {model: dict(parts) for model, parts in (table or {}).items()}

    def resolve(self, product_name: Optional[str], part: str) -> Optional[str]:
        if not product_name:
            return None
        return self._table.get(_model_name(product_name), {}).get(part)

    def has_part(self, parts: Iterable[str]) -> bool:
        wanted = set(parts)
        return any(wanted & set(entries) for entries in self._table.values())


class ReplacementOrders:
    """The order ledger in this process's memory: tests, offline, and until
    mongo_setup.py has made the index (stores.mongo.MongoOrderLedger is the
    same contract). One open order per bike and part."""

    mode = "memory"

    def __init__(self) -> None:
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def next_reference(self) -> str:
        with self._lock:
            self._seq += 1
            return order_reference(FIRST_ORDER_NUMBER - 1 + self._seq)

    def insert(self, order: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        with self._lock:
            key = order.get("open_key")
            for existing in self._orders.values():
                if key and existing.get("open_key") == key:
                    return copy.deepcopy(existing), False
            self._orders[order["_id"]] = copy.deepcopy(order)
            return copy.deepcopy(order), True

    def open_order(self, frame_number: str, part: str) -> Optional[Dict[str, Any]]:
        key = open_key(frame_number, part)
        with self._lock:
            found = next((o for o in self._orders.values() if o.get("open_key") == key), None)
            return copy.deepcopy(found) if found else None

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._orders.get(reference)
            return copy.deepcopy(found) if found else None

    def due(self, now_iso: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            ready = [o for o in self._orders.values()
                     if o.get("status") == "queued" and (o.get("next_attempt_at") or "") <= now_iso]
            ready.sort(key=lambda o: o.get("next_attempt_at") or "")
            return [copy.deepcopy(o) for o in ready[:limit]]

    def claim(self, reference: str, now_iso: str, lease_until_iso: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._orders.get(reference)
            if found is None or found.get("status") != "queued":
                return None
            if found.get("lease_until") and found["lease_until"] > now_iso:
                return None
            found["lease_until"] = lease_until_iso
            return copy.deepcopy(found)

    def save(self, order: Dict[str, Any]) -> None:
        with self._lock:
            self._orders[order["_id"]] = copy.deepcopy(order)

    def cancel(self, reference: str) -> None:
        with self._lock:
            found = self._orders.get(reference)
            if found is not None:
                found["status"] = "cancelled"
                found.pop("open_key", None)
```

Add `import copy`, `import re` and `from typing import Iterable, List, Tuple` to the imports, and update the module docstring's last paragraph to: `Orders are recorded in a ledger (spec 2026-10-10 replacement orders) and sent to OMS by order_worker.py when the switch is on.`

- [ ] **Step 4: Implement the MongoDB ledger** in `src/emotorad_ai/stores/mongo.py`

Add beside the other collection names:

```python
REPLACEMENT_ORDERS = "replacement_orders"
# The counters document behind RO-1000001, RO-1000002, ...
ORDER_COUNTER = "replacement_orders"
```

Add to `INDEXES`:

```python
    REPLACEMENT_ORDERS: [
        # One open order per bike and part (spec 2026-10-10 replacement
        # orders, section 3): a cancelled order unsets open_key, and sparse
        # lets any number of them sit without one.
        ([("open_key", 1)], {"name": "one_open_per_bike_part", "unique": True, "sparse": True}),
        # The worker's due query.
        ([("status", 1), ("next_attempt_at", 1)], {"name": "due"}),
        ([("conversation_id", 1)], {"name": "conversation"}),
    ],
```

Add the class after `MongoTicketStore`:

```python
class MongoOrderLedger:
    """The replacement order ledger for every server (spec 2026-10-10
    replacement orders, section 3), held to fulfilment.ReplacementOrders'
    contract. The unique sparse index on open_key decides a race."""

    mode = "mongodb"

    def __init__(self, db: Any) -> None:
        self._orders = db[REPLACEMENT_ORDERS]
        self._counters = db[COUNTERS]

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    def index_ready(self) -> bool:
        try:
            info = self._orders.index_information()
        except PyMongoError:
            return False
        return any(spec.get("unique") and [name for name, _ in spec.get("key", [])] == ["open_key"]
                   for spec in info.values())

    def next_reference(self) -> str:
        from ..fulfilment import FIRST_ORDER_NUMBER, order_reference

        for attempt in (1, 2):
            try:
                doc = self._guard("find_one_and_update", lambda: self._counters.find_one_and_update(
                    {"_id": ORDER_COUNTER}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER))
                break
            except DuplicateKeyError:
                if attempt == 2:
                    raise StoreUnavailable("MongoDB find_one_and_update failed (DuplicateKeyError)") from None
        return order_reference(FIRST_ORDER_NUMBER - 1 + int(doc["seq"]))

    def insert(self, order: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        try:
            self._guard("insert_one", lambda: self._orders.insert_one(copy.deepcopy(order)))
        except DuplicateKeyError:
            existing = self._guard("find_one", lambda: self._orders.find_one({"open_key": order.get("open_key")}))
            if existing is None:
                raise StoreUnavailable("MongoDB insert_one failed (DuplicateKeyError)") from None
            return existing, False
        return copy.deepcopy(order), True

    def open_order(self, frame_number: str, part: str) -> Optional[Dict[str, Any]]:
        from ..fulfilment import open_key

        return self._guard("find_one", lambda: self._orders.find_one({"open_key": open_key(frame_number, part)}))

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        return self._guard("find_one", lambda: self._orders.find_one({"_id": reference}))

    def due(self, now_iso: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._guard("find", lambda: list(self._orders.find(
            {"status": "queued", "next_attempt_at": {"$lte": now_iso}}).sort("next_attempt_at", 1).limit(limit)))

    def claim(self, reference: str, now_iso: str, lease_until_iso: str) -> Optional[Dict[str, Any]]:
        return self._guard("find_one_and_update", lambda: self._orders.find_one_and_update(
            {"_id": reference, "status": "queued",
             "$or": [{"lease_until": None}, {"lease_until": {"$lte": now_iso}}]},
            {"$set": {"lease_until": lease_until_iso}}, return_document=ReturnDocument.AFTER))

    def save(self, order: Dict[str, Any]) -> None:
        self._guard("replace_one", lambda: self._orders.replace_one({"_id": order["_id"]}, copy.deepcopy(order)))

    def cancel(self, reference: str) -> None:
        self._guard("update_one", lambda: self._orders.update_one(
            {"_id": reference}, {"$set": {"status": "cancelled"}, "$unset": {"open_key": ""}}))
```

Erasure: in `delete_person`'s `counts` tuple add `REPLACEMENT_ORDERS`; in `conversations_of`'s list add `(REPLACEMENT_ORDERS, "conversation_id")` only if the order records carry `user_key` — they do not, so instead add to `delete_conversation`'s dict:

```python
            # Replacement orders (spec 2026-10-10): the rider's details are on them.
            REPLACEMENT_ORDERS: self._remove(REPLACEMENT_ORDERS, {"conversation_id": conversation_id}, dry_run),
```

In `scripts/mongo_setup.py`, import `REPLACEMENT_ORDERS` and add it to `PERMANENT`.

- [ ] **Step 5: The stores bundle** — in `src/emotorad_ai/wiring.py`:

```python
    # The replacement order ledger (spec 2026-10-10 replacement orders).
    replacement_orders: Any = None
    replacement_orders_status: str = "memory"
```

added to `Stores`; in `build_stores`, the memory branch passes `replacement_orders=ReplacementOrders()` (import from `.fulfilment`), and the MongoDB branch:

```python
    ledger = MongoOrderLedger(db)
    if ledger.index_ready():
        orders, orders_status = ledger, "mongodb"
    else:
        from .fulfilment import ReplacementOrders

        orders, orders_status = ReplacementOrders(), "memory: index missing, run scripts/mongo_setup.py"
        if log is not None:
            log.emit("replacement_orders_index_missing", "replacement_orders", level="error")
```

passing `replacement_orders=orders, replacement_orders_status=orders_status` to `Stores(...)` (import `MongoOrderLedger` with the other Mongo stores). If `log.emit` takes no `level`, drop that keyword.

- [ ] **Step 6: The tool records into the ledger** — in `tools/mocks.py`:
  - `build_registry`'s parameters: replace `item_codes=None, approval_mode=...` with `product_ids=None, orders_live: bool = False`; inside, `codes = product_ids or ProductIds()`; change the import to `from ..fulfilment import PART_COMPONENTS, PART_FAULT, ProductIds, load_parts_table`.
  - Replace everything from `existing = replacement_orders.in_flight(frame, part)` to the end of the function with:

```python
            existing = replacement_orders.open_order(frame, part)
            if existing is None:
                record_line = delivery_address
                if use_record_address:
                    pins = re.findall(r"(?<![0-9])[1-9][0-9]{5}(?![0-9])", record_line)
                    oms_address = {"line1": record_line, "line2": "", "pincode": pins[-1] if pins else ""}
                else:
                    fields = parsed.customer_fields()
                    oms_address = {
                        "line1": ", ".join(v for v in (fields.get("house_or_flat"), fields.get("building_or_street")) if v),
                        "line2": ", ".join(v for v in (fields.get("area"), fields.get("landmark"),
                                                       place.district, place.state) if v),
                        "pincode": parsed.pincode,
                    }
                product_id = codes.resolve(bike.get("product_name"), part)
                now = utc_now_iso()
                reference = replacement_orders.next_reference()
                existing, created = replacement_orders.insert({
                    "_id": reference, "open_key": open_key(frame, part), "frame_number": frame, "part": part,
                    "product_id": product_id, "product_name": bike.get("product_name"),
                    "customer": dict(rider, address=oms_address), "delivery_address": delivery_address,
                    "phone": phone, "conversation_id": conversation_id, "ticket_reference": None,
                    "status": "queued" if (orders_live and product_id) else "recorded",
                    "oms": {"intent_at": None, "order_code": None, "order_id": None, "passes": 0, "last_error": None},
                    "next_attempt_at": now, "lease_until": None, "created_at": now, "updated_at": now,
                })
                if created:
                    return ok({"order_id": existing["_id"], "status": existing["status"], "part": part,
                               "delivery_address": existing["delivery_address"], "already_placed": False})
            return ok({
                "order_id": existing["_id"], "status": existing["status"], "part": part,
                "delivery_address": existing.get("delivery_address"), "already_placed": True,
                "placed_at_utc": existing.get("created_at"),
                "note": ("Nothing new was placed. A replacement for this bike and part was placed earlier, at "
                         "the time and to the address above. Tell the customer it already exists, quote the "
                         "order id and that address, and if they want the address changed, hand over to the "
                         "support team."),
            })
```

  (import `open_key` from `..fulfilment`; `parsed` and `place` exist only on the `address` path, which is the only path that reads them.)
  - Delete the `covered = True` line of Task 1 and the old `item_code`/`is_sure`/`decide` code.
  - In the tool's description, replace "and whether it can be approved now" with "and whether one is already placed".

- [ ] **Step 7: The order-claim post-check** — in `src/emotorad_ai/guardrails.py` (editor, not a heredoc) change `_ORDER_ID = re.compile(r"\bRO-\d{5}\b")` to `_ORDER_ID = re.compile(r"\bRO-\d{7}\b")`.

- [ ] **Step 8: Update the tests to the ledger**
  - Every `RO-` literal of five digits in `tests/` becomes seven digits by this rule: `RO-` + `10` + the five digits (`RO-00001` → `RO-1000001`, `RO-12345` → `RO-1012345`). Run it as a Python script file you write with the editor (it has a regex), over `tests/*.py`, with `re.sub(r"RO-(\d{5})\b", lambda m: "RO-10" + m.group(1), text)`.
  - `tests/test_fulfilment.py`: delete the `ItemCodes`, `is_sure`, `decide` and old `ReplacementOrders` tests (the ledger's are in `tests/test_order_ledger.py`); keep the parts-table tests.
  - `tests/test_replacement_order_tool.py`: `_registry(...)` becomes `build_registry(today=..., replacement_orders=orders or ReplacementOrders(), warranty_source=warranty_source)` (no `item_codes`, no `approval_mode`); delete `ItemCodeMissingTests` and `ApprovalModeTests` except the two Task 1 rewrote, which move to a class `EvidenceGateTests`; in `HappyPathTests`, `status` is now `"recorded"` (the switch is off) and `item_code` is gone; `orders.in_flight(...)` becomes `orders.open_order(...)`.
  - `tests/test_replacement_end_to_end.py`: `_runtime` builds `build_registry(today=..., replacement_orders=orders, warranty_source=...)`; drop `item_codes` and `approval_mode`; `orders.in_flight(...)` → `orders.open_order(...)`.
  - Add to `tests/test_replacement_gate.py`:

```python
class LedgerThroughTheToolTests(unittest.TestCase):
    def test_the_same_bike_and_part_twice_is_one_order(self):
        orders = ReplacementOrders()
        record = registration()
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [record], replacement_orders=orders)
        for cid in ("c1", "c2"):
            context = ToolContext(conversation_id=cid, phone=PHONE, late={
                "evidence_seen": lambda: True, "evidence_verified": lambda: "battery",
                "coverage_result": lambda: coverage_for(record), "customer_messages": lambda: TYPED})
            result = registry.call(PLACE_REPLACEMENT_ORDER, dict(
                {"frame_number": FRAME, "part": "battery", "use_record_address": True,
                 "idempotency_key": "k-" + cid}, **RIDER), context)
        self.assertTrue(result["data"]["already_placed"])
        self.assertEqual(result["data"]["order_id"], "RO-1000001")

    def test_with_the_switch_off_or_no_product_id_the_order_is_recorded_not_queued(self):
        self.assertEqual(place(registration())["data"]["status"], "recorded")

    def test_with_the_switch_on_and_a_product_id_the_order_is_queued(self):
        from emotorad_ai.fulfilment import ProductIds

        record = registration()
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [record],
                                  replacement_orders=ReplacementOrders(), orders_live=True,
                                  product_ids=ProductIds({"EMX Plus": {"battery": "uuid-1"}}))
        context = ToolContext(conversation_id="c1", phone=PHONE, late={
            "evidence_seen": lambda: True, "evidence_verified": lambda: "battery",
            "coverage_result": lambda: coverage_for(record), "customer_messages": lambda: TYPED})
        result = registry.call(PLACE_REPLACEMENT_ORDER, dict(
            {"frame_number": FRAME, "part": "battery", "use_record_address": True, "idempotency_key": "k"},
            **RIDER), context)
        self.assertEqual(result["data"]["status"], "queued")
```

  - `prompts/battery_support.md` section 6a: replace the `status: approved` and `status: pending_approval` bullets with:

```markdown
   - `status: queued` or `status: recorded` — tell them the order id, the address, and that the support team will confirm the replacement before it ships. Raise the ticket with the photos in the same message if you have not already.
```

  and in the `part_not_identified` bullet delete the sentence starting "A part it knows but cannot find an item code for".

- [ ] **Step 9: Run the ledger, order and prompt tests**

Run: `python3 -m unittest tests.test_order_ledger tests.test_replacement_gate tests.test_replacement_order_tool tests.test_replacement_end_to_end tests.test_fulfilment tests.test_order_claim_postcheck tests.test_navigation tests.test_prompt_rules -v`
Expected: PASS.

- [ ] **Step 10: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass except the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. Fix any other caller of `item_codes=`, `approval_mode=` on `build_registry`, `ItemCodes`, `is_sure`, `decide` or `in_flight` that the run names (the API is Task 6's; if `api.py` fails to import, pass `replacement_orders=stores.replacement_orders` and drop `item_codes`/`approval_mode` there now). If an erasure test pins the exact set of collections, add `replacement_orders` to it.

- [ ] **Step 11: Commit**

```bash
git add src/emotorad_ai/fulfilment.py src/emotorad_ai/stores/mongo.py src/emotorad_ai/wiring.py src/emotorad_ai/tools/mocks.py src/emotorad_ai/guardrails.py src/emotorad_ai/api.py scripts/mongo_setup.py prompts/battery_support.md tests/
git commit -m "feat(replacement-orders): the order ledger, one open order per bike and part, RO- and seven digits"
```

---

### Task 4: The OMS client

**Files:**
- Create: `src/emotorad_ai/oms_afs.py`
- Create: `tests/test_oms_afs.py`

**Interfaces:**
- Produces:
  - `oms_afs.SWITCH_ENV = "EMOTORAD_OMS_AFS_ORDERS"`, `oms_afs.SETTING_NAMES`, `oms_afs.AFSSettings(ws_url, login_url, email, password, token)`
  - `oms_afs.settings_from_env(environ) -> Tuple[Optional[AFSSettings], str]` (health text: `"on"`, `"off"`, `"misconfigured: <names>"`)
  - `oms_afs.AFSClient(settings, connect=None, post=None, log=None, timeout=20.0)` with `.find(reference) -> Optional[Dict[str, str]]` (`{"order_code", "order_id"}`), `.place(request: Dict, client_ref: str) -> Dict[str, str]`, `.sale_type_id(name: str) -> str`
  - exceptions `OMSAuthError`, `OMSCallError`, `OMSRefused(status, msg)`
  - `oms_afs.afs_request(order: Dict, pin_code_id: str, sale_type_id: str) -> Dict`

- [ ] **Step 1: Write the failing client tests** (`tests/test_oms_afs.py`)

```python
"""The OMS afs_order_add client (spec 2026-10-10 replacement orders, section 5).
No network: the WebSocket and the login are fakes."""

import json
import unittest

from emotorad_ai import oms_afs

SETTINGS = oms_afs.AFSSettings(ws_url="wss://oms.example/ws/", login_url="https://oms.example/user/login",
                               email="bot-admin@example.com", password="pw-secret-1", token="tok-old")


class FakeOMS:
    """One fake OMS: valid tokens, its orders, and every frame it was sent."""

    def __init__(self, valid=("tok-old",), orders=(), fail_add_after_create=False, down=False):
        self.valid, self.orders, self.sent = set(valid), list(orders), []
        self.fail_add_after_create, self.down, self.logins = fail_add_after_create, down, 0

    def connect(self, url, **kwargs):
        if self.down:
            raise OSError("refused")
        return FakeSocket(self, url.rsplit("/", 2)[-2])

    def post(self, url, json=None, timeout=None):
        self.logins += 1
        token = "tok-new"
        self.valid.add(token)
        return FakeResponse(200, {"msg": "ok", "data": {"access_token": token}})

    def answer(self, token, frame):
        if token not in self.valid:
            return {"transmit": "single", "url": "unauthorized"}
        url, request = frame["url"], frame.get("request") or {}
        frame = dict(frame)
        if url == "afs_order_list":
            rows = [o for o in self.orders if request.get("search", "") in (o.get("ticket_id") or "")]
            frame["response"] = ({"status": 200, "msg": "Order Found", "data": {"data": rows}} if rows
                                 else {"status": 400, "msg": "Order Not Found", "data": {}})
        elif url == "sale_type_list":
            frame["response"] = {"status": 200, "msg": "ok", "data": {"data": [
                {"id": "st-1", "sale_type": "After Sales"}, {"id": "st-2", "sale_type": "Warranty"}]}}
        elif url == "afs_order_add":
            self.sent.append(request)
            order = {"id": "o-%d" % (len(self.orders) + 1), "order_code": "AFS/26-27/EC/%d" % (len(self.orders) + 1),
                     "ticket_id": request.get("ticket_number")}
            self.orders.append(order)
            frame["response"] = ({"status": 400, "msg": "ERP timeout", "data": {}} if self.fail_add_after_create
                                 else {"status": 200, "msg": "Order Added", "data": order})
        return frame


class FakeSocket:
    def __init__(self, oms, token):
        self.oms, self.token, self.outbox = oms, token, []

    def send(self, text):
        self.outbox.append(json.dumps(self.oms.answer(self.token, json.loads(text))))

    def recv(self, timeout=None):
        if not self.outbox:
            raise TimeoutError("no frame")
        return self.outbox.pop(0)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


def client(oms, logged=None):
    return oms_afs.AFSClient(SETTINGS, connect=oms.connect, post=oms.post,
                             log=(lambda event, fields: logged.append((event, fields))) if logged is not None else None)


class SettingsTests(unittest.TestCase):
    def test_off_unless_exactly_on(self):
        for value in ("", "yes", "ON"):
            self.assertEqual(oms_afs.settings_from_env({oms_afs.SWITCH_ENV: value})[1], "off")

    def test_on_needs_every_required_setting(self):
        env = {oms_afs.SWITCH_ENV: "on", "EMOTORAD_OMS_WS_URL": "wss://x/ws/"}
        settings, health = oms_afs.settings_from_env(env)
        self.assertIsNone(settings)
        self.assertTrue(health.startswith("misconfigured: "))
        self.assertIn("EMOTORAD_OMS_ADMIN_PASSWORD", health)
        env.update({"EMOTORAD_OMS_LOGIN_URL": "https://x/user/login", "EMOTORAD_OMS_ADMIN_EMAIL": "a@b.c",
                    "EMOTORAD_OMS_ADMIN_PASSWORD": "p"})
        settings, health = oms_afs.settings_from_env(env)
        self.assertEqual(health, "on")
        self.assertIsNone(settings.token)

    def test_the_settings_never_print_their_values(self):
        self.assertNotIn("pw-secret-1", repr(SETTINGS))
        self.assertNotIn("tok-old", repr(SETTINGS))


class CallTests(unittest.TestCase):
    def test_find_matches_our_reference_exactly(self):
        oms = FakeOMS(orders=[{"id": "o-9", "order_code": "AFS/X/9", "ticket_id": "RO-10000011"},
                              {"id": "o-1", "order_code": "AFS/X/1", "ticket_id": "RO-1000001"}])
        self.assertEqual(client(oms).find("RO-1000001"), {"order_code": "AFS/X/1", "order_id": "o-1"})

    def test_find_with_no_order_is_none(self):
        self.assertIsNone(client(FakeOMS()).find("RO-1000001"))

    def test_place_returns_the_order(self):
        oms = FakeOMS()
        placed = client(oms).place({"ticket_number": "RO-1000001"}, "RO-1000001:1")
        self.assertEqual(placed, {"order_code": "AFS/26-27/EC/1", "order_id": "o-1"})

    def test_an_error_answer_is_refused_with_its_status(self):
        with self.assertRaises(oms_afs.OMSRefused) as caught:
            client(FakeOMS(fail_add_after_create=True)).place({"ticket_number": "RO-1000001"}, "RO-1000001:1")
        self.assertEqual(caught.exception.status, 400)

    def test_the_sale_type_id_is_looked_up_once(self):
        oms = FakeOMS()
        afs = client(oms)
        self.assertEqual(afs.sale_type_id("Warranty"), "st-2")
        oms.valid.clear()
        self.assertEqual(afs.sale_type_id("Warranty"), "st-2")

    def test_a_refused_token_logs_in_once_and_retries(self):
        oms = FakeOMS(valid=())
        afs = client(oms)
        self.assertIsNone(afs.find("RO-1000001"))
        self.assertEqual(oms.logins, 1)
        self.assertIsNone(afs.find("RO-1000001"))
        self.assertEqual(oms.logins, 1)

    def test_a_dead_oms_is_a_call_error(self):
        with self.assertRaises(oms_afs.OMSCallError):
            client(FakeOMS(down=True)).find("RO-1000001")

    def test_no_setting_value_reaches_an_error_or_a_log(self):
        logged = []

        class BadLogin(FakeOMS):
            def post(self, url, json=None, timeout=None):
                return FakeResponse(400, {"error": "Wrong password"})

        with self.assertRaises(oms_afs.OMSAuthError) as caught:
            client(BadLogin(valid=()), logged).find("RO-1000001")
        text = str(caught.exception) + json.dumps(logged)
        for secret in ("pw-secret-1", "tok-old", "bot-admin@example.com", "oms.example"):
            self.assertNotIn(secret, text)
        self.assertEqual(logged, [("oms_login_failed", {"status": 400})])


class RequestTests(unittest.TestCase):
    def test_the_request_is_a_free_warranty_customer_order(self):
        order = {"_id": "RO-1000001", "frame_number": "EMXP0001", "product_id": "uuid-1",
                 "customer": {"name": "Test Rider", "email": "t@example.com", "mobile": "9876543210",
                              "address": {"line1": "A1102, Park View", "line2": "Sector 49, Gurugram, Haryana",
                                          "pincode": "122018"}}}
        request = oms_afs.afs_request(order, pin_code_id="pin-1", sale_type_id="st-2")
        self.assertEqual(request["order_type"], "CO")
        self.assertEqual(request["items"], [{"product_id": "uuid-1", "product_qty": 1, "is_demo": False,
                                             "rate": 0, "amount": 0, "idx": 1}])
        self.assertEqual((request["sale_type"], request["sale_type_id"]), ("Warranty", "st-2"))
        self.assertEqual(request["ticket_number"], "RO-1000001")
        self.assertEqual(request["frame_number"], "EMXP0001")
        for side in ("bill", "ship"):
            self.assertEqual(request["%s_customer_name" % side], "Test Rider")
            self.assertEqual(request["%s_mobile" % side], "9876543210")
            self.assertEqual(request["%s_email" % side], "t@example.com")
            self.assertEqual(request["%s_pin_code_id" % side], "pin-1")
            self.assertEqual(request["%s_address" % side], "A1102, Park View")
            self.assertEqual(request["%s_address2" % side], "Sector 49, Gurugram, Haryana")
        self.assertEqual(request["remark"], "Warranty replacement placed by the EMotorad support chatbot.")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_oms_afs -v`
Expected: ERROR: `cannot import name 'oms_afs'`.

- [ ] **Step 3: Implement** `src/emotorad_ai/oms_afs.py`

```python
"""OMS after-sales orders over its WebSocket (spec 2026-10-10 replacement
orders, section 5).

`afs_order_add` exists only as a WebSocket action (em-biz-backend
order/afs_order.py), reached at `<ws_url><token>/` as the OMS admin user whose
email, password and token a person keeps in Secrets Manager. The stored token
is used first; when OMS refuses it, the client logs in once with the email and
password and keeps the new token in memory. One call opens one socket, sends
one frame, waits for the frame OMS echoes back with the same `url` and
`client_ref`, and closes. No setting value is ever logged or put in an error:
errors carry an HTTP status or an exception's class.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

SWITCH_ENV = "EMOTORAD_OMS_AFS_ORDERS"
REQUIRED = ("EMOTORAD_OMS_WS_URL", "EMOTORAD_OMS_LOGIN_URL", "EMOTORAD_OMS_ADMIN_EMAIL", "EMOTORAD_OMS_ADMIN_PASSWORD")
TOKEN_ENV = "EMOTORAD_OMS_ADMIN_TOKEN"
SETTING_NAMES = REQUIRED + (TOKEN_ENV,)
DEVICE_ID = "emotorad-ai-chatbot"
SALE_TYPE = "Warranty"
REMARK = "Warranty replacement placed by the EMotorad support chatbot."
AUTH_CODE = 403
OK_CODE = 200


class OMSAuthError(Exception):
    """OMS refused the token, or the login failed. The message is a status or a class name."""


class OMSCallError(Exception):
    """OMS could not be reached or did not answer in time. The message is a class name."""


class OMSRefused(Exception):
    """OMS answered with an error status. An order may still have been made."""

    def __init__(self, status: Any, msg: str) -> None:
        super().__init__("status %s" % status)
        self.status, self.msg = status, msg


@dataclass(frozen=True)
class AFSSettings:
    ws_url: str = field(repr=False)
    login_url: str = field(repr=False)
    email: str = field(repr=False)
    password: str = field(repr=False)
    token: Optional[str] = field(default=None, repr=False)


def settings_from_env(environ: Optional[Mapping[str, str]] = None) -> Tuple[Optional[AFSSettings], str]:
    """The settings when the switch is exactly `on` and every required one is
    set, with /health's text: "on", "off" or "misconfigured: <names>"."""
    env = os.environ if environ is None else environ
    if (env.get(SWITCH_ENV) or "").strip() != "on":
        return None, "off"
    missing = [name for name in REQUIRED if not (env.get(name) or "").strip()]
    if missing:
        return None, "misconfigured: " + ", ".join(missing)
    return AFSSettings(ws_url=env["EMOTORAD_OMS_WS_URL"].strip(), login_url=env["EMOTORAD_OMS_LOGIN_URL"].strip(),
                       email=env["EMOTORAD_OMS_ADMIN_EMAIL"].strip(), password=env["EMOTORAD_OMS_ADMIN_PASSWORD"],
                       token=(env.get(TOKEN_ENV) or "").strip() or None), "on"


def _ws_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)


def _http_post(url: str, json: Any = None, timeout: Optional[float] = None) -> Any:
    import httpx

    return httpx.post(url, json=json, timeout=timeout)


def afs_request(order: Dict[str, Any], pin_code_id: str, sale_type_id: str) -> Dict[str, Any]:
    """The `afs_order_add` request for one ledger order: a customer order (CO),
    free (rate 0), sale type Warranty, our reference in ticket_number."""
    customer = order.get("customer") or {}
    address = customer.get("address") or {}
    request: Dict[str, Any] = {"order_type": "CO"}
    for side in ("bill", "ship"):
        request.update({
            "%s_customer_name" % side: customer.get("name"),
            "%s_mobile" % side: customer.get("mobile"),
            "%s_email" % side: customer.get("email"),
            "%s_pin_code_id" % side: pin_code_id,
            "%s_address" % side: address.get("line1") or "",
            "%s_address2" % side: address.get("line2") or "",
        })
    request.update({
        "items": [{"product_id": order.get("product_id"), "product_qty": 1, "is_demo": False,
                   "rate": 0, "amount": 0, "idx": 1}],
        "sale_type": SALE_TYPE,
        "sale_type_id": sale_type_id,
        "frame_number": order.get("frame_number"),
        "ticket_number": order["_id"],
        "remark": REMARK,
    })
    return request


class AFSClient:
    def __init__(self, settings: AFSSettings, connect: Optional[Callable[..., Any]] = None,
                 post: Optional[Callable[..., Any]] = None, log: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 timeout: float = 20.0) -> None:
        self._settings = settings
        self._connect = connect or _ws_connect
        self._post = post or _http_post
        self._log = log
        self._timeout = timeout
        self._token: Optional[str] = settings.token
        self._sale_types: Dict[str, str] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "AFSClient(settings=set)"

    # -- the three calls -----------------------------------------------------

    def find(self, reference: str) -> Optional[Dict[str, str]]:
        """Our order in OMS, by the reference in its ticket_id, or None."""
        response = self._call("afs_order_list", {"search": reference, "limit": 5, "page_no": 1}, reference + ":find")
        if response.get("status") == OK_CODE:
            rows = (response.get("data") or {}).get("data") or []
            row = next((r for r in rows if r.get("ticket_id") == reference), None)
            return {"order_code": str(row.get("order_code")), "order_id": str(row.get("id"))} if row else None
        if "not found" in str(response.get("msg") or "").lower():
            return None
        raise OMSCallError("find status %s" % response.get("status"))

    def place(self, request: Dict[str, Any], client_ref: str) -> Dict[str, str]:
        response = self._call("afs_order_add", request, client_ref)
        if response.get("status") == OK_CODE:
            data = response.get("data") or {}
            return {"order_code": str(data.get("order_code")), "order_id": str(data.get("id"))}
        raise OMSRefused(response.get("status"), str(response.get("msg") or ""))

    def sale_type_id(self, name: str) -> str:
        with self._lock:
            if name in self._sale_types:
                return self._sale_types[name]
        response = self._call("sale_type_list", {"limit": 100, "page_no": 1}, "sale_type:" + name)
        rows = (response.get("data") or {}).get("data") or []
        found = next((r for r in rows if r.get("sale_type") == name), None)
        if response.get("status") != OK_CODE or found is None:
            raise OMSCallError("sale_type_missing")
        with self._lock:
            self._sale_types[name] = str(found["id"])
            return self._sale_types[name]

    # -- the socket ------------------------------------------------------------

    def _call(self, url: str, request: Dict[str, Any], client_ref: str) -> Dict[str, Any]:
        token = self._token or self._login()
        try:
            return self._once(token, url, request, client_ref)
        except OMSAuthError:
            return self._once(self._login(), url, request, client_ref)

    def _once(self, token: str, url: str, request: Dict[str, Any], client_ref: str) -> Dict[str, Any]:
        frame = {"transmit": "single", "url": url, "client_ref": client_ref, "request": request}
        deadline = time.monotonic() + self._timeout
        try:
            socket = self._connect("%s%s/" % (self._settings.ws_url, token), open_timeout=self._timeout,
                                   close_timeout=2)
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == AUTH_CODE:
                raise OMSAuthError("connect 403") from None
            raise OMSCallError(type(exc).__name__) from None
        with socket:
            try:
                socket.send(json.dumps(frame))
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise OMSCallError("timeout")
                    answer = json.loads(socket.recv(timeout=left))
                    if answer.get("url") == "unauthorized":
                        raise OMSAuthError("unauthorized")
                    if answer.get("url") == url and answer.get("client_ref") == client_ref:
                        response = answer.get("response") or {}
                        if response.get("status") == AUTH_CODE:
                            raise OMSAuthError("status 403")
                        return response
            except (OMSAuthError, OMSCallError):
                raise
            except Exception as exc:
                raise OMSCallError(type(exc).__name__) from None

    def _login(self) -> str:
        try:
            answer = self._post(self._settings.login_url, json={
                "email": self._settings.email, "password": self._settings.password,
                "device_type": "web", "device_id": DEVICE_ID}, timeout=10)
        except Exception as exc:
            if self._log is not None:
                self._log("oms_login_failed", {"error": type(exc).__name__})
            raise OMSAuthError("login %s" % type(exc).__name__) from None
        token = None
        if answer.status_code == 200:
            try:
                token = ((answer.json() or {}).get("data") or {}).get("access_token")
            except ValueError:
                token = None
        if not token:
            if self._log is not None:
                self._log("oms_login_failed", {"status": answer.status_code})
            raise OMSAuthError("login %s" % answer.status_code)
        with self._lock:
            self._token = token
        return token
```

- [ ] **Step 4: Run them**

Run: `python3 -m unittest tests.test_oms_afs -v`
Expected: PASS. (`test_a_refused_token_logs_in_once_and_retries`: the stored token `tok-old` is not valid in `FakeOMS(valid=())`, the socket answers `unauthorized`, one login gives `tok-new`, the retry answers; the second `find` uses the kept token.)

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/oms_afs.py tests/test_oms_afs.py
git commit -m "feat(replacement-orders): the OMS afs_order_add client: one socket per call, a login when the token is refused"
```

---

### Task 5: The order worker

**Files:**
- Create: `src/emotorad_ai/order_worker.py`
- Create: `tests/test_order_worker.py`

**Interfaces:**
- Consumes: Task 3's ledger interface; Task 4's `AFSClient.find/place/sale_type_id`, `afs_request`, `OMSAuthError`, `OMSCallError`, `OMSRefused`.
- Produces: `order_worker.OrderWorker(ledger, client, pin_codes: Callable[[str], Optional[str]], log=None, clock=None, interval=30.0)` with `.run_once() -> int`, `.process(order: dict) -> None`, `.start()`, `.stop()`, `.status: Dict[str, Any]`; constants `RETRY_MINUTES = (1, 5, 15, 60)`, `MAX_PASSES = 8`, `GIVE_UP_HOURS = 24`, `LEASE_SECONDS = 300`.

- [ ] **Step 1: Write the failing worker tests** (`tests/test_order_worker.py`)

```python
"""The order worker (spec 2026-10-10 replacement orders, section 4), against
the fake OMS of tests/test_oms_afs.py."""

import unittest
from datetime import datetime, timedelta, timezone

from emotorad_ai import oms_afs
from emotorad_ai.fulfilment import ReplacementOrders, open_key
from emotorad_ai.order_worker import MAX_PASSES, OrderWorker
from tests.test_oms_afs import FakeOMS, client

START = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)


def queued(ledger, at=START):
    ref = ledger.next_reference()
    order, _ = ledger.insert({
        "_id": ref, "open_key": open_key("EMXP0001", "battery"), "frame_number": "EMXP0001", "part": "battery",
        "product_id": "uuid-1", "product_name": "EMX Plus",
        "customer": {"name": "Test Rider", "email": "t@example.com", "mobile": "9876543210",
                     "address": {"line1": "A1102", "line2": "Sector 49", "pincode": "122018"}},
        "delivery_address": "x", "phone": "+919876543210", "conversation_id": "c1", "ticket_reference": None,
        "status": "queued",
        "oms": {"intent_at": None, "order_code": None, "order_id": None, "passes": 0, "last_error": None},
        "next_attempt_at": at.isoformat(), "lease_until": None, "created_at": at.isoformat(),
        "updated_at": at.isoformat()})
    return order


def worker(ledger, oms, now, logged=None):
    return OrderWorker(ledger, client(oms), pin_codes=lambda pincode: "pin-1",
                       log=(lambda event, fields: logged.append((event, fields))) if logged is not None else None,
                       clock=lambda: now[0])


class SendTests(unittest.TestCase):
    def test_a_queued_order_is_sent_once(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        self.assertEqual(worker(ledger, oms, now).run_once(), 1)
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "sent")
        self.assertEqual(saved["oms"]["order_code"], "AFS/26-27/EC/1")
        self.assertEqual(len(oms.sent), 1)
        self.assertEqual(oms.sent[0]["ticket_number"], order["_id"])
        self.assertEqual(oms.sent[0]["items"][0]["rate"], 0)
        self.assertEqual(oms.sent[0]["sale_type_id"], "st-2")

    def test_a_found_order_is_never_sent(self):
        ledger, now = ReplacementOrders(), [START]
        order = queued(ledger)
        oms = FakeOMS(orders=[{"id": "o-7", "order_code": "AFS/X/7", "ticket_id": order["_id"]}])
        worker(ledger, oms, now).run_once()
        self.assertEqual(oms.sent, [])
        self.assertEqual(ledger.get(order["_id"])["oms"]["order_code"], "AFS/X/7")

    def test_an_error_after_the_order_was_made_ends_sent_with_one_order(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(fail_add_after_create=True), [START]
        order = queued(ledger)
        worker(ledger, oms, now).run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")
        self.assertEqual(len(oms.orders), 1)

    def test_a_pass_after_a_crash_finds_the_order_and_never_sends(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        # The last pass sent it and died before saving: OMS has it, the ledger says queued with an intent.
        oms.orders.append({"id": "o-1", "order_code": "AFS/26-27/EC/1", "ticket_id": order["_id"]})
        order["oms"]["intent_at"] = START.isoformat()
        ledger.save(order)
        worker(ledger, oms, now).run_once()
        self.assertEqual(oms.sent, [])
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")


class RetryTests(unittest.TestCase):
    def test_the_kill_drill_ends_in_exactly_one_order(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now)
        afs.run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["oms"]["last_error"], "OMSCallError")
        self.assertEqual(saved["next_attempt_at"], (START + timedelta(minutes=1)).isoformat())
        oms.down = False
        now[0] = START + timedelta(minutes=2)
        afs.run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")
        self.assertEqual(len(oms.orders), 1)

    def test_retries_wait_1_5_15_60_minutes_then_hourly(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now)
        waits = []
        for _ in range(6):
            afs.run_once()
            saved = ledger.get(order["_id"])
            due = datetime.fromisoformat(saved["next_attempt_at"])
            waits.append(int((due - now[0]).total_seconds() // 60))
            now[0] = due
        self.assertEqual(waits, [1, 5, 15, 60, 60, 60])

    def test_after_eight_passes_it_fails_and_says_so(self):
        logged = []
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now, logged)
        for _ in range(MAX_PASSES):
            afs.run_once()
            now[0] = datetime.fromisoformat(ledger.get(order["_id"])["next_attempt_at"])
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["open_key"], open_key("EMXP0001", "battery"))
        self.assertIn(("replacement_order_failed", {"reference": order["_id"], "error": "OMSCallError"}), logged)

    def test_a_day_old_order_fails_on_its_next_failure(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START + timedelta(hours=25)]
        order = queued(ledger)
        worker(ledger, oms, now).run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "failed")

    def test_an_unknown_pin_code_waits(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        OrderWorker(ledger, client(oms), pin_codes=lambda pincode: None, clock=lambda: now[0]).run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual((saved["status"], saved["oms"]["last_error"]), ("queued", "pin_code_unknown"))
        self.assertEqual(oms.sent, [])

    def test_a_leased_order_is_not_taken_twice(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        ledger.claim(order["_id"], START.isoformat(), (START + timedelta(minutes=5)).isoformat())
        self.assertEqual(worker(ledger, oms, now).run_once(), 0)
        self.assertEqual(oms.sent, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_order_worker -v`
Expected: ERROR: `No module named 'emotorad_ai.order_worker'`.

- [ ] **Step 3: Implement** `src/emotorad_ai/order_worker.py`

```python
"""The order worker (spec 2026-10-10 replacement orders, section 4). It sends
queued replacement orders to OMS after the rider has their reply.

A daemon thread in the API process, started by the lifespan only when the
switch is on. It takes one due order at a time under a five-minute lease. OMS
has no protection against a repeat and can answer an error after it has made
the order, so before every send, and after every failed one, it looks the
order up by our reference (afs_order_list matches ticket_id), and an order it
finds is never sent again. It records its intent before sending and sends
nothing if that record fails. Retries after 1, 5, 15 and 60 minutes, then
hourly; after 8 passes or 24 hours the order is `failed` and left to a person,
still holding its bike and part, so the bot never orders it again by itself.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from .oms_afs import SALE_TYPE, OMSAuthError, OMSCallError, OMSRefused, afs_request

RETRY_MINUTES = (1, 5, 15, 60)
MAX_PASSES = 8
GIVE_UP_HOURS = 24
LEASE_SECONDS = 300
_OMS_ERRORS = (OMSAuthError, OMSCallError, OMSRefused)


class OrderWorker:
    def __init__(self, ledger: Any, client: Any, pin_codes: Callable[[str], Optional[str]],
                 log: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 clock: Optional[Callable[[], datetime]] = None, interval: float = 30.0) -> None:
        self._ledger = ledger
        self._client = client
        self._pin_codes = pin_codes
        self._log = log or (lambda event, fields: None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status: Dict[str, Any] = {"running": False, "last_pass": None}

    # -- the loop ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="order-worker", daemon=True)
        self._thread.start()
        self.status["running"] = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self.status["running"] = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # the loop never dies; the order stays queued
                self._log("order_worker_pass_failed", {"error": type(exc).__name__})
            self._stop.wait(self._interval)

    def run_once(self) -> int:
        now = self._clock()
        self.status["last_pass"] = now.isoformat()
        done = 0
        for order in self._ledger.due(now.isoformat()):
            claimed = self._ledger.claim(order["_id"], now.isoformat(),
                                         (now + timedelta(seconds=LEASE_SECONDS)).isoformat())
            if claimed is None:
                continue
            self.process(claimed)
            done += 1
        return done

    # -- one order --------------------------------------------------------------

    def process(self, order: Dict[str, Any]) -> None:
        reference = order["_id"]
        order["oms"]["passes"] = int(order["oms"].get("passes") or 0) + 1
        try:
            found = self._client.find(reference)
        except _OMS_ERRORS as exc:
            return self._retry(order, type(exc).__name__)
        if found:
            return self._sent(order, found)
        pin_code_id = self._pin_codes(((order.get("customer") or {}).get("address") or {}).get("pincode") or "")
        if not pin_code_id:
            return self._retry(order, "pin_code_unknown")
        try:
            sale_type_id = self._client.sale_type_id(SALE_TYPE)
        except _OMS_ERRORS as exc:
            return self._retry(order, type(exc).__name__)
        order["oms"]["intent_at"] = self._clock().isoformat()
        try:
            self._ledger.save(order)
        except Exception as exc:
            # No record of the intent: send nothing (the lease runs out and a later pass tries again).
            self._log("replacement_order_intent_unsaved", {"reference": reference, "error": type(exc).__name__})
            return None
        try:
            placed = self._client.place(afs_request(order, pin_code_id, sale_type_id),
                                        "%s:%d" % (reference, order["oms"]["passes"]))
            return self._sent(order, placed)
        except _OMS_ERRORS as exc:
            error = type(exc).__name__
        try:
            found = self._client.find(reference)
        except _OMS_ERRORS:
            found = None
        if found:
            return self._sent(order, found)
        return self._retry(order, error)

    def _sent(self, order: Dict[str, Any], found: Dict[str, str]) -> None:
        order["status"] = "sent"
        order["oms"].update({"order_code": found.get("order_code"), "order_id": found.get("order_id"),
                             "last_error": None})
        self._finish(order)
        self._log("replacement_order_sent", {"reference": order["_id"]})

    def _retry(self, order: Dict[str, Any], error: str) -> None:
        now = self._clock()
        order["oms"]["last_error"] = error
        passes = int(order["oms"]["passes"])
        created = datetime.fromisoformat(order["created_at"])
        if passes >= MAX_PASSES or now - created >= timedelta(hours=GIVE_UP_HOURS):
            order["status"] = "failed"
            self._finish(order)
            self._log("replacement_order_failed", {"reference": order["_id"], "error": error})
            return
        minutes = RETRY_MINUTES[passes - 1] if passes - 1 < len(RETRY_MINUTES) else 60
        order["next_attempt_at"] = (now + timedelta(minutes=minutes)).isoformat()
        self._finish(order)

    def _finish(self, order: Dict[str, Any]) -> None:
        order["lease_until"] = None
        order["updated_at"] = self._clock().isoformat()
        self._ledger.save(order)
```

- [ ] **Step 4: Run them**

Run: `python3 -m unittest tests.test_order_worker -v`
Expected: PASS. (`replacement_order_failed` is logged by the worker's `log`; Task 6 makes the API's `log` emit it at error level.)

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/order_worker.py tests/test_order_worker.py
git commit -m "feat(replacement-orders): the order worker: look first, record the intent, look again after an error"
```

---

### Task 6: Wiring, the motor agent and the docs

**Files:**
- Modify: `src/emotorad_ai/tools/oms_db.py` (`OMSDatabase.pin_code_id`)
- Modify: `src/emotorad_ai/api.py` (switch, client, worker, registry, `/health`)
- Modify: `src/emotorad_ai/agents/motor_support.py` (tool offered when a motor part may be ordered)
- Modify: `src/emotorad_ai/fulfilment.py` (`motor_orderable`)
- Modify: `tests/test_api_health.py`
- Create: `tests/test_api_replacement_orders.py`
- Modify: `docs/runbooks/config-store.md` (new section 10), `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `/health` keys `oms_afs_orders` and `replacement_orders`; `OMSDatabase.pin_code_id(pincode: str) -> Optional[str]`; `fulfilment.motor_orderable(parts_table, product_ids) -> bool`.

- [ ] **Step 1: Write the failing tests** (`tests/test_api_replacement_orders.py`)

```python
"""Replacement orders in the API (spec 2026-10-10 replacement orders, section 7)."""

import unittest
from unittest import mock

from emotorad_ai.fulfilment import ProductIds, load_parts_table, motor_orderable
from emotorad_ai.tools import oms_db
from tests.test_api_health import fresh_api

OFF = {"EMOTORAD_OMS_AFS_ORDERS": "", "EMOTORAD_OMS_WS_URL": "", "EMOTORAD_OMS_LOGIN_URL": "",
       "EMOTORAD_OMS_ADMIN_EMAIL": "", "EMOTORAD_OMS_ADMIN_PASSWORD": "", "EMOTORAD_OMS_ADMIN_TOKEN": ""}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: fresh_api(OFF))

    def test_off_by_default_with_no_worker(self):
        api = fresh_api(OFF)
        self.assertEqual(api.health()["oms_afs_orders"], "off")
        self.assertEqual(api.health()["replacement_orders"], "memory")
        self.assertIsNone(api.ORDER_WORKER)

    def test_on_without_its_settings_is_misconfigured_and_sends_nothing(self):
        api = fresh_api(dict(OFF, EMOTORAD_OMS_AFS_ORDERS="on"))
        self.assertTrue(api.health()["oms_afs_orders"].startswith("misconfigured: "))
        self.assertIsNone(api.ORDER_WORKER)

    def test_on_with_its_settings_builds_a_worker_that_is_not_started_at_import(self):
        api = fresh_api(dict(OFF, EMOTORAD_OMS_AFS_ORDERS="on", EMOTORAD_OMS_WS_URL="wss://x/ws/",
                             EMOTORAD_OMS_LOGIN_URL="https://x/user/login", EMOTORAD_OMS_ADMIN_EMAIL="a@b.c",
                             EMOTORAD_OMS_ADMIN_PASSWORD="p"))
        self.assertEqual(api.health()["oms_afs_orders"], "on")
        self.assertIsNotNone(api.ORDER_WORKER)
        self.assertFalse(api.ORDER_WORKER.status["running"])


class MotorTests(unittest.TestCase):
    def test_no_motor_part_may_be_ordered_today(self):
        self.assertFalse(motor_orderable(load_parts_table(), ProductIds()))

    def test_a_fitted_motor_part_with_a_product_id_would_be(self):
        from emotorad_ai.fulfilment import PartRule

        table = {"controller": PartRule(part="controller", technician=False)}
        self.assertTrue(motor_orderable(table, ProductIds({"EMX Plus": {"controller": "uuid-9"}})))
        self.assertFalse(motor_orderable(table, ProductIds()))

    def test_the_motor_agent_is_not_offered_the_tool_today(self):
        from emotorad_ai.agents.motor_support import TOOL_NAMES
        from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER

        self.assertNotIn(PLACE_REPLACEMENT_ORDER, TOOL_NAMES)


class PinCodeTests(unittest.TestCase):
    def test_the_pin_code_id_is_read_by_its_pin_code(self):
        from tests.test_oms_orders import FakeConnection

        fake = FakeConnection()
        fake.answers[oms_db.PIN_CODE_SQL] = [{"id": "pin-1"}]
        db = oms_db.OMSDatabase("postgresql://ro@oms/x", connect=fake)
        self.assertEqual(db.pin_code_id("122018"), "pin-1")
        self.assertIsNone(db.pin_code_id("12201"))


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_api_health.py`'s pinned body add `"oms_afs_orders": "off", "replacement_orders": "memory",` after `"oms_orders": "off",`, and add the six `OFF` names with `""` to that test's env dict.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_api_replacement_orders tests.test_api_health -v`
Expected: FAIL/ERROR (`motor_orderable` missing, no `oms_afs_orders` key, no `PIN_CODE_SQL`).

- [ ] **Step 3: Implement**

`src/emotorad_ai/fulfilment.py`, after `ProductIds`:

```python
MOTOR_PARTS = ("motor", "controller")


def motor_orderable(parts_table: Dict[str, PartRule], product_ids: ProductIds) -> bool:
    """Whether the motor agent may be offered place_replacement_order (spec
    2026-10-10 replacement orders, section 6): a motor-side part the customer
    can fit (no technician, no ask) with an OMS product id. None today."""
    fitted = [part for part in MOTOR_PARTS
              if part in parts_table and not parts_table[part].technician and not parts_table[part].ask]
    return bool(fitted) and product_ids.has_part(fitted)
```

`src/emotorad_ai/agents/motor_support.py`: after `TOOL_NAMES = (...)` add:

```python
# Replacement orders (spec 2026-10-10): offered once a motor-side part may be
# ordered; with none today, the motor agent keeps raising tickets.
from ..fulfilment import ProductIds, load_parts_table, motor_orderable  # noqa: E402
from ..tools.mocks import PLACE_REPLACEMENT_ORDER  # noqa: E402

if motor_orderable(load_parts_table(), ProductIds()):
    TOOL_NAMES = TOOL_NAMES + (PLACE_REPLACEMENT_ORDER,)
```

(If `PLACE_REPLACEMENT_ORDER` is already importable where the other tool names are imported at the top of the file, import it there instead.)

`src/emotorad_ai/tools/oms_db.py`: add after `ORDERS_SQL`:

```python
# OMS's id for a pin code, for afs_order_add (spec 2026-10-10 replacement orders).
PIN_CODE_SQL = "SELECT id FROM em_pin_code WHERE pin_code::text = %(pin)s LIMIT 1"
```

and to `OMSDatabase`:

```python
    def pin_code_id(self, pincode: str) -> Optional[str]:
        """OMS's em_pin_code id for a six-digit pin code, or None (never raises)."""
        pin = (pincode or "").strip()
        if len(pin) != 6 or not pin.isdigit():
            return None
        try:
            with self._connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, application_name=APPLICATION_NAME,
                               options="-c statement_timeout=%d -c default_transaction_read_only=on"
                                       % STATEMENT_TIMEOUT_MS) as conn:
                rows = conn.execute(PIN_CODE_SQL, {"pin": pin}).fetchall()
        except Exception:
            return None
        return str(rows[0]["id"]) if rows else None
```

`src/emotorad_ai/api.py`:
- imports: `from . import oms_afs`, `from .order_worker import OrderWorker`, `from .fulfilment import ProductIds`.
- after `OMS_DB = ...`:

```python
# Replacement orders to OMS (spec 2026-10-10 replacement orders): sent only
# with EMOTORAD_OMS_AFS_ORDERS=on and the OMS admin settings, by a worker the
# lifespan starts. Off: orders are recorded and ticketed, nothing is sent.
OMS_AFS_SETTINGS, OMS_AFS_HEALTH = oms_afs.settings_from_env()
PRODUCT_IDS = ProductIds()
```

- in `_build_registry`'s `build_registry(...)` call: replace `replacement_orders=replacement_orders, item_codes=ItemCodes(), approval_mode=settings.approval_mode` with `replacement_orders=stores.replacement_orders, product_ids=PRODUCT_IDS, orders_live=OMS_AFS_SETTINGS is not None`; delete the module-level `replacement_orders = ReplacementOrders()` and its imports.
- after the registry is built (beside the Zoho wiring):

```python
ORDER_WORKER = (OrderWorker(
    stores.replacement_orders,
    oms_afs.AFSClient(OMS_AFS_SETTINGS, log=lambda event, fields: log.emit(event, "oms_afs", **fields)),
    pin_codes=(OMS_DB.pin_code_id if OMS_DB is not None else (lambda pincode: None)),
    log=lambda event, fields: log.emit(event, "replacement_orders", **fields))
    if OMS_AFS_SETTINGS is not None else None)
```

- `_lifespan`: start `ORDER_WORKER` after the Zoho worker and stop it before the Zoho worker stops:

```python
    if ORDER_WORKER is not None:
        ORDER_WORKER.start()
```
```python
    if ORDER_WORKER is not None:
        ORDER_WORKER.stop()
```

- `health()`: after `"oms_orders": ...`:

```python
        "oms_afs_orders": OMS_AFS_HEALTH,
        "replacement_orders": stores.replacement_orders_status,
```

- [ ] **Step 4: Run them**

Run: `python3 -m unittest tests.test_api_replacement_orders tests.test_api_health tests.test_api_oms_db -v`
Expected: PASS.

- [ ] **Step 5: The runbook and CLAUDE.md**

Add to `docs/runbooks/config-store.md` a section `## 10. Replacement orders to OMS`:

````markdown
Spec: `docs/superpowers/specs/2026-10-10-oms-replacement-orders-design.md`. Off until
`EMOTORAD_OMS_AFS_ORDERS=on`; it stays off until the OMS product ids are agreed and Sachin says yes.

1. **The ledger (a person).** Run `python scripts/mongo_setup.py` once: it makes `replacement_orders` and its
   one-open-order index. Until it has, `/health` shows `"replacement_orders":"memory: index missing, run
   scripts/mongo_setup.py"` and orders are lost on a restart.
2. **The OMS admin settings (a person, in AWS CloudShell).** Add `EMOTORAD_OMS_WS_URL` (for example
   `wss://omsapi.emotorad.com/ws/`), `EMOTORAD_OMS_LOGIN_URL` (for example
   `https://omsrest.emotorad.com/user/login`), `EMOTORAD_OMS_ADMIN_EMAIL`, `EMOTORAD_OMS_ADMIN_PASSWORD` and,
   optionally, `EMOTORAD_OMS_ADMIN_TOKEN` to `/emotorad/stage/ai/app` with the section 9 script's `getpass`
   pattern (the names in its first loop). Type the command as shown, then paste each value only at its prompt.
   A Claude session never reads, prints or writes these values.
3. **Turn it on.** Add `-e EMOTORAD_OMS_AFS_ORDERS=on` to `deploy-staging.yml`'s `docker run` and deploy.
   `/health` shows `"oms_afs_orders":"on"`, or `misconfigured:` and the names missing.
4. **Check.** Place an order in a test chat with a registered, covered test bike and a passing evidence check;
   within a minute the order shows in the OMS admin under AFS Order Management with our `RO-` reference as its
   ticket, rate 0 and sale type Warranty.
5. **A failed order.** `replacement_order_failed` is logged at error level with the reference. The order keeps
   its bike and part, so the bot will not order it again: a person places it by hand in the OMS admin or
   cancels it. Cancelling in OMS does not cancel the ERP sales order.

Rollback: remove `-e EMOTORAD_OMS_AFS_ORDERS=on` and redeploy. Orders already sent stay in OMS.
````

Add to `CLAUDE.md`, after the "Bikes from OMS orders" rule:

```markdown
- **Replacement orders in OMS** (spec 2026-10-10, `docs/superpowers/specs/2026-10-10-oms-replacement-orders-design.md`): `place_replacement_order` orders only when the evidence check passed for the part's fault (`Runtime._verified_fault`, fact `evidence_verified`: the verdict's component, or `any` with the check off and a photo or video seen), the bike's date is from `em_purchase` (`ownership_source` `oms_purchase`; order bikes and OCR never), and the part's own cover is active (`fulfilment.PART_COMPONENTS`, so a charger is judged on its 6 months); the rider's name, email and mobile are typed and confirmed in the chat. Orders go to a ledger (`fulfilment.ReplacementOrders`, `stores.mongo.MongoOrderLedger`, collection `replacement_orders`, permanent, erased with the conversation), `RO-` and seven digits, one open order per bike and part (unique sparse index on `open_key`; only `cancelled` frees it). With `EMOTORAD_OMS_AFS_ORDERS=on` and the OMS admin settings, `order_worker.py` sends `queued` orders to OMS's `afs_order_add` over its WebSocket (`oms_afs.py`: rate 0, sale type Warranty, our reference in `ticket_number`), looking OMS up by the reference before every send and after every error, since OMS has no protection against repeats and can fail after making the order; retries 1, 5, 15, 60 minutes then hourly, `failed` after 8 passes or 24 hours. Off (the default): orders are `recorded` and ticketed, nothing is sent. The OMS product ids (`fulfilment.ProductIds`) are empty until agreed, so nothing is sent yet, and the motor agent is offered the tool only once a motor-side part may be ordered (`motor_orderable`). A Claude session never reads or sets the OMS admin settings. `/health` `oms_afs_orders`, `replacement_orders`. Set-up: `docs/runbooks/config-store.md` section 10. Rollback: remove the switch.
```

- [ ] **Step 6: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass except the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/tools/oms_db.py src/emotorad_ai/api.py src/emotorad_ai/agents/motor_support.py src/emotorad_ai/fulfilment.py tests/test_api_replacement_orders.py tests/test_api_health.py docs/runbooks/config-store.md CLAUDE.md
git commit -m "feat(replacement-orders): the switch, the worker in the lifespan, /health, the motor rule and the runbook"
```
